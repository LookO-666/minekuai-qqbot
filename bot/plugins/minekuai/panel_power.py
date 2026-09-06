"""Single-attempt power control using the current Minekuai console protocol.

The frontend sends ``auth`` then ``set state`` and observes ``status`` events.
There is no correlated power ACK: a returned state is an observation, not proof
that the instance has finished starting or that a restart has completed.
"""
import asyncio
import json
import logging
from urllib.parse import urlsplit

try:
    from websockets.asyncio.client import connect as _BaseConnect
except ImportError:  # Produce a safe operational error if deployment missed it.
    _BaseConnect = None


AUTH_TIMEOUT_SECONDS = 10.0
STATUS_TIMEOUT_SECONDS = 12.0
SEND_TIMEOUT_SECONDS = 5.0
_STATES = {"offline", "starting", "running", "stopping"}
_EXPECTED_STATES = {
    "start": {"starting", "running"},
    "stop": {"stopping", "offline"},
    "kill": {"offline"},
    "restart": {"stopping", "starting"},
}
_ALREADY_IN_STATE = {"start": "running", "stop": "offline", "kill": "offline"}
_TRANSPORT_LOGGER = logging.Logger("minekuai.panel_power.transport")
_TRANSPORT_LOGGER.disabled = True
_TRANSPORT_LOGGER.propagate = False


if _BaseConnect is not None:
    class _SingleTargetConnect(_BaseConnect):
        def process_redirect(self, exc):
            # Never send the console token to a redirect target, including
            # another path/host offered by an HTTP handshake response.
            return exc
else:
    _SingleTargetConnect = None


class PreSendAuthError(Exception):
    """Authentication failed before any power command could have been sent."""


class PowerTransportError(Exception):
    """A safe error without server-controlled text or transport credentials.

    ``command_sent`` is conservative: True means sending was attempted and the
    outcome may be unknown. Callers MUST NOT retry that write automatically.
    """

    def __init__(self, message: str, *, command_sent: bool = False):
        super().__init__(message)
        self.command_sent = command_sent


def _validate(socket_url: str, ws_token: str, signal: str) -> None:
    try:
        url = urlsplit(socket_url)
        valid = (
            isinstance(socket_url, str) and not any(ord(char) <= 32 for char in socket_url)
            and url.scheme == "wss" and bool(url.hostname)
            and url.username is None and url.password is None
            and not url.fragment
        )
        _ = url.port  # Invalid or out-of-range ports must also be rejected.
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise PowerTransportError("面板提供的 WebSocket 地址无效或不安全")
    if not isinstance(ws_token, str) or not ws_token.strip():
        raise PreSendAuthError("缺少面板 WebSocket 登录凭据")
    if not isinstance(signal, str) or signal not in _EXPECTED_STATES:
        raise PowerTransportError("不支持的实例电源操作")


async def _receive_event(ws, deadline: float):
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise asyncio.TimeoutError
        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(message, dict) or not isinstance(message.get("event"), str):
            continue
        args = message.get("args")
        return message["event"], args if isinstance(args, list) else []


def _check_error(event: str, *, command_sent: bool) -> None:
    if event in {"jwt error", "token expiring", "token expired"}:
        if not command_sent:
            raise PreSendAuthError("面板 WebSocket 登录凭据失效")
        raise PowerTransportError(
            "电源指令发送后登录凭据失效，操作结果未确认，请先查询状态",
            command_sent=True,
        )
    if event == "daemon error":
        raise PowerTransportError(
            "面板节点报告错误，电源操作结果未确认" if command_sent
            else "面板节点报告错误，尚未发送电源指令",
            command_sent=command_sent,
        )


async def send_power(socket_url: str, ws_token: str, signal: str) -> dict:
    """Authenticate and make at most one ``set state`` send attempt.

    Returns ``signal``, ``sent``, ``observed_state``, ``already_in_state`` and
    ``acknowledged=False`` (the protocol has no command ACK). A changed status
    compatible with the requested action is required after sending. An already
    running start / already offline stop or kill doesn't send a command.

    Only PreSendAuthError is safe for an outer authentication-refresh retry.
    All transport errors have static messages; post-send errors are uncertain.
    """
    _validate(socket_url, ws_token, signal)
    if _SingleTargetConnect is None:
        raise PowerTransportError("WebSocket 依赖不可用，请更新机器人部署依赖")

    command_sent = False
    try:
        async with _SingleTargetConnect(
            socket_url, origin="https://minekuai.com", proxy=None,
            open_timeout=10, close_timeout=2, max_size=256 * 1024,
            max_queue=16, logger=_TRANSPORT_LOGGER,
        ) as ws:
            deadline = asyncio.get_running_loop().time() + AUTH_TIMEOUT_SECONDS
            await asyncio.wait_for(
                ws.send(json.dumps({"event": "auth", "args": [ws_token]})),
                timeout=SEND_TIMEOUT_SECONDS,
            )
            authenticated = False
            while True:
                event, args = await _receive_event(ws, deadline)
                _check_error(event, command_sent=False)
                if event == "auth success":
                    authenticated = True
                elif (
                    authenticated and event == "status" and args
                    and isinstance(args[0], str) and args[0] in _STATES
                ):
                    baseline = args[0]
                    break

            if _ALREADY_IN_STATE.get(signal) == baseline:
                return {
                    "signal": signal, "sent": False, "observed_state": baseline,
                    "already_in_state": True, "acknowledged": False,
                }

            # Mark this BEFORE awaiting send: even a send exception can happen
            # after bytes have reached the peer, so it must not trigger a retry.
            command_sent = True
            await asyncio.wait_for(
                ws.send(json.dumps({"event": "set state", "args": [signal]})),
                timeout=SEND_TIMEOUT_SECONDS,
            )
            deadline = asyncio.get_running_loop().time() + STATUS_TIMEOUT_SECONDS
            while True:
                event, args = await _receive_event(ws, deadline)
                _check_error(event, command_sent=True)
                if (
                    event == "status" and args and isinstance(args[0], str)
                    and args[0] != baseline
                    and args[0] in _EXPECTED_STATES[signal]
                ):
                    return {
                        "signal": signal, "sent": True, "observed_state": args[0],
                        "already_in_state": False, "acknowledged": False,
                    }
    except (PreSendAuthError, PowerTransportError):
        raise
    except Exception:
        if command_sent:
            raise PowerTransportError(
                "电源指令可能已发送，但状态未确认；请先查询状态，勿立即重复操作",
                command_sent=True,
            ) from None
        raise PowerTransportError(
            "面板 WebSocket 连接、登录或状态查询失败，尚未发送电源指令",
        ) from None
