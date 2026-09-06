"""Response diagnostics use offline MockTransport and disclose categories only."""
import importlib
from pathlib import Path
import sys
import traceback

import httpx
import pytest


sys.path.insert(0, str(Path(__file__).parents[1] / "plugins" / "minekuai"))
client_mod = importlib.import_module("client")
PRIVATE = "private-body-header-path-value"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJkaWFnbm9zdGljcyJ9.signature"


def make_client(handler, *, panel=False):
    client = (
        client_mod.PanelClient(token="private-configured-token", client_id="public-client")
        if panel else client_mod.MinekuaiClient("private-configured-token", "public-client")
    )
    # A caller's global redirect setting must never replay a destructive POST.
    client._http = httpx.AsyncClient(
        base_url=client.BASE_URL, transport=httpx.MockTransport(handler), follow_redirects=True,
    )
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize("panel", [False, True])
@pytest.mark.parametrize("status", [200, 500])
@pytest.mark.parametrize("content,media_type,expected_media,expected_body", [
    (b"", None, "missing", "empty"),
    (b"", "text/html", "text/html", "empty"),
    (f"<html>{PRIVATE} {JWT}</html>".encode(), "text/html; charset=utf-8", "text/html", "HTML"),
    (f" <!DOCTYPE html><html>{PRIVATE}</html>".encode(), "text/plain", "text/plain", "HTML"),
    (f"<body>{PRIVATE}</body>".encode(), "application/xhtml+xml", "application/xhtml+xml", "HTML"),
    (f"ciphertext-{PRIVATE}-{JWT}".encode(), "application/octet-stream", "application/octet-stream", "other"),
    (PRIVATE.encode(), f"text/{PRIVATE}", "other", "other"),
    (b"invalid-json", "application/json", "application/json", "other"),
])
async def test_non_json_response_reports_only_safe_metadata(
    panel, status, content, media_type, expected_media, expected_body,
):
    calls = []
    logs = []

    def handler(request):
        calls.append(request)
        headers = {"encrypt-key": PRIVATE + JWT, "x-secret": PRIVATE}
        if media_type is not None:
            headers["content-type"] = media_type
        return httpx.Response(status, content=content, headers=headers)

    client = make_client(handler, panel=panel)
    sink = client_mod.logger.add(lambda message: logs.append(str(message)))
    try:
        with pytest.raises(client_mod.APIError) as error:
            if panel:
                await client.get_resources(PRIVATE)
            else:
                await client.switch_modpack("deadbeef", f"{PRIVATE}.zip", "123")
        text = str(error.value)
        assert text.startswith("面板 API" if panel else "整合包安装 API")
        assert f"HTTP {status}" in text
        assert f"Content-Type={expected_media}" in text
        assert f"body={expected_body}" in text
        assert "encrypt-key=true" in text and "redirect=false" in text
        assert "未确认操作成功" in text
        assert "安全验证" not in text and "WAF" not in text
        rendered = "".join(traceback.format_exception(error.type, error.value, error.tb))
        for secret in (PRIVATE, JWT, "private-configured-token"):
            assert secret not in text
            assert secret not in rendered
            assert secret not in "".join(logs)
        warnings = [line for line in logs if "WARNING" in line]
        assert len(warnings) == 1 and text in warnings[0]
        assert len(calls) == 1
    finally:
        client_mod.logger.remove(sink)
        await client._http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("payload", ["html", "empty", "success_json"])
async def test_install_redirect_is_not_followed_or_reported_as_success(status, payload):
    calls = []

    def handler(request):
        calls.append(request)
        headers = {"location": f"https://example.invalid/{PRIVATE}?token={JWT}"}
        if payload == "success_json":
            return httpx.Response(status, json={"code": 200}, headers=headers)
        return httpx.Response(
            status, content=f"<html>{PRIVATE}</html>".encode() if payload == "html" else b"",
            headers=headers,
        )

    client = make_client(handler)
    try:
        with pytest.raises(client_mod.APIError) as error:
            await client.switch_modpack("deadbeef", "file.zip", "123")
        text = str(error.value)
        assert "整合包安装 API" in text and "redirect=true" in text
        assert f"HTTP {status}" in text and "encrypt-key=false" in text
        assert "未确认操作成功" in text
        assert PRIVATE not in text and JWT not in text and "example.invalid" not in text
        assert len(calls) == 1 and calls[0].method == "POST"
    finally:
        await client._http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("path,service", [
    ("/system/mineKuaiMinecraft/v2/switchModpack", "整合包安装 API"),
    ("/system/modpacks/list", "整合包目录 API"),
    ("/system/timeBalance/user/userPackages", "计时卡 API"),
    (f"/system/timeBalance/user/startTiming/{PRIVATE}", "计时卡 API"),
    (f"/system/timeBalance/user/stopTiming/{PRIVATE}", "计时卡 API"),
    (f"/system/timeBalance/user/instance/{PRIVATE}/start", "计时卡 API"),
    (f"/system/timeBalance/user/instance/{PRIVATE}/stop", "计时卡 API"),
    (f"/panel/servers/{PRIVATE}/resources", "面板 API"),
    (f"/api/client/servers/{PRIVATE}", "面板 API"),
    (f"/unknown/{PRIVATE}", "麦块 API"),
])
@pytest.mark.parametrize("failure", ["non_json", "timeout", "network"])
async def test_request_diagnostics_use_fixed_service_names_without_path(path, service, failure):
    calls = []
    logs = []

    def handler(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout(f"{PRIVATE} {JWT} {request.url}", request=request)
        if failure == "network":
            raise httpx.ConnectError(f"{PRIVATE} {JWT} {request.url}", request=request)
        return httpx.Response(200, content=b"", headers={"content-type": "text/plain"})

    client = make_client(handler)
    sink = client_mod.logger.add(lambda message: logs.append(str(message)))
    try:
        with pytest.raises(client_mod.APIError) as error:
            await client._request("POST", path)
        text = str(error.value)
        assert service in text
        assert PRIVATE not in text and JWT not in text and path not in text
        assert PRIVATE not in "".join(logs)
        if failure == "non_json":
            warning = [line for line in logs if "WARNING" in line]
            assert len(warning) == 1
            assert service in warning[0] and "HTTP 200" in warning[0]
            assert "body=empty" in warning[0] and "encrypt-key=false" in warning[0]
        assert len(calls) == 1
    finally:
        client_mod.logger.remove(sink)
        await client._http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [False, True, 0.0, 200.0, None, [], {}, "accepted"])
async def test_install_requires_explicit_typed_success_code(code):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"code": code})

    client = make_client(handler)
    try:
        with pytest.raises(client_mod.APIError):
            await client.switch_modpack("deadbeef", "file.zip", "123")
        assert len(calls) == 1
    finally:
        await client._http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("panel", [False, True])
@pytest.mark.parametrize("code", [401, "401", 419, "419"])
async def test_auth_business_code_on_http_error_discards_remote_message(panel, code):
    client = make_client(lambda request: httpx.Response(
        500, json={"code": code, "msg": PRIVATE + JWT},
    ), panel=panel)
    try:
        with pytest.raises(client_mod.AuthError) as error:
            await client._request("GET", f"/unknown/{PRIVATE}")
        assert PRIVATE not in str(error.value) and JWT not in str(error.value)
    finally:
        await client._http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["redirect", "http_error", "timeout", "network"])
async def test_file_error_uses_safe_panel_diagnostics(failure):
    calls = []

    def handler(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout(PRIVATE, request=request)
        if failure == "network":
            raise httpx.ConnectError(PRIVATE, request=request)
        return httpx.Response(
            302 if failure == "redirect" else 502,
            text=f"<html>{PRIVATE} {JWT}</html>",
            headers={"location": f"https://example.invalid/{PRIVATE}", "encrypt-key": JWT},
        )

    client = make_client(handler, panel=True)
    try:
        with pytest.raises(client_mod.APIError) as error:
            await client.read_file_text(PRIVATE, f"/{PRIVATE}.log")
        text = str(error.value)
        assert "面板 API" in text and PRIVATE not in text and JWT not in text
        assert len(calls) == 1
    finally:
        await client._http.aclose()


@pytest.mark.asyncio
async def test_raw_file_content_remains_supported_without_json_requirement():
    text = "ordinary raw installation log\n整合包安装成功!"
    client = make_client(lambda request: httpx.Response(200, text=text), panel=True)
    try:
        assert await client.read_file_text("deadbeef", "/installserverlogs.log") == text
    finally:
        await client._http.aclose()
