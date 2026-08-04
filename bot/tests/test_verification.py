"""登录验证码内存中转的单元测试。"""

import asyncio
import importlib
import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parent.parent / "plugins" / "minekuai"))
verification = importlib.import_module("verification")


@pytest.mark.asyncio
async def test_submit_delivers_code_and_removes_request():
    broker = verification.VerificationBroker()
    request = broker.open(100, 200, "sms")

    assert broker.submit(100, 200, "sms", "123456") is True
    assert await broker.wait(request, 0.1) == "123456"
    assert broker.submit(100, 200, "sms", "654321") is False


@pytest.mark.asyncio
async def test_wrong_user_cannot_submit_code():
    broker = verification.VerificationBroker()
    request = broker.open(100, 200, "sms")

    assert broker.submit(101, 200, "sms", "123456") is False
    assert request.future.done() is False
    broker.close(request)


@pytest.mark.asyncio
async def test_private_message_can_answer_unique_group_request():
    broker = verification.VerificationBroker()
    request = broker.open(100, 200, "image")

    assert broker.submit(100, None, "image", "7") is True
    assert await broker.wait(request, 0.1) == "7"


@pytest.mark.asyncio
async def test_ambiguous_cross_context_submission_is_rejected():
    broker = verification.VerificationBroker()
    first = broker.open(100, 200, "sms")
    second = broker.open(100, 201, "sms")

    assert broker.submit(100, None, "sms", "123456") is False
    broker.close(first)
    broker.close(second)


@pytest.mark.asyncio
async def test_timeout_removes_request():
    broker = verification.VerificationBroker()
    request = broker.open(100, 200, "sms")

    with pytest.raises(asyncio.TimeoutError):
        await broker.wait(request, 0.01)
    assert broker.submit(100, 200, "sms", "123456") is False


@pytest.mark.asyncio
async def test_cancel_wakes_waiter():
    broker = verification.VerificationBroker()
    request = broker.open(100, 200, "image")
    waiter = asyncio.create_task(broker.wait(request, 1))

    assert broker.cancel(100, 200, "image") is True
    with pytest.raises(verification.VerificationCancelledError):
        await waiter
