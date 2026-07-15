"""permission.py 的单元测试"""
import importlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "plugins" / "minekuai"))
permission = importlib.import_module("permission")


def setup_function():
    """每个测试前清空内存状态"""
    permission._cooldown_record.clear()
    permission._pending_confirms.clear()


# ============================================================
# 白名单
# ============================================================

def test_no_restrictions_allows_anyone():
    """没配白名单时任何人都允许"""
    ok, _reason = permission.is_user_allowed(
        user_id=111, group_id=222,
        allowed_groups=[], allowed_users=[],
    )
    assert ok is True


def test_group_restriction():
    """配置了允许群，则不在群里的私聊用户被拒绝"""
    # 私聊（group_id=None）
    ok, reason = permission.is_user_allowed(
        user_id=111, group_id=None,
        allowed_groups=[12345], allowed_users=[],
    )
    assert ok is False
    assert "QQ 群" in reason

    # 不在白名单的群
    ok, _ = permission.is_user_allowed(
        user_id=111, group_id=99999,
        allowed_groups=[12345], allowed_users=[],
    )
    assert ok is False

    # 在白名单的群
    ok, _ = permission.is_user_allowed(
        user_id=111, group_id=12345,
        allowed_groups=[12345], allowed_users=[],
    )
    assert ok is True


def test_user_whitelist():
    """配置了用户白名单，非白名单用户被拒绝"""
    ok, reason = permission.is_user_allowed(
        user_id=111, group_id=12345,
        allowed_groups=[12345], allowed_users=[222, 333],
    )
    assert ok is False
    assert "权限" in reason

    ok, _ = permission.is_user_allowed(
        user_id=222, group_id=12345,
        allowed_groups=[12345], allowed_users=[222, 333],
    )
    assert ok is True


# ============================================================
# 冷却
# ============================================================

def test_cooldown_first_call_passes():
    """第一次调用不在冷却中"""
    cooling, remaining = permission.check_cooldown(111, "start", 30)
    assert cooling is False
    assert remaining == 0


def test_cooldown_blocks_within_window():
    """触发后立即再调用应该被冷却"""
    permission.update_cooldown(111, "start")
    cooling, remaining = permission.check_cooldown(111, "start", 30)
    assert cooling is True
    assert 0 < remaining <= 30


def test_cooldown_per_user_per_command():
    """冷却是按 (用户,指令) 组合的，不互相影响"""
    permission.update_cooldown(111, "start")
    cooling, _ = permission.check_cooldown(222, "start", 30)
    assert cooling is False
    cooling, _ = permission.check_cooldown(111, "stop", 30)
    assert cooling is False


def test_cooldown_disabled_when_zero():
    """cooldown=0 表示禁用冷却"""
    permission.update_cooldown(111, "start")
    cooling, _ = permission.check_cooldown(111, "start", 0)
    assert cooling is False


# ============================================================
# 关服待确认（带服务器名）
# ============================================================

def test_pending_confirm_normal_flow():
    """正常流程：标记某服务器 → 确认成功返回该服务器名"""
    permission.mark_pending_confirm(111, "GTNH")
    assert permission.has_pending_confirm(111) is True

    name = permission.consume_pending_confirm(111)
    assert name == "GTNH"

    # 消费后就没了
    assert permission.has_pending_confirm(111) is False
    assert permission.consume_pending_confirm(111) is None


def test_pending_confirm_no_record():
    """没标记过的用户确认返回 None"""
    assert permission.consume_pending_confirm(999) is None


def test_pending_confirm_expires():
    """超时的确认应该被拒绝"""
    permission.mark_pending_confirm(111, "GTNH")
    # 手动改时间戳，模拟过期（保留服务器名，只把时间往前推）
    _, name = permission._pending_confirms[111]
    permission._pending_confirms[111] = (
        time.time() - permission.CONFIRM_TIMEOUT - 10,
        name,
    )
    assert permission.has_pending_confirm(111) is False
    assert permission.consume_pending_confirm(111) is None


def test_pending_confirm_overwrites():
    """同一个用户先后标记两台服务器，confirm 拿到的是最新那台"""
    permission.mark_pending_confirm(111, "default")
    permission.mark_pending_confirm(111, "GTNH")
    assert permission.consume_pending_confirm(111) == "GTNH"


def test_admin_permission_requires_explicit_membership():
    ok, reason = permission.is_admin_allowed(
        user_id=111, group_id=12345,
        allowed_groups=[12345], allowed_users=[], admin_users=[222],
    )
    assert ok is False
    assert "管理员" in reason

    ok, reason = permission.is_admin_allowed(
        user_id=222, group_id=12345,
        allowed_groups=[12345], allowed_users=[], admin_users=[222],
    )
    assert ok is True
    assert reason == ""


def test_admin_permission_fails_closed_when_unconfigured():
    ok, reason = permission.is_admin_allowed(
        user_id=111, group_id=12345,
        allowed_groups=[12345], allowed_users=[], admin_users=[],
    )
    assert ok is False
    assert "ADMIN_USERS" in reason
