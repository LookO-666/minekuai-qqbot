"""idle_watcher.py 的 SLP 退避单元测试。"""
import importlib.util
import sys
import types
from pathlib import Path

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
