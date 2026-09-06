"""Serialize control workflows by billing card and enforce maintenance guards."""

import asyncio
from collections.abc import Callable, Iterable
from contextlib import AsyncExitStack, asynccontextmanager


class OperationBusyError(Exception):
    pass


_card_locks: dict[str, asyncio.Lock] = {}
_related_locks: dict[str, asyncio.Lock] = {}
_maintenance_guard: Callable[[str], None] | None = None
_related_lock_keys: Callable[[str], Iterable[str]] | None = None


def set_maintenance_guard(guard: Callable[[str], None] | None) -> None:
    """Register the persistent maintenance check without coupling its storage here."""
    global _maintenance_guard
    _maintenance_guard = guard


def set_related_lock_keys(resolver: Callable[[str], Iterable[str]] | None) -> None:
    """Resolve extra stable resource keys shared by aliases on different cards."""
    global _related_lock_keys
    _related_lock_keys = resolver


def ensure_card_available(card_id: str) -> None:
    """Fail closed if maintenance blocks the card or its check cannot complete."""
    if _maintenance_guard is None:
        return
    try:
        _maintenance_guard(card_id)
    except OperationBusyError:
        raise
    except Exception as exc:
        raise OperationBusyError(str(exc) or "无法确认维护状态，请稍后重试") from None


@asynccontextmanager
async def card_operation(card_id: str, *, allow_maintenance: bool = False):
    if not allow_maintenance:
        ensure_card_available(card_id)
    try:
        keys = set(_related_lock_keys(card_id)) if _related_lock_keys is not None else set()
        if any(not isinstance(key, str) or not key for key in keys):
            raise ValueError("invalid related lock key")
    except Exception:
        raise OperationBusyError("无法确认关联实例的操作锁，请稍后重试") from None
    locks = [_card_locks.setdefault(card_id, asyncio.Lock())]
    locks.extend(_related_locks.setdefault(key, asyncio.Lock()) for key in sorted(keys))
    if any(lock.locked() for lock in locks):
        raise OperationBusyError("这张计时卡或关联实例正在处理其他控制操作，请等待完成后再试")
    async with AsyncExitStack() as stack:
        for lock in locks:
            await stack.enter_async_context(lock)
        # The guard may change between the initial check and lock acquisition.
        if not allow_maintenance:
            ensure_card_available(card_id)
        yield
