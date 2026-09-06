"""Serialize power workflows by billing card, including shared-card servers."""

import asyncio
from contextlib import asynccontextmanager


class OperationBusyError(Exception):
    pass


_card_locks: dict[str, asyncio.Lock] = {}


@asynccontextmanager
async def card_operation(card_id: str):
    lock = _card_locks.setdefault(card_id, asyncio.Lock())
    if lock.locked():
        raise OperationBusyError("这张计时卡正在处理其他开关操作，请等待完成后再试")
    async with lock:
        yield
