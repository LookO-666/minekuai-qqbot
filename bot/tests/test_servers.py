"""servers.py 的单元测试 - 多服务器配置数据层 + accounts 表"""
import importlib
import os
import sys
import tempfile
from pathlib import Path

import pytest


# 用临时数据库文件，避免污染生产 DB
@pytest.fixture
def servers_mod(tmp_path):
    """每个测试用一个全新的临时 SQLite 文件"""
    db_path = tmp_path / "test.db"
    os.environ["OPERATION_LOG_DB"] = str(db_path)
    os.environ["CREDENTIAL_ENCRYPTION_KEY"] = (
        "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
    )

    sys.path.insert(0, str(Path(__file__).parent.parent / "plugins" / "minekuai"))
    # 强制重新加载，让模块拿到新的 DB_PATH
    if "servers" in sys.modules:
        del sys.modules["servers"]
    mod = importlib.import_module("servers")
    mod.init_db()
    return mod


# ============================================================
# servers 表
# ============================================================

def test_add_and_get_server(servers_mod):
    servers_mod.add_server("GTNH", "12345", "token1", "cid1")
    s = servers_mod.get_server("GTNH")
    assert s is not None
    assert s.name == "GTNH"
    assert s.card_id == "12345"
    assert s.token == "token1"
    assert s.client_id == "cid1"
    assert s.address == ""           # 默认空
    assert s.account_phone == ""     # 默认空


def test_add_server_with_address_and_account(servers_mod):
    servers_mod.add_account("13900000000", "pw")
    servers_mod.add_server(
        "GTNH", "12345", "token1", "cid1",
        address="mc.test.com:25565",
        account_phone="13900000000",
    )
    s = servers_mod.get_server("GTNH")
    assert s.address == "mc.test.com:25565"
    assert s.account_phone == "13900000000"


def test_add_duplicate_name_raises(servers_mod):
    servers_mod.add_server("GTNH", "1", "t", "c")
    with pytest.raises(ValueError):
        servers_mod.add_server("GTNH", "2", "t2", "c2")


def test_list_servers_sorted_by_name(servers_mod):
    servers_mod.add_server("zzz", "1", "t", "c")
    servers_mod.add_server("aaa", "2", "t", "c")
    servers_mod.add_server("mmm", "3", "t", "c")
    names = [s.name for s in servers_mod.list_servers()]
    assert names == ["aaa", "mmm", "zzz"]


def test_remove_server(servers_mod):
    servers_mod.add_server("X", "1", "t", "c")
    assert servers_mod.remove_server("X") is True
    assert servers_mod.get_server("X") is None
    assert servers_mod.remove_server("X") is False


def test_update_token(servers_mod):
    servers_mod.add_server("X", "1", "old", "c")
    assert servers_mod.update_token("X", "new") is True
    assert servers_mod.get_server("X").token == "new"
    assert servers_mod.update_token("nonexistent", "x") is False


def test_update_credentials_with_client_id(servers_mod):
    """update_credentials 同时更新 token 和 clientid"""
    servers_mod.add_server("X", "1", "old_token", "old_cid")
    assert servers_mod.update_credentials("X", "new_token", "new_cid") is True
    s = servers_mod.get_server("X")
    assert s.token == "new_token"
    assert s.client_id == "new_cid"


def test_update_credentials_without_client_id(servers_mod):
    """传空 client_id 时只更新 token，不动 client_id"""
    servers_mod.add_server("X", "1", "old_token", "old_cid")
    assert servers_mod.update_credentials("X", "new_token", "") is True
    s = servers_mod.get_server("X")
    assert s.token == "new_token"
    assert s.client_id == "old_cid"


def test_update_address(servers_mod):
    servers_mod.add_server("X", "1", "t", "c")
    assert servers_mod.update_address("X", "mc.example.com") is True
    assert servers_mod.get_server("X").address == "mc.example.com"


def test_rename_server(servers_mod):
    servers_mod.add_server("old", "1", "t", "c")
    assert servers_mod.rename_server("old", "new") is True
    assert servers_mod.get_server("old") is None
    assert servers_mod.get_server("new") is not None
    # 改到一个已存在的名字应失败
    servers_mod.add_server("taken", "2", "t", "c")
    servers_mod.add_server("source", "3", "t", "c")
    assert servers_mod.rename_server("source", "taken") is False


# ============================================================
# accounts 表
# ============================================================

def test_add_and_get_account(servers_mod):
    servers_mod.add_account("13900000000", "secretpw")
    acc = servers_mod.get_account("13900000000")
    assert acc is not None
    assert acc.phone == "13900000000"
    assert acc.password == "secretpw"
    assert acc.last_refresh_at == 0


def test_add_duplicate_account_raises(servers_mod):
    servers_mod.add_account("13900000000", "pw1")
    with pytest.raises(ValueError):
        servers_mod.add_account("13900000000", "pw2")


def test_list_accounts(servers_mod):
    servers_mod.add_account("13900000003", "pw")
    servers_mod.add_account("13900000001", "pw")
    servers_mod.add_account("13900000002", "pw")
    phones = [a.phone for a in servers_mod.list_accounts()]
    assert phones == ["13900000001", "13900000002", "13900000003"]


def test_remove_account_unbinds_servers(servers_mod):
    """删除账号时，所有绑了这个账号的服务器应该被自动解绑"""
    servers_mod.add_account("13900000000", "pw")
    servers_mod.add_server("S1", "1", "t", "c", account_phone="13900000000")
    servers_mod.add_server("S2", "2", "t", "c", account_phone="13900000000")
    servers_mod.add_server("S3", "3", "t", "c")  # 没绑账号

    assert servers_mod.remove_account("13900000000") is True

    # S1 / S2 应该已经被解绑
    assert servers_mod.get_server("S1").account_phone == ""
    assert servers_mod.get_server("S2").account_phone == ""
    # S3 保持原样
    assert servers_mod.get_server("S3").account_phone == ""
    # 账号本身没了
    assert servers_mod.get_account("13900000000") is None


def test_update_account_password(servers_mod):
    servers_mod.add_account("13900000000", "old")
    assert servers_mod.update_account_password("13900000000", "new") is True
    assert servers_mod.get_account("13900000000").password == "new"


def test_mark_account_refreshed(servers_mod):
    servers_mod.add_account("13900000000", "pw")
    before = servers_mod.get_account("13900000000")
    assert before.last_refresh_at == 0

    servers_mod.mark_account_refreshed("13900000000")
    after = servers_mod.get_account("13900000000")
    assert after.last_refresh_at > 0


def test_bind_server_account(servers_mod):
    servers_mod.add_account("13900000000", "pw")
    servers_mod.add_server("X", "1", "t", "c")
    assert servers_mod.bind_server_account("X", "13900000000") is True
    assert servers_mod.get_server("X").account_phone == "13900000000"
    # 解绑
    assert servers_mod.bind_server_account("X", "") is True
    assert servers_mod.get_server("X").account_phone == ""


# ============================================================
# 迁移
# ============================================================

def test_init_db_is_idempotent(servers_mod):
    """重复调用 init_db 不会破坏数据"""
    servers_mod.add_server("X", "1", "t", "c")
    servers_mod.init_db()
    servers_mod.init_db()
    assert servers_mod.get_server("X") is not None


def test_maybe_migrate_from_env_creates_default(servers_mod):
    servers_mod.maybe_migrate_from_env("legacy_tok", "legacy_cid", "legacy_card")
    s = servers_mod.get_server("default")
    assert s is not None
    assert s.token == "legacy_tok"
    assert s.client_id == "legacy_cid"
    assert s.card_id == "legacy_card"


def test_maybe_migrate_skipped_when_not_empty(servers_mod):
    servers_mod.add_server("existing", "1", "t", "c")
    servers_mod.maybe_migrate_from_env("legacy", "legacy", "legacy")
    assert servers_mod.get_server("default") is None
    assert servers_mod.get_server("existing") is not None


def test_maybe_migrate_skipped_when_env_empty(servers_mod):
    """三个 env 字段任一为空就不迁移"""
    servers_mod.maybe_migrate_from_env("", "cid", "card")
    assert servers_mod.get_server("default") is None
    servers_mod.maybe_migrate_from_env("tok", "", "card")
    assert servers_mod.get_server("default") is None


def test_sensitive_values_are_encrypted_at_rest(servers_mod):
    servers_mod.add_account("13900000000", "secret-password")
    servers_mod.update_account_session(
        "13900000000", "laravel-cookie", "xsrf-value"
    )
    servers_mod.update_account_panel_api_key(
        "13900000000", "ptlc_secret-key"
    )
    servers_mod.add_server("X", "1", "secret-token", "client-id")

    with servers_mod._connect() as conn:
        raw_token = conn.execute(
            "SELECT token FROM servers WHERE name = 'X'"
        ).fetchone()[0]
        raw_account = conn.execute(
            "SELECT password, session_cookie, xsrf_token, panel_api_key "
            "FROM accounts "
            "WHERE phone = '13900000000'"
        ).fetchone()

    assert raw_token.startswith("enc:v1:")
    assert all(value.startswith("enc:v1:") for value in raw_account)
    assert servers_mod.get_server("X").token == "secret-token"
    account = servers_mod.get_account("13900000000")
    assert account.password == "secret-password"
    assert account.session_cookie == "laravel-cookie"
    assert account.xsrf_token == "xsrf-value"
    assert account.panel_api_key == "ptlc_secret-key"


def test_plaintext_credentials_migrate_once(servers_mod):
    now = 1
    with servers_mod._connect() as conn:
        conn.execute(
            "INSERT INTO servers "
            "(name, card_id, token, client_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("legacy", "1", "plain-token", "cid", now, now),
        )
        conn.execute(
            "INSERT INTO accounts "
            "(phone, password, session_cookie, xsrf_token, panel_api_key, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "13900000000", "plain-pw", "plain-cookie", "plain-xsrf",
                "ptlc_plain-key", now, now,
            ),
        )
        first = servers_mod.migrate_plaintext_credentials(conn)
        second = servers_mod.migrate_plaintext_credentials(conn)
        conn.commit()

    assert first == {"server_tokens": 1, "account_secrets": 4}
    assert second == {"server_tokens": 0, "account_secrets": 0}
    assert servers_mod.get_server("legacy").token == "plain-token"
    account = servers_mod.get_account("13900000000")
    assert account.password == "plain-pw"
    assert account.panel_api_key == "ptlc_plain-key"
