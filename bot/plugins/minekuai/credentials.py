"""敏感凭据的应用层加密与明文数据库迁移。"""
from __future__ import annotations

import os
import sqlite3

from cryptography.fernet import Fernet, InvalidToken


ENCRYPTED_PREFIX = "enc:v1:"
KEY_ENV = "CREDENTIAL_ENCRYPTION_KEY"


class CredentialEncryptionError(RuntimeError):
    """加密配置缺失、密钥无效或密文损坏。"""


def _fernet() -> Fernet:
    key = os.getenv(KEY_ENV, "").strip()
    if not key:
        raise CredentialEncryptionError(
            f"缺少 {KEY_ENV}，无法安全读写账号密码和 token"
        )
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise CredentialEncryptionError(
            f"{KEY_ENV} 格式无效，应为 Fernet URL-safe Base64 密钥"
        ) from exc


def is_encrypted(value: str) -> bool:
    return value.startswith(ENCRYPTED_PREFIX)


def encrypt_secret(value: str) -> str:
    """加密非空明文；已经加密的值保持不变。"""
    if not value or is_encrypted(value):
        return value
    token = _fernet().encrypt(value.encode("utf-8")).decode("ascii")
    return ENCRYPTED_PREFIX + token


def decrypt_secret(value: str) -> str:
    """解密凭据。明文值必须先通过迁移函数处理。"""
    if not value:
        return value
    if not is_encrypted(value):
        raise CredentialEncryptionError("数据库中仍存在未迁移的明文凭据")
    token = value[len(ENCRYPTED_PREFIX):]
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, UnicodeEncodeError) as exc:
        raise CredentialEncryptionError(
            "凭据解密失败：密钥不匹配或数据库内容已损坏"
        ) from exc


def migrate_plaintext_credentials(conn: sqlite3.Connection) -> dict[str, int]:
    """在同一 SQLite 事务中把旧明文凭据迁移为 Fernet 密文。"""
    counts = {"server_tokens": 0, "account_secrets": 0}

    for name, token in conn.execute(
        "SELECT name, token FROM servers"
    ).fetchall():
        if token and not is_encrypted(token):
            conn.execute(
                "UPDATE servers SET token = ? WHERE name = ?",
                (encrypt_secret(token), name),
            )
            counts["server_tokens"] += 1

    rows = conn.execute(
        "SELECT phone, password, session_cookie, xsrf_token FROM accounts"
    ).fetchall()
    for phone, password, session_cookie, xsrf_token in rows:
        encrypted = (
            encrypt_secret(password),
            encrypt_secret(session_cookie),
            encrypt_secret(xsrf_token),
        )
        original = (password, session_cookie, xsrf_token)
        if encrypted != original:
            conn.execute(
                "UPDATE accounts SET password = ?, session_cookie = ?, "
                "xsrf_token = ? WHERE phone = ?",
                (*encrypted, phone),
            )
            counts["account_secrets"] += sum(
                bool(old) and old != new
                for old, new in zip(original, encrypted)
            )

    return counts
