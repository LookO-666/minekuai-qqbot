"""服务器配置 - 多个 minekuai 服务器的运行时存储

存储在 SQLite 数据库（共享 OPERATION_LOG_DB 路径，独立的 servers 表）。
群里通过『添加服务器』『删除服务器』『更新token』等指令增删改查，重启不丢。
"""
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from time import time

from loguru import logger

try:
    from .credentials import (
        decrypt_secret,
        encrypt_secret,
        migrate_plaintext_credentials,
    )
except ImportError:  # 独立加载模块的测试兼容
    from credentials import (
        decrypt_secret,
        encrypt_secret,
        migrate_plaintext_credentials,
    )

DB_PATH = Path(os.getenv("OPERATION_LOG_DB", "operation_log.db"))


@dataclass
class Server:
    name: str
    card_id: str
    token: str
    client_id: str
    address: str
    account_phone: str               # 绑定的账号（空字符串=未绑定，遇 401 时不自动刷）
    instance_uuid: str               # Pterodactyl 实例完整 UUID，留空=只开计时卡不开服务器
    auto_close_idle_minutes: int     # 空闲 N 分钟自动关停；0 = 关闭此功能
    last_started_at: int             # 最近一次成功启动/保活启动时间戳；0=未知
    created_at: int
    updated_at: int


@dataclass
class Account:
    phone: str
    password: str
    session_cookie: str         # Playwright 登录后捕获的整段 cookie，调 panel API 用
    xsrf_token: str             # 同一次登录里抓的 X-XSRF-TOKEN header 值
    created_at: int
    updated_at: int
    last_refresh_at: int        # 上次成功刷新 token 的时间戳


_SERVER_COLS = (
    "name, card_id, token, client_id, address, account_phone, "
    "instance_uuid, auto_close_idle_minutes, last_started_at, "
    "created_at, updated_at"
)
_ACCOUNT_COLS = (
    "phone, password, session_cookie, xsrf_token, "
    "created_at, updated_at, last_refresh_at"
)


def _server_from_row(row) -> Server:
    values = list(row)
    values[2] = decrypt_secret(values[2])
    return Server(*values)


def _account_from_row(row) -> Account:
    values = list(row)
    values[1] = decrypt_secret(values[1])
    values[2] = decrypt_secret(values[2])
    values[3] = decrypt_secret(values[3])
    return Account(*values)


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(DB_PATH)


def init_db() -> None:
    """初始化 servers / accounts 表（幂等，会自动迁移老库添加新列）"""
    with closing(_connect()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS servers (
                name                    TEXT PRIMARY KEY,
                card_id                 TEXT NOT NULL,
                token                   TEXT NOT NULL,
                client_id               TEXT NOT NULL,
                address                 TEXT NOT NULL DEFAULT '',
                account_phone           TEXT NOT NULL DEFAULT '',
                instance_uuid           TEXT NOT NULL DEFAULT '',
                auto_close_idle_minutes INTEGER NOT NULL DEFAULT 0,
                last_started_at         INTEGER NOT NULL DEFAULT 0,
                created_at              INTEGER NOT NULL,
                updated_at              INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                phone           TEXT PRIMARY KEY,
                password        TEXT NOT NULL,
                session_cookie  TEXT NOT NULL DEFAULT '',
                xsrf_token      TEXT NOT NULL DEFAULT '',
                created_at      INTEGER NOT NULL,
                updated_at      INTEGER NOT NULL,
                last_refresh_at INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        # 迁移：老库可能缺新列
        srv_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(servers)").fetchall()
        }
        for col_name, col_def in [
            ("address", "TEXT NOT NULL DEFAULT ''"),
            ("account_phone", "TEXT NOT NULL DEFAULT ''"),
            ("instance_uuid", "TEXT NOT NULL DEFAULT ''"),
            ("auto_close_idle_minutes", "INTEGER NOT NULL DEFAULT 0"),
            ("last_started_at", "INTEGER NOT NULL DEFAULT 0"),
        ]:
            if col_name not in srv_cols:
                conn.execute(
                    f"ALTER TABLE servers ADD COLUMN {col_name} {col_def}"
                )
                logger.info(f"已为 servers 表添加 {col_name} 列（迁移）")
        _backfill_last_started_at(conn)
        acc_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(accounts)").fetchall()
        }
        for col_name, col_def in [
            ("session_cookie", "TEXT NOT NULL DEFAULT ''"),
            ("xsrf_token", "TEXT NOT NULL DEFAULT ''"),
        ]:
            if col_name not in acc_cols:
                conn.execute(
                    f"ALTER TABLE accounts ADD COLUMN {col_name} {col_def}"
                )
                logger.info(f"已为 accounts 表添加 {col_name} 列（迁移）")
        migrated = migrate_plaintext_credentials(conn)
        if any(migrated.values()):
            logger.info(
                "敏感凭据迁移完成: "
                f"server_tokens={migrated['server_tokens']}, "
                f"account_secrets={migrated['account_secrets']}"
            )
        # QQ ↔ MC 玩家绑定（一个 QQ 对一个 MC 名，反向唯一）
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bindings (
                qq_id      INTEGER PRIMARY KEY,
                mc_name    TEXT NOT NULL UNIQUE COLLATE NOCASE,
                created_at INTEGER NOT NULL
            )
            """
        )
        # 在线时长按天累计（秒）
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS playtime (
                mc_name TEXT NOT NULL COLLATE NOCASE,
                day     TEXT NOT NULL,
                seconds INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (mc_name, day)
            )
            """
        )
        # 死亡次数按天累计
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS deaths (
                mc_name TEXT NOT NULL COLLATE NOCASE,
                day     TEXT NOT NULL,
                count   INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (mc_name, day)
            )
            """
        )
        conn.commit()
    logger.info(f"服务器配置数据库就绪: {DB_PATH.resolve()}")


def _backfill_last_started_at(conn: sqlite3.Connection) -> None:
    """Fill last_started_at for old databases without triggering keepalive immediately."""
    rows = conn.execute(
        "SELECT name FROM servers WHERE last_started_at = 0"
    ).fetchall()
    if not rows:
        return
    now = int(time())
    has_log = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name = 'operation_log'"
    ).fetchone()
    for (name,) in rows:
        started_at = 0
        if has_log:
            log_row = conn.execute(
                "SELECT ts FROM operation_log "
                "WHERE success = 1 AND ("
                "command = ? OR command = ? OR command = ? OR "
                "command LIKE ?) "
                "ORDER BY ts DESC LIMIT 1",
                (
                    f"start {name}",
                    f"restart {name}",
                    f"keepalive_start {name}",
                    f"start {name} %",
                ),
            ).fetchone()
            if log_row and log_row[0]:
                try:
                    started_at = int(datetime.fromisoformat(log_row[0]).timestamp())
                except ValueError:
                    started_at = 0
        if started_at <= 0:
            started_at = now
        conn.execute(
            "UPDATE servers SET last_started_at = ? WHERE name = ?",
            (started_at, name),
        )


def list_servers() -> list[Server]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            f"SELECT {_SERVER_COLS} FROM servers ORDER BY name"
        ).fetchall()
    return [_server_from_row(r) for r in rows]


def get_server(name: str) -> Server | None:
    with closing(_connect()) as conn:
        row = conn.execute(
            f"SELECT {_SERVER_COLS} FROM servers WHERE name = ?",
            (name,),
        ).fetchone()
    return _server_from_row(row) if row else None


def add_server(
    name: str,
    card_id: str,
    token: str,
    client_id: str,
    address: str = "",
    account_phone: str = "",
    instance_uuid: str = "",
) -> None:
    """名字已存在抛 ValueError"""
    now = int(time())
    try:
        with closing(_connect()) as conn:
            conn.execute(
                "INSERT INTO servers (name, card_id, token, client_id, "
                "address, account_phone, instance_uuid, last_started_at, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    name, card_id, encrypt_secret(token), client_id, address,
                    account_phone, instance_uuid, now, now, now,
                ),
            )
            conn.commit()
    except sqlite3.IntegrityError as e:
        raise ValueError(f"服务器『{name}』已存在") from e


def update_instance_uuid(name: str, instance_uuid: str) -> bool:
    now = int(time())
    with closing(_connect()) as conn:
        cur = conn.execute(
            "UPDATE servers SET instance_uuid = ?, updated_at = ? WHERE name = ?",
            (instance_uuid, now, name),
        )
        conn.commit()
    return cur.rowcount > 0


def update_auto_close(name: str, idle_minutes: int) -> bool:
    """设置自动关停的空闲分钟数。0 = 关闭自动关停。"""
    now = int(time())
    with closing(_connect()) as conn:
        cur = conn.execute(
            "UPDATE servers SET auto_close_idle_minutes = ?, updated_at = ? "
            "WHERE name = ?",
            (max(0, idle_minutes), now, name),
        )
        conn.commit()
    return cur.rowcount > 0


def mark_server_started(name: str, ts: int | None = None) -> bool:
    """记录服务器最近一次成功启动时间。"""
    now = int(time()) if ts is None else int(ts)
    with closing(_connect()) as conn:
        cur = conn.execute(
            "UPDATE servers SET last_started_at = ?, updated_at = ? "
            "WHERE name = ?",
            (now, now, name),
        )
        conn.commit()
    return cur.rowcount > 0


def bind_server_account(name: str, account_phone: str) -> bool:
    """把服务器绑定到某个账号（'' 解绑）。成功 True，服务器不存在 False。"""
    now = int(time())
    with closing(_connect()) as conn:
        cur = conn.execute(
            "UPDATE servers SET account_phone = ?, updated_at = ? WHERE name = ?",
            (account_phone, now, name),
        )
        conn.commit()
    return cur.rowcount > 0


# ============================================================
# accounts 表的 CRUD
# ============================================================

def list_accounts() -> list[Account]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            f"SELECT {_ACCOUNT_COLS} FROM accounts ORDER BY phone"
        ).fetchall()
    return [_account_from_row(r) for r in rows]


def get_account(phone: str) -> Account | None:
    with closing(_connect()) as conn:
        row = conn.execute(
            f"SELECT {_ACCOUNT_COLS} FROM accounts WHERE phone = ?",
            (phone,),
        ).fetchone()
    return _account_from_row(row) if row else None


def add_account(phone: str, password: str) -> None:
    """已存在抛 ValueError"""
    now = int(time())
    try:
        with closing(_connect()) as conn:
            conn.execute(
                "INSERT INTO accounts (phone, password, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (phone, encrypt_secret(password), now, now),
            )
            conn.commit()
    except sqlite3.IntegrityError as e:
        raise ValueError(f"账号『{phone}』已存在") from e


def update_account_session(
    phone: str, session_cookie: str, xsrf_token: str,
) -> bool:
    """登录后写入最新的 Pterodactyl cookies + XSRF。"""
    now = int(time())
    with closing(_connect()) as conn:
        cur = conn.execute(
            "UPDATE accounts SET session_cookie = ?, xsrf_token = ?, "
            "last_refresh_at = ?, updated_at = ? WHERE phone = ?",
            (encrypt_secret(session_cookie), encrypt_secret(xsrf_token), now, now, phone),
        )
        conn.commit()
    return cur.rowcount > 0


def update_account_password(phone: str, password: str) -> bool:
    now = int(time())
    with closing(_connect()) as conn:
        cur = conn.execute(
            "UPDATE accounts SET password = ?, updated_at = ? WHERE phone = ?",
            (encrypt_secret(password), now, phone),
        )
        conn.commit()
    return cur.rowcount > 0


def remove_account(phone: str) -> bool:
    """删除账号。同时把绑定到该账号的服务器解绑。"""
    with closing(_connect()) as conn:
        conn.execute(
            "UPDATE servers SET account_phone = '' WHERE account_phone = ?",
            (phone,),
        )
        cur = conn.execute("DELETE FROM accounts WHERE phone = ?", (phone,))
        conn.commit()
    return cur.rowcount > 0


def mark_account_refreshed(phone: str) -> None:
    """记录一次成功的 token 刷新"""
    now = int(time())
    with closing(_connect()) as conn:
        conn.execute(
            "UPDATE accounts SET last_refresh_at = ? WHERE phone = ?",
            (now, phone),
        )
        conn.commit()


def remove_server(name: str) -> bool:
    """删除成功返回 True，名字不存在返回 False"""
    with closing(_connect()) as conn:
        cur = conn.execute("DELETE FROM servers WHERE name = ?", (name,))
        conn.commit()
    return cur.rowcount > 0


def update_token(name: str, token: str) -> bool:
    now = int(time())
    with closing(_connect()) as conn:
        cur = conn.execute(
            "UPDATE servers SET token = ?, updated_at = ? WHERE name = ?",
            (encrypt_secret(token), now, name),
        )
        conn.commit()
    return cur.rowcount > 0


def update_credentials(name: str, token: str, client_id: str = "") -> bool:
    """同时更新 token 和（可选的）clientid。client_id 为空字符串则不动那个字段。

    用于自动登录刷新后写回 DB——Chromium 登录拿到的 clientid 可能跟原来不同。
    """
    now = int(time())
    if client_id:
        sql = (
            "UPDATE servers SET token = ?, client_id = ?, updated_at = ? "
            "WHERE name = ?"
        )
        params = (encrypt_secret(token), client_id, now, name)
    else:
        sql = "UPDATE servers SET token = ?, updated_at = ? WHERE name = ?"
        params = (encrypt_secret(token), now, name)
    with closing(_connect()) as conn:
        cur = conn.execute(sql, params)
        conn.commit()
    return cur.rowcount > 0


def update_address(name: str, address: str) -> bool:
    now = int(time())
    with closing(_connect()) as conn:
        cur = conn.execute(
            "UPDATE servers SET address = ?, updated_at = ? WHERE name = ?",
            (address, now, name),
        )
        conn.commit()
    return cur.rowcount > 0


def rename_server(old_name: str, new_name: str) -> bool:
    """改名。成功返回 True；旧名不存在或新名已被占用返回 False。"""
    now = int(time())
    try:
        with closing(_connect()) as conn:
            cur = conn.execute(
                "UPDATE servers SET name = ?, updated_at = ? WHERE name = ?",
                (new_name, now, old_name),
            )
            conn.commit()
            return cur.rowcount > 0
    except sqlite3.IntegrityError:
        # 新名重复（虽然调用方应该先检查，这里再防一次）
        return False


def maybe_migrate_from_env(token: str, client_id: str, card_id: str) -> None:
    """启动时自动从 .env 旧配置迁移：
    servers 表为空 + .env 三个字段都填了 → 建一台名为 default 的服务器。
    幂等（已迁移过就跳过）。
    """
    if list_servers():
        return
    if not (token and client_id and card_id):
        return
    add_server("default", card_id, token, client_id)
    logger.info("已从 .env 自动导入旧单台配置 → 服务器『default』")


# ============================================================
# QQ ↔ MC 玩家绑定
# ============================================================

def bind_player(qq_id: int, mc_name: str) -> None:
    """把 QQ 号绑定到 MC 名。覆盖该 QQ 的旧绑定；
    若 mc_name 已被别的 QQ 绑走 → 抛 ValueError。
    """
    mc_name = mc_name.strip()
    if not mc_name:
        raise ValueError("MC 名不能为空")
    now = int(time())
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT qq_id FROM bindings WHERE mc_name = ? COLLATE NOCASE",
            (mc_name,),
        ).fetchone()
        if row and row[0] != qq_id:
            raise ValueError(f"MC 名『{mc_name}』已被 QQ {row[0]} 绑定")
        conn.execute(
            "INSERT INTO bindings (qq_id, mc_name, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(qq_id) DO UPDATE SET mc_name = excluded.mc_name",
            (qq_id, mc_name, now),
        )
        conn.commit()


def unbind_player(qq_id: int) -> bool:
    with closing(_connect()) as conn:
        cur = conn.execute("DELETE FROM bindings WHERE qq_id = ?", (qq_id,))
        conn.commit()
    return cur.rowcount > 0


def get_mc_by_qq(qq_id: int) -> str | None:
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT mc_name FROM bindings WHERE qq_id = ?", (qq_id,),
        ).fetchone()
    return row[0] if row else None


def get_qq_by_mc(mc_name: str) -> int | None:
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT qq_id FROM bindings WHERE mc_name = ? COLLATE NOCASE",
            (mc_name.strip(),),
        ).fetchone()
    return row[0] if row else None


def list_bindings() -> list[tuple[int, str]]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT qq_id, mc_name FROM bindings ORDER BY mc_name COLLATE NOCASE"
        ).fetchall()
    return [(r[0], r[1]) for r in rows]


# ============================================================
# 在线时长统计
# ============================================================

def add_playtime(mc_name: str, day: str, seconds: int) -> None:
    """给某玩家某天累加在线秒数（幂等 upsert）。"""
    if seconds <= 0:
        return
    with closing(_connect()) as conn:
        conn.execute(
            "INSERT INTO playtime (mc_name, day, seconds) VALUES (?, ?, ?) "
            "ON CONFLICT(mc_name, day) DO UPDATE SET seconds = seconds + ?",
            (mc_name.strip(), day, int(seconds), int(seconds)),
        )
        conn.commit()


def playtime_leaderboard(
    since_day: str, until_day: str, limit: int = 15,
) -> list[tuple[str, int]]:
    """[since_day, until_day] 闭区间内按总时长降序。返回 (mc_name, 总秒数)。"""
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT mc_name, SUM(seconds) AS total FROM playtime "
            "WHERE day >= ? AND day <= ? "
            "GROUP BY mc_name COLLATE NOCASE ORDER BY total DESC LIMIT ?",
            (since_day, until_day, limit),
        ).fetchall()
    return [(r[0], r[1] or 0) for r in rows]


def get_playtime_total(mc_name: str, since_day: str, until_day: str) -> int:
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT SUM(seconds) FROM playtime "
            "WHERE mc_name = ? COLLATE NOCASE AND day >= ? AND day <= ?",
            (mc_name.strip(), since_day, until_day),
        ).fetchone()
    return (row[0] or 0) if row else 0


# ============================================================
# 死亡次数统计
# ============================================================

def add_death(mc_name: str, day: str, n: int = 1) -> None:
    with closing(_connect()) as conn:
        conn.execute(
            "INSERT INTO deaths (mc_name, day, count) VALUES (?, ?, ?) "
            "ON CONFLICT(mc_name, day) DO UPDATE SET count = count + ?",
            (mc_name.strip(), day, n, n),
        )
        conn.commit()


def death_leaderboard(
    since_day: str, until_day: str, limit: int = 15,
) -> list[tuple[str, int]]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT mc_name, SUM(count) AS total FROM deaths "
            "WHERE day >= ? AND day <= ? "
            "GROUP BY mc_name COLLATE NOCASE ORDER BY total DESC LIMIT ?",
            (since_day, until_day, limit),
        ).fetchall()
    return [(r[0], r[1] or 0) for r in rows]


def get_death_total(mc_name: str, since_day: str, until_day: str) -> int:
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT SUM(count) FROM deaths "
            "WHERE mc_name = ? COLLATE NOCASE AND day >= ? AND day <= ?",
            (mc_name.strip(), since_day, until_day),
        ).fetchone()
    return (row[0] or 0) if row else 0
