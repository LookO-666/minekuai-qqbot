"""Pure, bounded parsing of Minekuai modpack catalog rows.

Display fields are safe to embed in QQ plain text. Identity/file fields stay
unescaped so an installation can use precisely the selected backend values.
"""
from dataclasses import dataclass
import re
import unicodedata
from typing import Any


MAX_ROWS = 50
MAX_ID_LENGTH = 128
MAX_FILE_NAME_LENGTH = 2048


class CatalogError(ValueError):
    """Malformed catalog data; messages never include the untrusted payload."""


@dataclass(frozen=True)
class CatalogItem:
    project_id: str
    item_id: str
    name: str
    version: str
    game_version: str
    java_version: str
    file_name: str

    @property
    def installable(self) -> bool:
        return bool(self.file_name.strip())


def sanitize_display(value: Any, max_length: int = 160) -> str:
    """Remove control/format characters and escape QQ CQ delimiters."""
    if type(max_length) is not int or not 1 <= max_length <= 1024:
        raise ValueError("显示长度必须是 1 到 1024 的整数")
    if value is None:
        return ""
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise CatalogError("整合包展示字段格式异常")
    text = str(value)
    text = "".join(char for char in text if not unicodedata.category(char).startswith("C")).strip()
    parts = []
    length = 0
    for char in text:
        escaped = {"&": "&amp;", "[": "&#91;", "]": "&#93;"}.get(char, char)
        if length + len(escaped) > max_length:
            break
        parts.append(escaped)
        length += len(escaped)
    return "".join(parts)


def _identifier(value: Any) -> str:
    if type(value) is int and value > 0:
        value = str(value)
    if (
        not isinstance(value, str) or not 1 <= len(value) <= MAX_ID_LENGTH
        or not re.fullmatch(r"[A-Za-z0-9_-]+", value)
    ):
        raise CatalogError("整合包项目或版本 ID 格式异常")
    return value


def normalize_item(raw: Any) -> CatalogItem:
    """Normalize one row; missing fileName is browseable but not installable."""
    if not isinstance(raw, dict):
        raise CatalogError("整合包条目必须是对象")
    item_id = _identifier(raw.get("id"))
    primary_id = raw.get("primaryId")
    if primary_id is None or primary_id == "" or (type(primary_id) is int and primary_id == 0):
        primary_id = item_id
    project_id = _identifier(primary_id)
    file_name = raw.get("fileName")
    if file_name is None:
        file_name = ""
    if (
        not isinstance(file_name, str) or len(file_name) > MAX_FILE_NAME_LENGTH
        or any(unicodedata.category(char).startswith("C") for char in file_name)
    ):
        raise CatalogError("整合包文件参数格式异常")
    return CatalogItem(
        project_id=project_id, item_id=item_id,
        name=sanitize_display(raw.get("name"), 160),
        version=sanitize_display(raw.get("modpackVersion"), 80),
        game_version=sanitize_display(
            raw.get("gameVersion") or raw.get("minecraftVersion") or raw.get("mcVersion"), 64,
        ),
        java_version=sanitize_display(raw.get("javaVersion"), 32),
        file_name=file_name,
    )


def parse_catalog(payload: Any) -> tuple[tuple[CatalogItem, ...], int]:
    """Parse root-level rows/total, rejecting wrapped or malformed responses."""
    if not isinstance(payload, dict):
        raise CatalogError("整合包列表响应必须是对象")
    rows, total = payload.get("rows"), payload.get("total")
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise CatalogError("整合包列表 rows 格式或长度异常")
    if type(total) is not int or not len(rows) <= total <= 2**63 - 1:
        raise CatalogError("整合包列表 total 格式异常")
    return tuple(normalize_item(row) for row in rows), total
