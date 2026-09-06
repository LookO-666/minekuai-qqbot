"""Offline service orchestration with fake APIs and temporary SQLite guards."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import asdict, replace
import importlib
from pathlib import Path
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
        row=catalog_row(), built_clients=[], built_panels=[],
    )
    h.client = SimpleNamespace(
        search_modpacks=AsyncMock(side_effect=lambda *args, **kwargs: root(deepcopy(h.row))),
        list_modpack_versions=AsyncMock(side_effect=lambda *args, **kwargs: root(deepcopy(h.row))),
        switch_modpack=AsyncMock(return_value={"code": 200}),
    )
    h.panel = SimpleNamespace(
        get_server_info=AsyncMock(side_effect=lambda *args: deepcopy(h.info)),
        get_resources=AsyncMock(side_effect=lambda *args: deepcopy(h.resources)),
    )
    h.confirms = state_mod.InstallConfirmStore(clock=lambda: h.now)
    h.maintenance = state_mod.MaintenanceStore(tmp_path / "maintenance.db")
    h.maintenance.init_db()
    monkeypatch.setattr(operations, "_card_locks", {})
    monkeypatch.setattr(operations, "_maintenance_guard", h.maintenance.ensure_card_available)

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
    with pytest.raises(type(failure)):
        await h.service.confirm(SCOPE, pending.code, authorized=lambda: True, refresh=refresh)
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
