"""Modpack routes use MockTransport only; no real install or network request."""
import importlib
import json
from pathlib import Path
import sys

import httpx
import pytest


sys.path.insert(0, str(Path(__file__).parents[1] / "plugins" / "minekuai"))
client_mod = importlib.import_module("client")


def make_client(handler):
    client = client_mod.MinekuaiClient("private-test-token", "public-client")
    client._http = httpx.AsyncClient(
        base_url=client.BASE_URL, headers=client._build_headers(),
        transport=httpx.MockTransport(handler),
    )
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize("method,args,expected", [
    ("search_modpacks", ("ATM 10",), {
        "name": "ATM 10", "primaryId": "", "pageNum": "1", "pageSize": "9",
        "orderByColumn": "download_count", "isAsc": "desc",
    }),
    ("list_modpack_versions", ("12345",), {
        "primaryId": "12345", "pageNum": "1", "pageSize": "9",
        "orderByColumn": "createTime", "isAsc": "desc",
    }),
])
async def test_catalog_routes_and_root_response(method, args, expected):
    calls = []
    payload = {"code": 200, "rows": [{"id": "version-id"}], "total": 1}

    def handler(request):
        calls.append(request)
        assert str(request.url).startswith("https://api.minekuai.cn/system/modpacks/list?")
        assert request.method == "GET"
        assert dict(request.url.params) == expected
        assert request.headers["authorization"] == "Bearer private-test-token"
        assert request.headers["clientid"] == "public-client"
        return httpx.Response(200, json=payload)

    client = make_client(handler)
    try:
        assert await getattr(client, method)(*args) == payload
        assert len(calls) == 1
    finally:
        await client._http.aclose()


@pytest.mark.asyncio
async def test_pagination_limits_are_forwarded_exactly():
    def handler(request):
        assert request.url.params["pageNum"] == "1000"
        assert request.url.params["pageSize"] == "50"
        return httpx.Response(200, json={"rows": [], "total": 0})

    client = make_client(handler)
    try:
        await client.search_modpacks(" test ", page=1000, page_size=50)
    finally:
        await client._http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("method,first", [("search_modpacks", "ATM"), ("list_modpack_versions", "123")])
@pytest.mark.parametrize("kwargs", [
    {"page": 0}, {"page": 1001}, {"page": True}, {"page": "1"},
    {"page_size": 0}, {"page_size": 51}, {"page_size": False}, {"page_size": 1.5},
])
async def test_invalid_pagination_never_requests(method, first, kwargs):
    client = client_mod.MinekuaiClient("test-token", "client")
    with pytest.raises(ValueError):
        await getattr(client, method)(first, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("keyword", [None, 123, True, "", "   ", "x" * 101, "bad\nquery", "bad\u202equery"])
async def test_invalid_keyword_never_requests(keyword):
    client = client_mod.MinekuaiClient("test-token", "client")
    with pytest.raises(ValueError):
        await client.search_modpacks(keyword)


@pytest.mark.asyncio
@pytest.mark.parametrize("project_id", [None, True, 0, -1, "", "../123", "a" * 129, "a\n"])
async def test_invalid_project_id_never_requests(project_id):
    client = client_mod.MinekuaiClient("test-token", "client")
    with pytest.raises(ValueError):
        await client.list_modpack_versions(project_id)


@pytest.mark.asyncio
async def test_switch_modpack_exact_body_preserves_file_parameter():
    calls = []
    file_name = " https://cdn.example.test/packs/[release]&build=1.zip "
    response = {"code": 200, "msg": "accepted"}

    def handler(request):
        calls.append(request)
        assert request.method == "POST"
        assert str(request.url) == "https://api.minekuai.cn/system/mineKuaiMinecraft/v2/switchModpack"
        assert json.loads(request.content) == {
            "instanceId": "deadbeef", "fileName": file_name,
            "id": "1970000000000000001", "useExternalUrl": True,
        }
        return httpx.Response(200, json=response)

    client = make_client(handler)
    try:
        assert await client.switch_modpack("deadbeef", file_name, "1970000000000000001") == response
        assert len(calls) == 1
    finally:
        await client._http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("instance_id", ""), ("instance_id", "deadbeef-0000-0000-0000-000000000000"),
    ("instance_id", "../abcde"), ("instance_id", 12345678),
    ("file_name", ""), ("file_name", "   "), ("file_name", "x" * 2049),
    ("file_name", "bad\x00.zip"), ("file_name", "bad\u202efile.zip"),
    ("file_name", ["file.zip"]), ("modpack_id", ""), ("modpack_id", True),
])
async def test_invalid_install_arguments_never_requests(field, value):
    kwargs = {"instance_id": "deadbeef", "file_name": "file.zip", "modpack_id": "123"}
    kwargs[field] = value
    client = client_mod.MinekuaiClient("test-token", "client")
    with pytest.raises(ValueError):
        await client.switch_modpack(**kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["http_auth", "business_auth", "business", "timeout", "unknown_success"])
async def test_install_errors_are_redacted_and_never_retried(failure):
    calls = []

    def handler(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("private-test-token", request=request)
        if failure == "http_auth":
            return httpx.Response(401, text="private-test-token")
        if failure == "business_auth":
            return httpx.Response(200, json={"code": 401, "msg": "private-test-token"})
        if failure == "business":
            return httpx.Response(200, json={"code": 500, "msg": "failed private-test-token"})
        return httpx.Response(200, json={"message": "private-test-token"})

    client = make_client(handler)
    try:
        with pytest.raises(client_mod.MinekuaiError) as error:
            await client.switch_modpack("deadbeef", "file.zip", "123")
        assert "private-test-token" not in str(error.value)
        assert len(calls) == 1
    finally:
        await client._http.aclose()
