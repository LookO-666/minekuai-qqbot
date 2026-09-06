"""idle_watcher.py 的 SLP 退避单元测试。"""
import importlib.util
import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def idle_watcher_mod(monkeypatch):
    """不加载 NoneBot 插件入口，隔离导入 idle_watcher。"""
    package_name = "_idle_watcher_test_pkg"
    package = types.ModuleType(package_name)
    package.__path__ = []
    monkeypatch.setitem(sys.modules, package_name, package)
    servers_module = types.ModuleType(f"{package_name}.servers")
    servers_module.Server = type("Server", (), {})
    monkeypatch.setitem(
        sys.modules,
        f"{package_name}.servers",
        servers_module,
    )

    module_name = f"{package_name}.idle_watcher"
    path = (
        Path(__file__).parent.parent
        / "plugins"
        / "minekuai"
        / "idle_watcher.py"
    )
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module._slp_failures.clear()
    module._slp_retry_after.clear()
    module._breach_count.clear()
    module._last_alert_at.clear()
    return module


def test_slp_failure_backoff_is_capped(idle_watcher_mod, monkeypatch):
    monkeypatch.setattr(idle_watcher_mod, "time", lambda: 1_000.0)
    addr = "mc.example.com:25565"

    expected = (30, 60, 120, 300, 300)
    for failures, delay in enumerate(expected, start=1):
        idle_watcher_mod._record_slp_failure(addr)
        assert idle_watcher_mod._slp_failures[addr] == failures
        assert idle_watcher_mod._slp_retry_after[addr] == 1_000.0 + delay


@pytest.mark.asyncio
async def test_explicit_query_bypasses_background_backoff(
    idle_watcher_mod, monkeypatch,
):
    class FakeJavaServer:
        lookup_calls = 0

        @classmethod
        async def async_lookup(cls, _addr):
            cls.lookup_calls += 1
            return cls()

        async def async_status(self):
            return types.SimpleNamespace(
                players=types.SimpleNamespace(
                    sample=None, online=2, max=10,
                ),
                latency=3.7,
                version=types.SimpleNamespace(name="test"),
            )

    fake_mcstatus = types.ModuleType("mcstatus")
    fake_mcstatus.JavaServer = FakeJavaServer
    monkeypatch.setitem(sys.modules, "mcstatus", fake_mcstatus)

    addr = "mc.example.com:25565"
    idle_watcher_mod._slp_retry_after[addr] = idle_watcher_mod.time() + 60

    background = await idle_watcher_mod.query_status(
        addr, use_backoff=True,
    )
    assert background is None
    explicit = await idle_watcher_mod.query_status(addr)
    assert explicit is not None
    assert explicit.online == 2
    assert FakeJavaServer.lookup_calls == 1


@pytest.mark.asyncio
async def test_resource_alert_mentions_admins_not_all_allowed_users(
    idle_watcher_mod, monkeypatch,
):
    idle_watcher_mod._config = types.SimpleNamespace(
        alert_sustained_ticks=1,
        alert_cooldown_minutes=30,
        admin_users=[222],
        allowed_users=[111],
    )
    monkeypatch.setattr(idle_watcher_mod, "time", lambda: 1_000.0)
    sent = []

    async def fake_broadcast(text):
        sent.append(text)

    monkeypatch.setattr(idle_watcher_mod, "_broadcast", fake_broadcast)
    server = types.SimpleNamespace(name="test")

    await idle_watcher_mod._eval_breach(
        server, "cpu", True, "CPU alert",
    )

    assert sent == ["CPU alert [CQ:at,qq=222]"]


def _keepalive_server(name="ATM", card_id="shared-card"):
    return types.SimpleNamespace(
        name=name,
        card_id=card_id,
        instance_uuid="instance-uuid",
        account_phone="test-account",
        address="mc.example.com:25565",
        last_started_at=1_000_000,
        created_at=1_000_000,
        updated_at=1_000_000,
    )


def _configure_keepalive(module, monkeypatch, server):
    monkeypatch.setattr(module.servers, "list_servers", lambda: [server], raising=False)
    monkeypatch.setattr(module.servers, "get_server", lambda _name: server, raising=False)
    module._start_callback = AsyncMock(return_value=(False, "request timed out"))
    module._close_callback = AsyncMock(return_value=(True, "ok"))
    monkeypatch.setattr(module, "_broadcast", AsyncMock())
    monkeypatch.setattr(module, "_looks_running", AsyncMock(return_value=False))


@pytest.mark.asyncio
async def test_failed_keepalive_waits_thirty_minutes_before_retry(
    idle_watcher_mod, monkeypatch,
):
    module = idle_watcher_mod
    server = _keepalive_server()
    _configure_keepalive(module, monkeypatch, server)
    now = [4_000_000.0]
    monkeypatch.setattr(module, "time", lambda: now[0])

    await module._check_keepalive(server)
    await module._keepalive_tasks[server.name]
    assert module._start_callback.await_count == 1
    assert module._broadcast.await_count == 2
    assert module._keepalive_retry_after[server.name] >= now[0] + 30 * 60
    assert server.name not in module._keepalive_tasks

    now[0] += 60
    await module._check_keepalive(server)
    assert module._start_callback.await_count == 1
    assert module._broadcast.await_count == 2

    now[0] += 30 * 60
    await module._check_keepalive(server)
    await module._keepalive_tasks[server.name]
    assert module._start_callback.await_count == 2


@pytest.mark.asyncio
async def test_shared_card_never_starts_automatic_keepalive(
    idle_watcher_mod, monkeypatch,
):
    module = idle_watcher_mod
    server = _keepalive_server()
    other = _keepalive_server(name="bingo")
    _configure_keepalive(module, monkeypatch, server)
    monkeypatch.setattr(module.servers, "list_servers", lambda: [server, other])
    monkeypatch.setattr(module, "time", lambda: 4_000_000.0)

    await module._check_keepalive(server)
    await module._check_keepalive(other)
    module._looks_running.assert_not_awaited()
    module._start_callback.assert_not_awaited()
    module._broadcast.assert_not_awaited()
    assert module._keepalive_tasks == {}


@pytest.mark.asyncio
async def test_shared_card_does_not_schedule_idle_shutdown(
    idle_watcher_mod, monkeypatch,
):
    module = idle_watcher_mod
    server = _keepalive_server()
    server.auto_close_idle_minutes = 1
    other = _keepalive_server(name="bingo")
    monkeypatch.setattr(
        module.servers, "list_servers", lambda: [server, other], raising=False
    )
    monkeypatch.setattr(module, "time", lambda: 4_000_000.0)
    module._open_at[server.name] = 1_000_000.0
    module._last_active[server.name] = 1_000_000.0
    try:
        await module._check_idle(server, types.SimpleNamespace(online=0))
        assert module._pending_close == {}
    finally:
        for task in module._pending_close.values():
            task.cancel()


@pytest.mark.asyncio
async def test_unknown_status_defers_keepalive_without_starting(
    idle_watcher_mod, monkeypatch,
):
    module = idle_watcher_mod
    server = _keepalive_server()
    _configure_keepalive(module, monkeypatch, server)
    module._looks_running.return_value = None
    monkeypatch.setattr(module, "time", lambda: 4_000_000.0)

    await module._check_keepalive(server)
    await module._check_keepalive(server)
    assert module._looks_running.await_count == 1
    module._start_callback.assert_not_awaited()
    module._broadcast.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "err", "expected"),
    [
        ({"attributes": {"current_state": "running"}}, "ok", True),
        ({"attributes": {"current_state": "starting"}}, "ok", True),
        ({"attributes": {"current_state": "offline"}}, "ok", False),
        ({"attributes": {"current_state": "stopping"}}, "ok", None),
        ({"attributes": None}, "ok", None),
        ({"raw": "Verification"}, "ok", None),
        (None, "request timed out", None),
    ],
)
async def test_keepalive_requires_explicit_offline_state(
    idle_watcher_mod, monkeypatch, data, err, expected,
):
    module = idle_watcher_mod
    module._config = types.SimpleNamespace()
    module._panel_runner = AsyncMock(return_value=(data, err))
    monkeypatch.setattr(module, "query_status", AsyncMock(return_value=None))

    assert await module._looks_running(_keepalive_server()) is expected
    module.query_status.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("reachable", [True, False])
async def test_empty_server_is_running_and_unreachable_is_unknown(
    idle_watcher_mod, monkeypatch, reachable,
):
    module = idle_watcher_mod
    status = types.SimpleNamespace(online=0) if reachable else None
    monkeypatch.setattr(module, "query_status", AsyncMock(return_value=status))

    assert await module._looks_running(_keepalive_server()) is (
        True if reachable else None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("hook", ["mark_opened", "mark_closed"])
async def test_manual_state_change_cancels_keepalive(idle_watcher_mod, hook):
    module = idle_watcher_mod
    task = asyncio.create_task(asyncio.Event().wait())
    module._keepalive_tasks["ATM"] = task

    getattr(module, hook)("ATM")
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "ATM" not in module._keepalive_tasks
    assert module._keepalive_retry_after["ATM"] > module.time()


@pytest.mark.asyncio
async def test_keepalive_state_hooks_do_not_cancel_their_own_task(idle_watcher_mod):
    module = idle_watcher_mod
    current = asyncio.current_task()
    module._keepalive_tasks["ATM"] = current
    try:
        module.mark_opened("ATM")
        module.mark_closed("ATM")
        assert module._keepalive_tasks["ATM"] is current
        assert "ATM" not in module._keepalive_retry_after
        assert not current.cancelling()
    finally:
        module._keepalive_tasks.pop("ATM", None)


@pytest.mark.asyncio
async def test_manual_takeover_during_status_probe_prevents_keepalive(
    idle_watcher_mod, monkeypatch,
):
    module = idle_watcher_mod
    server = _keepalive_server()
    _configure_keepalive(module, monkeypatch, server)
    monkeypatch.setattr(module, "time", lambda: 4_000_000.0)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def probe(_server):
        entered.set()
        await release.wait()
        return False

    monkeypatch.setattr(module, "_looks_running", probe)
    check = asyncio.create_task(module._check_keepalive(server))
    await entered.wait()
    assert module.cancel_keepalive(server.name) is False
    release.set()
    await check
    module._start_callback.assert_not_awaited()
    assert module._keepalive_tasks == {}
