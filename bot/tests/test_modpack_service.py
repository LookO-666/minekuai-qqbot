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
        power=AsyncMock(side_effect=power),
    )
    h.confirms = state_mod.InstallConfirmStore(clock=lambda: h.now)
    h.maintenance = state_mod.MaintenanceStore(tmp_path / "maintenance.db")
    h.maintenance.init_db()
    monkeypatch.setattr(operations, "_card_locks", {})
    monkeypatch.setattr(operations, "_maintenance_guard", h.maintenance.ensure_card_available)
    monkeypatch.setattr(service_mod, "READY_ATTEMPTS", 5, raising=False)
    monkeypatch.setattr(service_mod, "READY_INTERVAL", 0, raising=False)
    monkeypatch.setattr(service_mod, "READY_TIMEOUT", 1, raising=False)

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
