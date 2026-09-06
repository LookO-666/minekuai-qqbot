import asyncio
import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "plugins" / "minekuai"))
operations = importlib.import_module("operations")


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
