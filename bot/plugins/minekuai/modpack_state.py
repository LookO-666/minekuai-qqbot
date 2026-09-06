"""Scoped install confirmations and durable, explicitly released protection."""
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
import math
import json
import re
import secrets
import sqlite3
import threading
import time
from typing import Callable


Scope = tuple[int, int, int | None]
_SHORT_INSTANCE = re.compile(r"[0-9a-f]{8}\Z")
_FULL_INSTANCE = re.compile(r"(?:[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\Z")


def _same_instance(left: str, right: str) -> bool:
    left, right = left.casefold(), right.casefold()
    if left == right:
        return True
    left_full, right_full = bool(_FULL_INSTANCE.fullmatch(left)), bool(_FULL_INSTANCE.fullmatch(right))
    if left_full and right_full:
        return left.replace("-", "") == right.replace("-", "")
    if left_full and _SHORT_INSTANCE.fullmatch(right):
        return left[:8] == right
    if right_full and _SHORT_INSTANCE.fullmatch(left):
        return right[:8] == left
    return False


class ConfirmError(Exception):
    """An install confirmation is absent, invalid, expired, or already used."""


class MaintenanceError(Exception):
    """Maintenance protection prevents an operation from proceeding safely."""


@dataclass(frozen=True)
class ServerIdentity:
    name: str
    card_id: str
    instance_uuid: str
    account_phone: str
    created_at: int

    @classmethod
    def from_server(cls, server) -> "ServerIdentity":
        return cls(
            name=server.name,
            card_id=server.card_id,
            instance_uuid=server.instance_uuid,
            account_phone=server.account_phone,
            created_at=server.created_at,
        )

    def matches(self, server) -> bool:
        if server is None:
            return False
        try:
            return self == self.from_server(server)
        except (AttributeError, TypeError, ValueError):
            return False


@dataclass(frozen=True)
class InstallChoice:
    project_id: str
    item_id: str
    name: str
    version: str
    game_version: str
    java_version: str
    file_name: str
    search_query: str = ""
    search_page: int = 1
    version_page: int = 0


@dataclass(frozen=True)
class PendingInstall:
    scope: Scope
    server: ServerIdentity
    choice: InstallChoice
    code: str
    expires_at: float


class InstallConfirmStore:
    """Short-lived in-memory confirmations; never shared across chat scopes."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, ttl: float = 300):
        if not math.isfinite(ttl) or ttl <= 0:
            raise ValueError("confirmation ttl must be positive")
        self._clock = clock
        self._ttl = ttl
        self._pending: dict[Scope, PendingInstall] = {}
        self._lock = threading.Lock()

    def issue(
        self, scope: Scope, server: ServerIdentity, choice: InstallChoice,
    ) -> PendingInstall:
        with self._lock:
            now = self._clock()
            previous = self._pending.get(scope)
            for key, item in list(self._pending.items()):
                if item.expires_at <= now:
                    del self._pending[key]
            code = str(secrets.randbelow(900000) + 100000)
            while previous is not None and code == previous.code:
                code = str(secrets.randbelow(900000) + 100000)
            pending = PendingInstall(scope, server, choice, code, now + self._ttl)
            self._pending[scope] = pending
            return pending

    def consume(self, scope: Scope, code: str) -> PendingInstall:
        with self._lock:
            pending = self._pending.get(scope)
            if pending is None:
                raise ConfirmError("没有待确认的安装操作，或已确认/取消，请重新选择整合包")
            if pending.expires_at <= self._clock():
                del self._pending[scope]
                raise ConfirmError("安装确认已过期，请重新选择整合包")
            if (
                not isinstance(code, str) or not code.strip().isascii()
                or not secrets.compare_digest(pending.code, code.strip())
            ):
                raise ConfirmError("安装确认码不匹配，请检查本次操作的确认码")
            # No await or external operation may precede this removal after matching.
            del self._pending[scope]
            return pending

    def cancel(self, scope: Scope) -> bool:
        with self._lock:
            pending = self._pending.pop(scope, None)
            return pending is not None and pending.expires_at > self._clock()


class MaintenanceStore:
    """File-backed protection that survives config deletion and process restart.

    Active rows survive restarts regardless of age or phase. A verified terminal
    reconciliation archives them separately; submission alone is never completion.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.db_path), timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _has_instance_guard(connection: sqlite3.Connection, instance_id: str) -> bool:
        rows = connection.execute(
            """SELECT instance_uuid FROM modpack_maintenance
            WHERE instance_uuid = ? COLLATE NOCASE
               OR lower(substr(instance_uuid, 1, 8)) = ?""",
            (instance_id, instance_id[:8].casefold()),
        )
        return any(_same_instance(row["instance_uuid"], instance_id) for row in rows)

    def init_db(self) -> None:
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS modpack_maintenance (
                        instance_uuid TEXT PRIMARY KEY NOT NULL COLLATE NOCASE,
                        card_id TEXT UNIQUE NOT NULL,
                        server_name TEXT NOT NULL,
                        server_created_at INTEGER NOT NULL,
                        phase TEXT NOT NULL CHECK (phase IN ('preparing', 'submitted', 'unknown')),
                        pack_name TEXT NOT NULL,
                        pack_version TEXT NOT NULL,
                        created_at INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL
                    )
                """)
                # Additive migration keeps existing unknown attempts protected.
                columns = {row[1] for row in connection.execute("PRAGMA table_info(modpack_maintenance)")}
                for name, definition in (
                    ("write_started_at", "INTEGER"),
                    ("baseline_log_stamp", "TEXT"),
                    ("install_outcome", "TEXT NOT NULL DEFAULT 'unknown'"),
                    ("attempt_id", "TEXT NOT NULL DEFAULT ''"),
                    ("pack_project_id", "TEXT NOT NULL DEFAULT ''"),
                    ("pack_item_id", "TEXT NOT NULL DEFAULT ''"),
                    ("pack_game_version", "TEXT NOT NULL DEFAULT ''"),
                    ("pack_java_version", "TEXT NOT NULL DEFAULT ''"),
                ):
                    if name not in columns:
                        connection.execute(f"ALTER TABLE modpack_maintenance ADD COLUMN {name} {definition}")
                connection.execute("""CREATE TABLE IF NOT EXISTS modpack_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    instance_uuid TEXT NOT NULL COLLATE NOCASE,
                    record TEXT NOT NULL
                )""")
        except (OSError, sqlite3.Error):
            raise MaintenanceError("无法初始化整合包维护保护，操作已停止") from None

    def begin(self, server: ServerIdentity, choice: InstallChoice) -> None:
        if not server.instance_uuid or not server.card_id:
            raise MaintenanceError("缺少实例或计时卡信息，无法建立整合包维护保护")
        now = int(time.time())
        try:
            with closing(self._connect()) as connection, connection:
                # Alias checks and insertion share a write transaction. The
                # normal UNIQUE constraint alone cannot equate short/full UUIDs.
                connection.execute("BEGIN IMMEDIATE")
                if self._has_instance_guard(connection, server.instance_uuid):
                    raise MaintenanceError(
                        "该实例已有整合包维护保护，请发『整合包状态』，确认结束后会自动解除",
                    )
                connection.execute(
                    """INSERT INTO modpack_maintenance
                    (instance_uuid, card_id, server_name, server_created_at, phase,
                     pack_name, pack_version, created_at, updated_at, write_started_at,
                     baseline_log_stamp, install_outcome, attempt_id,
                     pack_project_id, pack_item_id, pack_game_version, pack_java_version)
                    VALUES (?, ?, ?, ?, 'preparing', ?, ?, ?, ?, 0, '', 'unknown', ?, ?, ?, ?, ?)""",
                    (
                        server.instance_uuid, server.card_id, server.name, server.created_at,
                        choice.name, choice.version, now, now, secrets.token_hex(16),
                        choice.project_id, choice.item_id, choice.game_version, choice.java_version,
                    ),
                )
        except sqlite3.IntegrityError:
            raise MaintenanceError(
                "该实例或计时卡已有整合包维护保护，请发『整合包状态』自动核对",
            ) from None
        except (OSError, sqlite3.Error):
            raise MaintenanceError("无法保存整合包维护保护，操作已停止") from None

    def mark(self, instance_id: str, phase: str) -> None:
        if not isinstance(phase, str) or phase not in {"submitted", "unknown"}:
            raise MaintenanceError("无效的整合包维护状态")
        try:
            with closing(self._connect()) as connection, connection:
                result = connection.execute(
                    "UPDATE modpack_maintenance SET phase = ?, updated_at = ? WHERE instance_uuid = ?",
                    (phase, int(time.time()), instance_id),
                )
                if result.rowcount != 1:
                    raise MaintenanceError("找不到该实例的维护保护，操作已停止，请检查官网状态")
        except (OSError, sqlite3.Error):
            raise MaintenanceError("无法更新整合包维护保护，请检查官网状态") from None

    def start_write(self, instance_id: str, baseline_log_stamp: str) -> None:
        """Persist the one-shot write boundary before sending the installer POST."""
        if not isinstance(baseline_log_stamp, str) or len(baseline_log_stamp) > 2048:
            raise MaintenanceError("安装日志基线无效，未提交安装")
        try:
            with closing(self._connect()) as connection, connection:
                changed = connection.execute(
                    "UPDATE modpack_maintenance SET write_started_at=?, baseline_log_stamp=? "
                    "WHERE instance_uuid=? AND phase='preparing' AND write_started_at=0",
                    (int(time.time()), baseline_log_stamp, instance_id),
                )
                if changed.rowcount != 1:
                    raise MaintenanceError("安装提交记录已变化，未重复提交安装")
        except (OSError, sqlite3.Error):
            raise MaintenanceError("无法记录安装提交边界，未提交安装") from None

    def observe(self, entry: dict, outcome: str) -> None:
        """Record read-only evidence without releasing protection or changing receipt state."""
        if outcome not in {"unknown", "installing", "completed", "failed", "not_submitted"}:
            raise MaintenanceError("无效的安装观察状态")
        try:
            with closing(self._connect()) as connection, connection:
                changed = connection.execute(
                    "UPDATE modpack_maintenance SET install_outcome=CASE WHEN ?='unknown' "
                    "THEN install_outcome ELSE ? END WHERE instance_uuid=? "
                    "AND created_at=? AND card_id=? AND write_started_at IS ? AND attempt_id=?",
                    (outcome, outcome, entry["instance_uuid"], entry["created_at"], entry["card_id"],
                     entry.get("write_started_at"), entry.get("attempt_id", "")),
                )
                if changed.rowcount != 1:
                    raise MaintenanceError("维护记录已变化，本次观察结果已丢弃，请重新查询")
        except (OSError, sqlite3.Error):
            raise MaintenanceError("无法保存安装观察结果，维护保护保留") from None

    def get(self, instance_id: str) -> dict | None:
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT * FROM modpack_maintenance WHERE instance_uuid = ?", (instance_id,),
                ).fetchone()
                return dict(row) if row is not None else None
        except (OSError, sqlite3.Error):
            raise MaintenanceError("无法读取整合包维护保护，操作已停止") from None

    def ensure_card_available(self, card_id: str) -> None:
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT 1 FROM modpack_maintenance WHERE card_id = ? LIMIT 1", (card_id,),
                ).fetchone()
                if row is not None:
                    raise MaintenanceError(
                        "计时卡处于整合包维护保护中，请发『整合包状态 <服务器>』自动核对；也可用『整合包日志 <服务器>』查看进度",
                    )
        except (OSError, sqlite3.Error):
            raise MaintenanceError("无法检查整合包维护保护，操作已停止") from None

    def ensure_instance_available(self, instance_id: str) -> None:
        """Block exact identities and validated short/full UUID aliases.

        Empty IDs represent card-only configurations. They aren't interpreted
        as a prefix; callers must still check their card protection separately.
        """
        if not instance_id:
            return
        if not isinstance(instance_id, str):
            raise MaintenanceError("实例信息格式异常，操作已停止")
        try:
            with closing(self._connect()) as connection:
                if self._has_instance_guard(connection, instance_id):
                    raise MaintenanceError(
                        "实例处于整合包维护保护中，请发『整合包状态 <服务器>』自动核对安装结果",
                    )
        except (OSError, sqlite3.Error):
            raise MaintenanceError("无法检查实例维护保护，操作已停止") from None

    def latest(self, instance_id: str) -> dict | None:
        active = self.get(instance_id)
        if active:
            return active
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT record FROM modpack_history WHERE instance_uuid=? ORDER BY id DESC LIMIT 1",
                    (instance_id,),
                ).fetchone()
                return json.loads(row[0]) if row else None
        except (OSError, sqlite3.Error, ValueError):
            raise MaintenanceError("无法读取整合包安装历史，请稍后重试") from None

    def find_blocking(self, card_id: str, instance_id: str) -> dict | None:
        try:
            with closing(self._connect()) as connection:
                for row in connection.execute("SELECT * FROM modpack_maintenance"):
                    if row["card_id"] == card_id or (instance_id and _same_instance(row["instance_uuid"], instance_id)):
                        return dict(row)
                return None
        except (OSError, sqlite3.Error):
            raise MaintenanceError("无法核对关联整合包维护记录") from None

    def finish(self, instance_id: str, *, expected: dict | None = None, reason="manual") -> bool:
        if reason not in {"manual", "completed", "failed", "not_submitted"}:
            raise MaintenanceError("无效的维护结束原因")
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM modpack_maintenance WHERE instance_uuid=?", (instance_id,),
                ).fetchone()
                if row is None:
                    if expected is not None:
                        raise MaintenanceError("维护记录已变化，未解除其他任务的保护")
                    return False
                entry = dict(row)
                if expected is not None and any(entry.get(key) != expected.get(key) for key in (
                    "attempt_id", "created_at", "card_id", "server_created_at", "write_started_at",
                )):
                    raise MaintenanceError("维护记录已变化，未解除其他任务的保护")
                archived = {**entry, "released_at": int(time.time()), "release_reason": reason}
                connection.execute("INSERT INTO modpack_history(instance_uuid,record) VALUES (?,?)",
                                   (instance_id, json.dumps(archived, ensure_ascii=False)))
                connection.execute(
                    "DELETE FROM modpack_maintenance WHERE instance_uuid = ?", (instance_id,),
                )
                return True
        except (OSError, sqlite3.Error):
            raise MaintenanceError("无法解除整合包维护保护，请检查后重试") from None
