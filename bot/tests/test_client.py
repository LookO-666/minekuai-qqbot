"""
测试 MinekuaiClient 的逻辑。
用 httpx.MockTransport 拦截请求，不会真的去调麦块联机。
"""
import importlib
import sys
import traceback
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest

# 直接把 client.py 当独立模块加载，避开 __init__.py 触发 nonebot 导入
sys.path.insert(0, str(Path(__file__).parent.parent / "plugins" / "minekuai"))
client_mod = importlib.import_module("client")
MinekuaiClient = client_mod.MinekuaiClient
AuthError = client_mod.AuthError
APIError = client_mod.APIError
RateLimitError = client_mod.RateLimitError
PanelClient = client_mod.PanelClient


def make_client_with_mock(handler):
    """构造一个客户端，但把 transport 替换成 mock"""
    client = MinekuaiClient(token="fake_token", client_id="fake_cid")
    client._http = httpx.AsyncClient(
        base_url=MinekuaiClient.BASE_URL,
        headers=client._build_headers(),
        transport=httpx.MockTransport(handler),
    )
    return client


@pytest.mark.asyncio
async def test_start_timing_success():
    """开计时卡 - 成功"""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["auth"] = request.headers.get("authorization")
        captured["clientid"] = request.headers.get("clientid")
        return httpx.Response(200, json={"code": 200, "msg": "ok"})

    client = make_client_with_mock(handler)
    result = await client.start_timing("12345")

    assert "/system/timeBalance/user/startTiming/12345" in captured["url"]
    assert captured["method"] == "POST"
    assert captured["auth"] == "Bearer fake_token"
    assert captured["clientid"] == "fake_cid"
    assert result["code"] == 200


@pytest.mark.asyncio
async def test_stop_timing_success():
    """关计时卡 - 成功"""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert "/stopTiming/12345" in str(request.url)
        return httpx.Response(200, json={"code": 200, "msg": "ok"})

    client = make_client_with_mock(handler)
    await client.stop_timing("12345")  # 不抛就算过


@pytest.mark.asyncio
@pytest.mark.parametrize("operation, args, method, path", [
    ("get_user_packages", (), "GET", "/system/timeBalance/user/userPackages"),
    ("start_timing", ("12345",), "POST", "/system/timeBalance/user/startTiming/12345"),
    ("stop_timing", ("12345",), "POST", "/system/timeBalance/user/stopTiming/12345"),
])
async def test_timing_endpoints_use_migrated_api_host(operation, args, method, path):
    calls = []
    response = {"code": 200, "data": []}

    def handler(request):
        calls.append(request)
        assert str(request.url) == f"https://api.minekuai.cn{path}"
        assert request.method == method
        assert request.headers["authorization"] == "Bearer fake_token"
        assert request.headers["clientid"] == "fake_cid"
        assert request.content == b""
        return httpx.Response(200, json=response)

    client = make_client_with_mock(handler)
    try:
        assert await getattr(client, operation)(*args) == response
        assert len(calls) == 1
    finally:
        await client._http.aclose()


@pytest.mark.asyncio
async def test_token_expired_http_401_raises_auth_error():
    """HTTP 401 - 应该抛 AuthError 而不是 APIError"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": 401, "msg": "unauthorized"})

    client = make_client_with_mock(handler)
    with pytest.raises(AuthError):
        await client.start_timing("12345")


@pytest.mark.asyncio
async def test_token_expired_business_401_raises_auth_error():
    """HTTP 200 + 业务码 401（麦块联机 token 冻结的实际响应形式）- 应抛 AuthError"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"code": 401, "msg": "token 已被冻结：xxx"}
        )

    client = make_client_with_mock(handler)
    with pytest.raises(AuthError) as exc:
        await client.start_timing("12345")
    assert "冻结" in str(exc.value) or "过期" in str(exc.value)


@pytest.mark.asyncio
async def test_business_error_code_raises():
    """HTTP 200 但其它业务码非成功 - 应抛 APIError"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 503, "msg": "服务器内部错误"})

    client = make_client_with_mock(handler)
    with pytest.raises(APIError) as exc:
        await client.start_timing("12345")
    assert "服务器内部错误" in str(exc.value)


@pytest.mark.asyncio
async def test_rate_limit_raises_rate_limit_error():
    """500 + '操作太频繁' - 应该抛 RateLimitError 而不是普通 APIError"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"code": 500, "msg": "操作太频繁，请稍后再试~"}
        )

    client = make_client_with_mock(handler)
    with pytest.raises(RateLimitError):
        await client.start_timing("12345")


@pytest.mark.asyncio
async def test_open_server_only_calls_start_timing():
    """开服 - 现在只调 startTiming（服务器实例启动归 Pterodactyl，已不在 bot 范畴）"""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        return httpx.Response(200, json={"code": 200, "msg": "ok"})

    client = make_client_with_mock(handler)
    await client.open_server(card_id="CARD1")

    assert len(calls) == 1
    assert calls[0][0] == "POST"
    assert "startTiming/CARD1" in calls[0][1]


@pytest.mark.asyncio
async def test_close_server_only_calls_stop_timing():
    """关服 - 只调 stopTiming"""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"code": 200, "msg": "ok"})

    client = make_client_with_mock(handler)
    await client.close_server(card_id="CARD1")

    assert len(calls) == 1
    assert "stopTiming/CARD1" in calls[0]


@pytest.mark.asyncio
async def test_empty_credentials_rejected():
    """空 token / client_id 应该在构造时就被拒绝"""
    with pytest.raises(ValueError):
        MinekuaiClient(token="", client_id="x")
    with pytest.raises(ValueError):
        MinekuaiClient(token="x", client_id="")


def make_panel_client_with_mock(handler, **kwargs):
    client = PanelClient(**kwargs)
    client._http = httpx.AsyncClient(
        base_url=PanelClient.BASE_URL,
        headers=client._build_headers(),
        transport=httpx.MockTransport(handler),
    )
    return client


@pytest.mark.asyncio
async def test_panel_client_prefers_api_key():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("authorization")
        captured["cookie"] = request.headers.get("cookie")
        captured["xsrf"] = request.headers.get("x-xsrf-token")
        return httpx.Response(200, json={"object": "server"})

    client = make_panel_client_with_mock(
        handler,
        api_key="ptlc_test-key",
        session_cookie="legacy-cookie",
        xsrf_token="legacy-xsrf",
    )
    await client.get_server_info("server-id")
    await client._http.aclose()

    assert captured["authorization"] == "Bearer ptlc_test-key"
    assert captured["cookie"] is None
    assert captured["xsrf"] is None


@pytest.mark.asyncio
async def test_panel_client_keeps_session_fallback():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("authorization")
        captured["cookie"] = request.headers.get("cookie")
        captured["xsrf"] = request.headers.get("x-xsrf-token")
        return httpx.Response(200, json={"object": "server"})

    client = make_panel_client_with_mock(
        handler,
        session_cookie="legacy-cookie",
        xsrf_token="legacy-xsrf",
    )
    await client.get_server_info("server-id")
    await client._http.aclose()

    assert captured["authorization"] is None
    assert captured["cookie"] == "legacy-cookie"
    assert captured["xsrf"] == "legacy-xsrf"


def test_panel_client_requires_one_auth_method():
    with pytest.raises(ValueError):
        PanelClient()


@pytest.mark.asyncio
@pytest.mark.parametrize("error, expected", [
    (httpx.ConnectTimeout, "无法连接"),
    (httpx.ReadTimeout, "操作结果尚未确认"),
    (httpx.WriteTimeout, "操作结果尚未确认"),
])
async def test_timing_timeout_is_clear_and_never_replays_post(error, expected):
    calls = []

    def handler(request):
        calls.append(request)
        raise error("internal detail", request=request)

    client = make_client_with_mock(handler)
    try:
        with pytest.raises(APIError, match=expected) as exc:
            await client.open_timing_only("private-card-id")
        assert len(calls) == 1
        assert "private-card-id" not in str(exc.value)
        assert "internal detail" not in str(exc.value)
        assert "api.minekuai.cn" in str(exc.value)
        assert "api.minekuai.com" not in str(exc.value)
    finally:
        await client._http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("panel", [False, True])
@pytest.mark.parametrize("body", ["<html>Verification</html>", "[]", "null"])
async def test_non_api_response_never_claims_power_success(panel, body):
    def handler(request):
        return httpx.Response(200, text=body)

    client = (
        make_panel_client_with_mock(handler, api_key="test-key")
        if panel else make_client_with_mock(handler)
    )
    try:
        with pytest.raises(APIError, match="未确认操作成功"):
            if panel:
                await client.start_instance("server-id")
            else:
                await client.start_timing("card-id")
    finally:
        await client._http.aclose()


@pytest.mark.asyncio
async def test_panel_accepts_empty_204_power_success():
    client = make_panel_client_with_mock(
        lambda request: httpx.Response(204), api_key="test-key",
    )
    try:
        await client.start_instance("server-id")
    finally:
        await client._http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("panel", [False, True])
@pytest.mark.parametrize("code", [401, 419, "401", "419"])
@pytest.mark.parametrize("business", [False, True])
async def test_auth_failure_never_includes_backend_message(panel, code, business):
    private_message = "remote-detail: credential must never be forwarded"

    def handler(request):
        return httpx.Response(
            200 if business else int(code), json={"code": code, "msg": private_message},
        )

    client = (
        make_panel_client_with_mock(handler, api_key="test-key")
        if panel else make_client_with_mock(handler)
    )
    try:
        with pytest.raises(AuthError) as exc:
            await client._request("GET", "/test")
        assert "remote-detail" not in str(exc.value)
        assert private_message not in str(exc.value)
    finally:
        await client._http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("panel", [False, True])
@pytest.mark.parametrize("kind", ["http", "business", "rate_limit"])
async def test_error_text_redacts_configured_secrets_and_unrelated_jwt(panel, kind):
    other_jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJvdGhlciJ9.signature"
    secrets = ["ptlc_private-key", "private-session-value", "private-xsrf-value"] if panel else ["fake_token"]
    detail = "操作太频繁，请稍后再试 " + " ".join(secrets + [other_jwt])

    def handler(request):
        if kind == "http":
            return httpx.Response(502, text=detail)
        return httpx.Response(200, json={"code": 500 if kind == "rate_limit" else 503, "msg": detail})

    client = (
        make_panel_client_with_mock(
            handler, api_key=secrets[0], session_cookie=f"session={secrets[1]}; locale=zh_CN",
            xsrf_token=secrets[2],
        ) if panel else make_client_with_mock(handler)
    )
    try:
        with pytest.raises(RateLimitError if kind == "rate_limit" else APIError) as exc:
            await client._request("GET", "/test")
        text = str(exc.value)
        assert "操作太频繁，请稍后再试" in text
        assert "[REDACTED]" in text
        for secret in secrets + [other_jwt]:
            assert secret not in text
        assert "eyJ" not in text
    finally:
        await client._http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("panel", [False, True])
async def test_http_error_redacts_entire_secret_before_truncating(panel):
    secret = "private-secret-" * 30
    detail = "x" * 180 + secret

    def handler(request):
        return httpx.Response(500, text=detail)

    if panel:
        client = make_panel_client_with_mock(handler, api_key=secret)
    else:
        client = make_client_with_mock(handler)
        client._token = secret
    try:
        with pytest.raises(APIError) as exc:
            await client._request("GET", "/test")
        assert "private-secret" not in str(exc.value)
        assert "[REDACTED]" in str(exc.value)
    finally:
        await client._http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["timing", "panel", "file"])
@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ReadTimeout])
async def test_network_error_traceback_does_not_leak_credentials(entrypoint, error_type):
    secret = "ptlc_network-secret" if entrypoint != "timing" else "fake_token"

    def handler(request):
        raise error_type(f"connection failed: Authorization Bearer {secret}", request=request)

    client = (
        make_client_with_mock(handler) if entrypoint == "timing"
        else make_panel_client_with_mock(handler, api_key=secret)
    )
    try:
        with pytest.raises(APIError) as exc:
            if entrypoint == "file":
                await client.read_file_text("server-id", "server.properties")
            else:
                await client._request("GET", "/test")
        assert secret not in str(exc.value)
        assert secret not in "".join(traceback.format_exception(exc.type, exc.value, exc.tb))
    finally:
        await client._http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 419, 500])
async def test_file_http_errors_redact_credentials(status):
    secret = "ptlc_file-secret"
    client = make_panel_client_with_mock(
        lambda request: httpx.Response(status, text=f"file-error-detail {secret}"),
        api_key=secret,
    )
    try:
        with pytest.raises(AuthError if status in (401, 419) else APIError) as exc:
            await client.read_file_text("server-id", "server.properties")
        assert secret not in str(exc.value)
        if status in (401, 419):
            assert "file-error-detail" not in str(exc.value)
        else:
            assert "file-error-detail" in str(exc.value)
    finally:
        await client._http.aclose()


def test_encoded_and_json_escaped_known_secrets_are_redacted():
    secret = 'private+secret/="value"'
    encoded = quote(secret, safe="")
    escaped = secret.replace('"', '\\"')
    text = client_mod._safe_error_text(f"detail {secret} {encoded} {escaped}", secret)
    assert text == "detail [REDACTED] [REDACTED] [REDACTED]"
