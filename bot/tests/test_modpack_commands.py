"""Offline QQ command tests; real confirmation state, fake UI and service I/O."""
from dataclasses import replace
import ast
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


PLUGIN = Path(__file__).parents[1] / "plugins" / "minekuai"
PROGRESS_MESSAGES = (
    "正在开启计时卡...",
    "计时卡已开启，正在核对实例就绪状态...",
    "计时卡已开启且实例离线，正在提交安装...",
)


class Finished(Exception):
    pass


class Rejected(Exception):
    pass


class Message:
    def __init__(self, text=""):
        self.text = str(text)

    def extract_plain_text(self):
        return self.text


class Matcher:
    def __init__(self):
        self.state = {}
        self.args = {}
        self.sent = []
        self.final = ""

    def set_arg(self, name, message):
        self.args[name] = message

    async def send(self, text):
        self.sent.append(str(text))

    async def finish(self, text):
        self.final = str(text)
        raise Finished(self.final)


@pytest.fixture
def ui(monkeypatch):
    registered = {}

    class Command:
        def __init__(self, name, **kwargs):
            self.name, self.options, self.handlers = name, kwargs, {}
            self.prompts = {}
            registered[name] = self

        def handle(self):
            def decorate(fn):
                self.handlers[fn.__name__] = fn
                return fn
            return decorate

        def got(self, key, **kwargs):
            self.prompts[key] = kwargs.get("prompt", "")
            return self.handle()

        async def reject(self, text):
            raise Rejected(str(text))

    modules = {
        "nonebot": {"on_command": Command},
        "nonebot.adapters": {}, "nonebot.adapters.onebot": {},
        "nonebot.adapters.onebot.v11": {
            "Bot": object, "Message": Message, "MessageEvent": object,
            "MessageSegment": SimpleNamespace(text=lambda text: str(text)),
        },
        "nonebot.matcher": {"Matcher": Matcher},
        "nonebot.params": {"ArgPlainText": lambda name: None, "CommandArg": lambda: None},
    }
    for name, members in modules.items():
        module = ModuleType(name)
        module.__path__ = []
        vars(module).update(members)
        monkeypatch.setitem(sys.modules, name, module)

    package_name = "_modpack_commands_test"
    package = ModuleType(package_name)
    package.__path__ = [str(PLUGIN)]
    monkeypatch.setitem(sys.modules, package_name, package)

    def load(name):
        qualified = f"{package_name}.{name}"
        spec = importlib.util.spec_from_file_location(qualified, PLUGIN / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, qualified, module)
        spec.loader.exec_module(module)
        return module

    for name, error in [("client", "MinekuaiError"), ("operations", "OperationBusyError")]:
        module = ModuleType(f"{package_name}.{name}")
        setattr(module, error, type(error, (Exception,), {}))
        monkeypatch.setitem(sys.modules, module.__name__, module)
    state, catalog = load("modpack_state"), load("modpack_catalog")
    commands = load("modpack_commands")
    now = [1000.0]
    monkeypatch.setattr(commands, "time", SimpleNamespace(monotonic=lambda: now[0]))
    server = SimpleNamespace(
        name="test", card_id="card", instance_uuid="instance", account_phone="account", created_at=123,
    )
    project = catalog.CatalogItem("project", "project", "Test Pack", "v1", "1.20.1", "17", "pack.zip")
    version = catalog.CatalogItem("project", "release", "Test Pack", "v2", "1.21.1", "21", "release.zip")
    confirms = state.InstallConfirmStore(clock=lambda: now[0])
    maintenance = SimpleNamespace(ensure_card_available=Mock(), get=Mock(return_value=None), begin=Mock())
    service = SimpleNamespace(
        confirms=confirms, maintenance=maintenance,
        current=Mock(return_value=server),
        preflight=AsyncMock(return_value=(server, server.instance_uuid)),
        search=AsyncMock(return_value=((project,), 1)),
        versions=AsyncMock(return_value=((version,), 1)),
        read=AsyncMock(return_value=({"attributes": {"status": None, "is_installing": False}}, server)),
        finish_maintenance=AsyncMock(),
    )

    async def prepare(scope, selected_server, choice, refresh=None):
        return confirms.issue(scope, state.ServerIdentity.from_server(selected_server), choice)

    async def confirm(scope, code, *, authorized, refresh=None, progress=None):
        pending = confirms.consume(scope, code)
        if not authorized():
            raise state.ConfirmError("管理员权限已变化，本次确认已取消")
        maintenance.begin(pending.server, pending.choice)
        if progress is not None:
            for text in PROGRESS_MESSAGES:
                await progress(text)
        return pending

    service.prepare = AsyncMock(side_effect=prepare)
    service.confirm = AsyncMock(side_effect=confirm)
    allowed = [True]
    check_admin = Mock(side_effect=lambda event: (allowed[0], "没有管理员权限"))
    audit = Mock()
    registry = commands.register_modpack_commands(
        servers=SimpleNamespace(
            list_servers=lambda: [server], get_server=lambda name: server if name == server.name else None,
        ),
        service=service, check_admin=check_admin, refresh_factory=lambda matcher, event: None,
        audit=audit, display_name=lambda event: "tester",
    )
    return SimpleNamespace(
        commands=commands, registry=registry, registered=registered, service=service,
        state_module=state, server=server, project=project, version=version,
        now=now, allowed=allowed, check_admin=check_admin, audit=audit,
        matcher=Matcher(), bot=SimpleNamespace(self_id="100"),
        event=SimpleNamespace(user_id=200, group_id=300),
    )


async def choose_project(ui):
    handlers = ui.registry["change"].handlers
    await handlers["begin"](ui.matcher, ui.bot, ui.event, Message("test Test Pack"))
    assert ui.matcher.args["mp_name"].extract_plain_text() == "test"
    assert ui.matcher.args["mp_query"].extract_plain_text() == "Test Pack"
    await handlers["choose_server"](ui.matcher, ui.bot, ui.event, "test")
    await handlers["search"](ui.matcher, ui.bot, ui.event, "Test Pack")
    await handlers["choose_pack"](ui.matcher, ui.bot, ui.event, "1")


async def choose_release(ui, answer="0"):
    await choose_project(ui)
    with pytest.raises(Finished):
        await ui.registry["change"].handlers["choose_version"](
            ui.matcher, ui.bot, ui.event, answer,
        )
    return ui.service.prepare.await_args.args[2]


def current_code(ui):
    return ui.service.confirms._pending[ui.commands.scope_of(ui.bot, ui.event)].code


async def invoke(ui, command, text="", *, event=None, bot=None):
    matcher = Matcher()
    event, bot = event or ui.event, bot or ui.bot
    handlers = ui.registry[command].handlers
    fn = next(iter(handlers.values()))
    with pytest.raises(Finished):
        if command in {"status", "finish"}:
            await fn(matcher, event, Message(text))
        elif command == "cancel":
            await fn(matcher, bot, event)
        else:
            await fn(matcher, bot, event, Message(text))
    return matcher.final


@pytest.mark.asyncio
@pytest.mark.parametrize("answer,item_id,version_page", [("0", "project", 0), ("1", "release", 1)])
async def test_selection_only_prepares_and_warns_about_world_deletion(ui, answer, item_id, version_page):
    choice = await choose_release(ui, answer)
    assert choice.item_id == item_id
    assert choice.version_page == version_page
    assert choice.search_query == "Test Pack"
    assert choice.search_page == 1
    warning = ui.matcher.final
    for text in ["覆盖全部文件", "世界存档", "不会自动备份", "自行备份", "5 分钟", "确认清空安装"]:
        assert text in warning
    assert "确认同时授权：开启计时卡（消耗时长）＋清空安装" in warning
    assert "若平台自动启动实例，会尝试一次正常停服" in warning
    assert "已解冻且离线后" in warning
    assert "开卡或就绪检查失败不会提交安装，但维护保护会保留" in warning
    assert "不会主动发送游戏启动指令或强杀" in warning
    assert "不会自动关卡，计费可能持续" in warning
    ui.service.confirm.assert_not_awaited()
    ui.service.finish_maintenance.assert_not_awaited()


@pytest.mark.asyncio
async def test_only_explicit_confirmation_calls_install_service_and_reports_submission(ui):
    await choose_release(ui)
    code = current_code(ui)
    ui.service.confirm.assert_not_awaited()
    message = await invoke(ui, "confirm", code)
    ui.service.confirm.assert_awaited_once()
    assert ui.service.confirm.await_args.args[:2] == ((100, 200, 300), code)
    assert "安装请求已提交，尚未确认完成" in message
    assert "维护保护已开启" in message
    assert "先开启计时卡" in message
    assert "平台若自动启动则正常停服后再安装" in message
    assert "计费可能仍在继续，机器人不会自动关卡" in message
    assert "我已核对" in message
    assert "✅" not in await invoke(ui, "confirm", code)


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", [
    {"user_id": 201}, {"group_id": 301}, {"group_id": None},
])
async def test_confirmation_cannot_cross_user_group_or_private_scope(ui, overrides):
    await choose_release(ui)
    code = current_code(ui)
    other = SimpleNamespace(**{**vars(ui.event), **overrides})
    message = await invoke(ui, "confirm", code, event=other)
    assert message.startswith("❌")
    assert "已提交，尚未确认完成" not in message
    assert "已提交，尚未确认完成" in await invoke(ui, "confirm", code)


@pytest.mark.asyncio
async def test_confirmation_cannot_cross_bot_scope(ui):
    await choose_release(ui)
    code = current_code(ui)
    assert (await invoke(ui, "confirm", code, bot=SimpleNamespace(self_id="101"))).startswith("❌")
    assert "已提交，尚未确认完成" in await invoke(ui, "confirm", code)


@pytest.mark.asyncio
async def test_expired_confirmation_is_rejected(ui):
    await choose_release(ui)
    code = current_code(ui)
    ui.now[0] += 300
    message = await invoke(ui, "confirm", code)
    assert "过期" in message
    assert "已提交，尚未确认完成" not in message
    assert not ui.service.confirms.cancel((100, 200, 300))


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["scope", "time"])
async def test_selection_session_checks_scope_and_timeout(ui, mismatch):
    await choose_project(ui)
    event = ui.event
    if mismatch == "scope":
        event = SimpleNamespace(user_id=200, group_id=301)
    else:
        ui.now[0] += 301
    with pytest.raises(Finished):
        await ui.registry["change"].handlers["choose_version"](ui.matcher, ui.bot, event, "0")
    assert "不匹配" in ui.matcher.final if mismatch == "scope" else "超时" in ui.matcher.final
    ui.service.prepare.assert_not_awaited()
    ui.service.confirm.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_pending_choice_does_not_release_maintenance_or_install(ui):
    await choose_release(ui)
    assert "已取消" in await invoke(ui, "cancel")
    assert not ui.service.confirms.cancel((100, 200, 300))
    ui.service.confirm.assert_not_awaited()
    ui.service.finish_maintenance.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_from_other_group_does_not_cancel_pending_choice(ui):
    await choose_release(ui)
    code = current_code(ui)
    message = await invoke(ui, "cancel", event=SimpleNamespace(user_id=200, group_id=301))
    assert "没有待确认" in message
    assert current_code(ui) == code


@pytest.mark.asyncio
async def test_cancel_word_stops_selection_before_preparation(ui):
    await choose_project(ui)
    with pytest.raises(Finished):
        await ui.registry["change"].handlers["choose_version"](ui.matcher, ui.bot, ui.event, "取消")
    assert "未提交安装" in ui.matcher.final
    ui.service.prepare.assert_not_awaited()
    ui.service.confirm.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("command,text", [
    ("change", "test Test Pack"), ("confirm", "123456"), ("cancel", ""),
    ("status", "test"), ("finish", "test 我已核对"),
])
async def test_all_commands_require_current_admin_permission(ui, command, text):
    ui.allowed[0] = False
    assert "没有管理员权限" in await invoke(ui, command, text)
    ui.service.preflight.assert_not_awaited()
    ui.service.prepare.assert_not_awaited()
    ui.service.confirm.assert_not_awaited()
    ui.service.read.assert_not_awaited()
    ui.service.finish_maintenance.assert_not_awaited()


@pytest.mark.asyncio
async def test_permission_revoked_after_selection_blocks_confirmation(ui):
    await choose_release(ui)
    code = current_code(ui)
    ui.allowed[0] = False
    assert "没有管理员权限" in await invoke(ui, "confirm", code)
    ui.service.confirm.assert_not_awaited()


@pytest.mark.asyncio
async def test_search_and_version_pagination_preserve_confirmation_catalog_context(ui):
    ui.service.search.return_value = ((ui.project,), 18)
    ui.service.versions.return_value = ((ui.version,), 18)
    await choose_project(ui)
    handlers = ui.registry["change"].handlers
    with pytest.raises(Rejected):
        await handlers["choose_pack"](ui.matcher, ui.bot, ui.event, "下一页")
    assert ui.matcher.state["mp_page"] == 2
    await handlers["choose_pack"](ui.matcher, ui.bot, ui.event, "1")
    with pytest.raises(Rejected):
        await handlers["choose_version"](ui.matcher, ui.bot, ui.event, "下一页")
    assert ui.matcher.state["mp_vpage"] == 2
    with pytest.raises(Finished):
        await handlers["choose_version"](ui.matcher, ui.bot, ui.event, "1")
    choice = ui.service.prepare.await_args.args[2]
    assert choice.search_page == 2
    assert choice.version_page == 2
    ui.service.confirm.assert_not_awaited()


@pytest.mark.asyncio
async def test_uninstallable_main_version_is_rejected_without_prepare(ui):
    ui.project = replace(ui.project, file_name="")
    ui.service.search.return_value = ((ui.project,), 1)
    await choose_project(ui)
    with pytest.raises(Rejected, match="没有可安装文件"):
        await ui.registry["change"].handlers["choose_version"](ui.matcher, ui.bot, ui.event, "0")
    ui.service.prepare.assert_not_awaited()
    ui.service.confirm.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["test", "test 确认", "test 我已核对 立即", "我已核对"])
async def test_release_requires_exact_acknowledgment_phrase(ui, text):
    message = await invoke(ui, "finish", text)
    assert "我已核对" in message
    ui.service.finish_maintenance.assert_not_awaited()


@pytest.mark.asyncio
async def test_acknowledged_release_does_not_claim_installation_success(ui):
    message = await invoke(ui, "finish", "test 我已核对")
    ui.service.finish_maintenance.assert_awaited_once()
    assert ui.service.finish_maintenance.await_args.kwargs["authorized"]()
    assert "维护保护已解除" in message
    assert "没有自动开服" in message
    assert "未据此判定安装成功" in message
    assert "计时卡没有被自动关闭，计费可能继续" in message
    ui.service.confirm.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", [None, "preparing", "submitted", "unknown"])
async def test_status_query_is_read_only_and_does_not_identify_completed_pack(ui, phase):
    if phase is not None:
        ui.service.maintenance.get.return_value = {
            "phase": phase, "pack_name": "Test Pack", "pack_version": "v1",
        }
    message = await invoke(ui, "status", "test")
    assert "这不是已安装整合包的识别结果" in message
    assert "请在官网核对安装结果与文件" in message
    assert "计时卡可能已开启并继续计费" in message
    assert "不会自动关卡" in message
    assert "不代表安装成功" in message if phase else "不代表当前安装状态" in message
    ui.service.confirm.assert_not_awaited()
    ui.service.finish_maintenance.assert_not_awaited()


def test_registers_expected_commands_and_alias(ui):
    assert set(ui.registered) == {
        "更换整合包", "确认清空安装", "取消更换整合包", "整合包状态", "结束整合包维护",
    }
    assert ui.registered["更换整合包"].options["aliases"] == {"切换整合包"}


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", ["choose_pack", "choose_version"])
async def test_overlong_numeric_selection_is_rejected_without_integer_conversion(ui, handler):
    await choose_project(ui)
    with pytest.raises(Rejected):
        await ui.registry["change"].handlers[handler](
            ui.matcher, ui.bot, ui.event, "1" * 6000,
        )
    ui.service.prepare.assert_not_awaited()
    ui.service.confirm.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_confirmation_warns_about_billing_and_manual_recovery(ui):
    await choose_release(ui)
    code = current_code(ui)
    ui.service.confirm.side_effect = ui.commands.MinekuaiError("开卡状态未知，未提交安装")
    message = await invoke(ui, "confirm", code)
    assert "未提交安装" in message
    assert "维护保护会保留" in message
    assert "计时卡可能已开启并继续消耗时长" in message
    assert "不会自动关卡" in message
    assert "到官网核对安装与计费" in message
    assert "手动关闭计时卡、解除保护" in message


def test_modpack_help_discloses_billing_and_platform_autostart():
    tree = ast.parse((PLUGIN / "__init__.py").read_text(encoding="utf-8"))
    assignment = next(
        node for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "MODPACK_HELP" for target in node.targets)
    )
    text = ast.literal_eval(assignment.value)
    for phrase in ("开卡计费", "消耗时长", "平台若自动启动则正常停服", "不强杀", "不会自动关卡", "计费可能继续"):
        assert phrase in text


@pytest.mark.asyncio
async def test_confirm_progress_is_sent_after_consumption_and_guard(ui):
    await choose_release(ui)
    code = current_code(ui)
    matcher = Matcher()

    async def send(text):
        assert (100, 200, 300) not in ui.service.confirms._pending
        ui.service.maintenance.begin.assert_called_once()
        matcher.sent.append(str(text))

    matcher.send = send
    with pytest.raises(Finished):
        await ui.registry["confirm"].handlers["install"](
            matcher, ui.bot, ui.event, Message(code),
        )
    assert matcher.sent == list(PROGRESS_MESSAGES)
    assert callable(ui.service.confirm.await_args.kwargs["progress"])
    assert "安装请求已提交，尚未确认完成" in matcher.final


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["code", "scope", "expired"])
async def test_invalid_confirmation_does_not_emit_billing_progress(ui, invalid):
    await choose_release(ui)
    code = current_code(ui)
    event = ui.event
    if invalid == "code":
        code = "wrong-code"
    elif invalid == "scope":
        event = SimpleNamespace(user_id=200, group_id=301)
    else:
        ui.now[0] += 300
    matcher = Matcher()
    with pytest.raises(Finished):
        await ui.registry["confirm"].handlers["install"](
            matcher, ui.bot, event, Message(code),
        )
    assert not matcher.sent
    ui.service.maintenance.begin.assert_not_called()
    assert matcher.final.startswith("❌")
