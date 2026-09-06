"""Offline regression tests for gateway auth in every panel caller.

Load the selected functions without importing the NoneBot plugin entry point,
which would register handlers and open its production configuration database.
"""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


PLUGIN_DIR = Path(__file__).parents[1] / "plugins" / "minekuai"
CALLERS = ("_start_instance", "_with_panel_refresh", "_panel_run_bg", "_mc_cmd")


def _load_functions(path, names, namespace):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nodes = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in names
    ]
    assert {node.name for node in nodes} == set(names)
    for node in nodes:
        node.decorator_list = []
    module = ast.Module(
        body=ast.parse("from __future__ import annotations").body + nodes,
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)


@pytest.fixture
def panel_callers():
    state = SimpleNamespace(
        server=SimpleNamespace(
            name="test", token="old-jwt", client_id="test-client",
            account_phone="test-account", instance_uuid="test-uuid",
        ),
        account=SimpleNamespace(
            panel_api_key="legacy-key", session_cookie="legacy-cookie",
            xsrf_token="legacy-xsrf",
        ),
        failures=0, panel_args=[],
    )

    class MinekuaiError(Exception):
        pass

    class AuthError(MinekuaiError):
        pass

    class Finish(Exception):
        pass

    class Event:
        user_id = 1
        group_id = 2

    class Panel:
        def __init__(self, **kwargs):
            state.panel_args.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def perform(self, *args):
            if state.failures:
                state.failures -= 1
                raise AuthError("invalid auth")
            return {"ok": True}

        start_instance = perform
        send_command = perform

    async def refresh(*args):
        state.server = SimpleNamespace(**{
            **vars(state.server), "token": "fresh-jwt", "client_id": "fresh-client",
        })
        return True, "refreshed"

    async def finish(text):
        raise Finish(text)

    namespace = {
        "PanelClient": Panel, "AuthError": AuthError,
        "MinekuaiError": MinekuaiError, "RateLimitError": type("RateLimitError", (MinekuaiError,), {}),
        "MatcherException": Finish, "GroupMessageEvent": Event,
        "CommandArg": lambda: None,
        "servers": SimpleNamespace(
            get_server=lambda name: state.server if name == state.server.name else None,
            list_servers=lambda: [state.server],
            get_account=lambda phone: state.account if phone else None,
        ),
        "_refresh_token_for": AsyncMock(side_effect=refresh),
        "_interactive_verification_provider": Mock(return_value=None),
        "_check_admin_perm": lambda event: (True, "ok"),
        "_user_display_name": lambda event: "tester", "_mask_phone": lambda phone: "***",
        "log_operation": Mock(), "logger": Mock(),
    }
    _load_functions(
        PLUGIN_DIR / "__init__.py",
        {
            "_account_has_panel_auth", "_server_has_panel_token", "_has_panel_auth",
            "_build_panel_client", "_ensure_panel_auth", *CALLERS,
        },
        namespace,
    )
    state.namespace = namespace
    state.matcher = SimpleNamespace(send=AsyncMock(), finish=finish)
    state.event = Event()
    state.Finish = Finish
    return state


async def _run(state, caller):
    fn = state.namespace[caller]
    if caller == "_start_instance":
        ok, error = await fn(state.matcher, state.event, state.server)
        return "ok" if ok else error
    if caller == "_with_panel_refresh":
        _, error, _ = await fn(
            state.matcher, state.event, state.server, lambda panel: panel.perform(),
        )
        return error
    if caller == "_panel_run_bg":
        _, error = await fn(state.server, lambda panel: panel.perform())
        return error
    with pytest.raises(state.Finish) as result:
        await fn(
            state.matcher, state.event,
            SimpleNamespace(extract_plain_text=lambda: "test say hello"),
        )
    message = str(result.value)
    return "ok" if message.startswith("✅") else message


def test_panel_builder_accepts_jwt_without_account(panel_callers):
    state = panel_callers
    state.namespace["_build_panel_client"](state.server)
    assert state.panel_args == [{
        "token": "old-jwt", "client_id": "test-client", "api_key": "",
        "session_cookie": "", "xsrf_token": "",
    }]


def test_panel_builder_preserves_legacy_fallback_without_partial_jwt(panel_callers):
    state = panel_callers
    state.server.client_id = ""
    state.namespace["_build_panel_client"](state.server, state.account)
    assert state.panel_args[0]["token"] == ""
    assert state.panel_args[0]["client_id"] == ""
    assert state.panel_args[0]["api_key"] == "legacy-key"
    assert state.panel_args[0]["session_cookie"] == "legacy-cookie"


@pytest.mark.asyncio
async def test_background_snapshot_uses_previously_refreshed_jwt(panel_callers):
    state = panel_callers
    stale = SimpleNamespace(**vars(state.server))
    state.server.token = "already-refreshed-jwt"
    _, error = await state.namespace["_panel_run_bg"](
        stale, lambda panel: panel.perform(),
    )
    assert error == "ok"
    assert state.panel_args[0]["token"] == "already-refreshed-jwt"
    state.namespace["_refresh_token_for"].assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", CALLERS)
async def test_jwt_callers_work_without_account(panel_callers, caller):
    state = panel_callers
    state.server.account_phone = ""
    state.account = None
    assert await _run(state, caller) == "ok"
    assert state.panel_args[0]["token"] == "old-jwt"
    state.namespace["_refresh_token_for"].assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", CALLERS)
async def test_jwt_refresh_overrides_legacy_key_and_reloads_server(panel_callers, caller):
    state = panel_callers
    state.failures = 1
    assert await _run(state, caller) == "ok"
    assert [args["token"] for args in state.panel_args] == ["old-jwt", "fresh-jwt"]
    assert state.panel_args[-1]["client_id"] == "fresh-client"
    state.namespace["_refresh_token_for"].assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", CALLERS)
async def test_jwt_refresh_is_bounded_to_once(panel_callers, caller):
    state = panel_callers
    state.failures = 10
    assert await _run(state, caller) != "ok"
    assert len(state.panel_args) == 2
    state.namespace["_refresh_token_for"].assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", CALLERS)
async def test_jwt_without_account_fails_with_manual_recovery(panel_callers, caller):
    state = panel_callers
    state.server.account_phone = ""
    state.account = None
    state.failures = 1
    assert "更新 token" in await _run(state, caller)
    assert len(state.panel_args) == 1
    state.namespace["_refresh_token_for"].assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", CALLERS)
async def test_legacy_api_key_auth_error_still_does_not_relogin(panel_callers, caller):
    state = panel_callers
    state.server.token = ""
    state.failures = 1
    assert "API Key" in await _run(state, caller)
    assert len(state.panel_args) == 1
    state.namespace["_refresh_token_for"].assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", CALLERS)
async def test_legacy_cookie_refresh_can_upgrade_to_jwt(panel_callers, caller):
    state = panel_callers
    state.server.token = ""
    state.account.panel_api_key = ""
    state.failures = 1
    assert await _run(state, caller) == "ok"
    assert [args["token"] for args in state.panel_args] == ["", "fresh-jwt"]
    state.namespace["_refresh_token_for"].assert_awaited_once()


@pytest.mark.parametrize("token,client_id,account_phone,expected", [
    ("jwt", "client", "", True),
    ("jwt", "", "", False),
    ("", "client", "", False),
    ("", "", "account", True),
])
def test_idle_panel_eligibility_includes_jwt_without_account(
    token, client_id, account_phone, expected,
):
    namespace = {"_panel_runner": object(), "_config": object()}
    _load_functions(PLUGIN_DIR / "idle_watcher.py", {"_can_panel"}, namespace)
    server = SimpleNamespace(
        token=token, client_id=client_id, account_phone=account_phone, instance_uuid="id",
    )
    assert namespace["_can_panel"](server) is expected
