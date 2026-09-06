"""Manual control reconciliation must not weaken guards, locks, or permissions."""
import ast
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import re
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


PLUGIN = Path(__file__).parents[1] / "plugins" / "minekuai"
sys.path.insert(0, str(PLUGIN))
import operations
from client import AuthError, MinekuaiError, RateLimitError
from modpack_state import ConfirmError, MaintenanceError, ServerIdentity


@pytest.fixture
def manual(monkeypatch):
    monkeypatch.setattr(operations, "_card_locks", {})
    monkeypatch.setattr(operations, "_related_locks", {})
    monkeypatch.setattr(operations, "_related_lock_keys", lambda card: ["instance:deadbeef"])

    class Finish(Exception):
        pass

    class Event:
        user_id = 11
        group_id = 22

    h = SimpleNamespace(
        server=SimpleNamespace(name="test", card_id="card", instance_uuid="deadbeef",
            account_phone="phone", created_at=123, token="original-token", address=""),
        event=Event(), user_allowed=True, admin_allowed=True, maintained=False,
        on_locked=None, before_panel=None, events=[], Finish=Finish,
    )

    def guard(card):
        if h.maintained:
            raise MaintenanceError("安装结果未知，请发整合包状态或整合包日志")

    monkeypatch.setattr(operations, "_maintenance_guard", guard)

    async def finish(message):
        raise Finish(message)

    def current(identity):
        if h.server is None or not identity.matches(h.server):
            raise MinekuaiError("服务器绑定已变化")
        return h.server

    async def reconcile(server, *, authorized, refresh):
        assert authorized()
        h.events.append("reconcile")
        if h.maintained:
            raise MinekuaiError("安装中或结果未知，请发整合包状态或整合包日志")
        return {"released": False, "maintenance": False}

    @asynccontextmanager
    async def operation(card_id, **kwargs):
        assert kwargs.get("allow_maintenance", False) is False
        async with operations.card_operation(card_id, **kwargs):
            h.events.append("control-lock")
            if h.on_locked:
                h.on_locked()
            yield

    def assert_control_lock():
        assert operations._card_locks["card"].locked()
        assert operations._related_locks["instance:deadbeef"].locked()
        h.events.append("write")

    async def locked(*args):
        assert_control_lock()

    async def power(instance_id, signal):
        assert_control_lock()
        assert instance_id == "deadbeef" and signal == "restart"

    async def panel_call(matcher, event, server, fn):
        if h.before_panel:
            h.before_panel()
        try:
            return await fn(h.panel), "ok", h.server
        except (operations.OperationBusyError, MinekuaiError) as exc:
            return None, str(exc), h.server

    h.reconcile = AsyncMock(side_effect=reconcile)
    h.refresh = AsyncMock()
    h.matcher = SimpleNamespace(finish=finish, reject=finish, send=AsyncMock())
    h.panel = SimpleNamespace(power=AsyncMock(side_effect=power))
    h.start = AsyncMock(side_effect=locked)
    h.stop = AsyncMock(side_effect=locked)
    h.servers = SimpleNamespace(
        get_server=lambda name: h.server if h.server and h.server.name == name else None,
        mark_server_started=Mock(),
    )
    namespace = {
        "ServerIdentity": ServerIdentity, "MaintenanceError": MaintenanceError,
        "ConfirmError": ConfirmError, "MinekuaiError": MinekuaiError,
        "OperationBusyError": operations.OperationBusyError, "card_operation": operation,
        "modpack_service": SimpleNamespace(current=current, reconcile_for_operation=h.reconcile),
        "_modpack_refresh_factory": lambda matcher, event: h.refresh,
        "_check_perm": lambda event: (h.user_allowed, "无权限"),
        "_check_admin_perm": lambda event: (h.user_allowed and h.admin_allowed, "无管理员权限"),
        "_pick_target_server": lambda *args: (h.server, None),
        "_with_panel_refresh": panel_call, "_user_display_name": lambda event: "tester",
        "_start_server_locked": h.start, "_stop_server_locked": h.stop,
        "servers": h.servers, "CANCEL_WORDS": {"取消"},
        "CommandArg": lambda: None, "ArgPlainText": lambda name: None,
        "GroupMessageEvent": Event,
        "idle_watcher": SimpleNamespace(cancel_keepalive=Mock(), mark_opened=Mock(), watch_for_ready=Mock()),
        "log_operation": Mock(),
    }
    names = {"_manual_control_current", "_reconcile_manual_control", "_start_step", "_do_stop", "_restart"}
    tree = ast.parse((PLUGIN / "__init__.py").read_text(encoding="utf-8"))
    nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    assert {node.name for node in nodes} == names
    for node in nodes:
        node.decorator_list = []
    module = ast.Module(body=ast.parse("from __future__ import annotations").body + nodes, type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "manual-control-handlers", "exec"), namespace)
    h.namespace = namespace
    return h


async def invoke(h, operation):
    if operation == "start":
        return await h.namespace["_start_step"](h.matcher, h.event, "test")
    if operation == "stop":
        return await h.namespace["_do_stop"](h.matcher, h.event, "tester", h.server)
    return await h.namespace["_restart"](
        h.matcher, h.event, SimpleNamespace(extract_plain_text=lambda: "test"))


async def invoke_success(h, operation):
    if operation == "restart":
        with pytest.raises(h.Finish, match="重启"):
            await invoke(h, operation)
    else:
        await invoke(h, operation)


def assert_no_control(h):
    h.start.assert_not_awaited()
    h.stop.assert_not_awaited()
    h.panel.power.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["start", "stop", "restart"])
async def test_manual_controls_reconcile_before_original_guarded_write(manual, operation):
    h = manual
    await invoke_success(h, operation)
    h.reconcile.assert_awaited_once()
    assert h.reconcile.await_args.kwargs["refresh"] is h.refresh
    assert h.events == ["reconcile", "control-lock", "write"]
    h.matcher.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["start", "stop", "restart"])
async def test_completed_guard_is_released_before_normal_control(manual, operation):
    h = manual
    h.maintained = True

    async def reconcile(*args, **kwargs):
        assert kwargs["authorized"]()
        h.events.append("reconcile")
        h.maintained = False
        return {"released": True, "maintenance": False}

    h.reconcile.side_effect = reconcile
    await invoke_success(h, operation)
    assert h.events == ["reconcile", "control-lock", "write"]
    h.matcher.send.assert_awaited_once()
    assert "自动解除" in h.matcher.send.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["start", "stop", "restart"])
async def test_active_or_unknown_maintenance_never_reaches_control(manual, operation):
    h = manual
    h.maintained = True
    with pytest.raises(h.Finish, match="整合包状态"):
        await invoke(h, operation)
    assert_no_control(h)
    assert h.maintained and "control-lock" not in h.events


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [MinekuaiError("API error"), MaintenanceError("DB error"),
    ConfirmError("权限变化"), operations.OperationBusyError("另一个操作正在处理")])
async def test_reconcile_failures_stop_the_manual_operation(manual, failure):
    h = manual
    h.reconcile.side_effect = failure
    with pytest.raises(h.Finish, match=str(failure)):
        await invoke(h, "start")
    assert_no_control(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("report", [None, {}, {"released": True}, {"maintenance": True}, {"maintenance": 0}])
async def test_malformed_or_still_maintained_report_cannot_bypass_guard(manual, report):
    h = manual
    h.reconcile.side_effect = None
    h.reconcile.return_value = report
    with pytest.raises(h.Finish, match="维护状态尚未确认"):
        await invoke(h, "start")
    assert_no_control(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["start", "stop", "restart"])
async def test_permission_denied_before_reconcile_has_no_api_or_control(manual, operation):
    h = manual
    h.user_allowed = False
    with pytest.raises(h.Finish, match="权限"):
        await invoke(h, operation)
    h.reconcile.assert_not_awaited()
    assert_no_control(h)


@pytest.mark.asyncio
async def test_restart_never_inherits_regular_user_permission(manual):
    h = manual
    h.admin_allowed = False
    with pytest.raises(h.Finish, match="管理员"):
        await invoke(h, "restart")
    h.reconcile.assert_not_awaited()
    assert_no_control(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["start", "stop", "restart"])
async def test_permission_revocation_during_reconcile_prevents_write(manual, operation):
    h = manual

    async def reconcile(*args, **kwargs):
        h.user_allowed = False
        assert kwargs["authorized"]() is False
        return {"released": True, "maintenance": False}

    h.reconcile.side_effect = reconcile
    with pytest.raises(h.Finish, match="权限"):
        await invoke(h, operation)
    assert_no_control(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("name", "new"), ("card_id", "new-card"),
    ("instance_uuid", "cafebabe"), ("account_phone", "other"), ("created_at", 124)])
async def test_binding_change_during_reconcile_cannot_control_new_target(manual, field, value):
    h = manual

    async def reconcile(*args, **kwargs):
        setattr(h.server, field, value)
        return {"released": False, "maintenance": False}

    h.reconcile.side_effect = reconcile
    with pytest.raises(h.Finish, match="绑定已变化"):
        await invoke(h, "start")
    assert_no_control(h)


@pytest.mark.asyncio
async def test_token_refresh_preserves_identity_and_uses_fresh_target(manual):
    h = manual

    async def reconcile(*args, **kwargs):
        h.server = SimpleNamespace(**{**vars(h.server), "token": "fresh-token"})
        return {"released": False, "maintenance": False}

    h.reconcile.side_effect = reconcile
    await invoke(h, "start")
    assert h.start.await_args.args[-1] is h.server
    assert h.start.await_args.args[-1].token == "fresh-token"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["start", "stop", "restart"])
@pytest.mark.parametrize("change", ["permission", "identity"])
async def test_identity_and_permission_are_rechecked_inside_control_lock(manual, operation, change):
    h = manual

    def mutate():
        if change == "permission":
            h.user_allowed = False
        else:
            h.server.instance_uuid = "cafebabe"

    h.on_locked = mutate
    with pytest.raises(h.Finish, match="权限|绑定已变化"):
        await invoke(h, operation)
    assert "control-lock" in h.events
    assert_no_control(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["start", "stop", "restart"])
async def test_new_guard_between_reconcile_and_control_is_not_bypassed(manual, operation):
    h = manual

    async def reconcile(*args, **kwargs):
        h.maintained = True
        return {"released": True, "maintenance": False}

    h.reconcile.side_effect = reconcile
    with pytest.raises(h.Finish, match="整合包状态"):
        await invoke(h, operation)
    assert_no_control(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["start", "stop", "restart"])
async def test_reconcile_does_not_bypass_other_controls_held_instance_lock(manual, operation):
    h = manual
    async with operations.card_operation("other-card"):
        with pytest.raises(h.Finish, match="正在处理"):
            await invoke(h, operation)
    assert_no_control(h)


def test_reconciliation_is_not_added_to_background_or_generic_control_wrappers():
    tree = ast.parse((PLUGIN / "__init__.py").read_text(encoding="utf-8"))
    callers = {
        node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for call in ast.walk(node) if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name) and call.func.id == "_reconcile_manual_control"
    }
    assert callers == {"_start_step", "_do_stop", "_restart"}


def test_modpack_help_supports_entirely_in_group_status_and_logs():
    tree = ast.parse((PLUGIN / "__init__.py").read_text(encoding="utf-8"))
    value = next(node.value for node in tree.body if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id == "MODPACK_HELP" for target in node.targets))
    help_text = ast.literal_eval(value)
    assert "整合包日志" in help_text and "自动解除" in help_text
    assert "我已核对" not in help_text and "官网核对" not in help_text


@pytest.fixture
def billing_start(manual, monkeypatch):
    h = manual
    h.active = True
    h.server.client_id = "fake-client"
    h.built_clients = []
    h.sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", h.sleep)

    async def billing(server, identifier, *, refresh):
        h.events.append("read-billing")
        return h.active

    async def open_billing(**kwargs):
        h.events.append("open-billing")

    async def start_game(matcher, event, server):
        h.events.append("start-game")
        return True, "started"

    @asynccontextmanager
    async def client(server):
        h.built_clients.append(dict(vars(server)))
        yield SimpleNamespace(open_timing_only=h.open_billing)

    h.billing = AsyncMock(side_effect=billing)
    h.open_billing = AsyncMock(side_effect=open_billing)
    h.start_game = AsyncMock(side_effect=start_game)
    h.token_refresh = AsyncMock(return_value=(True, "refreshed"))
    h.namespace["modpack_service"].billing_active = h.billing
    h.namespace.update({
        "re": re, "AuthError": AuthError, "RateLimitError": RateLimitError,
        "MatcherException": h.Finish, "logger": Mock(),
        "_build_client": client, "_start_instance": h.start_game,
        "_interactive_verification_provider": lambda *args: None,
        "_refresh_token_for": h.token_refresh, "_mask_phone": lambda phone: "***",
        "update_cooldown": Mock(),
    })
    names = {"_manual_start_billing_status", "_start_server_locked"}
    tree = ast.parse((PLUGIN / "__init__.py").read_text(encoding="utf-8"))
    nodes = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name in names]
    assert {node.name for node in nodes} == names
    module = ast.Module(body=ast.parse("from __future__ import annotations").body + nodes, type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "manual-billing-start", "exec"), h.namespace)
    return h


@pytest.mark.asyncio
async def test_active_billing_after_install_starts_game_without_reopening_card(billing_start):
    h = billing_start
    with pytest.raises(h.Finish, match="计时卡已开启"):
        await invoke(h, "start")
    h.billing.assert_awaited_once_with(h.server, "deadbeef", refresh=h.refresh)
    h.open_billing.assert_not_awaited()
    h.sleep.assert_not_awaited()
    h.start_game.assert_awaited_once_with(h.matcher, h.event, h.server)
    assert h.events == ["reconcile", "control-lock", "read-billing", "start-game"]


@pytest.mark.asyncio
async def test_paused_billing_opens_exact_card_once_before_game_start(billing_start):
    h = billing_start
    h.active = False
    with pytest.raises(h.Finish, match="计时卡已开启"):
        await invoke(h, "start")
    h.open_billing.assert_awaited_once_with(card_id="card", instance_id="deadbeef")
    h.sleep.assert_awaited_once_with(2)
    h.start_game.assert_awaited_once()
    assert h.events == ["reconcile", "control-lock", "read-billing", "open-billing", "start-game"]


@pytest.mark.asyncio
async def test_billing_query_failure_never_guesses_or_sends_open_card(billing_start):
    h = billing_start
    h.billing.side_effect = MinekuaiError("无法确认计费状态")
    with pytest.raises(h.Finish, match="无法确认计费状态"):
        await invoke(h, "start")
    h.open_billing.assert_not_awaited()
    h.start_game.assert_not_awaited()
    assert h.built_clients == []


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", [None, 0, 1, "true", {}, []])
async def test_unknown_billing_status_cannot_trigger_write(billing_start, unknown):
    h = billing_start
    h.billing.side_effect = None
    h.billing.return_value = unknown
    with pytest.raises(h.Finish, match="计费状态未知"):
        await invoke(h, "start")
    h.open_billing.assert_not_awaited()
    h.start_game.assert_not_awaited()


@pytest.mark.asyncio
async def test_card_only_configuration_retains_legacy_billing_workflow(billing_start):
    h = billing_start
    h.server.instance_uuid = ""
    with pytest.raises(h.Finish, match="计时卡已开启"):
        await invoke(h, "start")
    h.billing.assert_not_awaited()
    h.open_billing.assert_awaited_once_with(card_id="card", instance_id="")
    h.start_game.assert_not_awaited()
    h.sleep.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("instance", [
    "DEADBEEF", "DEADBEEF-1234-5678-90AB-1234567890AB", "DEADBEEF1234567890AB1234567890AB",
])
@pytest.mark.parametrize("active", [False, True])
async def test_valid_complete_ids_map_to_exact_short_billing_identifier(billing_start, instance, active):
    h = billing_start
    h.server.instance_uuid = instance
    h.active = active
    with pytest.raises(h.Finish, match="计时卡已开启"):
        await invoke(h, "start")
    h.billing.assert_awaited_once_with(h.server, "deadbeef", refresh=h.refresh)
    h.start_game.assert_awaited_once_with(h.matcher, h.event, h.server)
    if active:
        h.open_billing.assert_not_awaited()
    else:
        h.open_billing.assert_awaited_once_with(card_id="card", instance_id="deadbeef")


@pytest.mark.asyncio
@pytest.mark.parametrize("instance", [
    "deadbeef/../../other", "deadbeef-not-a-uuid", "deadbeef-1234-5678-90ab-short",
    "zzzzzzzz", "deadbee", "deadbeef ", 123, True,
])
async def test_invalid_id_is_not_blindly_truncated_for_billing(billing_start, instance):
    h = billing_start
    h.server.instance_uuid = instance
    with pytest.raises(h.Finish, match="实例 ID 格式无效"):
        await invoke(h, "start")
    h.billing.assert_not_awaited()
    h.open_billing.assert_not_awaited()
    h.start_game.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [False, True])
async def test_billing_read_refresh_uses_fresh_credentials_for_control(billing_start, active):
    h = billing_start

    async def refresh(server):
        h.server = SimpleNamespace(**{**vars(h.server), "token": "fresh-token", "client_id": "fresh-client"})
        return True, "refreshed"

    async def billing(server, identifier, *, refresh):
        assert await refresh(server) == (True, "refreshed")
        return active

    h.refresh.side_effect = refresh
    h.billing.side_effect = billing
    with pytest.raises(h.Finish, match="计时卡已开启"):
        await invoke(h, "start")
    h.refresh.assert_awaited_once()
    h.start_game.assert_awaited_once_with(h.matcher, h.event, h.server)
    if not active:
        assert h.built_clients[0]["token"] == "fresh-token"
        assert h.built_clients[0]["client_id"] == "fresh-client"
        h.open_billing.assert_awaited_once()
    else:
        h.open_billing.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["permission", "identity"])
@pytest.mark.parametrize("active", [False, True])
async def test_billing_read_permission_or_binding_change_stops_all_writes(billing_start, change, active):
    h = billing_start

    async def billing(*args, **kwargs):
        if change == "permission":
            h.user_allowed = False
        else:
            h.server.card_id = "replacement-card"
        return active

    h.billing.side_effect = billing
    with pytest.raises(h.Finish, match="权限|绑定已变化"):
        await invoke(h, "start")
    h.open_billing.assert_not_awaited()
    h.start_game.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["permission", "identity"])
async def test_change_during_new_card_sync_wait_prevents_game_start(billing_start, change):
    h = billing_start
    h.active = False

    async def sleep(_):
        if change == "permission":
            h.user_allowed = False
        else:
            h.server.instance_uuid = "cafebabe"

    h.sleep.side_effect = sleep
    with pytest.raises(h.Finish, match="权限|绑定已变化"):
        await invoke(h, "start")
    h.open_billing.assert_awaited_once()
    h.start_game.assert_not_awaited()


@pytest.mark.asyncio
async def test_billing_401_refresh_rechecks_active_and_does_not_repeat_open(billing_start):
    h = billing_start
    h.billing.side_effect = [False, True]
    h.open_billing.side_effect = AuthError("expired response")

    async def refresh(*args):
        h.server = SimpleNamespace(**{**vars(h.server), "token": "fresh-token"})
        return True, "refreshed"

    h.token_refresh.side_effect = refresh
    with pytest.raises(h.Finish, match="计时卡已开启"):
        await invoke(h, "start")
    assert h.billing.await_count == 2
    h.open_billing.assert_awaited_once()
    h.start_game.assert_awaited_once_with(h.matcher, h.event, h.server)
    h.token_refresh.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("initial", [False, True])
@pytest.mark.parametrize("change", ["permission", "identity"])
async def test_token_refresh_cannot_switch_binding_or_restore_revoked_permission(billing_start, initial, change):
    h = billing_start
    h.active = False
    if initial:
        h.server.token = ""
    else:
        h.open_billing.side_effect = AuthError("expired response")

    async def refresh(*args):
        if change == "permission":
            h.user_allowed = False
        else:
            h.server.card_id = "another-card"
        return True, "refreshed"

    h.token_refresh.side_effect = refresh
    with pytest.raises(h.Finish, match="权限|绑定已变化"):
        await invoke(h, "start")
    assert h.open_billing.await_count == (0 if initial else 1)
    h.start_game.assert_not_awaited()
    h.token_refresh.assert_awaited_once()


def test_background_billing_start_does_not_use_manual_billing_helper():
    tree = ast.parse((PLUGIN / "__init__.py").read_text(encoding="utf-8"))
    callers = {
        node.name for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
        for call in ast.walk(node) if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name) and call.func.id == "_manual_start_billing_status"
    }
    assert callers == {"_start_server_locked"}
