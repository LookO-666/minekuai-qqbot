"""Every billing start/stop caller must select the configured instance."""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


PATH = Path(__file__).parents[1] / "plugins" / "minekuai" / "__init__.py"
TREE = ast.parse(PATH.read_text(encoding="utf-8"))
METHODS = {
    "open_timing_only", "close_timing_only", "open_server", "close_server",
    "start_timing", "stop_timing",
}
CALLS = [
    (function.name, call)
    for function in TREE.body if isinstance(function, ast.AsyncFunctionDef)
    for call in ast.walk(function)
    if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
    and isinstance(call.func.value, ast.Name) and call.func.value.id == "client"
    and call.func.attr in METHODS
]


@pytest.mark.parametrize("function,call", CALLS, ids=[
    f"{name}:{call.lineno}" for name, call in CALLS
])
def test_all_billing_calls_forward_matching_instance_id(function, call):
    """Covers manual, background, refresh retries, and startup-failure cleanup."""
    kwargs = {keyword.arg: keyword.value for keyword in call.keywords}
    assert "card_id" in kwargs, function
    assert "instance_id" in kwargs, function
    card = kwargs["card_id"]
    instance = kwargs["instance_id"]
    assert isinstance(card, ast.Attribute) and card.attr == "card_id"
    if function == "_start_server_locked":
        # Manual start validates the complete ID before normalizing it to the
        # official per-instance billing endpoint's short identifier.
        assert isinstance(card.value, ast.Name) and card.value.id == "server"
        expected = ast.parse('server.instance_uuid[:8].lower() if server.instance_uuid else ""', mode="eval").body
        assert ast.dump(instance) == ast.dump(expected)
        expression = compile(ast.Expression(instance), "manual-start-instance", "eval")
        for saved, short in [
            ("deadbeef", "deadbeef"), ("DEADBEEF", "deadbeef"),
            ("DEADBEEF-1234-5678-90AB-1234567890AB", "deadbeef"), ("", ""),
        ]:
            assert eval(expression, {"server": SimpleNamespace(instance_uuid=saved)}) == short
        return
    assert isinstance(instance, ast.Attribute) and instance.attr == "instance_uuid"
    assert ast.dump(card.value) == ast.dump(instance.value)


def test_expected_billing_flows_are_covered():
    assert len(CALLS) == 6
    assert {name for name, _ in CALLS} == {
        "_auto_close_locked", "_auto_start_locked",
        "_start_server_locked", "_stop_server_locked",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("expired", [False, True])
async def test_auto_close_forwards_instance_and_reloads_after_auth(expired):
    class AuthError(Exception):
        pass

    original = SimpleNamespace(
        name="test", card_id="old-card", instance_uuid="old-instance",
        account_phone="account",
    )
    fresh = SimpleNamespace(
        name="test", card_id="new-card", instance_uuid="new-instance",
        account_phone="account",
    )
    close = AsyncMock(side_effect=[AuthError(), None] if expired else [None])

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        close_server = close

    namespace = {
        "_build_client": lambda server: Client(),
        "_refresh_token_for": AsyncMock(return_value=(True, "ok")),
        "servers": SimpleNamespace(get_server=lambda name: fresh),
        "log_operation": Mock(), "AuthError": AuthError,
    }
    function = next(node for node in TREE.body if getattr(node, "name", "") == "_auto_close_locked")
    module = ast.Module(
        body=ast.parse("from __future__ import annotations").body + [function],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(module), str(PATH), "exec"), namespace)
    ok, _ = await namespace["_auto_close_locked"](original)
    assert ok
    assert close.await_args_list[0].kwargs == {
        "card_id": "old-card", "instance_id": "old-instance",
    }
    if expired:
        assert close.await_args_list[1].kwargs == {
            "card_id": "new-card", "instance_id": "new-instance",
        }
        namespace["_refresh_token_for"].assert_awaited_once()
    else:
        namespace["_refresh_token_for"].assert_not_awaited()
