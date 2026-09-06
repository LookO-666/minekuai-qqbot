"""Client links are exact-row-only, credential-safe, and entirely read-only."""
import importlib
from pathlib import Path
import socket
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "plugins" / "minekuai"))
download = importlib.import_module("modpack_download")

INFO = {"name": "Create Delight", "version": "0.4.8.15", "game_version": "1.20.1", "java_version": "17"}


@pytest.mark.parametrize("url", [
    "https://cdn.example.com/client-0.4.8.15.zip",
    "http://downloads.example.org/client.zip",
    "https://cdn.example.com:443/packs/client.zip?version=0.4.8.15",
    "http://downloads.example.org:80/client.zip",
    "https://www.alipan.com/s/public-share?pwd=abcd",
    "https://pan.baidu.com/s/public-client?pwd=A_12",
    "https://www.123865.com/s/another-share?pwd=Ab12",
    "https://8.8.8.8/client.zip",
    "https://[2606:4700:4700::1111]/client.zip",
])
def test_valid_public_client_url_is_used_unchanged(url):
    result = download.build_client_download(INFO, {"clientUrl": url})
    assert result == INFO | {"url": url, "code": "", "detail": result["detail"], "exact": True}
    assert "客户端" in result["detail"]


@pytest.mark.parametrize("raw", [None, [], "invalid", {}, {"clientUrl": None}, {"clientUrl": False}, {"clientUrl": ["https://example.com/client.zip"]}])
def test_missing_or_invalid_input_uses_explicit_free_directory(raw):
    result = download.build_client_download(INFO, raw)
    assert result["url"] == download.FREE_CLIENT_DIRECTORY
    assert result["exact"] is False and result["code"] == ""
    assert "合集目录" in result["detail"] and "不是所选版本的直链" in result["detail"]
    assert "不会消耗积分" in result["detail"] and "不会下载或上传群文件" in result["detail"]
    for key, value in INFO.items():
        assert result[key] == value


@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "file:///etc/passwd", "ftp://example.com/client.zip",
    "//example.com/client.zip", "https:///client.zip", "https://example.com",
    "https://user:password@example.com/client.zip", "https://user@example.com/client.zip",
    "https://example.com:8443/client.zip", "https://example.com:80/client.zip",
    "https://localhost/client.zip", "https://sub.localhost/client.zip", "https://host.local/client.zip",
    "https://host.internal/client.zip", "https://host.lan/client.zip", "https://host/client.zip",
    "https://host.invalid/client.zip", "https://host.test/client.zip",
    "http://127.0.0.1/client.zip", "http://10.4.153.59/client.zip", "http://192.168.1.1/client.zip",
    "http://172.16.0.1/client.zip", "http://169.254.169.254/client.zip", "http://0.0.0.0/client.zip",
    "http://224.0.0.1/client.zip", "http://100.64.0.1/client.zip", "http://[::1]/client.zip",
    "http://[fc00::1]/client.zip", "http://2130706433/client.zip", "http://127.1/client.zip",
    "http://0177.0.0.1/client.zip", "http://0x7f000001/client.zip", "http://example.com./client.zip",
    "https://example.com/client.zip\n", "https://example.com/a b.zip", "https://example.com/a%20b.zip",
    "https://example.com/a\u202eb.zip", "https://example.com/a%0ab.zip", "https://example.com/a%250ab.zip",
    "https://example.com/a%FF.zip", "https://example.com/%", "https://example.com/%GG",
    "https://example.com\\private/client.zip", "https://example.com/%5cprivate/client.zip",
    "https://example.com/[CQ:at,qq=all]", "https://example.com/%5BCQ%3Aat,qq=all%5D",
    "https://example.com/%255BCQ%253Aat,qq=all%255D", "https://example.com/client.zip?x=[CQ:at,qq=all]",
    "https://example.com/client.zip?x=&#91;CQ:at,qq=all&#93;",
    "https://example.com/client.zip#%5BCQ:at,qq=all%5D", "https://example.com/<script>",
    "https://example.com/client.zip https://evil.example/client.zip", "https://example.com/" + "a" * 2048,
    "https://example.com/client.zip?pwd=private", "https://evil.pan.baidu.com/client.zip?pwd=abcd",
    "https://pan.baidu.com/s/public-client?pwd=../../secret",
    "https://example.com/client.zip#access_token=private",
])
def test_unsafe_urls_are_not_echoed_and_never_used(url):
    result = download.build_client_download(INFO, {"clientUrl": url})
    assert result["url"] == download.FREE_CLIENT_DIRECTORY
    assert result["exact"] is False
    assert url not in str(result)


@pytest.mark.parametrize("key", [
    "token", "access_token", "api_key", "API-Key", "Signature", "sign", "cookie", "Authorization",
    "X-Amz-Credential", "x-amz-security-token", "x-oss-signature", "password", "expires", "Policy",
    "session_id", "client_secret", "oauth_token", "refreshToken", "ticket", "code", "auth_token", "api_key_id",
    "xsrf", "AWSAccessKeyId", "Key-Pair-Id",
])
def test_private_or_signed_query_keys_are_rejected(key):
    assert download.build_client_download(INFO, {"clientUrl": f"https://example.com/client.zip?{key}=private"})["exact"] is False


@pytest.mark.parametrize("suffix", [
    "?%74oken=private", "?%2574oken=private", "?v=1;token=private", "?v=1%26token%3Dprivate",
    "?v=1%2526token%253Dprivate", "?v=eyJhbGciOiJIUzI1NiJ9.abc123.signature",
    "/eyJhbGciOiJIUzI1NiJ9.abc123.signature", "?v=" + "a=1&" * 40,
])
def test_encoded_credentials_jwt_and_unbounded_query_are_rejected(suffix):
    assert download.build_client_download(INFO, {"clientUrl": "https://example.com/client.zip" + suffix})["exact"] is False


@pytest.mark.parametrize("url", [
    download.FREE_CLIENT_DIRECTORY,
    download.FREE_CLIENT_DIRECTORY + "/",
    download.FREE_CLIENT_DIRECTORY + "?version=0.4.8.15",
    "http://123865.com/s/CiAtjv-xGYr",
    "https://www.123865.com/s/%43iAtjv-xGYr",
    "https://www.123pan.com/s/CiAtjv-xGYr",
])
def test_known_collection_is_never_labeled_exact(url):
    assert download.build_client_download(INFO, {"clientUrl": url})["exact"] is False


def test_never_uses_parent_server_or_paid_download_metadata():
    result = download.build_client_download(INFO, {
        "fileName": "https://example.com/server.zip", "aliyunDownloadAvailable": True,
        "aliyunShareUrl": "https://example.com/paid-client.zip", "aliyunSharePwd": "private",
        "aliyunServerShareUrl": "https://example.com/server.zip", "parent": {"clientUrl": "https://example.com/old-client.zip"},
        "downloadUrl": "https://example.com/personalized-client.zip",
    })
    assert result["url"] == download.FREE_CLIENT_DIRECTORY and result["code"] == "" and not result["exact"]
    assert "private" not in str(result)


@pytest.mark.parametrize("secret, encoded", [
    ("opaque-secret", "opaque-secret"), ("private key", "private%20key"),
    ("private/key", "private%2Fkey"), ("private/key", "private%252Fkey"),
])
def test_supplied_credentials_are_never_published(secret, encoded):
    result = download.build_client_download(INFO, {"clientUrl": f"https://example.com/client.zip?value={encoded}"}, secrets=(secret,))
    assert result["url"] == download.FREE_CLIENT_DIRECTORY
    assert secret not in str(result) and encoded not in str(result)


def test_metadata_is_bounded_and_defensive_without_double_escaping():
    info = INFO | {"name": "already &#91;safe&#93; [CQ:at,qq=all]\x00private"}
    result = download.build_client_download(info, secrets=("private",))
    assert "already &#91;safe&#93;" in result["name"]
    assert "[CQ:" not in result["name"] and "private" not in result["name"] and "\x00" not in result["name"]
    assert len(download.build_client_download(INFO | {"name": "a" * 300})["name"]) <= 160
    assert download.build_client_download(None)["name"] == ""


def test_building_download_data_has_no_network_file_or_dns_io(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("download presentation must remain pure")
    monkeypatch.setattr("builtins.open", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    assert download.build_client_download(INFO, {"clientUrl": "https://example.com/client.zip"})["exact"]
    assert not download.build_client_download(INFO)["exact"]
