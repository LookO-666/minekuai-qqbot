"""Offline service orchestration with fake APIs and temporary SQLite guards."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import importlib
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


sys.path.insert(0, str(Path(__file__).parents[1] / "plugins" / "minekuai"))
service_mod = importlib.import_module("modpack_service")
state_mod = importlib.import_module("modpack_state")
catalog_mod = importlib.import_module("modpack_catalog")
client_mod = importlib.import_module("client")
operations = importlib.import_module("operations")

SCOPE = (100, 200, 300)
IDENTIFIER = "deadbeef"
UUID = "deadbeef-0000-4000-8000-000000000000"


def catalog_row(**changes):
    return {
        "id": "project-1", "primaryId": "", "name": "Test Pack",
        "modpackVersion": "1.0", "gameVersion": "1.21.1", "javaVersion": "21",
        "fileName": "https://downloads.example.test/pack.zip", **changes,
    }


def root(row):
    return {"code": 200, "rows": [row], "total": 1}


@pytest.fixture
def harness(monkeypatch, tmp_path):
    h = SimpleNamespace(
        now=1000.0,
        server=SimpleNamespace(
            name="test", card_id="fake-card", instance_uuid=IDENTIFIER,
            account_phone="fake-account", created_at=100,
            token="fake-token", client_id="fake-client", updated_at=100,
        ),
        info={"attributes": {
            "identifier": IDENTIFIER, "uuid": UUID, "egg_id": 1,
            "is_suspended": False, "status": None, "is_installing": False,
            "is_transferring": False, "is_node_under_maintenance": False,
        }},
        resources={"attributes": {"current_state": "offline"}},
        billing={"data": [{"balanceId": "fake-card", "instances": [
            {"serverId": IDENTIFIER, "timingStatus": 1},
        ]}]},
        row=catalog_row(), built_clients=[], built_panels=[],
    )
    async def start_timing(card_id, *, instance_id):
        assert card_id == h.server.card_id and instance_id == IDENTIFIER
        h.billing["data"][0]["instances"][0]["timingStatus"] = 1
        h.info["attributes"]["is_suspended"] = False
        h.info["attributes"]["status"] = None
        h.resources["attributes"]["current_state"] = "starting"
        return {"code": 200}

    async def power(instance_id, signal):
        assert instance_id == IDENTIFIER and signal == "stop"
        h.resources["attributes"]["current_state"] = "offline"
        return {"sent": True, "observed_state": "offline"}

    h.client = SimpleNamespace(
        search_modpacks=AsyncMock(side_effect=lambda *args, **kwargs: root(deepcopy(h.row))),
        list_modpack_versions=AsyncMock(side_effect=lambda *args, **kwargs: root(deepcopy(h.row))),
        switch_modpack=AsyncMock(return_value={"code": 200}),
        get_user_packages=AsyncMock(side_effect=lambda: deepcopy(h.billing)),
        start_timing=AsyncMock(side_effect=start_timing),
        stop_timing=AsyncMock(), close_server=AsyncMock(), close_timing_only=AsyncMock(),
    )
    h.panel = SimpleNamespace(
        get_server_info=AsyncMock(side_effect=lambda *args: deepcopy(h.info)),
        get_resources=AsyncMock(side_effect=lambda *args: deepcopy(h.resources)),
        get_live_state=AsyncMock(return_value={"state": "offline", "stable_offline": True}),
        list_directory=AsyncMock(return_value=[]),
        read_file_text=AsyncMock(return_value=""),
        power=AsyncMock(side_effect=power),
        _error_text=client_mod.PanelClient(
            api_key="fake-panel-api-key", session_cookie="session=fake-session-secret; locale=zh",
            xsrf_token="fake-xsrf-secret",
        )._error_text,
    )
    h.confirms = state_mod.InstallConfirmStore(clock=lambda: h.now)
    h.maintenance = state_mod.MaintenanceStore(tmp_path / "maintenance.db")
    h.maintenance.init_db()
    monkeypatch.setattr(operations, "_card_locks", {})
    monkeypatch.setattr(operations, "_related_locks", {})
    monkeypatch.setattr(operations, "_related_lock_keys", None)
    monkeypatch.setattr(operations, "_maintenance_guard", h.maintenance.ensure_card_available)
    monkeypatch.setattr(service_mod, "READY_ATTEMPTS", 5, raising=False)
    monkeypatch.setattr(service_mod, "READY_INTERVAL", 0, raising=False)
    monkeypatch.setattr(service_mod, "READY_TIMEOUT", 1, raising=False)
    monkeypatch.setattr(service_mod, "INSTALL_TIMEOUT", .1)
    monkeypatch.setattr(service_mod, "INSTALL_INTERVAL", .001)

    @asynccontextmanager
    async def build_client(server):
        h.built_clients.append((server.token, server.client_id))
        yield h.client

    @asynccontextmanager
    async def build_panel(server):
        h.built_panels.append((server.token, server.client_id))
        yield h.panel

    h.cancel_background = Mock()
    h.service = service_mod.ModpackService(
        get_server=lambda name: h.server if h.server and h.server.name == name else None,
        build_client=build_client, build_panel=build_panel,
        confirms=h.confirms, maintenance=h.maintenance,
        card_operation=operations.card_operation, cancel_background=h.cancel_background,
    )
    return h


def choice(h, **changes):
    item = catalog_mod.normalize_item(h.row)
    return state_mod.InstallChoice(**{
        **asdict(item), "search_query": "Test", "search_page": 2, "version_page": 0,
        **changes,
    })


def issue(h, scope=SCOPE, selected=None):
    return h.confirms.issue(
        scope, state_mod.ServerIdentity.from_server(h.server), selected or choice(h),
    )


def begin_guard(h, phase="submitted"):
    h.maintenance.begin(state_mod.ServerIdentity.from_server(h.server), choice(h))
    if phase != "preparing":
        h.maintenance.mark(h.server.instance_uuid, phase)


def installer_log(h, *, text=None, **changes):
    """A public fake log newer than the durable write timestamp, never real I/O."""
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    china_now = now.astimezone(timezone(timedelta(hours=8)))
    text = text if text is not None else f"[{china_now:%Y-%m-%d %H:%M:%S}] 整合包安装成功!\n"
    item = {
        "name": "installserverlogs.log", "is_file": True, "is_symlink": False,
        "size": len(text.encode("utf-8")), "created_at": now.isoformat(),
        "modified_at": now.isoformat(), **changes,
    }
    h.panel.list_directory.return_value = [item]
    h.panel.read_file_text.return_value = text
    return item


def begin_written_guard(h, baseline="", phase="submitted"):
    begin_guard(h, "preparing")
    h.maintenance.start_write(h.server.instance_uuid, baseline)
    h.maintenance.mark(h.server.instance_uuid, phase)
    return h.maintenance.get(h.server.instance_uuid)


def assert_archived(h, outcome):
    assert h.maintenance.get(h.server.instance_uuid) is None
    entry = h.maintenance.latest(h.server.instance_uuid)
    assert entry["install_outcome"] == outcome
    assert entry["release_reason"] == outcome and entry["released_at"] > 0
    h.maintenance.ensure_card_available(h.server.card_id)
    h.maintenance.ensure_instance_available(h.server.instance_uuid)
    return entry


@pytest.mark.parametrize("field,value", [
    ("name", "renamed"), ("card_id", "replacement-card"),
    ("instance_uuid", "cafe1234"), ("account_phone", "replacement-account"),
    ("created_at", 101),
])
def test_current_rejects_changed_identity(harness, field, value):
    h = harness
    identity = state_mod.ServerIdentity.from_server(h.server)
    setattr(h.server, field, value)
    with pytest.raises(service_mod.ModpackError, match="绑定已变化"):
        h.service.current(identity)


def test_current_accepts_token_refresh_but_not_deleted_config(harness):
    h = harness
    identity = state_mod.ServerIdentity.from_server(h.server)
    h.server.token = "fresh-token"
    h.server.updated_at += 10
    assert h.service.current(identity) is h.server
    h.server = None
    with pytest.raises(service_mod.ModpackError):
        h.service.current(identity)


@pytest.mark.asyncio
@pytest.mark.parametrize("instance", [IDENTIFIER, UUID, UUID.upper()])
async def test_preflight_accepts_matching_short_or_full_identity_offline(harness, instance):
    h = harness
    h.server.instance_uuid = instance
    server, identifier = await h.service.preflight(h.server)
    assert server is h.server and identifier == IDENTIFIER
    h.panel.get_resources.assert_awaited_once_with(IDENTIFIER)
    h.panel.get_live_state.assert_not_awaited()  # Initial preflight isn't the install-ready check.
    h.client.switch_modpack.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("frozen", [{"is_suspended": True}, {"status": "suspended"}])
async def test_frozen_instance_preflight_does_not_require_unavailable_resources(harness, frozen):
    h = harness
    h.info["attributes"].update(frozen)
    h.panel.get_resources.side_effect = client_mod.APIError("HTTP 409 suspended")
    _, identifier = await h.service.preflight(h.server)
    assert identifier == IDENTIFIER
    h.panel.get_resources.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("current_state", ["running", "starting", "stopping", "unknown", None])
async def test_non_offline_preflight_refuses_destructive_operation(harness, current_state):
    h = harness
    h.resources["attributes"]["current_state"] = current_state
    with pytest.raises(service_mod.ModpackError, match="停止实例"):
        await h.service.preflight(h.server)
    h.client.switch_modpack.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"egg_id": 208}, {"egg_id": "208"}, {"is_installing": True},
    {"is_transferring": True}, {"is_node_under_maintenance": True},
    {"status": "installing"}, {"identifier": "cafe1234", "uuid": "unrelated"},
    {"identifier": ""},
])
async def test_mcdr_busy_or_mismatched_instance_is_refused(harness, changes):
    h = harness
    h.info["attributes"].update(changes)
    with pytest.raises(service_mod.ModpackError):
        await h.service.preflight(h.server)
    h.client.switch_modpack.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("info", [{}, {"attributes": []}, {"attributes": None}])
async def test_preflight_rejects_malformed_instance_info(harness, info):
    h = harness
    h.info = info
    with pytest.raises(service_mod.ModpackError):
        await h.service.preflight(h.server)


@pytest.mark.asyncio
async def test_prepare_is_readonly_and_creates_bound_confirmation(harness):
    h = harness
    selected = choice(h)
    pending = await h.service.prepare(SCOPE, h.server, selected)
    assert pending.scope == SCOPE and pending.choice == selected
    assert pending.server.matches(h.server)
    assert pending.expires_at == h.now + 300
    h.client.search_modpacks.assert_awaited_once_with("Test", page=2, page_size=9)
    h.client.switch_modpack.assert_not_awaited()
    assert h.maintenance.get(IDENTIFIER) is None
    h.cancel_background.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("primaryId", "changed-project"), ("id", "changed-item"),
    ("name", "Changed Pack"), ("modpackVersion", "2.0"),
    ("gameVersion", "1.20"), ("javaVersion", "17"),
    ("fileName", "different.zip"), ("fileName", ""),
])
async def test_catalog_revalidation_rejects_every_changed_snapshot_field(harness, field, value):
    h = harness
    selected = choice(h)
    h.row[field] = value
    with pytest.raises(service_mod.ModpackError):
        await h.service.validate_choice(h.server, selected)
    h.client.switch_modpack.assert_not_awaited()


@pytest.mark.asyncio
async def test_version_choice_revalidated_against_same_project_and_pages(harness):
    h = harness
    version = catalog_row(id="release-2", primaryId="project-1", modpackVersion="2.0", fileName="release-2.zip")
    selected = state_mod.InstallChoice(
        **asdict(catalog_mod.normalize_item(version)),
        search_query="Test", search_page=2, version_page=3,
    )
    h.client.list_modpack_versions.side_effect = lambda *args, **kwargs: root(version)
    await h.service.validate_choice(h.server, selected)
    h.client.list_modpack_versions.assert_awaited_once_with("project-1", page=3, page_size=9)
    version["fileName"] = "changed.zip"
    with pytest.raises(service_mod.ModpackError):
        await h.service.validate_choice(h.server, selected)


@pytest.mark.asyncio
async def test_confirm_submits_exact_choice_once_and_persists_guard(harness):
    h = harness
    pending = issue(h)
    assert await h.service.confirm(SCOPE, pending.code, authorized=lambda: True) is pending
    h.client.switch_modpack.assert_awaited_once_with(IDENTIFIER, pending.choice.file_name, pending.choice.item_id)
    h.cancel_background.assert_called_once_with("test")
    restarted = state_mod.MaintenanceStore(h.maintenance.db_path)
    assert restarted.get(IDENTIFIER)["phase"] == "submitted"
    with pytest.raises(state_mod.MaintenanceError):
        restarted.ensure_card_available("fake-card")
    with pytest.raises(state_mod.ConfirmError):
        await h.service.confirm(SCOPE, pending.code, authorized=lambda: True)
    assert h.client.switch_modpack.await_count == 1


@pytest.mark.asyncio
async def test_simultaneous_duplicate_confirmation_only_one_write(harness):
    h = harness
    pending = issue(h)
    entered, release = asyncio.Event(), asyncio.Event()

    async def switch(*args):
        entered.set()
        await release.wait()
        return {"code": 200}

    h.client.switch_modpack.side_effect = switch
    first = asyncio.create_task(h.service.confirm(SCOPE, pending.code, authorized=lambda: True))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(state_mod.ConfirmError):
            await h.service.confirm(SCOPE, pending.code, authorized=lambda: True)
        release.set()
        await first
    finally:
        release.set()
        if not first.done():
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
    assert h.client.switch_modpack.await_count == 1


@pytest.mark.asyncio
async def test_other_administrator_confirmation_cannot_overlap_same_card(harness):
    h = harness
    first_pending = issue(h)
    other_pending = issue(h, (100, 201, 300))
    entered, release = asyncio.Event(), asyncio.Event()

    async def switch(*args):
        assert h.maintenance.get(IDENTIFIER)["phase"] == "preparing"
        entered.set()
        await release.wait()

    h.client.switch_modpack.side_effect = switch
    first = asyncio.create_task(h.service.confirm(SCOPE, first_pending.code, authorized=lambda: True))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(operations.OperationBusyError):
            await h.service.confirm(other_pending.scope, other_pending.code, authorized=lambda: True)
        release.set()
        await first
    finally:
        release.set()
        if not first.done():
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
    assert h.client.switch_modpack.await_count == 1
    with pytest.raises(state_mod.ConfirmError):
        h.confirms.consume(other_pending.scope, other_pending.code)


@pytest.mark.asyncio
async def test_guard_must_be_persisted_before_any_install_write(harness, monkeypatch):
    h = harness
    pending = issue(h)
    monkeypatch.setattr(h.maintenance, "begin", Mock(side_effect=state_mod.MaintenanceError("disk error")))
    with pytest.raises(state_mod.MaintenanceError):
        await h.service.confirm(SCOPE, pending.code, authorized=lambda: True)
    h.client.switch_modpack.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("other_scope", [(101, 200, 300), (100, 201, 300), (100, 200, 301), (100, 200, None)])
async def test_confirm_scope_cannot_cross_bot_user_group_or_private(harness, other_scope):
    h = harness
    pending = issue(h)
    with pytest.raises(state_mod.ConfirmError):
        await h.service.confirm(other_scope, pending.code, authorized=lambda: True)
    h.client.switch_modpack.assert_not_awaited()
    assert h.confirms.consume(SCOPE, pending.code) is pending


@pytest.mark.asyncio
async def test_expired_confirmation_never_reads_or_writes(harness):
    h = harness
    pending = issue(h)
    h.now += 300
    with pytest.raises(state_mod.ConfirmError, match="过期"):
        await h.service.confirm(SCOPE, pending.code, authorized=lambda: True)
    h.panel.get_server_info.assert_not_awaited()
    h.client.switch_modpack.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("at_final_check", [False, True])
async def test_authorization_is_checked_twice_and_refusal_consumes_confirmation(harness, at_final_check):
    h = harness
    pending = issue(h)
    authorized = Mock(side_effect=[True, False] if at_final_check else [False])
    with pytest.raises(state_mod.ConfirmError):
        await h.service.confirm(SCOPE, pending.code, authorized=authorized)
    h.client.switch_modpack.assert_not_awaited()
    assert h.maintenance.get(IDENTIFIER) is None
    with pytest.raises(state_mod.ConfirmError):
        h.confirms.consume(SCOPE, pending.code)


@pytest.mark.asyncio
async def test_binding_change_while_reading_catalog_prevents_write(harness):
    h = harness
    pending = issue(h)

    async def changed_search(*args, **kwargs):
        h.server.instance_uuid = "cafe1234"
        return root(h.row)

    h.client.search_modpacks.side_effect = changed_search
    with pytest.raises(service_mod.ModpackError, match="绑定已变化"):
        await h.service.confirm(SCOPE, pending.code, authorized=lambda: True)
    h.client.switch_modpack.assert_not_awaited()
    assert h.maintenance.get(IDENTIFIER) is None


@pytest.mark.asyncio
async def test_busy_card_rejects_and_consumes_confirmation(harness):
    h = harness
    pending = issue(h)
    async with operations.card_operation(h.server.card_id):
        with pytest.raises(operations.OperationBusyError):
            await h.service.confirm(SCOPE, pending.code, authorized=lambda: True)
    h.client.switch_modpack.assert_not_awaited()
    with pytest.raises(state_mod.ConfirmError):
        h.confirms.consume(SCOPE, pending.code)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [client_mod.APIError("timeout; result unknown"), client_mod.AuthError("401"), asyncio.CancelledError()])
async def test_failed_destructive_request_is_never_refreshed_or_retried(harness, failure):
    h = harness
    pending = issue(h)
    h.client.switch_modpack.side_effect = failure
    refresh = AsyncMock(return_value=(True, "refreshed"))
    expected = asyncio.CancelledError if isinstance(failure, asyncio.CancelledError) else service_mod.ModpackError
    with pytest.raises(expected) as caught:
        await h.service.confirm(SCOPE, pending.code, authorized=lambda: True, refresh=refresh)
    if not isinstance(failure, asyncio.CancelledError):
        assert "结果未确认" in str(caught.value)
        assert str(failure) in str(caught.value)
    assert h.client.switch_modpack.await_count == 1
    refresh.assert_not_awaited()
    restarted = state_mod.MaintenanceStore(h.maintenance.db_path)
    assert restarted.get(IDENTIFIER)["phase"] == "unknown"
    with pytest.raises(state_mod.MaintenanceError):
        restarted.ensure_card_available("fake-card")
    with pytest.raises(state_mod.ConfirmError):
        h.confirms.consume(SCOPE, pending.code)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["preparing", "submitted", "unknown"])
async def test_existing_persistent_guard_blocks_new_prepare_and_confirm(harness, phase):
    h = harness
    pending = issue(h)
    begin_guard(h, phase)
    with pytest.raises(state_mod.MaintenanceError):
        await h.service.prepare(SCOPE, h.server, choice(h))
    with pytest.raises(operations.OperationBusyError):
        await h.service.confirm(SCOPE, pending.code, authorized=lambda: True)
    h.client.switch_modpack.assert_not_awaited()
    assert h.maintenance.get(IDENTIFIER)["phase"] == phase


@pytest.mark.asyncio
@pytest.mark.parametrize("panel", [False, True])
async def test_readonly_auth_refresh_uses_new_credentials_once(harness, panel):
    h = harness
    callback = AsyncMock(side_effect=[client_mod.AuthError("expired"), {"result": "ok"}])

    async def refresh(server):
        h.server = SimpleNamespace(**{**vars(server), "token": "fresh-token", "updated_at": 200})
        return True, "refreshed"

    refresh_mock = AsyncMock(side_effect=refresh)
    result, server = await h.service.read(h.server, callback, panel=panel, refresh=refresh_mock)
    assert result == {"result": "ok"} and server.token == "fresh-token"
    assert callback.await_count == 2
    refresh_mock.assert_awaited_once()
    assert (h.built_panels if panel else h.built_clients) == [
        ("fake-token", "fake-client"), ("fresh-token", "fake-client"),
    ]
    h.client.switch_modpack.assert_not_awaited()


@pytest.mark.asyncio
async def test_readonly_auth_refresh_is_bounded_and_identity_change_is_rejected(harness):
    h = harness
    callback = AsyncMock(side_effect=client_mod.AuthError("expired"))
    refresh = AsyncMock(return_value=(True, "ok"))
    with pytest.raises(client_mod.AuthError):
        await h.service.read(h.server, callback, refresh=refresh)
    assert callback.await_count == 2
    refresh.assert_awaited_once()

    async def rebind(server):
        server.account_phone = "different-account"
        return True, "ok"

    callback.reset_mock()
    with pytest.raises(service_mod.ModpackError, match="绑定已变化"):
        await h.service.read(h.server, callback, refresh=rebind)
    assert callback.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("refresh_mode", ["absent", "failed"])
async def test_readonly_auth_without_successful_refresh_is_not_replayed(harness, refresh_mode):
    h = harness
    callback = AsyncMock(side_effect=client_mod.AuthError("expired"))
    refresh = None if refresh_mode == "absent" else AsyncMock(return_value=(False, "login refused"))
    expected = client_mod.AuthError if refresh is None else service_mod.ModpackError
    with pytest.raises(expected):
        await h.service.read(h.server, callback, refresh=refresh)
    assert callback.await_count == 1
    h.client.switch_modpack.assert_not_awaited()


@pytest.mark.asyncio
async def test_finish_requires_explicit_authorization_and_retains_guard_on_denial(harness):
    h = harness
    begin_guard(h)
    with pytest.raises(state_mod.ConfirmError):
        await h.service.finish_maintenance(h.server, authorized=lambda: False)
    assert h.maintenance.get(IDENTIFIER) is not None
    h.panel.get_server_info.assert_not_awaited()
    assert await h.service.finish_maintenance(h.server, authorized=lambda: True)
    assert h.maintenance.get(IDENTIFIER) is None
    h.client.switch_modpack.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"is_installing": True}, {"is_transferring": True},
    {"status": "installing"}, {"status": "reinstalling"}, {"status": "restoring_backup"},
    {"status": "unknown-future-state"}, {"is_node_under_maintenance": True},
])
async def test_finish_refuses_active_install_or_transfer(harness, changes):
    h = harness
    begin_guard(h)
    h.info["attributes"].update(changes)
    with pytest.raises(service_mod.ModpackError):
        await h.service.finish_maintenance(h.server, authorized=lambda: True)
    assert h.maintenance.get(IDENTIFIER) is not None


@pytest.mark.asyncio
async def test_finish_rechecks_authorization_after_read(harness):
    h = harness
    begin_guard(h)
    authorized = Mock(side_effect=[True, False])
    with pytest.raises(state_mod.ConfirmError):
        await h.service.finish_maintenance(h.server, authorized=authorized)
    assert h.maintenance.get(IDENTIFIER) is not None


@pytest.mark.asyncio
async def test_finish_rejects_binding_change_during_read(harness):
    h = harness
    begin_guard(h)

    async def changed_info(*args):
        h.server.card_id = "replacement-card"
        return deepcopy(h.info)

    h.panel.get_server_info.side_effect = changed_info
    with pytest.raises(service_mod.ModpackError, match="绑定已变化"):
        await h.service.finish_maintenance(h.server, authorized=lambda: True)
    assert h.maintenance.get(IDENTIFIER) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("info", [
    {}, {"attributes": {}}, {"attributes": None},
    {"attributes": {"identifier": "cafe1234", "uuid": "wrong-instance"}},
])
async def test_finish_refuses_missing_or_wrong_remote_identity(harness, info):
    h = harness
    begin_guard(h)
    h.info = info
    with pytest.raises(service_mod.ModpackError):
        await h.service.finish_maintenance(h.server, authorized=lambda: True)
    assert h.maintenance.get(IDENTIFIER) is not None


def set_billing(h, active):
    h.billing["data"][0]["instances"][0]["timingStatus"] = active


def assert_billing_not_automatically_closed(h):
    h.client.stop_timing.assert_not_awaited()
    h.client.close_server.assert_not_awaited()
    h.client.close_timing_only.assert_not_awaited()


def mirror_live_resource(h):
    """Explicit active-state scenarios must update both observation channels."""
    def live(*args, **kwargs):
        state = h.resources["attributes"]["current_state"]
        return {"state": state, "stable_offline": state == "offline"}

    h.panel.get_live_state.side_effect = live


@pytest.mark.asyncio
async def test_active_billing_is_not_started_again(harness):
    h = harness
    pending = issue(h)
    await h.service.confirm(SCOPE, pending.code, authorized=lambda: True)
    h.client.start_timing.assert_not_awaited()
    h.panel.power.assert_not_awaited()
    h.client.switch_modpack.assert_awaited_once()
    assert h.client.get_user_packages.await_count >= 2
    assert_billing_not_automatically_closed(h)


@pytest.mark.asyncio
async def test_start_billing_precedes_two_offline_rounds_and_final_revalidation(harness):
    h = harness
    set_billing(h, 0)
    h.info["attributes"]["is_suspended"] = True
    pending = issue(h)
    events = []
    original_start = h.client.start_timing.side_effect
    mirror_live_resource(h)

    async def start(card_id, *, instance_id):
        assert h.maintenance.get(IDENTIFIER)["phase"] == "preparing"
        events.append("start")
        return await original_start(card_id, instance_id=instance_id)

    async def resources(*args):
        events.append(h.resources["attributes"]["current_state"])
        return deepcopy(h.resources)

    async def packages():
        events.append("billing")
        return deepcopy(h.billing)

    async def switch(*args):
        events.append("switch")
        return {"code": 200}

    h.client.start_timing.side_effect = start
    h.client.get_user_packages.side_effect = packages
    h.panel.get_resources.side_effect = resources
    h.client.switch_modpack.side_effect = switch
    await h.service.confirm(SCOPE, pending.code, authorized=lambda: True)
    h.client.start_timing.assert_awaited_once_with("fake-card", instance_id=IDENTIFIER)
    before_install = events[events.index("start") + 1:events.index("switch")]
    assert before_install.count("offline") >= 3  # Two readiness rounds plus final preflight.
    assert before_install.count("billing") >= 3  # Readiness rounds and final billing validation.
    assert h.client.search_modpacks.await_count >= 2  # Choice checked before and after billing.
    h.panel.power.assert_awaited_once_with(IDENTIFIER, "stop")
    assert h.maintenance.get(IDENTIFIER)["phase"] == "submitted"
    assert_billing_not_automatically_closed(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["starting", "running"])
async def test_new_billing_auto_start_is_stopped_once_before_install(harness, state):
    h = harness
    set_billing(h, 0)
    h.info["attributes"]["is_suspended"] = True
    pending = issue(h)
    original_start = h.client.start_timing.side_effect
    mirror_live_resource(h)

    async def start(card_id, *, instance_id):
        await original_start(card_id, instance_id=instance_id)
        h.resources["attributes"]["current_state"] = state

    h.client.start_timing.side_effect = start
    await h.service.confirm(SCOPE, pending.code, authorized=lambda: True)
    h.panel.power.assert_awaited_once_with(IDENTIFIER, "stop")
    h.client.start_timing.assert_awaited_once()
    h.client.switch_modpack.assert_awaited_once()
    assert_billing_not_automatically_closed(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [client_mod.APIError("billing timeout"), client_mod.AuthError("billing 401")])
async def test_billing_start_errors_never_refresh_retry_install_or_close_billing(harness, failure):
    h = harness
    set_billing(h, 0)
    pending = issue(h)
    h.client.start_timing.side_effect = failure
    refresh = AsyncMock(return_value=(True, "ok"))
    with pytest.raises(service_mod.ModpackError, match="未提交更换整合包"):
        await h.service.confirm(SCOPE, pending.code, authorized=lambda: True, refresh=refresh)
    h.client.start_timing.assert_awaited_once()
    h.client.switch_modpack.assert_not_awaited()
    h.panel.power.assert_not_awaited()
    refresh.assert_not_awaited()
    assert h.maintenance.get(IDENTIFIER)["phase"] == "unknown"
    assert_billing_not_automatically_closed(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [client_mod.APIError("stop timeout"), client_mod.AuthError("stop 401")])
async def test_normal_stop_errors_never_refresh_or_retry_the_stop(harness, failure):
    h = harness
    set_billing(h, 0)
    pending = issue(h)
    original_start = h.client.start_timing.side_effect
    mirror_live_resource(h)

    async def start(card_id, *, instance_id):
        await original_start(card_id, instance_id=instance_id)
        h.resources["attributes"]["current_state"] = "running"

    h.client.start_timing.side_effect = start
    h.panel.power.side_effect = failure
    refresh = AsyncMock(return_value=(True, "ok"))
    with pytest.raises(service_mod.ModpackError, match="未提交更换整合包"):
        await h.service.confirm(SCOPE, pending.code, authorized=lambda: True, refresh=refresh)
    h.panel.power.assert_awaited_once_with(IDENTIFIER, "stop")
    refresh.assert_not_awaited()
    h.client.switch_modpack.assert_not_awaited()
    assert h.maintenance.get(IDENTIFIER)["phase"] == "unknown"
    assert_billing_not_automatically_closed(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["billing_zero", "suspended", "stopping", "still_running", "unknown_state"])
async def test_not_ready_after_billing_start_never_installs(harness, fault):
    h = harness
    set_billing(h, 0)
    pending = issue(h)
    original_start = h.client.start_timing.side_effect
    mirror_live_resource(h)

    async def start(card_id, *, instance_id):
        await original_start(card_id, instance_id=instance_id)
        if fault == "billing_zero":
            set_billing(h, 0)
        elif fault == "suspended":
            h.info["attributes"]["is_suspended"] = True
        else:
            h.resources["attributes"]["current_state"] = {
                "stopping": "stopping", "still_running": "running", "unknown_state": "unknown",
            }[fault]

    h.client.start_timing.side_effect = start
    h.panel.power.side_effect = None  # An accepted stop does not prove it stopped.
    h.panel.power.return_value = {"sent": True, "observed_state": "stopping"}
    with pytest.raises(service_mod.ModpackError):
        await h.service.confirm(SCOPE, pending.code, authorized=lambda: True)
    assert h.client.start_timing.await_count == 1
    assert h.panel.power.await_count <= 1
    h.client.switch_modpack.assert_not_awaited()
    assert h.maintenance.get(IDENTIFIER)["phase"] == "unknown"
    assert_billing_not_automatically_closed(h)


@pytest.mark.asyncio
async def test_previously_active_billing_does_not_authorize_stopping_new_activity(harness):
    h = harness
    pending = issue(h)
    calls = [0]

    async def resources(*args):
        calls[0] += 1
        return {"attributes": {"current_state": "offline" if calls[0] == 1 else "running"}}

    h.panel.get_resources.side_effect = resources
    with pytest.raises(service_mod.ModpackError):
        await h.service.confirm(SCOPE, pending.code, authorized=lambda: True)
    h.client.start_timing.assert_not_awaited()
    h.panel.power.assert_not_awaited()
    h.client.switch_modpack.assert_not_awaited()
    assert h.maintenance.get(IDENTIFIER)["phase"] == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("timing_status", [None, True, False, "2", "active", 2, -1, 1.0, 0.0, {}, []])
async def test_malformed_billing_status_prevents_all_writes(harness, timing_status):
    h = harness
    set_billing(h, timing_status)
    pending = issue(h)
    with pytest.raises(service_mod.ModpackError):
        await h.service.confirm(SCOPE, pending.code, authorized=lambda: True)
    h.client.start_timing.assert_not_awaited()
    h.client.switch_modpack.assert_not_awaited()
    assert h.maintenance.get(IDENTIFIER) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["no_data", "not_list", "wrong_card", "wrong_instance", "duplicate_card", "duplicate_instance", "missing_instances"])
async def test_ambiguous_or_mismatched_billing_identity_prevents_all_writes(harness, fault):
    h = harness
    if fault == "no_data":
        h.billing = {}
    elif fault == "not_list":
        h.billing["data"] = {}
    elif fault == "wrong_card":
        h.billing["data"][0]["balanceId"] = "different-card"
    elif fault == "wrong_instance":
        h.billing["data"][0]["instances"][0]["serverId"] = "cafe1234"
    elif fault == "duplicate_card":
        h.billing["data"].append(deepcopy(h.billing["data"][0]))
    elif fault == "duplicate_instance":
        h.billing["data"][0]["instances"].append(deepcopy(h.billing["data"][0]["instances"][0]))
    else:
        h.billing["data"][0].pop("instances")
    pending = issue(h)
    with pytest.raises(service_mod.ModpackError):
        await h.service.confirm(SCOPE, pending.code, authorized=lambda: True)
    h.client.start_timing.assert_not_awaited()
    h.client.switch_modpack.assert_not_awaited()
    assert h.maintenance.get(IDENTIFIER) is None


@pytest.mark.asyncio
async def test_catalog_change_after_billing_start_keeps_guard_and_prevents_install(harness):
    h = harness
    set_billing(h, 0)
    pending = issue(h)
    original_start = h.client.start_timing.side_effect
    mirror_live_resource(h)

    async def start(card_id, *, instance_id):
        await original_start(card_id, instance_id=instance_id)
        h.row["fileName"] = "changed-file.zip"

    h.client.start_timing.side_effect = start
    with pytest.raises(service_mod.ModpackError):
        await h.service.confirm(SCOPE, pending.code, authorized=lambda: True)
    h.client.start_timing.assert_awaited_once()
    assert h.client.search_modpacks.await_count >= 2
    h.client.switch_modpack.assert_not_awaited()
    assert h.maintenance.get(IDENTIFIER)["phase"] == "unknown"
    assert_billing_not_automatically_closed(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expected", [(0, False), (1, True), ("0", False), ("1", True)])
async def test_billing_active_accepts_only_known_numeric_and_string_states(harness, status, expected):
    h = harness
    set_billing(h, status)
    assert await h.service.billing_active(h.server, IDENTIFIER) is expected
    h.client.start_timing.assert_not_awaited()
    h.client.switch_modpack.assert_not_awaited()


@pytest.mark.asyncio
async def test_billing_readonly_auth_refresh_is_bounded_to_one(harness):
    h = harness
    h.client.get_user_packages.side_effect = [client_mod.AuthError("expired"), deepcopy(h.billing)]
    refresh = AsyncMock(return_value=(True, "ok"))
    assert await h.service.billing_active(h.server, IDENTIFIER, refresh) is True
    assert h.client.get_user_packages.await_count == 2
    refresh.assert_awaited_once()
    h.client.start_timing.assert_not_awaited()

    h.client.get_user_packages.reset_mock()
    h.client.get_user_packages.side_effect = client_mod.AuthError("still expired")
    refresh.reset_mock()
    with pytest.raises(client_mod.AuthError):
        await h.service.billing_active(h.server, IDENTIFIER, refresh)
    assert h.client.get_user_packages.await_count == 2
    refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_start_stop_transition_then_two_consecutive_offline_rounds(harness):
    h = harness
    set_billing(h, 0)
    h.info["attributes"]["is_suspended"] = True
    pending = issue(h)
    sequence = iter(["starting", "stopping", "offline", "offline", "offline"])
    observed = []
    mirror_live_resource(h)

    async def resources(*args):
        current = next(sequence)
        observed.append(current)
        h.resources["attributes"]["current_state"] = current
        return {"attributes": {"current_state": current}}

    h.panel.get_resources.side_effect = resources
    await h.service.confirm(SCOPE, pending.code, authorized=lambda: True)
    assert observed == ["starting", "stopping", "offline", "offline", "offline"]
    h.panel.power.assert_awaited_once_with(IDENTIFIER, "stop")
    h.client.switch_modpack.assert_awaited_once()
    assert_billing_not_automatically_closed(h)


@pytest.mark.asyncio
async def test_nonconsecutive_offline_observations_do_not_authorize_install(harness, monkeypatch):
    h = harness
    set_billing(h, 0)
    h.info["attributes"]["is_suspended"] = True
    pending = issue(h)
    monkeypatch.setattr(service_mod, "READY_ATTEMPTS", 4)
    sequence = iter(["starting", "offline", "running", "offline"])
    mirror_live_resource(h)

    async def resources(*args):
        current = next(sequence)
        h.resources["attributes"]["current_state"] = current
        return {"attributes": {"current_state": current}}

    h.panel.get_resources.side_effect = resources
    with pytest.raises(service_mod.ModpackError, match="未提交更换整合包"):
        await h.service.confirm(SCOPE, pending.code, authorized=lambda: True)
    h.panel.power.assert_awaited_once_with(IDENTIFIER, "stop")
    h.client.switch_modpack.assert_not_awaited()
    assert h.maintenance.get(IDENTIFIER)["phase"] == "unknown"


@pytest.mark.asyncio
async def test_ready_wait_has_total_deadline_and_keeps_unknown_guard(harness, monkeypatch):
    h = harness
    pending = issue(h)
    monkeypatch.setattr(service_mod, "READY_TIMEOUT", 0.02)
    calls = [0]

    async def packages():
        calls[0] += 1
        if calls[0] > 1:
            await asyncio.Future()
        return deepcopy(h.billing)

    h.client.get_user_packages.side_effect = packages
    with pytest.raises(service_mod.ModpackError, match="未提交更换整合包") as error:
        await asyncio.wait_for(h.service.confirm(SCOPE, pending.code, authorized=lambda: True), 1)
    assert "TimeoutError" not in str(error.value)
    for detail in ("等待", "计费", "HTTP=", "实时="):
        assert detail in str(error.value)
    h.client.switch_modpack.assert_not_awaited()
    assert h.maintenance.get(IDENTIFIER)["phase"] == "unknown"
    assert_billing_not_automatically_closed(h)


@pytest.mark.asyncio
async def test_billing_changes_after_readiness_block_final_install(harness):
    h = harness
    pending = issue(h)
    calls = [0]

    async def packages():
        calls[0] += 1
        value = deepcopy(h.billing)
        if calls[0] >= 4:  # Initial + two ready rounds passed; final check changes.
            value["data"][0]["instances"][0]["timingStatus"] = 0
        return value

    h.client.get_user_packages.side_effect = packages
    with pytest.raises(service_mod.ModpackError, match="未提交更换整合包"):
        await h.service.confirm(SCOPE, pending.code, authorized=lambda: True)
    h.client.switch_modpack.assert_not_awaited()
    h.client.start_timing.assert_not_awaited()
    assert h.maintenance.get(IDENTIFIER)["phase"] == "unknown"


@pytest.mark.asyncio
async def test_permissions_revoked_during_final_catalog_recheck_block_install(harness):
    h = harness
    pending = issue(h)
    authorized = [True]
    calls = [0]

    async def search(*args, **kwargs):
        calls[0] += 1
        if calls[0] > 1:
            authorized[0] = False
        return root(deepcopy(h.row))

    h.client.search_modpacks.side_effect = search
    with pytest.raises(service_mod.ModpackError, match="未提交更换整合包"):
        await h.service.confirm(SCOPE, pending.code, authorized=lambda: authorized[0])
    h.client.switch_modpack.assert_not_awaited()
    assert h.maintenance.get(IDENTIFIER)["phase"] == "unknown"


@pytest.mark.asyncio
async def test_new_billing_offline_only_is_allowed_with_stable_live_confirmation(harness):
    h = harness
    set_billing(h, 0)
    pending = issue(h)
    original_start = h.client.start_timing.side_effect

    async def start(card_id, *, instance_id):
        await original_start(card_id, instance_id=instance_id)
        h.resources["attributes"]["current_state"] = "offline"

    h.client.start_timing.side_effect = start
    await h.service.confirm(SCOPE, pending.code, authorized=lambda: True)
    h.client.start_timing.assert_awaited_once()
    h.panel.power.assert_not_awaited()
    assert h.panel.get_live_state.await_count >= 3  # Two ready rounds plus final preflight.
    h.client.switch_modpack.assert_awaited_once()
    assert h.maintenance.get(IDENTIFIER)["phase"] == "submitted"
    assert_billing_not_automatically_closed(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("active_state", ["starting", "running"])
async def test_live_activity_beats_cached_http_offline_for_authorized_billing_stop(harness, active_state):
    h = harness
    set_billing(h, 0)
    pending = issue(h)
    original_start = h.client.start_timing.side_effect

    async def start(card_id, *, instance_id):
        await original_start(card_id, instance_id=instance_id)
        h.resources["attributes"]["current_state"] = "offline"

    h.client.start_timing.side_effect = start
    h.panel.get_live_state.side_effect = [
        {"state": active_state, "stable_offline": False},
        {"state": "offline", "stable_offline": True},
        {"state": "offline", "stable_offline": True},
        {"state": "offline", "stable_offline": True},
    ]
    await h.service.confirm(SCOPE, pending.code, authorized=lambda: True)
    h.panel.power.assert_awaited_once_with(IDENTIFIER, "stop")
    h.client.switch_modpack.assert_awaited_once()
    assert h.panel.get_live_state.await_count == 4
    assert_billing_not_automatically_closed(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("active_state", ["starting", "running", "stopping"])
async def test_existing_billing_live_activity_refuses_even_when_http_says_offline(harness, active_state):
    h = harness
    pending = issue(h)
    h.panel.get_live_state.return_value = {"state": active_state, "stable_offline": False}
    with pytest.raises(service_mod.ModpackError, match="未提交更换整合包"):
        await h.service.confirm(SCOPE, pending.code, authorized=lambda: True)
    h.client.start_timing.assert_not_awaited()
    h.panel.power.assert_not_awaited()
    h.client.switch_modpack.assert_not_awaited()
    assert h.maintenance.get(IDENTIFIER)["phase"] == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("live", [
    {"state": "offline", "stable_offline": False},
    {"state": "unknown", "stable_offline": False},
    {"state": None, "stable_offline": True}, {}, None,
])
async def test_unstable_or_unknown_live_state_does_not_fallback_to_http(harness, live):
    h = harness
    pending = issue(h)
    h.panel.get_live_state.return_value = live
    with pytest.raises(service_mod.ModpackError, match="未提交更换整合包"):
        await h.service.confirm(SCOPE, pending.code, authorized=lambda: True)
    h.panel.get_live_state.assert_awaited()
    h.client.switch_modpack.assert_not_awaited()
    h.panel.power.assert_not_awaited()
    assert h.maintenance.get(IDENTIFIER)["phase"] == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("stable", [None, 1, 0, "true", "false", [], {}])
async def test_non_boolean_live_stability_never_authorizes_install(harness, stable):
    h = harness
    pending = issue(h)
    h.panel.get_live_state.return_value = {"state": "offline", "stable_offline": stable}
    with pytest.raises(service_mod.ModpackError, match="未提交更换整合包"):
        await h.service.confirm(SCOPE, pending.code, authorized=lambda: True)
    h.client.switch_modpack.assert_not_awaited()
    assert h.maintenance.get(IDENTIFIER)["phase"] == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [client_mod.APIError("live disconnected"), client_mod.AuthError("live 401")])
async def test_live_read_failure_keeps_guard_and_does_not_fallback_to_http(harness, failure):
    h = harness
    pending = issue(h)
    h.panel.get_live_state.side_effect = failure
    with pytest.raises(service_mod.ModpackError, match="未提交更换整合包"):
        await h.service.confirm(SCOPE, pending.code, authorized=lambda: True)
    h.panel.get_live_state.assert_awaited_once()
    h.client.switch_modpack.assert_not_awaited()
    h.panel.power.assert_not_awaited()
    assert h.maintenance.get(IDENTIFIER)["phase"] == "unknown"
    assert_billing_not_automatically_closed(h)


@pytest.mark.asyncio
async def test_readonly_live_auth_refresh_is_bounded_and_then_requires_stability(harness):
    h = harness
    pending = issue(h)
    h.panel.get_live_state.side_effect = [
        client_mod.AuthError("expired"),
        {"state": "offline", "stable_offline": True},
        {"state": "offline", "stable_offline": True},
        {"state": "offline", "stable_offline": True},
    ]
    refresh = AsyncMock(return_value=(True, "refreshed"))
    await h.service.confirm(SCOPE, pending.code, authorized=lambda: True, refresh=refresh)
    refresh.assert_awaited_once()
    assert h.panel.get_live_state.await_count == 4
    h.client.switch_modpack.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("final_live", [
    {"state": "running", "stable_offline": False},
    {"state": "offline", "stable_offline": False},
])
async def test_final_preflight_rechecks_live_state_after_two_ready_rounds(harness, final_live):
    h = harness
    pending = issue(h)
    h.panel.get_live_state.side_effect = [
        {"state": "offline", "stable_offline": True},
        {"state": "offline", "stable_offline": True},
        final_live,
    ]
    with pytest.raises(service_mod.ModpackError, match="未提交更换整合包"):
        await h.service.confirm(SCOPE, pending.code, authorized=lambda: True)
    assert h.panel.get_live_state.await_count == 3
    h.client.switch_modpack.assert_not_awaited()
    assert h.maintenance.get(IDENTIFIER)["phase"] == "unknown"


@pytest.mark.asyncio
async def test_stale_http_activity_does_not_send_stop_when_live_is_stably_offline(harness):
    h = harness
    set_billing(h, 0)
    h.info["attributes"]["is_suspended"] = True
    pending = issue(h)
    readings = iter(["running", "offline", "offline", "offline"])
    seen = []

    async def resources(*args):
        current = next(readings)
        seen.append(current)
        return {"attributes": {"current_state": current}}

    h.panel.get_resources.side_effect = resources
    await h.service.confirm(SCOPE, pending.code, authorized=lambda: True)
    assert seen == ["running", "offline", "offline", "offline"]
    h.panel.power.assert_not_awaited()
    h.client.switch_modpack.assert_awaited_once()
    assert h.panel.get_live_state.await_count == 4
    assert_billing_not_automatically_closed(h)


@pytest.mark.asyncio
async def test_installer_snapshot_reads_only_exact_known_file_and_public_metadata(harness):
    h = harness
    item = installer_log(h)
    h.panel.list_directory.return_value.insert(0, {"name": "secrets.env", "size": 999})
    snapshot = await h.service._log_snapshot(h.server)
    h.panel.list_directory.assert_awaited_once_with(IDENTIFIER, "/")
    h.panel.read_file_text.assert_awaited_once_with(IDENTIFIER, "/installserverlogs.log")
    assert snapshot["text"] == h.panel.read_file_text.return_value
    assert snapshot["modified_at"] == datetime.fromisoformat(item["modified_at"]).timestamp()
    assert len(snapshot["content_digest"]) == 64
    assert json.loads(snapshot["stamp"]) == [
        item["created_at"], item["modified_at"], item["size"], snapshot["content_digest"],
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("files", [[], [{"name": "Installserverlogs.log"}], [{"name": "../installserverlogs.log"}], [None]])
async def test_installer_snapshot_missing_exact_file_never_reads_other_content(harness, files):
    h = harness
    h.panel.list_directory.return_value = files
    assert await h.service._log_snapshot(h.server) is None
    h.panel.read_file_text.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("files", [{}, None, "not-a-directory"])
async def test_installer_snapshot_rejects_invalid_directory_shape(harness, files):
    h = harness
    h.panel.list_directory.return_value = files
    with pytest.raises(service_mod.ModpackError, match="目录格式异常"):
        await h.service._log_snapshot(h.server)
    h.panel.read_file_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_installer_snapshot_rejects_duplicate_known_log(harness):
    h = harness
    item = installer_log(h)
    h.panel.list_directory.return_value.append(deepcopy(item))
    with pytest.raises(service_mod.ModpackError, match="重复"):
        await h.service._log_snapshot(h.server)
    h.panel.read_file_text.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"is_file": 1}, {"is_file": False}, {"is_symlink": 0}, {"is_symlink": True},
    {"size": True}, {"size": False}, {"size": -1}, {"size": 1.5}, {"size": "20"},
    {"modified_at": True}, {"modified_at": False}, {"modified_at": None},
    {"modified_at": 1234567890}, {"modified_at": "2026-09-06T12:00:00"},
    {"modified_at": "2026-02-30T12:00:00Z"}, {"modified_at": "invalid"},
])
async def test_installer_snapshot_rejects_unsafe_metadata(harness, changes):
    h = harness
    installer_log(h, **changes)
    with pytest.raises(service_mod.ModpackError):
        await h.service._log_snapshot(h.server)
    h.panel.read_file_text.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [0, service_mod.INSTALL_LOG_LIMIT + 1, 10**12])
async def test_empty_or_oversized_installer_log_is_not_downloaded(harness, size):
    h = harness
    installer_log(h, size=size)
    snapshot = await h.service._log_snapshot(h.server)
    assert snapshot["text"] == "" and snapshot["content_digest"] == ""
    h.panel.read_file_text.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [None, b"bytes", {}, "x" * (service_mod.INSTALL_LOG_LIMIT + 1), "中" * (service_mod.INSTALL_LOG_LIMIT // 2)],
                         ids=["none", "bytes", "object", "too-many-ascii-bytes", "too-many-utf8-bytes"])
async def test_installer_log_actual_content_must_be_bounded_utf8_text(harness, text):
    h = harness
    installer_log(h)
    h.panel.read_file_text.return_value = text
    with pytest.raises(service_mod.ModpackError, match="过大或格式异常"):
        await h.service._log_snapshot(h.server)


@pytest.mark.asyncio
async def test_installer_snapshot_metadata_only_never_downloads_file(harness):
    h = harness
    installer_log(h)
    snapshot = await h.service._log_snapshot(h.server, read_content=False)
    assert snapshot["text"] == "" and snapshot["content_digest"] == ""
    h.panel.read_file_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_fresh_success_after_write_is_persisted_without_releasing_guard(harness):
    h = harness
    entry = begin_written_guard(h)
    installer_log(h)
    result = await h.service.install_status(h.server)
    assert result["outcome"] == "completed" and result["billing_active"] is True
    h.panel.get_server_info.assert_awaited()
    assert h.panel.get_server_info.await_count == 2  # Verify no newer task appeared after reading.
    restarted = state_mod.MaintenanceStore(h.maintenance.db_path)
    assert restarted.get(IDENTIFIER) == {**entry, "install_outcome": "completed"}
    with pytest.raises(state_mod.MaintenanceError):
        restarted.ensure_card_available(h.server.card_id)
    h.client.switch_modpack.assert_not_awaited()
    h.client.start_timing.assert_not_awaited()
    assert_billing_not_automatically_closed(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("guard", [False, True])
async def test_success_without_recorded_write_cannot_complete_new_preflight(harness, guard):
    h = harness
    if guard:
        begin_guard(h, "preparing")
    installer_log(h)
    assert (await h.service.install_status(h.server))["outcome"] == "unknown"
    h.panel.list_directory.assert_not_awaited()
    h.panel.read_file_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_unchanged_baseline_success_is_not_this_installation(harness):
    h = harness
    installer_log(h)
    baseline = await h.service._log_snapshot(h.server)
    begin_written_guard(h, baseline["stamp"])
    assert (await h.service.install_status(h.server))["outcome"] == "unknown"


@pytest.mark.asyncio
async def test_touching_old_success_without_changing_content_does_not_prove_install(harness):
    h = harness
    old = installer_log(h)
    baseline = await h.service._log_snapshot(h.server)
    begin_written_guard(h, baseline["stamp"])
    old["modified_at"] = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    snapshot = await h.service._log_snapshot(h.server)
    assert snapshot["stamp"] != baseline["stamp"]
    assert snapshot["content_digest"] == baseline["content_digest"]
    assert (await h.service.install_status(h.server))["outcome"] == "unknown"


@pytest.mark.asyncio
async def test_changed_log_older_than_write_boundary_is_not_fresh(harness):
    h = harness
    entry = begin_written_guard(h)
    installer_log(h, modified_at=datetime.fromtimestamp(entry["write_started_at"] - 1, timezone.utc).isoformat())
    assert (await h.service.install_status(h.server))["outcome"] == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "整合包安装成功!", "[2026-02-30 01:00:00] 整合包安装成功!",
    "[2026-09-06 01:00:00] 整合包安装成功!\ninstalling next task",
    "prefix [2026-09-06 01:00:00] 整合包安装成功!",
    "[2026-09-06 01:00:00] 整合包安装成功! unexpected trailing text", "",
])
async def test_completion_requires_exact_valid_final_success_line(harness, text):
    h = harness
    begin_written_guard(h)
    installer_log(h, text=text)
    assert (await h.service.install_status(h.server))["outcome"] == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("changes,expected", [
    ({"is_installing": True}, "installing"), ({"status": "installing"}, "installing"),
    ({"status": "reinstalling"}, "installing"), ({"status": "install_failed"}, "unknown"),
    ({"status": "reinstall_failed"}, "unknown"), ({"is_transferring": True}, "unknown"),
    ({"is_node_under_maintenance": True}, "unknown"), ({"status": "future-state"}, "unknown"),
])
async def test_active_or_abnormal_panel_state_overrides_success_log(harness, changes, expected):
    h = harness
    begin_written_guard(h)
    installer_log(h)
    h.info["attributes"].update(changes)
    assert (await h.service.install_status(h.server))["outcome"] == expected
    assert h.maintenance.get(IDENTIFIER)["install_outcome"] == expected
    h.panel.read_file_text.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [{"is_installing": True}, {"is_transferring": True}, {"status": "installing"}, {"identifier": "cafebabe"}])
async def test_install_starting_while_success_log_is_read_prevents_completion(harness, changes):
    h = harness
    begin_written_guard(h)
    installer_log(h)
    latest = deepcopy(h.info)
    latest["attributes"].update(changes)
    h.panel.get_server_info.side_effect = [deepcopy(h.info), latest]
    assert (await h.service.install_status(h.server))["outcome"] == "unknown"
    assert h.maintenance.get(IDENTIFIER)["install_outcome"] == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("paused", [{"is_suspended": True}, {"status": "suspended"}])
async def test_manually_paused_billing_keeps_fresh_verified_install_complete(harness, paused):
    h = harness
    begin_written_guard(h)
    installer_log(h)
    assert (await h.service.install_status(h.server))["outcome"] == "completed"
    h.info["attributes"].update(paused)
    set_billing(h, 0)
    result = await h.service.install_status(h.server)
    assert result["outcome"] == "completed" and result["billing_active"] is False
    assert h.maintenance.get(IDENTIFIER)["install_outcome"] == "completed"
    assert h.panel.read_file_text.await_count == 2
    h.client.start_timing.assert_not_awaited()
    h.client.switch_modpack.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [True, False])
async def test_success_requires_explicit_false_installing_flag(harness, missing):
    h = harness
    begin_written_guard(h)
    installer_log(h)
    if missing:
        del h.info["attributes"]["is_installing"]
    else:
        h.info["attributes"]["is_installing"] = None
    assert (await h.service.install_status(h.server))["outcome"] == "unknown"
    assert h.maintenance.get(IDENTIFIER)["install_outcome"] == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("fresh", [False, True])
async def test_legacy_guard_uses_creation_boundary_and_survives_restart(harness, fresh):
    h = harness
    begin_guard(h, "unknown")
    entry = h.maintenance.get(IDENTIFIER)
    with sqlite3.connect(h.maintenance.db_path) as con:
        con.execute("UPDATE modpack_maintenance SET write_started_at=NULL, baseline_log_stamp=NULL")
    h.service.maintenance = state_mod.MaintenanceStore(h.maintenance.db_path)
    h.service.maintenance.init_db()
    modified = datetime.fromtimestamp(entry["created_at"] + (1 if fresh else -1), timezone.utc)
    installer_log(h, modified_at=modified.isoformat())
    result = await h.service.install_status(h.server)
    assert result["outcome"] == ("completed" if fresh else "unknown")
    assert h.service.maintenance.get(IDENTIFIER)["write_started_at"] is None
    with pytest.raises(state_mod.MaintenanceError):
        h.service.maintenance.ensure_card_available(h.server.card_id)


@pytest.mark.asyncio
async def test_billing_read_failure_does_not_erase_verified_install_result(harness):
    h = harness
    begin_written_guard(h)
    installer_log(h)
    h.client.get_user_packages.side_effect = client_mod.APIError("temporary billing error")
    result = await h.service.install_status(h.server)
    assert result["outcome"] == "completed" and result["billing_active"] is None


@pytest.mark.asyncio
async def test_polling_status_can_skip_billing_entirely(harness):
    h = harness
    begin_written_guard(h)
    installer_log(h)
    result = await h.service.install_status(h.server, include_billing=False)
    assert result["outcome"] == "completed" and result["billing_active"] is None
    h.client.get_user_packages.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt_error", [None, client_mod.APIError("invalid response"), client_mod.AuthError("401")])
async def test_one_post_receipt_is_reconciled_by_fresh_success_not_replayed(harness, receipt_error):
    h = harness
    pending = issue(h)
    log = installer_log(h)
    h.panel.list_directory.side_effect = lambda *args: [log] if h.client.switch_modpack.await_count else []
    if receipt_error is not None:
        h.client.switch_modpack.side_effect = receipt_error
    progress = AsyncMock()
    refresh = AsyncMock(return_value=(True, "refreshed"))
    result = await h.service.confirm(SCOPE, pending.code, authorized=lambda: True, refresh=refresh, progress=progress)
    assert result is pending
    h.client.switch_modpack.assert_awaited_once_with(IDENTIFIER, pending.choice.file_name, pending.choice.item_id)
    refresh.assert_not_awaited()
    entry = assert_archived(h, "completed")
    assert entry["write_started_at"] > 0 and entry["baseline_log_stamp"] == ""
    assert entry["install_outcome"] == "completed"
    assert entry["phase"] == ("unknown" if receipt_error else "submitted")
    assert progress.await_count == 3
    assert_billing_not_automatically_closed(h)
    with pytest.raises(state_mod.ConfirmError):
        await h.service.confirm(SCOPE, pending.code, authorized=lambda: True)
    assert h.client.switch_modpack.await_count == 1


@pytest.mark.asyncio
async def test_confirm_records_existing_content_digest_before_exactly_one_post(harness):
    h = harness
    pending = issue(h)
    old = installer_log(h, text="[2026-01-01 00:00:00] 整合包安装成功!\n")

    async def switch(*args):
        entry = h.maintenance.get(IDENTIFIER)
        assert entry["write_started_at"] > 0
        baseline = json.loads(entry["baseline_log_stamp"])
        assert baseline[:3] == [old["created_at"], old["modified_at"], old["size"]]
        assert len(baseline[3]) == 64
        installer_log(h)
        return {"code": 200}

    h.client.switch_modpack.side_effect = switch
    assert await h.service.confirm(SCOPE, pending.code, authorized=lambda: True) is pending
    assert_archived(h, "completed")
    h.client.switch_modpack.assert_awaited_once()


@pytest.mark.asyncio
async def test_error_receipt_with_only_old_success_stays_unknown_and_protected(harness):
    h = harness
    pending = issue(h)
    installer_log(h)
    h.client.switch_modpack.side_effect = client_mod.APIError("unconfirmed receipt")
    with pytest.raises(service_mod.ModpackError, match="结果未确认"):
        await h.service.confirm(SCOPE, pending.code, authorized=lambda: True)
    entry = h.maintenance.get(IDENTIFIER)
    assert entry["phase"] == "unknown" and entry["install_outcome"] == "unknown"
    assert h.panel.list_directory.await_count >= 2
    h.client.switch_modpack.assert_awaited_once()
    with pytest.raises(state_mod.MaintenanceError):
        h.maintenance.ensure_card_available(h.server.card_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt_error", [False, True])
async def test_explicit_install_failure_is_reported_after_single_post(harness, receipt_error, monkeypatch):
    h = harness
    monkeypatch.setattr(service_mod, "INSTALL_TIMEOUT", .1)
    pending = issue(h)
    observations = []

    async def info(*args):
        result = deepcopy(h.info)
        if h.client.switch_modpack.await_count:
            status = "installing" if not observations else "install_failed"
            observations.append(status)
            result["attributes"]["status"] = status
        return result

    async def switch(*args):
        if receipt_error:
            raise client_mod.APIError("unconfirmed receipt")
        return {"code": 200}

    h.client.switch_modpack.side_effect = switch
    h.panel.get_server_info.side_effect = info
    with pytest.raises(service_mod.ModpackError, match="安装失败.*保护已自动解除"):
        await h.service.confirm(SCOPE, pending.code, authorized=lambda: True)
    assert_archived(h, "failed")
    assert observations == ["installing", "install_failed", "install_failed", "install_failed"]
    h.client.switch_modpack.assert_awaited_once()
    assert_billing_not_automatically_closed(h)


@pytest.mark.asyncio
async def test_baseline_read_failure_prevents_post_and_leaves_zero_write_boundary(harness):
    h = harness
    pending = issue(h)
    h.panel.list_directory.side_effect = client_mod.APIError("files unavailable")
    with pytest.raises(service_mod.ModpackError, match="未提交更换整合包"):
        await h.service.confirm(SCOPE, pending.code, authorized=lambda: True)
    h.client.switch_modpack.assert_not_awaited()
    entry = h.maintenance.get(IDENTIFIER)
    assert entry["phase"] == "unknown" and entry["write_started_at"] == 0


@pytest.mark.asyncio
async def test_cancelling_post_observation_preserves_guard_and_never_retries(harness):
    h = harness
    pending = issue(h)

    async def directory(*args):
        if h.client.switch_modpack.await_count:
            raise asyncio.CancelledError
        return []

    h.panel.list_directory.side_effect = directory
    with pytest.raises(asyncio.CancelledError):
        await h.service.confirm(SCOPE, pending.code, authorized=lambda: True)
    entry = h.maintenance.get(IDENTIFIER)
    assert entry["write_started_at"] > 0 and entry["install_outcome"] == "unknown"
    h.client.switch_modpack.assert_awaited_once()
    assert not h.confirms.cancel(SCOPE)
    assert h.maintenance.get(IDENTIFIER) == entry
    with pytest.raises(state_mod.MaintenanceError):
        h.maintenance.ensure_card_available(h.server.card_id)
    assert_billing_not_automatically_closed(h)


@pytest.mark.asyncio
async def test_read_only_polling_can_recover_transient_error_without_second_post(harness, monkeypatch):
    h = harness
    monkeypatch.setattr(service_mod, "INSTALL_TIMEOUT", .1)
    pending = issue(h)
    log = installer_log(h)
    reads = 0

    async def directory(*args):
        nonlocal reads
        if not h.client.switch_modpack.await_count:
            return []
        reads += 1
        if reads == 1:
            raise client_mod.APIError("transient read failure")
        return [log]

    h.panel.list_directory.side_effect = directory
    h.client.switch_modpack.side_effect = client_mod.APIError("receipt unknown")
    assert await h.service.confirm(SCOPE, pending.code, authorized=lambda: True) is pending
    assert reads == 3  # A final read-only reconciliation runs before automatic release.
    assert_archived(h, "completed")
    h.client.switch_modpack.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["install_failed", "reinstall_failed"])
async def test_fresh_download_log_with_old_failure_flag_does_not_end_new_attempt(harness, status):
    h = harness
    begin_written_guard(h)
    installer_log(h, text="Downloading new modpack: 10%\n")
    h.info["attributes"]["status"] = status
    result = await h.service.install_status(h.server)
    assert result["outcome"] == "unknown"
    assert h.maintenance.get(IDENTIFIER)["install_outcome"] == "unknown"


@pytest.mark.asyncio
async def test_stale_failure_flag_can_clear_and_later_confirm_fresh_success(harness, monkeypatch):
    h = harness
    monkeypatch.setattr(service_mod, "INSTALL_TIMEOUT", .1)
    pending = issue(h)
    log = installer_log(h)
    observations = []

    async def info(*args):
        result = deepcopy(h.info)
        if h.client.switch_modpack.await_count:
            result["attributes"]["status"] = "install_failed" if not observations else None
            observations.append(result["attributes"]["status"])
        return result

    h.panel.get_server_info.side_effect = info
    h.panel.list_directory.side_effect = lambda *args: [log] if h.client.switch_modpack.await_count else []
    h.client.switch_modpack.side_effect = client_mod.APIError("receipt unknown")
    assert await h.service.confirm(SCOPE, pending.code, authorized=lambda: True) is pending
    assert observations == ["install_failed", None, None, None, None, None]
    assert_archived(h, "completed")
    h.client.switch_modpack.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_old_success_line_with_touched_metadata_cannot_complete_attempt(harness, legacy):
    h = harness
    entry = begin_written_guard(h)
    if legacy:
        with sqlite3.connect(h.maintenance.db_path) as con:
            con.execute("UPDATE modpack_maintenance SET write_started_at=NULL, baseline_log_stamp=NULL")
    old = datetime.fromtimestamp(entry["created_at"] - 60, timezone(timedelta(hours=8)))
    installer_log(h, text=f"[{old:%Y-%m-%d %H:%M:%S}] 整合包安装成功!\n")
    assert (await h.service.install_status(h.server))["outcome"] == "unknown"


@pytest.mark.asyncio
async def test_success_line_far_after_file_mtime_is_not_credible(harness):
    h = harness
    begin_written_guard(h)
    future = datetime.now(timezone(timedelta(hours=8))) + timedelta(hours=1)
    installer_log(h, text=f"[{future:%Y-%m-%d %H:%M:%S}] 整合包安装成功!\n")
    assert (await h.service.install_status(h.server))["outcome"] == "unknown"


@pytest.mark.asyncio
async def test_guard_changed_during_log_read_never_reports_old_completed_result(harness):
    h = harness
    begin_written_guard(h)
    installer_log(h)
    old = h.maintenance.get(IDENTIFIER)
    text = h.panel.read_file_text.return_value

    async def read(*args):
        assert h.maintenance.finish(IDENTIFIER)
        begin_written_guard(h)
        return text

    h.panel.read_file_text.side_effect = read
    with pytest.raises(state_mod.MaintenanceError, match="维护记录已变化"):
        await h.service.install_status(h.server)
    current = h.maintenance.get(IDENTIFIER)
    assert current["attempt_id"] != old["attempt_id"]
    assert current["install_outcome"] == "unknown"
    h.client.switch_modpack.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_panel_observation_retains_installing_evidence_for_later_failure(harness):
    h = harness
    begin_written_guard(h)
    h.info["attributes"]["status"] = "installing"
    assert (await h.service.install_status(h.server))["outcome"] == "installing"
    h.info["attributes"]["status"] = "future-state"
    assert (await h.service.install_status(h.server))["outcome"] == "unknown"
    assert h.maintenance.get(IDENTIFIER)["install_outcome"] == "installing"
    h.info["attributes"]["status"] = "install_failed"
    assert (await h.service.install_status(h.server))["outcome"] == "failed"
    assert h.maintenance.get(IDENTIFIER)["install_outcome"] == "failed"


@pytest.mark.asyncio
async def test_full_paused_billing_install_flow_reconciles_bad_receipt_then_automatically_finishes(harness):
    h = harness
    set_billing(h, 0)
    h.info["attributes"].update(is_suspended=True, status="suspended")
    pending = issue(h)
    events = []
    installation_reads = 0
    original_start = h.client.start_timing.side_effect
    original_stop = h.panel.power.side_effect
    h.panel.start_instance = AsyncMock()
    h.panel.reinstall = AsyncMock()  # No guessed or second installation endpoint is permitted.
    log = installer_log(h)

    def authorized():
        events.append("authorized")
        return True

    async def start(card_id, *, instance_id):
        assert "authorized" in events
        assert h.maintenance.get(IDENTIFIER)["phase"] == "preparing"
        events.append("start-billing")
        result = await original_start(card_id, instance_id=instance_id)
        assert h.resources["attributes"]["current_state"] == "starting"
        events.append("platform-auto-start")
        return result

    async def power(instance_id, signal):
        assert signal == "stop"
        events.append("graceful-stop")
        return await original_stop(instance_id, signal)

    async def resources(*args):
        events.append("http:" + h.resources["attributes"]["current_state"])
        return deepcopy(h.resources)

    async def live(*args, **kwargs):
        current = h.resources["attributes"]["current_state"]
        events.append("live:" + current)
        return {"state": current, "stable_offline": current == "offline"}

    async def switch(*args):
        assert h.billing["data"][0]["instances"][0]["timingStatus"] == 1
        assert h.maintenance.get(IDENTIFIER)["write_started_at"] > 0
        between = events[events.index("graceful-stop") + 1:]
        assert between.count("http:offline") >= 2
        assert between.count("live:offline") >= 2
        events.append("write-install")
        raise client_mod.APIError("计时卡 API返回了网页或无效响应")

    async def info(*args):
        nonlocal installation_reads
        result = deepcopy(h.info)
        if h.client.switch_modpack.await_count:
            installation_reads += 1
            if installation_reads == 1:
                result["attributes"].update(is_installing=True, status="installing")
                events.append("observed-installing")
            else:
                result["attributes"].update(is_installing=False, status=None)
                events.append("panel-normal")
        return result

    async def directory(*args):
        return [log] if installation_reads >= 2 else []

    h.client.start_timing.side_effect = start
    h.panel.power.side_effect = power
    h.panel.get_resources.side_effect = resources
    h.panel.get_live_state.side_effect = live
    h.panel.get_server_info.side_effect = info
    h.panel.list_directory.side_effect = directory
    h.client.switch_modpack.side_effect = switch
    assert await h.service.confirm(SCOPE, pending.code, authorized=authorized) is pending
    entry = assert_archived(h, "completed")
    assert entry["phase"] == "unknown" and entry["install_outcome"] == "completed"
    assert state_mod.MaintenanceStore(h.maintenance.db_path).latest(IDENTIFIER) == entry
    events.append("observed-completed")
    events.append("guard-released")
    assert h.maintenance.get(IDENTIFIER) is None
    h.maintenance.ensure_card_available(h.server.card_id)
    assert h.billing["data"][0]["instances"][0]["timingStatus"] == 1
    h.client.start_timing.assert_awaited_once_with(h.server.card_id, instance_id=IDENTIFIER)
    h.panel.power.assert_awaited_once_with(IDENTIFIER, "stop")  # Never start, restart or kill.
    h.client.switch_modpack.assert_awaited_once_with(IDENTIFIER, pending.choice.file_name, pending.choice.item_id)
    h.panel.start_instance.assert_not_awaited()
    h.panel.reinstall.assert_not_awaited()
    assert_billing_not_automatically_closed(h)
    important = {"start-billing", "platform-auto-start", "graceful-stop", "write-install",
                 "observed-installing", "observed-completed", "guard-released"}
    assert [event for event in events if event in important] == [
        "start-billing", "platform-auto-start", "graceful-stop", "write-install",
        "observed-installing", "observed-completed", "guard-released",
    ]


def assert_no_remote_writes(h):
    h.client.start_timing.assert_not_awaited()
    h.client.switch_modpack.assert_not_awaited()
    h.panel.power.assert_not_awaited()
    assert_billing_not_automatically_closed(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["preparing", "submitted", "unknown"])
@pytest.mark.parametrize("status", [None, "", "suspended", "install_failed", "reinstall_failed"])
async def test_reconcile_recovers_prewrite_guard_without_any_remote_write(harness, phase, status):
    h = harness
    begin_guard(h, phase)
    h.info["attributes"]["status"] = status
    original = h.maintenance.get(IDENTIFIER)
    assert original["write_started_at"] == 0
    report = await h.service.reconcile_maintenance(h.server, authorized=lambda: True)
    assert report["outcome"] == "not_submitted"
    assert report["maintenance"] is False and report["released"] is True
    archived = assert_archived(h, "not_submitted")
    assert archived["attempt_id"] == original["attempt_id"]
    assert_no_remote_writes(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"is_installing": True}, {"is_installing": None}, {"is_installing": 0},
    {"is_installing": "false"}, {"is_transferring": True},
    {"is_node_under_maintenance": True}, {"status": "future-status"},
    {"status": "installing"}, {"status": "reinstalling"}, {"status": "restoring_backup"},
])
async def test_reconcile_prewrite_requires_strict_terminal_flags(harness, changes):
    h = harness
    begin_guard(h, "unknown")
    h.info["attributes"].update(changes)
    report = await h.service.reconcile_maintenance(h.server, authorized=lambda: True)
    assert report["maintenance"] is True and report["released"] is False
    assert h.maintenance.get(IDENTIFIER) is not None
    assert_no_remote_writes(h)


@pytest.mark.asyncio
async def test_missing_installing_flag_cannot_recover_prewrite_guard(harness):
    h = harness
    begin_guard(h, "unknown")
    del h.info["attributes"]["is_installing"]
    report = await h.service.reconcile_maintenance(h.server, authorized=lambda: True)
    assert report["maintenance"] is True and report["released"] is False
    assert h.maintenance.get(IDENTIFIER) is not None
    assert_no_remote_writes(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["submitted", "unknown"])
async def test_unknown_written_attempt_cannot_be_manually_acknowledged_away(harness, phase):
    h = harness
    begin_written_guard(h, phase=phase)
    report = await h.service.reconcile_maintenance(h.server, authorized=lambda: True)
    assert report["outcome"] == "unknown" and report["maintenance"] is True
    assert report["released"] is False
    with pytest.raises(service_mod.ModpackError, match="维护保护保留"):
        await h.service.finish_maintenance(h.server, authorized=lambda: True)
    assert h.maintenance.get(IDENTIFIER)["phase"] == phase
    assert_no_remote_writes(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["completed", "failed"])
async def test_reconcile_terminal_attempt_archives_then_finish_is_idempotent(harness, outcome):
    h = harness
    entry = begin_written_guard(h, phase="unknown")
    if outcome == "completed":
        installer_log(h)
    else:
        h.maintenance.observe(entry, "installing")
        h.info["attributes"]["status"] = "install_failed"
    report = await h.service.reconcile_maintenance(h.server, authorized=lambda: True)
    assert report["outcome"] == outcome
    assert report["maintenance"] is False and report["released"] is True
    archived = assert_archived(h, outcome)
    assert await h.service.finish_maintenance(h.server, authorized=lambda: True)
    assert h.maintenance.latest(IDENTIFIER) == archived
    with sqlite3.connect(h.maintenance.db_path) as connection:
        assert connection.execute("SELECT count(*) FROM modpack_history").fetchone()[0] == 1
    assert_no_remote_writes(h)


@pytest.mark.asyncio
async def test_status_query_reads_history_without_observing_or_releasing_again(harness, monkeypatch):
    h = harness
    begin_written_guard(h)
    installer_log(h)
    await h.service.reconcile_maintenance(h.server, authorized=lambda: True)
    original = assert_archived(h, "completed")
    observer = Mock(side_effect=AssertionError("history must not be observed as an active guard"))
    finisher = Mock(side_effect=AssertionError("plain status must not release a guard"))
    monkeypatch.setattr(h.maintenance, "observe", observer)
    monkeypatch.setattr(h.maintenance, "finish", finisher)
    report = await h.service.install_status(h.server)
    assert report["outcome"] == "completed"
    assert report["maintenance"] is False and report["released"] is False
    assert h.maintenance.latest(IDENTIFIER) == original
    h.panel.list_directory.return_value = []
    assert (await h.service.install_status(h.server))["outcome"] == "unknown"
    observer.assert_not_called()
    finisher.assert_not_called()
    assert_no_remote_writes(h)


@pytest.mark.asyncio
async def test_read_only_status_never_releases_even_a_verified_completed_guard(harness, monkeypatch):
    h = harness
    begin_written_guard(h)
    installer_log(h)
    finisher = Mock(side_effect=AssertionError("status has no release authority"))
    monkeypatch.setattr(h.maintenance, "finish", finisher)
    report = await h.service.install_status(h.server)
    assert report["outcome"] == "completed"
    assert report["maintenance"] is True and report["released"] is False
    assert h.maintenance.get(IDENTIFIER)["install_outcome"] == "completed"
    finisher.assert_not_called()
    assert_no_remote_writes(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["before", "final"])
async def test_reconcile_permission_denial_never_releases_completed_guard(harness, stage):
    h = harness
    begin_written_guard(h)
    installer_log(h)
    allowed = stage != "before"
    reads = 0

    async def info(*args):
        nonlocal reads, allowed
        reads += 1
        if reads == 3:
            allowed = False
        return deepcopy(h.info)

    h.panel.get_server_info.side_effect = info
    with pytest.raises(state_mod.ConfirmError):
        await h.service.reconcile_maintenance(h.server, authorized=lambda: allowed)
    assert reads == (0 if stage == "before" else 3)
    assert h.maintenance.get(IDENTIFIER) is not None
    assert_no_remote_writes(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"is_installing": True}, {"status": "future-state"}, {"is_transferring": True}])
async def test_reconcile_final_terminal_recheck_prevents_stale_success_release(harness, change):
    h = harness
    begin_written_guard(h)
    installer_log(h)
    reads = 0

    async def info(*args):
        nonlocal reads
        reads += 1
        result = deepcopy(h.info)
        if reads >= 3:
            result["attributes"].update(change)
        return result

    h.panel.get_server_info.side_effect = info
    report = await h.service.reconcile_maintenance(h.server, authorized=lambda: True)
    assert report["maintenance"] is True and report["released"] is False
    assert h.maintenance.get(IDENTIFIER) is not None
    assert_no_remote_writes(h)


@pytest.mark.asyncio
async def test_auto_release_happens_inside_existing_operation_lock(harness, monkeypatch):
    h = harness
    pending = issue(h)
    log = installer_log(h)
    h.panel.list_directory.side_effect = lambda *args: [log] if h.client.switch_modpack.await_count else []
    original_finish = h.maintenance.finish
    snapshots = []

    def finish(instance_id, *, expected=None, reason="manual"):
        assert operations._card_locks[h.server.card_id].locked()
        assert expected and expected["attempt_id"] == h.maintenance.get(instance_id)["attempt_id"]
        snapshots.append(expected)
        return original_finish(instance_id, expected=expected, reason=reason)

    monkeypatch.setattr(h.maintenance, "finish", finish)
    assert await h.service.confirm(SCOPE, pending.code, authorized=lambda: True) is pending
    assert len(snapshots) == 1
    assert_archived(h, "completed")
    assert not operations._card_locks[h.server.card_id].locked()
    h.client.switch_modpack.assert_awaited_once()
    assert_billing_not_automatically_closed(h)


@pytest.mark.asyncio
async def test_auto_release_archive_failure_keeps_guard_and_never_replays_install(harness):
    h = harness
    pending = issue(h)
    log = installer_log(h)
    h.panel.list_directory.side_effect = lambda *args: [log] if h.client.switch_modpack.await_count else []
    with sqlite3.connect(h.maintenance.db_path) as connection:
        connection.execute("CREATE TRIGGER fail_archive BEFORE INSERT ON modpack_history "
                           "BEGIN SELECT RAISE(ABORT, 'private-archive-failure'); END")
    with pytest.raises(state_mod.MaintenanceError) as error:
        await h.service.confirm(SCOPE, pending.code, authorized=lambda: True)
    assert "private-archive-failure" not in str(error.value)
    assert h.maintenance.get(IDENTIFIER)["install_outcome"] == "completed"
    h.client.switch_modpack.assert_awaited_once()
    with pytest.raises(state_mod.ConfirmError):
        await h.service.confirm(SCOPE, pending.code, authorized=lambda: True)
    assert h.client.switch_modpack.await_count == 1
    assert_billing_not_automatically_closed(h)


@pytest.mark.asyncio
async def test_reconcile_stale_attempt_cannot_release_replacement_guard(harness):
    h = harness
    old = begin_written_guard(h)
    installer_log(h)
    reads = 0

    async def info(*args):
        nonlocal reads
        reads += 1
        if reads == 3:
            h.maintenance.finish(IDENTIFIER, expected=old)
            begin_written_guard(h)
        return deepcopy(h.info)

    h.panel.get_server_info.side_effect = info
    with pytest.raises(state_mod.MaintenanceError, match="维护记录已变化"):
        await h.service.reconcile_maintenance(h.server, authorized=lambda: True)
    current = h.maintenance.get(IDENTIFIER)
    assert current["attempt_id"] != old["attempt_id"] and current["install_outcome"] == "unknown"
    assert_no_remote_writes(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("same_card", [True, False])
async def test_reconcile_for_operation_uses_guard_owner_not_target_credentials(harness, same_card):
    h = harness
    owner = h.server
    begin_guard(h, "unknown")
    target = SimpleNamespace(**{**vars(owner), "name": "other-config", "token": "target-token",
                               "account_phone": "target-account", "created_at": 200,
                               "card_id": owner.card_id if same_card else "other-card",
                               "instance_uuid": "cafebabe" if same_card else UUID})
    configs = {owner.name: owner, target.name: target}
    h.service.get_server = configs.get
    result = await h.service.reconcile_for_operation(target, authorized=lambda: True)
    assert result["released"] is True and result["maintenance"] is False
    assert_archived(h, "not_submitted")
    assert h.built_panels and all(token == owner.token for token, _ in h.built_panels)
    assert h.built_clients and all(token == owner.token for token, _ in h.built_clients)
    assert_no_remote_writes(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["missing_owner", "card", "instance", "created_at", "unauthorized"])
async def test_reconcile_for_operation_rejects_unresolved_owner_or_permission(harness, fault):
    h = harness
    owner = h.server
    begin_guard(h, "unknown")
    target = SimpleNamespace(**{**vars(owner), "name": "other-config", "token": "target-token"})
    if fault == "card":
        owner.card_id = "replacement-card"
    elif fault == "instance":
        owner.instance_uuid = "cafebabe"
    elif fault == "created_at":
        owner.created_at += 1
    configs = {target.name: target}
    if fault != "missing_owner":
        configs[owner.name] = owner
    h.service.get_server = configs.get
    expected = state_mod.ConfirmError if fault == "unauthorized" else service_mod.ModpackError
    with pytest.raises(expected):
        await h.service.reconcile_for_operation(target, authorized=lambda: fault != "unauthorized")
    assert h.maintenance.get(IDENTIFIER) is not None
    h.panel.get_server_info.assert_not_awaited()
    assert not h.built_clients and not h.built_panels
    assert_no_remote_writes(h)


@pytest.mark.asyncio
async def test_reconcile_for_operation_unknown_guard_blocks_target_without_writes(harness):
    h = harness
    owner = h.server
    begin_written_guard(h, phase="unknown")
    target = SimpleNamespace(**{**vars(owner), "name": "alias", "token": "target-token"})
    h.service.get_server = {owner.name: owner, target.name: target}.get
    with pytest.raises(service_mod.ModpackError, match="仍受维护保护"):
        await h.service.reconcile_for_operation(target, authorized=lambda: True)
    assert h.maintenance.get(IDENTIFIER) is not None
    assert_no_remote_writes(h)


@pytest.mark.asyncio
async def test_install_log_redacts_actual_panel_account_and_server_secrets(harness):
    h = harness
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJsb2cifQ.signature"
    secrets = [h.server.token, "fake-panel-api-key", "fake-session-secret", "fake-xsrf-secret", jwt]
    installer_log(h, text="\x1b[31mprogress\x1b[0m\n" + "\n".join(secrets)
                  + "\nhttps://download.example.test/private-access-token.zip\nlast useful line\n")
    text = await h.service.install_log(h.server)
    assert "progress" in text and "last useful line" in text
    assert "[REDACTED]" in text and "[下载链接已隐藏]" in text
    assert "\x1b" not in text and "private-access-token" not in text
    for secret in secrets:
        assert secret not in text
    h.panel.read_file_text.assert_awaited_once_with(IDENTIFIER, "/installserverlogs.log")
    assert_no_remote_writes(h)


@pytest.mark.asyncio
async def test_install_log_returns_only_last_30_lines_and_at_most_3000_characters(harness):
    h = harness
    installer_log(h, text="\n".join(f"line-{index:02d} " + "x" * 120 for index in range(50)))
    text = await h.service.install_log(h.server)
    assert len(text) <= 3000 and len(text.splitlines()) <= 30
    assert "line-00" not in text and "line-49" in text
    assert_no_remote_writes(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("server_name", "deleted-original-config"), ("card_id", "old-card"),
    ("server_created_at", 99),
])
@pytest.mark.parametrize("method", ["reconcile_maintenance", "finish_maintenance"])
async def test_direct_reconcile_rejects_guard_bound_to_old_server_configuration(harness, field, value, method):
    h = harness
    begin_guard(h, "unknown")
    with sqlite3.connect(h.maintenance.db_path) as connection:
        connection.execute(f"UPDATE modpack_maintenance SET {field}=?", (value,))
    original = h.maintenance.get(IDENTIFIER)
    with pytest.raises(service_mod.ModpackError, match="绑定"):
        await getattr(h.service, method)(h.server, authorized=lambda: True)
    assert h.maintenance.get(IDENTIFIER) == original
    h.panel.get_server_info.assert_not_awaited()
    assert_no_remote_writes(h)


@pytest.mark.asyncio
async def test_cancellation_during_automatic_release_recheck_preserves_written_guard(harness):
    h = harness
    pending = issue(h)
    log = installer_log(h)
    h.panel.list_directory.side_effect = lambda *args: [log] if h.client.switch_modpack.await_count else []
    reads_after_post = 0

    async def info(*args):
        nonlocal reads_after_post
        if h.client.switch_modpack.await_count:
            reads_after_post += 1
            if reads_after_post == 5:
                raise asyncio.CancelledError()
        return deepcopy(h.info)

    h.panel.get_server_info.side_effect = info
    with pytest.raises(asyncio.CancelledError):
        await h.service.confirm(SCOPE, pending.code, authorized=lambda: True)
    assert reads_after_post == 5
    entry = h.maintenance.get(IDENTIFIER)
    assert entry["write_started_at"] > 0 and entry["install_outcome"] == "completed"
    assert "released_at" not in entry
    assert not h.confirms.cancel(SCOPE)
    with pytest.raises(state_mod.ConfirmError):
        await h.service.confirm(SCOPE, pending.code, authorized=lambda: True)
    assert h.maintenance.get(IDENTIFIER) == entry
    h.client.switch_modpack.assert_awaited_once()
    assert_billing_not_automatically_closed(h)
