"""Offline integration checks for scenario help and its chat-bridge boundary."""

import ast
from contextlib import asynccontextmanager
import importlib
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


PLUGIN_DIR = Path(__file__).parents[1] / "plugins" / "minekuai"
sys.path.insert(0, str(PLUGIN_DIR))
help_content = importlib.import_module("help_content")
permission = importlib.import_module("permission")


class PrivateEvent:
    def __init__(self, text="帮助", user_id=1):
        self.text = text
        self.user_id = user_id

    def get_plaintext(self):
        return self.text


class GroupEvent(PrivateEvent):
    def __init__(self, text="帮助", user_id=1, group_id=2):
        super().__init__(text, user_id)
        self.group_id = group_id


@pytest.fixture
def help_handlers():
    class Finished(Exception):
        pass

    class OperationBusyError(RuntimeError):
        pass

    async def finish(message):
        raise Finished(message)

    config = SimpleNamespace(
        allowed_groups=[2], allowed_users=[], admin_all_group_members=False,
        stop_need_confirm=True, command_cooldown=5,
        chat_bridge=True, chat_qq_to_mc=True,
    )
    server = SimpleNamespace(
        name="test", card_id="help-test-card", instance_uuid="abcd1234",
        account_phone="test-account",
    )
    state = SimpleNamespace(
        Finished=Finished, OperationBusyError=OperationBusyError,
        config=config, server=server,
        matcher=SimpleNamespace(finish=finish, send=AsyncMock()),
        panel=SimpleNamespace(send_command=AsyncMock()),
        servers=SimpleNamespace(list_servers=Mock(return_value=[server])),
        guard=Mock(),
        render=Mock(wraps=help_content.render_help),
        segment=SimpleNamespace(
            text=Mock(side_effect=lambda value: SimpleNamespace(type="text", data={"text": value})),
            image=Mock(side_effect=AssertionError("help must not send a legacy image")),
        ),
    )

    @asynccontextmanager
    async def card_operation(card_id):
        state.guard(card_id)
        yield

    async def panel_run(server, callback):
        return await callback(state.panel), "ok"

    namespace = {
        "config": config,
        "GroupMessageEvent": GroupEvent,
        "MessageSegment": state.segment,
        "is_user_allowed": permission.is_user_allowed,
        "parse_help_topic": help_content.parse_help_topic,
        "render_help": state.render,
        "servers": state.servers,
        "idle_watcher": SimpleNamespace(has_online_players=Mock(return_value=True)),
        "_user_display_name": lambda event: "tester",
        "_server_has_panel_token": lambda server: True,
        "card_operation": card_operation,
        "OperationBusyError": OperationBusyError,
        "_panel_run_bg": AsyncMock(side_effect=panel_run),
    }
    names = {"_check_perm", "_help", "_chat_relay"}
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
    exec(compile(ast.fix_missing_locations(module), "scenario-help-handlers", "exec"), namespace)
    state.namespace = namespace
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [
    "帮助", "help", "Help", "HELP", " HeLp ",
    "帮助 服务器", "help 服务器", "帮助 玩家", "帮助 整合包", "帮助 管理",
    "帮助 不存在的分类",
])
async def test_help_topics_render_one_text_message_without_server_reads(help_handlers, message):
    state = help_handlers
    state.servers.list_servers.side_effect = AssertionError("help must not query server state")
    event = GroupEvent(message)
    with pytest.raises(state.Finished) as finished:
        await state.namespace["_help"](state.matcher, event)
    segment = finished.value.args[0]
    topic = help_content.parse_help_topic(message)
    assert segment.type == "text"
    assert segment.data["text"] == help_content.render_help(
        topic, admin_all_group_members=False, stop_need_confirm=True, command_cooldown=5,
    )
    state.render.assert_called_once_with(
        topic, admin_all_group_members=False, stop_need_confirm=True, command_cooldown=5,
    )
    state.segment.text.assert_called_once()
    state.segment.image.assert_not_called()
    state.matcher.send.assert_not_awaited()
    state.servers.list_servers.assert_not_called()
    state.namespace["_panel_run_bg"].assert_not_awaited()
    state.guard.assert_not_called()


@pytest.mark.asyncio
async def test_help_passes_current_behavior_settings_to_the_single_renderer(help_handlers):
    state = help_handlers
    state.config.admin_all_group_members = True
    state.config.stop_need_confirm = False
    state.config.command_cooldown = 17
    event = GroupEvent("帮助 管理")
    with pytest.raises(state.Finished):
        await state.namespace["_help"](state.matcher, event)
    state.render.assert_called_once_with(
        help_content.parse_help_topic(event.text),
        admin_all_group_members=True, stop_need_confirm=False, command_cooldown=17,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("event,allowed_users,expected_reason", [
    (PrivateEvent(), [], "请在指定的 QQ 群内使用本机器人"),
    (GroupEvent(user_id=3), [1], "你没有使用本机器人的权限"),
    (GroupEvent(group_id=3), [], None),
])
async def test_help_preserves_existing_permission_denials(
    help_handlers, event, allowed_users, expected_reason,
):
    state = help_handlers
    state.config.allowed_users = allowed_users
    if expected_reason:
        with pytest.raises(state.Finished) as finished:
            await state.namespace["_help"](state.matcher, event)
        assert finished.value.args[0].data["text"] == expected_reason
    else:
        await state.namespace["_help"](state.matcher, event)
        state.segment.text.assert_not_called()
    state.render.assert_not_called()
    state.servers.list_servers.assert_not_called()


@pytest.mark.asyncio
async def test_private_help_still_works_when_group_restriction_is_disabled(help_handlers):
    state = help_handlers
    state.config.allowed_groups = []
    with pytest.raises(state.Finished):
        await state.namespace["_help"](state.matcher, PrivateEvent("帮助 管理"))
    state.render.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["hello", "helpful", "help服务器", "请帮助我", "帮助一下", "开服"])
async def test_non_help_messages_are_not_claimed_by_help_handler(help_handlers, message):
    state = help_handlers
    await state.namespace["_help"](state.matcher, GroupEvent(message))
    state.render.assert_not_called()
    state.segment.text.assert_not_called()


def test_help_matcher_is_anchored_and_blocks_before_chat_relay():
    tree = ast.parse((PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8"))
    registrations = {}
    namespace = {
        "HELP_PATTERN": help_content.HELP_PATTERN,
        "on_regex": lambda *args, **kwargs: (args, kwargs),
        "on_message": lambda *args, **kwargs: (args, kwargs),
    }
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        names = {target.id for target in node.targets if isinstance(target, ast.Name)}
        if names & {"help_cmd", "chat_relay"}:
            exec(compile(ast.Module(body=[node], type_ignores=[]), "help-registration", "exec"), namespace)
            registrations.update({name: namespace[name] for name in names})
    (patterns, help_options) = registrations["help_cmd"]
    (_, relay_options) = registrations["chat_relay"]
    assert patterns == (help_content.HELP_PATTERN,)
    assert help_options["block"] is True
    assert help_options["priority"] < relay_options["priority"]
    for text in ("帮助", "help", " HELP 玩家 ", "帮助 未知分类"):
        assert re.search(patterns[0], text)
    for text in ("helpful", "我需要帮助", "帮助我", "no help", "help服务器"):
        assert not re.search(patterns[0], text)


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [
    "帮助", "help", "HELP", "帮助 服务器", "帮助 玩家", "帮助 整合包", "帮助 管理",
    "帮助 不存在的分类", "help unknown", " 帮助\n玩家 ",
])
async def test_help_navigation_never_reaches_chat_bridge(help_handlers, message):
    state = help_handlers
    await state.namespace["_chat_relay"](None, GroupEvent(message))
    state.servers.list_servers.assert_not_called()
    state.namespace["_panel_run_bg"].assert_not_awaited()
    state.panel.send_command.assert_not_awaited()
    state.guard.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["hello", "helpful", "请帮助我", "帮助一下"])
async def test_ordinary_chat_still_reaches_online_server(help_handlers, message):
    state = help_handlers
    await state.namespace["_chat_relay"](None, GroupEvent(message))
    state.guard.assert_called_once_with(state.server.card_id)
    state.panel.send_command.assert_awaited_once()
    instance, command = state.panel.send_command.await_args.args
    assert instance == state.server.instance_uuid
    assert command.startswith("tellraw @a ")
    assert message in command


@pytest.mark.asyncio
@pytest.mark.parametrize("guard_reason", ["整合包维护保护中", "计时卡操作进行中"])
async def test_chat_relay_still_obeys_maintenance_and_operation_guards(help_handlers, guard_reason):
    state = help_handlers
    state.guard.side_effect = state.OperationBusyError(guard_reason)
    await state.namespace["_chat_relay"](None, GroupEvent("hello"))
    state.guard.assert_called_once_with(state.server.card_id)
    state.namespace["_panel_run_bg"].assert_not_awaited()
    state.panel.send_command.assert_not_awaited()
