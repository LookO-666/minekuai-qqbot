"""登录验证码的短期内存中转。

验证码只存在于等待中的 ``Future`` 里：不写数据库，不进入操作审计，
提交成功、取消或超时后立即清理。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal


ChallengeKind = Literal["image", "sms"]


class VerificationBusyError(RuntimeError):
    """同一用户和会话已经有同类验证码在等待。"""


class VerificationCancelledError(RuntimeError):
    """用户取消了本次验证码输入。"""


@dataclass(frozen=True)
class ChallengeKey:
    user_id: int
    group_id: int | None
    kind: ChallengeKind


@dataclass(eq=False)
class VerificationRequest:
    key: ChallengeKey
    future: asyncio.Future[str]


class VerificationBroker:
    """在登录协程和后续 QQ 消息之间传递一次性验证码。"""

    def __init__(self) -> None:
        self._pending: dict[ChallengeKey, VerificationRequest] = {}

    def open(
        self,
        user_id: int,
        group_id: int | None,
        kind: ChallengeKind,
    ) -> VerificationRequest:
        key = ChallengeKey(user_id=user_id, group_id=group_id, kind=kind)
        current = self._pending.get(key)
        if current and not current.future.done():
            raise VerificationBusyError("已有验证码正在等待输入")

        request = VerificationRequest(
            key=key,
            future=asyncio.get_running_loop().create_future(),
        )
        self._pending[key] = request
        return request

    async def wait(
        self,
        request: VerificationRequest,
        timeout_seconds: float,
    ) -> str:
        try:
            return await asyncio.wait_for(
                asyncio.shield(request.future), timeout=timeout_seconds
            )
        finally:
            self.close(request)

    def submit(
        self,
        user_id: int,
        group_id: int | None,
        kind: ChallengeKind,
        code: str,
    ) -> bool:
        request = self._resolve(user_id, group_id, kind)
        if request is None or request.future.done():
            return False
        request.future.set_result(code)
        return True

    def cancel(
        self,
        user_id: int,
        group_id: int | None,
        kind: ChallengeKind,
    ) -> bool:
        request = self._resolve(user_id, group_id, kind)
        if request is None or request.future.done():
            return False
        request.future.set_exception(VerificationCancelledError("用户已取消"))
        return True

    def close(self, request: VerificationRequest) -> None:
        if self._pending.get(request.key) is request:
            self._pending.pop(request.key, None)
        if not request.future.done():
            request.future.cancel()

    def _resolve(
        self,
        user_id: int,
        group_id: int | None,
        kind: ChallengeKind,
    ) -> VerificationRequest | None:
        """优先匹配原群；私聊时允许命中该用户唯一的等待项。"""
        exact = self._pending.get(
            ChallengeKey(user_id=user_id, group_id=group_id, kind=kind)
        )
        if exact and not exact.future.done():
            return exact

        matches = [
            request
            for key, request in self._pending.items()
            if key.user_id == user_id
            and key.kind == kind
            and not request.future.done()
        ]
        return matches[0] if len(matches) == 1 else None

    def clear(self) -> None:
        """测试和关闭流程使用；取消全部尚未完成的等待项。"""
        for request in list(self._pending.values()):
            self.close(request)

