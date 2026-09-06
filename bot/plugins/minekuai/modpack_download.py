"""Pure, conservative client-download presentation; never fetch or buy a file."""
from __future__ import annotations

import ipaddress
from html import unescape
import json
import re
import unicodedata
from urllib.parse import parse_qsl, quote, quote_plus, unquote, unquote_plus, urlsplit


# Minekuai's free "download client" button (server.59dc6642.js, 2026-09-06).
# This shared folder is not a guarantee that any particular release is present.
FREE_CLIENT_DIRECTORY = "https://www.123865.com/s/CiAtjv-xGYr"
MAX_URL_LENGTH = 2048
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*")
_BAD_PERCENT = re.compile(r"%(?![0-9a-fA-F]{2})")
_SECRET_KEYS = frozenset({
    "token", "accesstoken", "refreshtoken", "idtoken", "apikey", "key",
    "accesskey", "accesskeyid", "secret", "secretkey", "clientsecret",
    "credential", "credentials", "authorization", "auth", "authkey",
    "cookie", "cookies", "setcookie", "password", "passwd", "session",
    "sessionid", "sid", "signature", "sign", "sig", "expires", "expire",
    "expiration", "policy", "securitytoken", "ticket", "code", "pass",
    "xsrf", "csrf", "keypairid", "awsaccesskeyid", "ossaccesskeyid",
})
_PUBLIC_SHARE_HOSTS = frozenset({
    "pan.baidu.com", "pan.quark.cn", "aliyundrive.com", "www.aliyundrive.com",
    "alipan.com", "www.alipan.com", "123pan.com", "www.123pan.com",
    "123865.com", "www.123865.com", "123684.com", "www.123684.com",
    "123912.com", "www.123912.com",
})
_FALLBACK_DETAIL = (
    "这是官网提供的免费客户端合集目录，不是所选版本的直链。"
    "请在目录中按上面的整合包名称和版本选择客户端，不要下载服务端。"
    "机器人不会消耗积分购买下载链接，也不会下载或上传群文件。"
)


def _secret_variants(secrets: tuple[str, ...]) -> tuple[str, ...]:
    variants = set()
    for secret in secrets:
        if not isinstance(secret, str) or not secret:
            continue
        variants.update((
            secret, unquote(secret), unquote_plus(secret), quote(secret, safe=""),
            quote_plus(secret), json.dumps(secret)[1:-1],
            json.dumps(secret, ensure_ascii=False)[1:-1],
        ))
    return tuple(sorted((value for value in variants if value), key=len, reverse=True))


def _display(value, limit: int, secrets: tuple[str, ...]) -> str:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return ""
    text = _JWT.sub("已隐藏", str(value))
    for secret in secrets:
        text = text.replace(secret, "已隐藏")
    text = "".join(char for char in text if not unicodedata.category(char).startswith("C"))
    # Metadata is normally already catalog-sanitized; do not escape its existing
    # entities again. Raw CQ delimiters are never passed through nevertheless.
    return text.replace("[", "&#91;").replace("]", "&#93;").strip()[:limit]


def _public_host(host: str) -> bool:
    if not host or host.endswith(".") or "%" in host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if len(host) > 253 or "." not in host:
            return False
        if host.endswith((".localhost", ".local", ".localdomain", ".internal", ".lan", ".home", ".corp", ".test", ".invalid", ".example")):
            return False
        # Reject numeric/hex/octal URL host spellings that browsers may treat as
        # an IP even though ipaddress deliberately does not accept them.
        if host.rsplit(".", 1)[-1].isdigit() or host.startswith("0x"):
            return False
        return all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                   for label in host.split("."))
    return address.is_global and not (
        address.is_multicast or address.is_reserved or address.is_unspecified
        or address.is_loopback or address.is_link_local
    )


def _safe_query(value: str, host: str) -> bool:
    try:
        pairs = parse_qsl(value.replace(";", "&"), keep_blank_values=True, max_num_fields=32)
    except (ValueError, UnicodeError):
        return False
    for key, content in pairs:
        normalized = re.sub(r"[-_.]", "", key).casefold()
        if (normalized in _SECRET_KEYS
                or normalized.startswith(("xamz", "xoss", "oauth", "authorization", "apikey", "accesskey", "session", "cookie"))
                or normalized.endswith(("token", "signature", "credential", "secret"))):
            return False
        if normalized == "pwd" and (
            host not in _PUBLIC_SHARE_HOSTS or re.fullmatch(r"[A-Za-z0-9_-]{1,32}", content) is None
        ):
            return False
    return True


def _public_client_url(value, secrets: tuple[str, ...]) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_URL_LENGTH:
        return None
    forms = []
    decoded = value
    for _ in range(5):
        if _BAD_PERCENT.search(decoded):
            return None
        if (any(char.isspace() or unicodedata.category(char).startswith("C") for char in decoded)
                or any(char in decoded for char in "\\<>\"'`")
                or "[cq:" in unescape(decoded).casefold()
                or _JWT.search(decoded)
                or any(secret in decoded for secret in secrets)):
            return None
        forms.append(decoded)
        following = unquote(decoded, errors="strict")
        if following == decoded:
            break
        decoded = following
    else:
        return None
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        if (parsed.scheme not in ("http", "https") or not parsed.netloc
                or parsed.username is not None or parsed.password is not None
                or not _public_host(host)
                or parsed.port not in (None, 80 if parsed.scheme == "http" else 443)):
            return None
        # Percent-encoded userinfo, CQ delimiters, or parser separators must not
        # gain meaning after decoding. IPv6 brackets are allowed only in host.
        for form in forms:
            candidate = urlsplit(form)
            if candidate.netloc != parsed.netloc:
                return None
            if any(mark in candidate.path + candidate.query + candidate.fragment for mark in "[]"):
                return None
            if not _safe_query(candidate.query, host) or not _safe_query(candidate.fragment, host):
                return None
            if (host in _PUBLIC_SHARE_HOSTS and host.removeprefix("www.").startswith("123")
                    and candidate.path.rstrip("/") == "/s/CiAtjv-xGYr"):
                return None
        # The website's shared collection can never prove an exact pack version,
        # even if a catalog row happens to repeat that URL or append a query.
        if parsed.path in ("", "/"):
            return None
    except (ValueError, UnicodeError):
        return None
    return value


def build_client_download(info: dict, raw: dict | None = None, *, secrets: tuple[str, ...] = ()) -> dict:
    """Build safe QQ data from the exact selected catalog row, with free fallback.

    The caller establishes row/version identity. This helper never follows a URL,
    reads files, resolves DNS, requests a paid link, or uses server-file fields.
    """
    secrets = _secret_variants(secrets)
    info = info if isinstance(info, dict) else {}
    result = {key: _display(info.get(key), limit, secrets) for key, limit in (
        ("name", 160), ("version", 80), ("game_version", 64), ("java_version", 32),
    )}
    try:
        url = _public_client_url(raw.get("clientUrl"), secrets) if isinstance(raw, dict) else None
    except (ValueError, UnicodeError):
        url = None
    result.update({
        "url": url or FREE_CLIENT_DIRECTORY,
        "code": "",
        "detail": ("该链接来自所选版本的官网客户端字段；机器人不会消耗积分，也不会下载或上传群文件。"
                   if url else _FALLBACK_DETAIL),
        "exact": bool(url),
    })
    return result
