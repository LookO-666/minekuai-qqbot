"""
测试 MinekuaiClient 的逻辑。
用 httpx.MockTransport 拦截请求，不会真的去调麦块联机。
"""
import importlib
import sys
from pathlib import Path

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
