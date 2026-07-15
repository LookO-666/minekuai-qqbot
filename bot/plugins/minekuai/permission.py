"""权限校验 + 冷却控制"""
from time import time
from typing import Iterable

# 内存里的冷却记录: {(user_id, command): 上次触发时间戳}
# 重启后会清空，对这种场景够用
_cooldown_record: dict[tuple[int, str], float] = {}

# 关服二次确认的状态: {user_id: (上次发"关服"的时间戳, 服务器名)}
# 5 分钟内没确认就过期
_pending_confirms: dict[int, tuple[float, str]] = {}
CONFIRM_TIMEOUT = 300  # 秒


def is_user_allowed(
    user_id: int,
    group_id: int | None,
    allowed_groups: Iterable[int],
    allowed_users: Iterable[int],
) -> tuple[bool, str]:
    """
    检查用户是否有权使用机器人。
    返回 (是否允许, 拒绝理由)
    """
    allowed_groups = list(allowed_groups)
    allowed_users = list(allowed_users)

    # 群限制：如果配置了允许的群，则必须在群内使用
    if allowed_groups:
        if group_id is None:
            return False, "请在指定的 QQ 群内使用本机器人"
        if group_id not in allowed_groups:
            return False, ""  # 不在允许群里，静默忽略避免打扰

    # 用户限制：如果配置了白名单，则用户必须在白名单里
    if allowed_users and user_id not in allowed_users:
        return False, "你没有使用本机器人的权限"

    return True, ""


def is_admin_allowed(
    user_id: int,
    group_id: int | None,
    allowed_groups: Iterable[int],
    allowed_users: Iterable[int],
    admin_users: Iterable[int],
    admin_all_group_members: bool = False,
) -> tuple[bool, str]:
    """先执行普通权限校验，再检查全员管理员开关或管理员名单。"""
    ok, reason = is_user_allowed(
        user_id, group_id, allowed_groups, allowed_users
    )
    if not ok:
        return ok, reason
    if admin_all_group_members and group_id is not None:
        return True, ""
    admins = set(admin_users)
    if not admins:
        return False, "机器人尚未配置管理员，请在 .env 设置 ADMIN_USERS"
    if user_id not in admins:
        return False, "该指令仅限机器人管理员使用"
    return True, ""


def check_cooldown(
    user_id: int,
    command: str,
    cooldown_seconds: int,
) -> tuple[bool, int]:
    """
    检查冷却。
    返回 (是否在冷却中, 剩余秒数)
    """
    if cooldown_seconds <= 0:
        return False, 0

    key = (user_id, command)
    now = time()
    last = _cooldown_record.get(key, 0)
    elapsed = now - last

    if elapsed < cooldown_seconds:
        return True, int(cooldown_seconds - elapsed)

    return False, 0


def update_cooldown(user_id: int, command: str) -> None:
    """记录一次成功操作的时间，启动冷却计时"""
    _cooldown_record[(user_id, command)] = time()


def mark_pending_confirm(user_id: int, server_name: str) -> None:
    """记录一个待确认的关服请求（针对指定服务器）"""
    _pending_confirms[user_id] = (time(), server_name)


def consume_pending_confirm(user_id: int) -> str | None:
    """
    尝试消费一个待确认请求。
    成功返回服务器名（5 分钟内有确认）；过期或无返回 None。
    """
    entry = _pending_confirms.pop(user_id, None)
    if entry is None:
        return None
    ts, name = entry
    if time() - ts > CONFIRM_TIMEOUT:
        return None
    return name


def has_pending_confirm(user_id: int) -> bool:
    """这个用户是否有未过期的待确认关服请求"""
    entry = _pending_confirms.get(user_id)
    if entry is None:
        return False
    ts, _ = entry
    if time() - ts > CONFIRM_TIMEOUT:
        _pending_confirms.pop(user_id, None)
        return False
    return True
