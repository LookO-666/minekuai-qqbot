"""Power transport tests use fake WebSockets and never connect to a server."""
import asyncio
import importlib.util
import json
import sys
import traceback
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


URL = "wss://node.example.test/api/servers/test/ws?opaque=test-url-secret"
TOKEN = "test-websocket-secret"


def frame(event, *args):
    return json.dumps({"event": event, "args": list(args)})


@pytest.fixture
def power(monkeypatch):
    # The local minimal test venv may not include websockets. A stub base class
    # also lets us verify our no-redirect policy without making any handshake.
    for name in ("websockets", "websockets.asyncio", "websockets.asyncio.client"):
        package = ModuleType(name)
        package.__path__ = []
        monkeypatch.setitem(sys.modules, name, package)
    sys.modules["websockets.asyncio.client"].connect = type("Connect", (), {})
    path = Path(__file__).parents[1] / "plugins" / "minekuai" / "panel_power.py"
    spec = importlib.util.spec_from_file_location("_test_panel_power", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "AUTH_TIMEOUT_SECONDS", 0.03)
    monkeypatch.setattr(module, "STATUS_TIMEOUT_SECONDS", 0.03)
    monkeypatch.setattr(module, "SEND_TIMEOUT_SECONDS", 0.03)
    return module


def fake_connection(monkeypatch, power, frames, *, fail_enter=False,
                    fail_power_send=False, fail_exit=False):
    state = SimpleNamespace(sent=[], connects=[], received=[], frames=list(frames))

    class Socket:
        async def send(self, raw):
            packet = json.loads(raw)
            state.sent.append(packet)
            if packet["event"] == "set state" and fail_power_send:
                raise RuntimeError(f"send failed {URL} {TOKEN}")

        async def recv(self):
            if not state.frames:
                await asyncio.Future()
            item = state.frames.pop(0)
            if isinstance(item, BaseException):
                raise item
            state.received.append(item)
            return item

    class Context:
        async def __aenter__(self):
            if fail_enter:
                raise RuntimeError(f"connection failed {URL} {TOKEN}")
            return Socket()

        async def __aexit__(self, *args):
            if fail_exit:
                raise RuntimeError(f"close failed {URL} {TOKEN}")

    def connect(url, **kwargs):
        state.connects.append((url, kwargs))
        return Context()

    monkeypatch.setattr(power, "_SingleTargetConnect", connect)
    return state


def assert_redacted(error):
    rendered = "".join(traceback.format_exception(error))
    assert TOKEN not in rendered
    assert URL not in rendered
    assert "test-url-secret" not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize("signal,baseline,observed", [
    ("start", "offline", "starting"),
    ("start", "offline", "running"),
    ("stop", "running", "stopping"),
    ("stop", "running", "offline"),
    ("restart", "running", "stopping"),
    ("restart", "offline", "starting"),
    ("kill", "running", "offline"),
])
async def test_auth_then_single_power_send_and_changed_status(
    power, monkeypatch, signal, baseline, observed,
):
    state = fake_connection(monkeypatch, power, [
        frame("auth success"), frame("status", baseline),
        frame("status", baseline), frame("console output", TOKEN),
        frame("status", observed),
    ])
    result = await power.send_power(URL, TOKEN, signal)
    assert state.sent == [
        {"event": "auth", "args": [TOKEN]},
        {"event": "set state", "args": [signal]},
    ]
    assert result == {
        "signal": signal, "sent": True, "observed_state": observed,
        "already_in_state": False, "acknowledged": False,
    }
    assert len(state.connects) == 1
    assert state.connects[0][0] == URL
    assert state.connects[0][1]["proxy"] is None
    assert state.connects[0][1]["logger"].disabled


@pytest.mark.asyncio
@pytest.mark.parametrize("signal,baseline", [
    ("start", "running"), ("stop", "offline"), ("kill", "offline"),
])
async def test_idempotent_target_already_satisfied_does_not_send_power(
    power, monkeypatch, signal, baseline,
):
    state = fake_connection(monkeypatch, power, [
        frame("auth success"), frame("status", baseline),
    ])
    result = await power.send_power(URL, TOKEN, signal)
    assert result["already_in_state"] is True
    assert result["sent"] is False
    assert state.sent == [{"event": "auth", "args": [TOKEN]}]


@pytest.mark.asyncio
@pytest.mark.parametrize("event", ["jwt error", "token expired", "token expiring"])
async def test_pre_send_auth_failure_is_retryable_and_redacted(power, monkeypatch, event):
    state = fake_connection(monkeypatch, power, [frame(event, TOKEN)])
    with pytest.raises(power.PreSendAuthError) as error:
        await power.send_power(URL, TOKEN, "start")
    assert_redacted(error.value)
    assert len(state.sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("event", [
    "jwt error", "token expired", "token expiring", "daemon error",
])
async def test_post_send_errors_are_uncertain_never_auth_retry(power, monkeypatch, event):
    state = fake_connection(monkeypatch, power, [
        frame("auth success"), frame("status", "offline"), frame(event, TOKEN, URL),
    ])
    with pytest.raises(power.PowerTransportError) as error:
        await power.send_power(URL, TOKEN, "start")
    assert error.value.command_sent is True
    assert_redacted(error.value)
    assert len(state.connects) == 1
    assert len(state.sent) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("frames", [
    [], [frame("auth success")],
    [frame("status", "running"), frame("auth success")],
])
async def test_auth_and_baseline_required_before_writing(power, monkeypatch, frames):
    state = fake_connection(monkeypatch, power, frames)
    with pytest.raises(power.PowerTransportError) as error:
        await power.send_power(URL, TOKEN, "start")
    assert error.value.command_sent is False
    assert len(state.sent) == 1


@pytest.mark.asyncio
async def test_unchanged_status_cannot_confirm_power_command(power, monkeypatch):
    state = fake_connection(monkeypatch, power, [
        frame("auth success"), frame("status", "starting"), frame("status", "starting"),
    ])
    with pytest.raises(power.PowerTransportError) as error:
        await power.send_power(URL, TOKEN, "start")
    assert error.value.command_sent is True
    assert len(state.sent) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,expected_sent", [
    ("enter", False), ("send", True), ("receive", True), ("exit", True),
])
async def test_transport_failures_do_not_leak_secrets_or_retry(
    power, monkeypatch, failure, expected_sent,
):
    frames = [frame("auth success"), frame("status", "offline")]
    frames.append(
        RuntimeError(f"recv {URL} {TOKEN}") if failure == "receive"
        else frame("status", "starting")
    )
    state = fake_connection(
        monkeypatch, power, frames, fail_enter=failure == "enter",
        fail_power_send=failure == "send", fail_exit=failure == "exit",
    )
    with pytest.raises(power.PowerTransportError) as error:
        await power.send_power(URL, TOKEN, "start")
    assert error.value.command_sent is expected_sent
    assert_redacted(error.value)
    assert len(state.connects) == 1
    assert sum(message["event"] == "set state" for message in state.sent) <= 1


@pytest.mark.asyncio
async def test_bad_frames_ignored_without_affecting_protocol(power, monkeypatch):
    fake_connection(monkeypatch, power, [
        "not json", "[]", "null", frame("status", "running"),
        frame("auth success"), frame("status", {}), frame("status", "offline"),
        frame("status", []), frame("status", "starting"),
    ])
    result = await power.send_power(URL, TOKEN, "start")
    assert result["sent"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "ws://example.test/ws", "https://example.test/ws", "wss:///ws",
    "wss://user:password@example.test/ws", "wss://example.test/ws#fragment",
    "wss://example.test:99999/ws", "wss://[bad-ip/ws",
    "wss://example.test/\nws", " wss://example.test/ws",
])
async def test_unsafe_url_rejected_before_connect(power, monkeypatch, url):
    state = fake_connection(monkeypatch, power, [])
    with pytest.raises(power.PowerTransportError):
        await power.send_power(url, TOKEN, "start")
    assert not state.connects


def test_connector_refuses_all_redirects(power):
    connector = power._SingleTargetConnect()
    failure = RuntimeError("redirect to another target")
    assert connector.process_redirect(failure) is failure


@pytest.mark.asyncio
async def test_missing_dependency_is_safe_operational_error(power, monkeypatch):
    monkeypatch.setattr(power, "_SingleTargetConnect", None)
    with pytest.raises(power.PowerTransportError) as error:
        await power.send_power(URL, TOKEN, "start")
    assert error.value.command_sent is False
    assert_redacted(error.value)


@pytest.mark.asyncio
async def test_unknown_signal_is_rejected_without_connecting(power, monkeypatch):
    state = fake_connection(monkeypatch, power, [])
    with pytest.raises(power.PowerTransportError):
        await power.send_power(URL, TOKEN, "destroy")
    assert not state.connects


@pytest.mark.asyncio
async def test_empty_token_is_rejected_without_connecting(power, monkeypatch):
    state = fake_connection(monkeypatch, power, [])
    with pytest.raises(power.PreSendAuthError):
        await power.send_power(URL, "", "start")
    assert not state.connects


@pytest.mark.asyncio
async def test_cancellation_propagates_without_resending_power(power, monkeypatch):
    state = fake_connection(monkeypatch, power, [
        frame("auth success"), frame("status", "offline"), asyncio.CancelledError(),
    ])
    with pytest.raises(asyncio.CancelledError):
        await power.send_power(URL, TOKEN, "start")
    assert sum(message["event"] == "set state" for message in state.sent) == 1
    assert len(state.connects) == 1
