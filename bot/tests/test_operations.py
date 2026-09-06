import asyncio
import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "plugins" / "minekuai"))
operations = importlib.import_module("operations")


@pytest.fixture(autouse=True)
def reset_maintenance_guard():
    previous = operations._maintenance_guard
    previous_keys = operations._related_lock_keys
    operations.set_maintenance_guard(None)
    operations.set_related_lock_keys(None)
    yield
    operations.set_maintenance_guard(previous)
    operations.set_related_lock_keys(previous_keys)


@pytest.mark.asyncio
async def test_same_card_is_rejected_but_other_card_can_run():
    async with operations.card_operation("shared-card"):
        with pytest.raises(operations.OperationBusyError):
            async with operations.card_operation("shared-card"):
                pytest.fail("concurrent card operation admitted")
        async with operations.card_operation("other-card"):
            pass
    async with operations.card_operation("shared-card"):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
async def test_lock_released_after_failure_or_cancellation(error):
    with pytest.raises(error):
        async with operations.card_operation("failed-card"):
            raise error()
    async with operations.card_operation("failed-card"):
        pass


@pytest.mark.asyncio
async def test_maintenance_rejects_before_lock_acquisition():
    def guard(card_id):
        if card_id == "maintenance-card":
            raise RuntimeError("整合包维护保护中")

    operations.set_maintenance_guard(guard)
    with pytest.raises(operations.OperationBusyError, match="维护保护"):
        async with operations.card_operation("maintenance-card"):
            pytest.fail("maintenance operation admitted")
    async with operations.card_operation("available-card"):
        pass


@pytest.mark.asyncio
async def test_guard_is_rechecked_after_lock_and_failure_releases_lock():
    checks = []

    def guard(card_id):
        checks.append(card_id)
        if len(checks) == 2:
            raise RuntimeError("维护状态刚刚改变")

    operations.set_maintenance_guard(guard)
    with pytest.raises(operations.OperationBusyError, match="刚刚改变"):
        async with operations.card_operation("changing-maintenance-card"):
            pytest.fail("guard changed after initial check")
    assert checks == ["changing-maintenance-card"] * 2
    async with operations.card_operation("changing-maintenance-card"):
        pass


@pytest.mark.asyncio
async def test_explicit_maintenance_override_still_preserves_card_lock():
    def guard(card_id):
        pytest.fail("explicit override should not run maintenance guard")

    operations.set_maintenance_guard(guard)
    async with operations.card_operation("inspection-card", allow_maintenance=True):
        with pytest.raises(operations.OperationBusyError, match="正在处理"):
            async with operations.card_operation("inspection-card", allow_maintenance=True):
                pytest.fail("maintenance override bypassed concurrency lock")


def test_synchronous_guard_converts_maintenance_error_and_can_be_unregistered():
    class MaintenanceError(Exception):
        pass

    def guard(card_id):
        raise MaintenanceError("等待管理员解除维护")

    operations.set_maintenance_guard(guard)
    with pytest.raises(operations.OperationBusyError, match="管理员解除"):
        operations.ensure_card_available("card")
    operations.set_maintenance_guard(None)
    operations.ensure_card_available("card")


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_maintenance", [False, True])
async def test_related_resource_lock_rejects_other_cards_even_for_explicit_finish(allow_maintenance):
    operations.set_related_lock_keys(lambda card: ["instance:shared", "instance:shared"])
    async with operations.card_operation("alias-card-a"):
        with pytest.raises(operations.OperationBusyError, match="关联实例"):
            async with operations.card_operation("alias-card-b", allow_maintenance=allow_maintenance):
                pytest.fail("different card bypassed shared instance lock")
        assert not operations._card_locks["alias-card-b"].locked()
    async with operations.card_operation("alias-card-b"):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
async def test_all_related_locks_release_after_failure_or_cancellation(error):
    operations.set_related_lock_keys(lambda card: ["instance:release-one", "instance:release-two"])
    with pytest.raises(error):
        async with operations.card_operation("multi-card"):
            raise error()
    assert not operations._card_locks["multi-card"].locked()
    assert not operations._related_locks["instance:release-one"].locked()
    assert not operations._related_locks["instance:release-two"].locked()
    async with operations.card_operation("other-multi-card"):
        pass


@pytest.mark.asyncio
async def test_second_guard_failure_releases_all_related_locks():
    calls = 0

    def guard(card):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("维护刚开始")

    operations.set_maintenance_guard(guard)
    operations.set_related_lock_keys(lambda card: ["instance:guard-release"])
    with pytest.raises(operations.OperationBusyError, match="维护"):
        async with operations.card_operation("guard-multi-card"):
            pytest.fail("second guard admitted operation")
    assert not operations._card_locks["guard-multi-card"].locked()
    assert not operations._related_locks["instance:guard-release"].locked()


@pytest.mark.asyncio
async def test_held_resource_lock_survives_config_alias_deletion():
    keys = {"deleted-alias-card": ["instance:stable"], "remaining-alias-card": ["instance:stable"]}
    operations.set_related_lock_keys(lambda card: keys.get(card, []))
    async with operations.card_operation("deleted-alias-card"):
        keys.pop("deleted-alias-card")
        with pytest.raises(operations.OperationBusyError):
            async with operations.card_operation("remaining-alias-card"):
                pytest.fail("config deletion dropped a held resource lock")
    async with operations.card_operation("remaining-alias-card"):
        pass


@pytest.mark.asyncio
async def test_related_resource_lookup_errors_fail_closed_even_for_explicit_finish():
    def resolver(card):
        raise RuntimeError("private database detail")

    operations.set_related_lock_keys(resolver)
    with pytest.raises(operations.OperationBusyError, match="无法确认关联实例") as caught:
        async with operations.card_operation("lookup-error-card", allow_maintenance=True):
            pytest.fail("failed lookup admitted operation")
    assert "private" not in str(caught.value)
