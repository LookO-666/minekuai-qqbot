"""
操作日志 - 记录谁在什么时候做了什么。
用 SQLite 持久化，重启不丢。

数据库路径可通过环境变量 OPERATION_LOG_DB 自定义，
默认是当前目录的 operation_log.db。
Docker 部署时建议设为 /app/data/operation_log.db。
"""
import os
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from loguru import logger

DB_PATH = Path(os.getenv("OPERATION_LOG_DB", "operation_log.db"))


def init_db() -> None:
    """初始化数据表（幂等）"""
    # 如果数据库目录不存在就创建，方便 Docker 挂载
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS operation_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT    NOT NULL,
                user_id     INTEGER NOT NULL,
                user_name   TEXT,
                group_id    INTEGER,
                command     TEXT    NOT NULL,
                success     INTEGER NOT NULL,
                detail      TEXT
            )
            """
        )
        conn.commit()
    logger.info(f"操作日志数据库就绪: {DB_PATH.resolve()}")


def log_operation(
    user_id: int,
    user_name: str,
    group_id: int | None,
    command: str,
    success: bool,
    detail: str = "",
) -> None:
    """记录一次操作"""
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            conn.execute(
                """
                INSERT INTO operation_log
                    (ts, user_id, user_name, group_id, command, success, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now().isoformat(timespec="seconds"),
                    user_id,
                    user_name,
                    group_id,
                    command,
                    1 if success else 0,
                    detail[:500],  # 防止 detail 过长撑爆
                ),
            )
            conn.commit()
    except Exception as e:
        # 日志失败不能影响主流程
        logger.error(f"写入操作日志失败: {e}")
