"""Read-only, exact-release client lookup and isolated install notifications."""
import asyncio
from copy import deepcopy
import sqlite3
from unittest.mock import AsyncMock, Mock

import pytest

from test_modpack_service import (
    IDENTIFIER, SCOPE, assert_archived, catalog_row, choice, client_mod,
    harness, installer_log, issue, root, service_mod, state_mod,
)
from modpack_download import FREE_CLIENT_DIRECTORY


CLIENT_URL = "https://pan.baidu.com/s/1ExactRelease?pwd=abcd"


def child_choice(h, **changes):
    h.row = catalog_row(id="release-2", primaryId="project-1", clientUrl=CLIENT_URL)
    return choice(h, version_page=2, **changes)


@pytest.fixture
def capture_download(monkeypatch):
    builder = Mock(side_effect=lambda info, selected, *, secrets: {
        "info": info, "selected": selected, "exact": selected is not None,
    })
    monkeypatch.setattr(service_mod, "build_client_download", builder)
    return builder


def assert_catalog_reads_only(h):
    h.client.switch_modpack.assert_not_awaited()
    h.client.start_timing.assert_not_awaited()
    h.client.stop_timing.assert_not_awaited()
    h.client.close_server.assert_not_awaited()
    h.client.close_timing_only.assert_not_awaited()
    h.client.get_user_packages.assert_not_awaited()
    h.panel.power.assert_not_awaited()
    assert h.built_panels == []


@pytest.mark.asyncio
async def test_child_client_uses_own_exact_release_row_without_install_side_effects(
    harness, capture_download,
):
    h = harness
    selected = child_choice(h)
    result = await h.service.client_download(h.server, selected)
    assert result["exact"] is True
    assert result["selected"]["id"] == selected.item_id
    assert result["selected"]["clientUrl"] == CLIENT_URL
    h.client.list_modpack_versions.assert_awaited_once_with("project-1", page=2, page_size=9)
    h.client.search_modpacks.assert_not_awaited()
    assert capture_download.call_args.kwargs["secrets"] == (h.server.token, h.server.client_id)
    assert h.maintenance.latest(IDENTIFIER) is None
    assert_catalog_reads_only(h)


@pytest.mark.asyncio
async def test_project_selection_uses_its_original_search_page(harness, capture_download):
    h = harness
    h.row["clientUrl"] = CLIENT_URL
    selected = choice(h)
    result = await h.service.client_download(h.server, selected)
    assert result["exact"] is True
    h.client.search_modpacks.assert_awaited_once_with("Test", page=2, page_size=9)
    h.client.list_modpack_versions.assert_not_awaited()
    assert_catalog_reads_only(h)


@pytest.mark.asyncio
async def test_child_never_substitutes_parent_latest_client(harness, capture_download):
    h = harness
    selected = child_choice(h)
    h.client.list_modpack_versions.side_effect = lambda *args, **kwargs: root(
        catalog_row(clientUrl="https://pan.baidu.com/s/1ParentLatest"))
    result = await h.service.client_download(h.server, selected)
    assert result["exact"] is False and result["selected"] is None
    assert result["info"]["version"] == selected.version
    h.client.search_modpacks.assert_not_awaited()
    assert_catalog_reads_only(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"id": "different-release"}, {"primaryId": "different-project"},
    {"name": "Different Pack"}, {"modpackVersion": "9.9"},
    {"gameVersion": "1.12.2"}, {"javaVersion": "8"},
])
async def test_changed_catalog_identity_never_offers_another_version(
    harness, capture_download, changes,
):
    h = harness
    selected = child_choice(h)
    h.row.update(changes)
    result = await h.service.client_download(h.server, selected)
    assert result["exact"] is False and result["selected"] is None
    assert_catalog_reads_only(h)


@pytest.mark.asyncio
async def test_duplicate_exact_catalog_rows_fail_closed(harness, capture_download):
    h = harness
    selected = child_choice(h)
    h.client.list_modpack_versions.side_effect = lambda *args, **kwargs: {
        "rows": [deepcopy(h.row), deepcopy(h.row)], "total": 2,
    }
    result = await h.service.client_download(h.server, selected)
    assert result["exact"] is False and result["selected"] is None
    h.client.list_modpack_versions.assert_awaited_once()
    assert_catalog_reads_only(h)


@pytest.mark.asyncio
async def test_history_exact_release_lookup_is_read_only_and_keeps_archive(
    harness, capture_download,
):
    h = harness
    selected = child_choice(h)
    h.maintenance.begin(state_mod.ServerIdentity.from_server(h.server), selected)
    entry = h.maintenance.get(IDENTIFIER)
    h.maintenance.finish(IDENTIFIER, expected=entry, reason="completed")
    archived = h.maintenance.latest(IDENTIFIER)
    h.client.list_modpack_versions.side_effect = [
        {"rows": [catalog_row(id="different", primaryId="project-1")], "total": 100},
        root(deepcopy(h.row)),
    ]
    result = await h.service.client_download(h.server)
    assert result["exact"] is True and result["selected"]["id"] == selected.item_id
    assert h.maintenance.latest(IDENTIFIER) == archived
    assert h.maintenance.get(IDENTIFIER) is None
    assert [call.kwargs for call in h.client.list_modpack_versions.await_args_list] == [
        {"page": 1, "page_size": 50}, {"page": 2, "page_size": 50},
    ]
    assert_catalog_reads_only(h)


@pytest.mark.asyncio
async def test_legacy_history_has_labelled_fallback_without_guessing_catalog_identity(
    harness, capture_download,
):
    h = harness
    selected = child_choice(h)
    h.maintenance.begin(state_mod.ServerIdentity.from_server(h.server), selected)
    with sqlite3.connect(h.maintenance.db_path) as connection:
        connection.execute("UPDATE modpack_maintenance SET pack_project_id='',pack_item_id='',"
                           "pack_game_version='',pack_java_version=''")
    h.maintenance.finish(IDENTIFIER, reason="completed")
    before = h.maintenance.latest(IDENTIFIER)
    result = await h.service.client_download(h.server)
    assert result["exact"] is False and result["selected"] is None
    assert result["info"]["name"] == selected.name
    assert result["info"]["version"] == selected.version
    assert result["info"]["game_version"] == result["info"]["java_version"] == ""
    h.client.list_modpack_versions.assert_not_awaited()
    h.client.search_modpacks.assert_not_awaited()
    assert h.maintenance.latest(IDENTIFIER) == before
    assert_catalog_reads_only(h)


@pytest.mark.asyncio
async def test_missing_history_cannot_invent_current_client(harness, capture_download):
    with pytest.raises(service_mod.ModpackError, match="没有整合包选择记录"):
        await harness.service.client_download(harness.server)
    capture_download.assert_not_called()
    assert_catalog_reads_only(harness)


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("server_name", "other-name"), ("card_id", "other-card"),
    ("server_created_at", 999),
])
async def test_history_for_rebound_server_cannot_provide_client(
    harness, capture_download, field, value,
):
    h = harness
    h.maintenance.begin(state_mod.ServerIdentity.from_server(h.server), child_choice(h))
    with sqlite3.connect(h.maintenance.db_path) as connection:
        connection.execute(f"UPDATE modpack_maintenance SET {field}=?", (value,))
    with pytest.raises(service_mod.ModpackError, match="绑定不一致"):
        await h.service.client_download(h.server)
    capture_download.assert_not_called()
    h.client.list_modpack_versions.assert_not_awaited()
    assert_catalog_reads_only(h)


@pytest.mark.asyncio
async def test_server_binding_changed_during_catalog_read_is_rejected(harness, capture_download):
    h = harness
    selected = child_choice(h)

    async def changed(*args, **kwargs):
        h.server.card_id = "replacement-card"
        return root(deepcopy(h.row))

    h.client.list_modpack_versions.side_effect = changed
    with pytest.raises(service_mod.ModpackError, match="绑定已变化"):
        await h.service.client_download(h.server, selected)
    capture_download.assert_not_called()
    assert_catalog_reads_only(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("attempt_id", "replacement-attempt"), ("created_at", 999),
    ("pack_item_id", "replacement-release"), ("pack_version", "9.9"),
])
async def test_history_changed_during_read_is_rejected(harness, capture_download, field, value):
    h = harness
    selected = child_choice(h)
    h.maintenance.begin(state_mod.ServerIdentity.from_server(h.server), selected)

    async def changed(*args, **kwargs):
        with sqlite3.connect(h.maintenance.db_path) as connection:
            connection.execute(f"UPDATE modpack_maintenance SET {field}=?", (value,))
        return root(deepcopy(h.row))

    h.client.list_modpack_versions.side_effect = changed
    with pytest.raises(service_mod.ModpackError, match="选择记录已变化"):
        await h.service.client_download(h.server)
    capture_download.assert_not_called()
    assert_catalog_reads_only(h)


@pytest.mark.asyncio
async def test_history_paging_is_bounded_to_three_pages(harness, capture_download):
    h = harness
    h.maintenance.begin(state_mod.ServerIdentity.from_server(h.server), child_choice(h))
    h.client.list_modpack_versions.side_effect = lambda *args, **kwargs: {
        "rows": [catalog_row(id="other-release", primaryId="project-1")], "total": 10000,
    }
    result = await h.service.client_download(h.server)
    assert result["exact"] is False
    assert [call.kwargs["page"] for call in h.client.list_modpack_versions.await_args_list] == [1, 2, 3]
    assert_catalog_reads_only(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [client_mod.APIError("secret API body"), ValueError("secret value")])
async def test_catalog_errors_fall_back_without_exposing_errors_or_writing(
    harness, capture_download, error,
):
    h = harness
    selected = child_choice(h)
    h.client.list_modpack_versions.side_effect = error
    result = await h.service.client_download(h.server, selected)
    assert result["exact"] is False
    assert "secret" not in str(result)
    assert_catalog_reads_only(h)


@pytest.mark.asyncio
async def test_real_builder_returns_exact_child_client_and_no_group_file_claim(harness):
    h = harness
    selected = child_choice(h)
    result = await h.service.client_download(h.server, selected)
    assert result["exact"] is True and result["url"] == CLIENT_URL
    assert result["name"] == selected.name and result["version"] == selected.version
    assert result["game_version"] == selected.game_version
    assert result["java_version"] == selected.java_version
    assert result["code"] == ""  # No unverified extraction-code field is invented.
    assert "不会下载或上传群文件" in result["detail"]
    assert_catalog_reads_only(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("client_url", [
    None, "", "file:///etc/passwd", "https://127.0.0.1/private.zip",
    "https://localhost/private.zip", "https://private.internal/client.zip",
    "https://downloads.example.com/client.zip?token=private-token",
    "https://downloads.example.com/fake-token.zip",
    "https://downloads.example.com/fake-client.zip",
    "https://downloads.example.com/client.zip?X-Amz-Signature=secret",
    "https://user:password@example.com/client.zip",
    "https://downloads.example.com/client.zip\n[CQ:at,qq=all]",
    FREE_CLIENT_DIRECTORY,
])
async def test_unsafe_or_nonexact_client_field_uses_free_collection_only(harness, client_url):
    h = harness
    selected = child_choice(h)
    h.row.update(clientUrl=client_url, aliyunDownloadAvailable=True,
                 downloadUrl="https://paid.example.com/secret-client.zip",
                 fileName=selected.file_name)
    result = await h.service.client_download(h.server, selected)
    assert result["exact"] is False and result["url"] == FREE_CLIENT_DIRECTORY
    assert "不是所选版本的直链" in result["detail"]
    assert result["code"] == ""
    for secret in (h.server.token, h.server.client_id, "private-token", "paid.example.com"):
        assert secret not in str(result)
    assert_catalog_reads_only(h)


@pytest.mark.asyncio
async def test_catalog_auth_refresh_is_bounded_and_still_read_only(harness):
    h = harness
    selected = child_choice(h)
    h.client.list_modpack_versions.side_effect = [client_mod.AuthError("401"), root(deepcopy(h.row))]
    refresh = AsyncMock(return_value=(True, "refreshed"))
    result = await h.service.client_download(h.server, selected, refresh=refresh)
    assert result["exact"] is True
    assert h.client.list_modpack_versions.await_count == 2
    refresh.assert_awaited_once_with(h.server)
    assert_catalog_reads_only(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt_error", [None, client_mod.APIError("invalid response"), client_mod.AuthError("401")])
async def test_submission_notification_runs_once_after_single_post_and_before_observation(
    harness, receipt_error,
):
    h = harness
    pending = issue(h)
    log = installer_log(h)
    h.panel.list_directory.side_effect = lambda *args: [log] if h.client.switch_modpack.await_count else []
    if receipt_error is not None:
        h.client.switch_modpack.side_effect = receipt_error

    async def notify(selected):
        assert selected is pending
        h.client.switch_modpack.assert_awaited_once()
        entry = h.maintenance.get(IDENTIFIER)
        assert entry["write_started_at"] > 0
        assert entry["phase"] == ("unknown" if receipt_error else "submitted")
        assert entry["install_outcome"] == "unknown"

    callback = AsyncMock(side_effect=notify)
    assert await h.service.confirm(
        SCOPE, pending.code, authorized=lambda: True, on_submitted=callback,
    ) is pending
    callback.assert_awaited_once_with(pending)
    assert_archived(h, "completed")
    with pytest.raises(state_mod.ConfirmError):
        await h.service.confirm(SCOPE, pending.code, authorized=lambda: True, on_submitted=callback)
    callback.assert_awaited_once()
    h.client.switch_modpack.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["code", "permission", "preflight", "authentication", "billing"])
async def test_submission_notification_never_runs_before_an_install_request(harness, failure):
    h = harness
    pending = issue(h)
    callback = AsyncMock()
    code = "invalid" if failure == "code" else pending.code
    if failure == "preflight":
        h.resources["attributes"]["current_state"] = "running"
    elif failure == "authentication":
        h.panel.get_server_info.side_effect = client_mod.AuthError("401")
    elif failure == "billing":
        h.client.get_user_packages.side_effect = client_mod.APIError("billing unavailable")
    with pytest.raises((service_mod.MinekuaiError, state_mod.ConfirmError)):
        await h.service.confirm(
            SCOPE, code, authorized=lambda: failure != "permission", on_submitted=callback,
        )
    callback.assert_not_awaited()
    h.client.switch_modpack.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error", "timeout"])
@pytest.mark.parametrize("receipt_error", [False, True])
async def test_optional_notification_failure_does_not_skip_observation_or_reinstall(
    harness, monkeypatch, failure, receipt_error,
):
    h = harness
    pending = issue(h)
    log = installer_log(h)
    h.panel.list_directory.side_effect = lambda *args: [log] if h.client.switch_modpack.await_count else []
    if receipt_error:
        h.client.switch_modpack.side_effect = client_mod.APIError("response unknown")
    real_wait_for = asyncio.wait_for
    waits = []

    async def bounded_wait(awaitable, timeout):
        waits.append(timeout)
        return await real_wait_for(awaitable, .001 if timeout == 20.0 else timeout)

    monkeypatch.setattr(service_mod.asyncio, "wait_for", bounded_wait)

    async def optional(selected):
        assert selected is pending
        if failure == "timeout":
            await asyncio.Future()
        raise RuntimeError("QQ send unavailable")

    callback = AsyncMock(side_effect=optional)
    assert await h.service.confirm(
        SCOPE, pending.code, authorized=lambda: True, on_submitted=callback,
    ) is pending
    assert 20.0 in waits
    callback.assert_awaited_once_with(pending)
    h.client.switch_modpack.assert_awaited_once()
    assert_archived(h, "completed")


@pytest.mark.asyncio
async def test_notification_failure_keeps_unknown_write_protected(harness):
    h = harness
    pending = issue(h)
    h.client.switch_modpack.side_effect = client_mod.APIError("unconfirmed receipt")
    callback = AsyncMock(side_effect=RuntimeError("QQ send unavailable"))
    with pytest.raises(service_mod.ModpackError, match="结果未确认"):
        await h.service.confirm(SCOPE, pending.code, authorized=lambda: True, on_submitted=callback)
    callback.assert_awaited_once_with(pending)
    h.client.switch_modpack.assert_awaited_once()
    entry = h.maintenance.get(IDENTIFIER)
    assert entry["write_started_at"] > 0 and entry["phase"] == "unknown"
    with pytest.raises(state_mod.MaintenanceError):
        h.maintenance.ensure_card_available(h.server.card_id)
