"""Offline checks that maintenance cannot be bypassed through existing handlers."""

import ast
import importlib
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


PLUGIN_DIR = Path(__file__).parents[1] / "plugins" / "minekuai"
sys.path.insert(0, str(PLUGIN_DIR))
operations = importlib.import_module("operations")
modpack_state = importlib.import_module("modpack_state")


@pytest.fixture
def handlers():
    previous = operations._maintenance_guard
    previous_keys = operations._related_lock_keys
    operations.set_maintenance_guard(None)
    operations.set_related_lock_keys(None)

    class Finish(Exception):
        pass

    class Event:
        user_id = 1
        group_id = 2

        def get_plaintext(self):
            return "hello"

    async def finish(text):
        raise Finish(text)

    server = SimpleNamespace(
        name="test", card_id="maintenance-test-card", instance_uuid="instance",
        account_phone="account", address="example.invalid:25565", created_at=123,
    )
    state = SimpleNamespace(
        server=server, servers=[server], event=Event(),
        matcher=SimpleNamespace(finish=finish, send=AsyncMock()), Finish=Finish,
        panel=SimpleNamespace(power=AsyncMock(), send_command=AsyncMock()),
        maintenance=SimpleNamespace(ensure_card_available=Mock(), ensure_instance_available=Mock()),
        auth=SimpleNamespace(
            LoginError=RuntimeError,
            refresh_token=AsyncMock(return_value=("fake-token", "client", "", "")),
        ),
    )
    writes = SimpleNamespace(**{
        name: Mock(return_value=True) for name in (
            "update_address", "update_instance_uuid", "remove_server", "rename_server",
            "remove_account", "bind_server_account", "mark_server_started",
            "update_auto_close", "add_server",
        )
    })
    writes.get_server = lambda name: next((s for s in state.servers if s.name == name), None)
    writes.list_servers = lambda: state.servers
    writes.get_account = lambda phone: SimpleNamespace(phone=phone, password="test-password")
    state.reconcile = AsyncMock(return_value={"released": False, "maintenance": False})

    async def with_panel(matcher, event, target, fn):
        try:
            return await fn(state.panel), "ok", target
        except operations.OperationBusyError as exc:
            return None, str(exc), target

    async def panel_background(target, fn):
        return await fn(state.panel), "ok"

    namespace = {
        "OperationBusyError": operations.OperationBusyError,
        "re": re,
        "card_operation": operations.card_operation,
        "ensure_card_available": operations.ensure_card_available,
        "MaintenanceError": modpack_state.MaintenanceError,
        "ConfirmError": modpack_state.ConfirmError,
        "ServerIdentity": modpack_state.ServerIdentity,
        "MinekuaiError": RuntimeError,
        "modpack_maintenance": state.maintenance,
        "modpack_service": SimpleNamespace(
            reconcile_for_operation=state.reconcile,
            current=lambda identity: writes.get_server(identity.name),
        ),
        "_modpack_refresh_factory": lambda matcher, event: AsyncMock(),
        "auth": state.auth,
        "_interactive_verification_provider": lambda *args: None,
        "servers": writes,
        "GroupMessageEvent": Event,
        "CommandArg": lambda: None,
        "ArgPlainText": lambda name: None,
        "CANCEL_WORDS": {"取消", "cancel"},
        "_check_perm": lambda event: (True, ""),
        "_check_admin_perm": lambda event: (True, ""),
        "_user_display_name": lambda event: "tester",
        "_mask_phone": lambda phone: "***",
        "_server_has_panel_token": lambda target: True,
        "_pick_target_server": lambda *args: (state.server, ""),
        "_with_panel_refresh": with_panel,
        "_panel_run_bg": panel_background,
        "config": SimpleNamespace(chat_bridge=True, chat_qq_to_mc=True),
        "idle_watcher": SimpleNamespace(
            has_online_players=lambda name: True, mark_opened=Mock(), watch_for_ready=Mock(),
        ),
        "log_operation": Mock(),
    }
    names = {
        "_ensure_server_config_mutable", "_ensure_instance_config_mutable",
        "_ensure_new_server_config_mutable", "_configured_card_maintenance_guard",
        "_configured_instance_lock_keys",
        "_manual_control_current", "_reconcile_manual_control",
        "_restart", "_chat_relay", "_addr_update_finish",
        "_uuid_update_finish", "_del_finish", "_rename_finish", "_del_account_finish",
        "_bind_account", "_auto_close", "_add_finish",
    }
    tree = ast.parse((PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8"))
    nodes = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names
    ]
    assert {node.name for node in nodes} == names
    for node in nodes:
        node.decorator_list = []
    module = ast.Module(
        body=ast.parse("from __future__ import annotations").body + nodes,
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(module), "maintenance-handlers", "exec"), namespace)
    state.namespace = namespace
    state.writes = writes
    yield state
    operations.set_maintenance_guard(previous)
    operations.set_related_lock_keys(previous_keys)


def _block_card(card_id):
    if card_id == "maintenance-test-card":
        raise RuntimeError("实例正在整合包维护保护中")


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", ["_restart", "_chat_relay"])
@pytest.mark.parametrize("blocker", ["maintenance", "card_lock"])
async def test_existing_controls_cannot_bypass_maintenance_or_card_lock(handlers, caller, blocker):
    state = handlers

    async def call():
        if caller == "_restart":
            with pytest.raises(state.Finish, match="维护|正在处理"):
                await state.namespace[caller](
                    state.matcher, state.event,
                    SimpleNamespace(extract_plain_text=lambda: "test"),
                )
        else:
            await state.namespace[caller](None, state.event)

    if blocker == "maintenance":
        operations.set_maintenance_guard(_block_card)
        await call()
    else:
        async with operations.card_operation(state.server.card_id):
            await call()
    state.panel.power.assert_not_awaited()
    state.panel.send_command.assert_not_awaited()
    state.writes.mark_server_started.assert_not_called()


@pytest.mark.asyncio
async def test_chat_bridge_still_sends_to_an_unprotected_card(handlers):
    state = handlers
    state.servers.append(SimpleNamespace(
        name="other", card_id="other-maintenance-test-card", instance_uuid="other-instance",
        account_phone="account",
    ))
    operations.set_maintenance_guard(_block_card)
    await state.namespace["_chat_relay"](None, state.event)
    state.panel.send_command.assert_awaited_once()
    assert state.panel.send_command.await_args.args[0] == "other-instance"


@pytest.mark.asyncio
@pytest.mark.parametrize("caller,args,write", [
    ("_addr_update_finish", ("test", "changed.invalid:25565"), "update_address"),
    ("_uuid_update_finish", ("test", "changed-instance"), "update_instance_uuid"),
    ("_del_finish", ("test", "确认"), "remove_server"),
    ("_rename_finish", ("test", "new-name"), "rename_server"),
    ("_del_account_finish", ("account", "确认"), "remove_account"),
    ("_bind_account", (SimpleNamespace(extract_plain_text=lambda: "test new-account"),), "bind_server_account"),
    ("_auto_close", (SimpleNamespace(extract_plain_text=lambda: "test 10"),), "update_auto_close"),
])
async def test_config_writes_cannot_detach_a_protected_server(handlers, caller, args, write):
    state = handlers
    operations.set_maintenance_guard(_block_card)
    with pytest.raises(state.Finish, match="维护保护"):
        await state.namespace[caller](state.matcher, state.event, *args)
    getattr(state.writes, write).assert_not_called()


@pytest.mark.asyncio
async def test_config_write_remains_available_after_maintenance_is_removed(handlers):
    state = handlers
    with pytest.raises(state.Finish, match="已更新"):
        await state.namespace["_uuid_update_finish"](state.matcher, state.event, "test", "new-instance")
    state.writes.update_instance_uuid.assert_called_once_with("test", "new-instance")


@pytest.mark.asyncio
async def test_preexisting_different_card_alias_is_also_protected(handlers):
    state = handlers
    state.maintenance.ensure_instance_available.side_effect = modpack_state.MaintenanceError(
        "实例维护保护中",
    )
    operations.set_maintenance_guard(state.namespace["_configured_card_maintenance_guard"])
    with pytest.raises(operations.OperationBusyError, match="实例维护"):
        async with operations.card_operation(state.server.card_id):
            pytest.fail("alias bypassed maintenance")
    state.maintenance.ensure_card_available.assert_called_once_with(state.server.card_id)
    state.maintenance.ensure_instance_available.assert_called_once_with(state.server.instance_uuid)
    async with operations.card_operation("unrelated-card"):
        pass


@pytest.mark.asyncio
async def test_uuid_edit_cannot_attach_unprotected_card_to_protected_instance(handlers):
    state = handlers
    state.maintenance.ensure_instance_available.side_effect = modpack_state.MaintenanceError(
        "目标实例维护保护中",
    )
    with pytest.raises(state.Finish, match="目标实例维护"):
        await state.namespace["_uuid_update_finish"](
            state.matcher, state.event, "test", "protected-instance",
        )
    state.writes.update_instance_uuid.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("block_after_login", [False, True])
async def test_add_server_checks_target_before_and_after_login(handlers, block_after_login):
    state = handlers
    error = modpack_state.MaintenanceError("目标实例维护保护中")
    state.maintenance.ensure_instance_available.side_effect = [None, error] if block_after_login else error
    with pytest.raises(state.Finish, match="目标实例维护"):
        await state.namespace["_add_finish"](
            state.matcher, state.event, "new-server", "unprotected-card", "example.invalid",
            "protected-instance", "13000000000",
        )
    state.writes.add_server.assert_not_called()
    assert state.auth.refresh_token.await_count == int(block_after_login)


@pytest.mark.asyncio
@pytest.mark.parametrize("saved_instance,configured_instance", [
    ("abcd1234-1234-5678-90ab-123456789012", "ABCD1234"),
    ("abcd1234", "ABCD1234-1234-5678-90AB-123456789012"),
])
async def test_persisted_instance_alias_protection_reaches_existing_controls(
    handlers, tmp_path, saved_instance, configured_instance,
):
    state = handlers
    store = modpack_state.MaintenanceStore(tmp_path / "maintenance.db")
    store.init_db()
    store.begin(
        modpack_state.ServerIdentity("original", "original-card", saved_instance, "account", 1),
        modpack_state.InstallChoice("project", "release", "Pack", "1", "1.20", "17", "pack.zip"),
    )
    # Reopening the DB simulates process restart; the alias uses another card.
    state.namespace["modpack_maintenance"] = modpack_state.MaintenanceStore(store.db_path)
    state.server.instance_uuid = configured_instance
    operations.set_maintenance_guard(state.namespace["_configured_card_maintenance_guard"])
    with pytest.raises(state.Finish, match="维护保护"):
        await state.namespace["_restart"](
            state.matcher, state.event, SimpleNamespace(extract_plain_text=lambda: "test"),
        )
    await state.namespace["_chat_relay"](None, state.event)
    state.panel.power.assert_not_awaited()
    state.panel.send_command.assert_not_awaited()
    store.finish(saved_instance)
    async with operations.card_operation(state.server.card_id):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("short_id,full_id", [
    ("ABCD1234", "abcd1234-1234-5678-90ab-123456789012"),
    ("abcd1234", "ABCD12341234567890AB123456789012"),
])
async def test_configured_instance_alias_locks_block_controls_before_maintenance_exists(
    handlers, short_id, full_id,
):
    state = handlers
    state.server.instance_uuid = short_id
    state.servers.append(SimpleNamespace(
        name="other", card_id="alias-install-card", instance_uuid=full_id, account_phone="account",
    ))
    operations.set_related_lock_keys(state.namespace["_configured_instance_lock_keys"])
    async with operations.card_operation("alias-install-card"):
        with pytest.raises(state.Finish, match="关联实例"):
            await state.namespace["_restart"](
                state.matcher, state.event, SimpleNamespace(extract_plain_text=lambda: "test"),
            )
        await state.namespace["_chat_relay"](None, state.event)
    state.panel.power.assert_not_awaited()
    state.panel.send_command.assert_not_awaited()
    # The reverse direction also blocks install/finish while a control owns the alias.
    async with operations.card_operation(state.server.card_id):
        with pytest.raises(operations.OperationBusyError, match="关联实例"):
            async with operations.card_operation("alias-install-card", allow_maintenance=True):
                pytest.fail("finish bypassed an alias control")


def test_noncanonical_instance_lock_keys_only_match_exact_casefolded_value(handlers):
    state = handlers
    state.server.instance_uuid = "Some-Instance"
    assert list(state.namespace["_configured_instance_lock_keys"](state.server.card_id)) == [
        "instance:exact:some-instance",
    ]
