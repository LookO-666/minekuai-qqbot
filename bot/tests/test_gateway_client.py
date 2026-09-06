"""New official gateway contract, tested without network access."""
import importlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "plugins" / "minekuai"))
client_mod = importlib.import_module("client")
PanelClient = client_mod.PanelClient


def make_client(handler):
    client = PanelClient(token="new-token", client_id="web-client",
                         api_key="old-key", session_cookie="old-cookie", xsrf_token="old-xsrf")
    client._http = httpx.AsyncClient(base_url=client.BASE_URL,
        headers=client._build_headers(), transport=httpx.MockTransport(handler))
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize("method,args,verb,path,payload,expected", [
    ("get_server_info", ("abc",), "GET", "/panel/servers/abc", {"attributes": {"name": "ATM"}}, {"attributes": {"name": "ATM"}}),
    ("get_resources", ("abc",), "GET", "/panel/servers/abc/resources", {"attributes": {"current_state": "offline"}}, {"attributes": {"current_state": "offline"}}),
    ("list_directory", ("abc", "/mods"), "GET", "/panel/servers/abc/files/list", {"data": [{"attributes": {"name": "a.jar"}}]}, [{"name": "a.jar"}]),
    ("get_ws_credentials", ("abc",), "GET", "/panel/servers/abc/websocket", {"token": "ws-token", "socket": "wss://example.invalid/ws"}, {"token": "ws-token", "socket": "wss://example.invalid/ws"}),
    ("send_command", ("abc", "/list"), "POST", "/panel/servers/abc/command", None, None),
])
async def test_gateway_contract(method, args, verb, path, payload, expected):
    calls = []
    def handler(request):
        calls.append(request)
        assert request.url.host == "api.minekuai.cn"
        assert request.url.path == path and request.method == verb
        assert request.headers["authorization"] == "Bearer new-token"
        assert request.headers["clientid"] == "web-client"
        assert "cookie" not in request.headers and "x-xsrf-token" not in request.headers
        if method == "send_command":
            assert request.content == b'{"command":"list"}'
        if method == "list_directory":
            assert request.url.params["directory"] == "/mods"
        return httpx.Response(200, json={"code": 200, "data": payload})
    client = make_client(handler)
    try:
        assert await getattr(client, method)(*args) == expected
        assert len(calls) == 1
    finally:
        await client._http.aclose()


@pytest.mark.asyncio
async def test_websocket_uncoded_envelope():
    payload = {"token": "short-lived", "socket": "wss://example.invalid/ws"}
    client = make_client(lambda _: httpx.Response(200, json={"data": payload}))
    try:
        assert await client.get_ws_credentials("abc") == payload
    finally:
        await client._http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    {"attributes": {}}, {"code": 200, "data": []}, {"code": 200, "data": "text"},
])
async def test_gateway_rejects_invalid_envelope(body):
    client = make_client(lambda _: httpx.Response(200, json=body))
    try:
        with pytest.raises(client_mod.APIError):
            await client.get_resources("abc")
    finally:
        await client._http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [401, 419, "401", "419"])
async def test_file_business_auth_error_is_not_file_text(code):
    client = make_client(lambda _: httpx.Response(200, json={"code": code, "msg": "new-token"}))
    try:
        with pytest.raises(client_mod.AuthError) as error:
            await client.read_file_text("abc", "/logs/latest.log")
        assert "new-token" not in str(error.value)
    finally:
        await client._http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [500, "500", 403, "403", 503])
@pytest.mark.parametrize("message_field", ["msg", "message"])
async def test_file_business_error_is_not_forwarded_as_log_text(code, message_field):
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJvdGhlciJ9.signature"
    secrets = ["new-token", "old-key", "old-cookie", "old-xsrf", jwt]
    body = {"code": code, message_field: "upstream unavailable " + " ".join(secrets)}
    client = make_client(lambda _: httpx.Response(200, json=body))
    try:
        with pytest.raises(client_mod.APIError) as error:
            await client.read_file_text("abc", "/logs/latest.log")
        assert "upstream unavailable" in str(error.value)
        assert "[REDACTED]" in str(error.value)
        for secret in secrets:
            assert secret not in str(error.value)
    finally:
        await client._http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [
    '{"setting":true}', '{"code":500,"setting":true}',
    '{"code":200,"msg":"file value"}', '"hello"', '[]', 'null',
])
async def test_json_file_without_error_envelope_remains_raw_text(content):
    client = make_client(lambda _: httpx.Response(200, text=content))
    try:
        assert await client.read_file_text("abc", "/config.json") == content
    finally:
        await client._http.aclose()


@pytest.mark.asyncio
async def test_file_contents_are_raw_text():
    def handler(request):
        assert request.url.path == "/panel/servers/abc/files/contents"
        assert request.url.params["file"] == "/logs/latest.log"
        return httpx.Response(200, text="[Server] hello\n")
    client = make_client(handler)
    try:
        assert await client.read_file_text("abc", "/logs/latest.log") == "[Server] hello\n"
    finally:
        await client._http.aclose()


@pytest.mark.parametrize("kwargs", [{"token": "x"}, {"client_id": "x"}])
def test_incomplete_gateway_credentials_do_not_silently_fallback(kwargs):
    with pytest.raises(ValueError):
        PanelClient(api_key="old-key", **kwargs)


@pytest.mark.asyncio
async def test_gateway_power_uses_websocket_not_guessed_http_power(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        assert request.method == "GET" and request.url.path.endswith("/websocket")
        return httpx.Response(200, json={"code": 200, "data": {"token": "ws-only", "socket": "wss://example.invalid/ws"}})
    sender = AsyncMock(return_value={"sent": True, "observed_state": "starting"})
    monkeypatch.setattr(client_mod, "send_power", sender)
    client = make_client(handler)
    try:
        assert (await client.power("abc", "start"))["observed_state"] == "starting"
        sender.assert_awaited_once_with("wss://example.invalid/ws", "ws-only", "start")
        assert len(calls) == 1
    finally:
        await client._http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("after_send", [False, True])
async def test_only_pre_send_auth_failure_can_trigger_refresh(monkeypatch, after_send):
    error = (client_mod.PowerTransportError("unconfirmed", command_sent=True) if after_send
             else client_mod.PreSendAuthError("expired"))
    monkeypatch.setattr(client_mod, "send_power", AsyncMock(side_effect=error))
    client = make_client(lambda _: httpx.Response(200, json={"code": 200, "data": {"token": "ws-only", "socket": "wss://example.invalid/ws"}}))
    try:
        with pytest.raises(client_mod.APIError if after_send else client_mod.AuthError):
            await client.power("abc", "start")
    finally:
        await client._http.aclose()
