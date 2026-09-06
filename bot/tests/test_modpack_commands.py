"""Offline QQ command tests; real confirmation state, fake UI and service I/O."""
from dataclasses import replace
import ast
import asyncio
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
    maintenance = SimpleNamespace(ensure_card_available=Mock(), get=Mock(return_value=None),
                                  latest=Mock(return_value=None), begin=Mock())
    service = SimpleNamespace(
        confirms=confirms, maintenance=maintenance,
        current=Mock(return_value=server),
        preflight=AsyncMock(return_value=(server, server.instance_uuid)),
        search=AsyncMock(return_value=((project,), 1)),
        versions=AsyncMock(return_value=((version,), 1)),
        read=AsyncMock(return_value=({"attributes": {"status": None, "is_installing": False}}, server)),
        install_status=AsyncMock(return_value={
            "outcome": "unknown", "detail": "暂无可确认的本次安装结果", "billing_active": None,
        }),
        reconcile_maintenance=AsyncMock(return_value={
            "outcome": "unknown", "detail": "暂无可确认的本次安装结果", "billing_active": None,
            "maintenance": True, "released": False,
        }),
        reconcile_for_operation=AsyncMock(return_value={"maintenance": False, "released": False}),
        install_log=AsyncMock(return_value="[时间] 正在安装..."),
        client_download=AsyncMock(return_value={
            "name": "Test Pack", "version": "v2", "game_version": "1.21.1", "java_version": "21",
            "url": "https://www.123865.com/s/CiAtjv-xGYr", "code": "", "exact": False,
            "detail": "免费目录，不调用扣积分直链接口。",
        }),
        finish_maintenance=AsyncMock(),
    )

    async def prepare(scope, selected_server, choice, refresh=None):
        return confirms.issue(scope, state.ServerIdentity.from_server(selected_server), choice)

    async def confirm(scope, code, *, authorized, refresh=None, progress=None, on_submitted=None):
        pending = confirms.consume(scope, code)
        if not authorized():
            raise state.ConfirmError("管理员权限已变化，本次确认已取消")
        maintenance.begin(pending.server, pending.choice)
        if progress is not None:
            for text in PROGRESS_MESSAGES:
                await progress(text)
        if on_submitted is not None:
            await on_submitted(pending)
        return pending

    service.prepare = AsyncMock(side_effect=prepare)
    service.confirm = AsyncMock(side_effect=confirm)
    allowed = [True]
    read_allowed = [True]
    check_admin = Mock(side_effect=lambda event: (allowed[0], "没有管理员权限"))
    check_permission = Mock(side_effect=lambda event: (read_allowed[0], "没有机器人访问权限"))
    audit = Mock()
    registry = commands.register_modpack_commands(
        servers=SimpleNamespace(
            list_servers=lambda: [server], get_server=lambda name: server if name == server.name else None,
        ),
        service=service, check_admin=check_admin, refresh_factory=lambda matcher, event: None,
        check_permission=check_permission,
        audit=audit, display_name=lambda event: "tester",
    )
    return SimpleNamespace(
        commands=commands, registry=registry, registered=registered, service=service,
        state_module=state, server=server, project=project, version=version,
        now=now, allowed=allowed, check_admin=check_admin, audit=audit,
        read_allowed=read_allowed, check_permission=check_permission,
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


async def invoke(ui, command, text="", *, event=None, bot=None, matcher=None):
    matcher = matcher or Matcher()
    ui.last_matcher = matcher
    event, bot = event or ui.event, bot or ui.bot
    handlers = ui.registry[command].handlers
    fn = next(iter(handlers.values()))
    with pytest.raises(Finished):
        if command in {"status", "finish", "logs", "client"}:
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
    assert "整合包日志 test" in message
    assert "我已核对" not in message
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
    ("status", "test"), ("finish", "test"), ("logs", "test"),
])
async def test_all_commands_require_current_admin_permission(ui, command, text):
    ui.allowed[0] = False
    assert "没有管理员权限" in await invoke(ui, command, text)
    ui.service.preflight.assert_not_awaited()
    ui.service.prepare.assert_not_awaited()
    ui.service.confirm.assert_not_awaited()
    ui.service.read.assert_not_awaited()
    ui.service.install_status.assert_not_awaited()
    ui.service.finish_maintenance.assert_not_awaited()
    ui.service.reconcile_maintenance.assert_not_awaited()
    ui.service.reconcile_for_operation.assert_not_awaited()
    ui.service.install_log.assert_not_awaited()


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
@pytest.mark.parametrize("text", ["test", "test 我已核对", "", "我已核对"])
async def test_release_no_longer_requires_acknowledgment_but_always_checks_safety(ui, text):
    message = await invoke(ui, "finish", text)
    assert "维护保护：保留" in message
    assert "已自动解除" not in message
    ui.service.reconcile_maintenance.assert_awaited_once()
    assert ui.service.reconcile_maintenance.await_args.kwargs["authorized"]()
    ui.service.finish_maintenance.assert_not_awaited()


@pytest.mark.asyncio
async def test_safe_unsubmitted_release_does_not_claim_installation_success(ui):
    ui.service.reconcile_maintenance.return_value.update(
        outcome="not_submitted", maintenance=False, released=True,
        detail="本次未发送安装请求且平台无活动安装", billing_active=True,
    )
    message = await invoke(ui, "finish", "test")
    assert "维护保护：已自动解除" in message
    assert "本次安装观察：未提交安装" in message
    assert "已确认完成" not in message
    assert "不会自动启动游戏" in message
    assert "计费：已开启，正在消耗时长" in message
    assert "关服 test" in message and "确认关服" in message
    ui.service.confirm.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", [None, "preparing", "submitted", "unknown"])
async def test_status_reconciles_without_installing_or_identifying_completed_pack(ui, phase):
    if phase is not None:
        ui.service.maintenance.latest.return_value = {
            "phase": phase, "pack_name": "Test Pack", "pack_version": "v1",
        }
    message = await invoke(ui, "status", "test")
    assert "不代表已识别当前整合包" in message
    assert "计费：尚未确认，可能仍在消耗时长" in message
    assert "不会自动关卡" in message
    assert "维护保护：保留" in message
    if phase:
        assert "记录选择：Test Pack · v1" in message
    ui.service.reconcile_maintenance.assert_awaited_once()
    ui.service.install_status.assert_not_awaited()
    ui.service.confirm.assert_not_awaited()
    ui.service.finish_maintenance.assert_not_awaited()


def test_registers_expected_commands_and_alias(ui):
    assert set(ui.registered) == {
        "更换整合包", "确认清空安装", "取消更换整合包", "整合包状态", "结束整合包维护", "整合包日志", "整合包客户端",
    }
    assert ui.registered["更换整合包"].options["aliases"] == {"切换整合包"}
    assert ui.registered["整合包客户端"].options["aliases"] == {"客户端", "下载客户端"}


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
async def test_failed_confirmation_warns_about_billing_and_qq_recovery(ui):
    await choose_release(ui)
    code = current_code(ui)
    ui.service.confirm.side_effect = ui.commands.MinekuaiError("开卡状态未知，未提交安装")
    message = await invoke(ui, "confirm", code)
    assert "未提交安装" in message
    assert "维护保护会保留" in message
    assert "计时卡可能已开启并继续消耗时长" in message
    assert "不会自动关卡" in message
    assert "整合包日志 <服务器>" in message
    assert "确认任务安全结束后会自动解除" in message
    assert "结束整合包维护 <服务器>" in message
    assert "我已核对" not in message and "官网" not in message


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
    assert matcher.sent[:len(PROGRESS_MESSAGES)] == list(PROGRESS_MESSAGES)
    assert len(matcher.sent) == len(PROGRESS_MESSAGES) + 1
    assert "下载入口" in matcher.sent[-1]
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


@pytest.mark.asyncio
async def test_service_readiness_error_preserves_reason_without_duplicate_warnings(ui):
    await choose_release(ui)
    reason = (
        "开计时卡或安装前检查未完成：HTTP 实例离线；WS 已认证持续离线；计费状态待确认。"
        "未提交更换整合包；计费可能已经开启，维护保护保留，"
        "请先到官网核对计费和实例状态；机器人不会自动关卡"
    )
    ui.service.confirm.side_effect = ui.commands.MinekuaiError(reason)
    message = await invoke(ui, "confirm", current_code(ui))
    assert reason in message
    for phrase in ("未提交更换整合包", "维护保护保留", "不会自动关卡", "计费可能已经开启"):
        assert message.count(phrase) == 1
    assert "若已开始开卡或安装" not in message
    assert "计时卡可能已开启并继续消耗时长" not in message
    assert "整合包状态 <服务器>" in message
    assert "结束整合包维护 <服务器>" in message
    assert "整合包状态 test" not in message


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [
    "安装请求结果未知，维护保护会保留。",
    "开卡计费结果未知，机器人不会自动关卡。",
    "未提交更换整合包，维护保护仍然保留；计费可能开启，机器人不自动关卡。",
])
async def test_partial_service_warnings_only_add_missing_safety_information(ui, reason):
    await choose_release(ui)
    ui.service.confirm.side_effect = ui.commands.MinekuaiError(reason)
    message = await invoke(ui, "confirm", current_code(ui))
    assert reason in message
    assert message.count("维护保护") == 1
    assert message.count("自动关卡") == 1
    assert "计费" in message
    assert "请勿重复安装" in message


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome,expected", [
    ("completed", "本次维护期间的安装已确认完成"),
    ("failed", "平台报告本次安装失败"),
    ("installing", "平台仍在安装中"),
    ("unknown", "尚未确认完成"),
])
async def test_confirmation_result_uses_persisted_install_observation(ui, outcome, expected):
    await choose_release(ui)
    ui.service.maintenance.latest.return_value = {"install_outcome": outcome}
    message = await invoke(ui, "confirm", current_code(ui))
    assert expected in message
    assert "维护保护已开启" in message
    assert "不会自动关卡" in message
    if outcome == "completed":
        assert "这不代表游戏已经启动或可以进入" in message
        assert "尚未确认完成" not in message
    ui.service.finish_maintenance.assert_not_awaited()


@pytest.mark.asyncio
async def test_api_response_error_is_uncertain_not_claimed_install_failure(ui):
    await choose_release(ui)
    ui.service.confirm.side_effect = ui.commands.MinekuaiError("安装接口响应无法解析，结果未知")
    message = await invoke(ui, "confirm", current_code(ui))
    assert message.startswith("⚠️")
    assert "安装请求可能已受理" in message
    assert "安装失败" not in message
    assert "不要再次提交" in message
    assert "整合包状态 <服务器>" in message


@pytest.mark.asyncio
async def test_explicit_platform_failure_is_not_downgraded_to_accepted(ui):
    await choose_release(ui)
    ui.service.confirm.side_effect = ui.commands.MinekuaiError("平台安装失败，请检查安装日志")
    message = await invoke(ui, "confirm", current_code(ui))
    assert message.startswith("❌")
    assert "平台安装失败" in message
    assert "可能已受理" not in message


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome,label", [
    ("completed", "已确认完成"), ("failed", "平台报告失败"),
    ("installing", "仍在安装中"), ("unknown", "尚未确认结果"),
])
@pytest.mark.parametrize("billing,label_billing", [
    (True, "计费：已开启，正在消耗时长"),
    (False, "计费：本次查询显示未开启"),
    (None, "计费：尚未确认，可能仍在消耗时长"),
])
async def test_install_status_displays_observed_result_and_billing_without_pack_detection(
    ui, outcome, label, billing, label_billing,
):
    ui.service.reconcile_maintenance.return_value = {
        "outcome": outcome,
        "detail": "本次维护期间的安装日志确认成功" if outcome == "completed" else "本次安装观察说明",
        "billing_active": billing,
        "maintenance": outcome in {"unknown", "installing"},
        "released": outcome in {"completed", "failed"},
    }
    message = await invoke(ui, "status", "test")
    assert f"本次安装观察：{label}" in message
    assert label_billing in message
    assert "不代表已识别当前整合包" in message
    assert "不会自动关卡" in message
    assert "官网" not in message
    if outcome == "completed":
        assert "本次维护期间的安装日志确认成功" in message
    ui.service.reconcile_maintenance.assert_awaited_once()
    ui.service.confirm.assert_not_awaited()
    ui.service.finish_maintenance.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirm_displays_archived_completion_and_qq_power_commands(ui):
    await choose_release(ui)
    ui.service.maintenance.latest.return_value = {
        "install_outcome": "completed", "released_at": 1234567890,
        "pack_name": "Test Pack", "pack_version": "v1",
    }
    message = await invoke(ui, "confirm", current_code(ui))
    assert "安装已确认完成" in message
    assert "维护保护已自动解除" in message
    assert "安装结果已归档" in message
    assert "开服 test" in message
    assert "关服 test" in message and "确认关服" in message
    assert "官网" not in message and "我已核对" not in message
    assert "维护保护已开启" not in message
    ui.service.maintenance.latest.assert_called_once_with("instance")
    ui.service.maintenance.get.assert_not_called()
    ui.service.reconcile_maintenance.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirm_history_read_failure_never_claims_release(ui):
    await choose_release(ui)
    ui.service.maintenance.latest.side_effect = RuntimeError("unavailable")
    message = await invoke(ui, "confirm", current_code(ui))
    assert "尚未确认完成" in message
    assert "维护保护已开启" in message
    assert "已自动解除" not in message
    assert "整合包状态 test" in message


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["status", "finish"])
@pytest.mark.parametrize("report", [
    None, {}, {"maintenance": False}, {"maintenance": "false", "released": True},
    {"maintenance": True, "released": True}, {"maintenance": False, "released": 1},
])
async def test_invalid_reconciliation_result_never_claims_released(ui, command, report):
    ui.service.reconcile_maintenance.return_value = report
    message = await invoke(ui, command, "test")
    assert "结果格式异常" in message
    assert "已自动解除" not in message
    assert "开服 test" not in message
    ui.service.confirm.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["unknown", "installing"])
async def test_legacy_acknowledgment_cannot_override_active_or_uncertain_task(ui, outcome):
    ui.service.reconcile_maintenance.return_value.update(outcome=outcome)
    message = await invoke(ui, "finish", "test 我已核对")
    assert "维护保护：保留" in message
    assert "已自动解除" not in message
    assert "不要重新安装" in message
    ui.service.finish_maintenance.assert_not_awaited()


@pytest.mark.asyncio
async def test_logs_show_service_redacted_excerpt_without_install_or_release(ui):
    ui.service.install_log.return_value = "[10:00] 下载完成\n[10:01] token=[已脱敏]"
    message = await invoke(ui, "logs", "test")
    assert "安装日志（已脱敏，最近片段）" in message
    assert "token=[已脱敏]" in message
    assert "整合包状态 test" in message
    ui.service.install_log.assert_awaited_once_with(ui.server, refresh=None)
    ui.service.reconcile_maintenance.assert_not_awaited()
    ui.service.confirm.assert_not_awaited()


@pytest.mark.asyncio
async def test_logs_use_single_server_default_and_bound_output(ui):
    ui.service.install_log.return_value = "x" * 4000
    message = await invoke(ui, "logs")
    assert "x" * 3000 in message
    assert "x" * 3001 not in message
    ui.service.install_log.assert_awaited_once_with(ui.server, refresh=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, {}, []])
async def test_logs_reject_malformed_service_result(ui, payload):
    ui.service.install_log.return_value = payload
    assert "安装日志格式异常" in await invoke(ui, "logs", "test")
    ui.service.reconcile_maintenance.assert_not_awaited()


@pytest.mark.asyncio
async def test_selection_reconciles_old_guard_before_entry_check_and_preparation(ui):
    events = []

    async def reconcile(*args, **kwargs):
        assert args == (ui.server,)
        assert kwargs["authorized"]()
        events.append("reconcile")

    ui.service.reconcile_for_operation.side_effect = reconcile
    ui.service.maintenance.ensure_card_available.side_effect = lambda card: events.append("guard")
    original = ui.service.prepare.side_effect

    async def prepare(*args, **kwargs):
        events.append("prepare")
        return await original(*args, **kwargs)

    ui.service.prepare.side_effect = prepare
    await choose_release(ui)
    assert events == ["reconcile", "guard", "reconcile", "prepare"]
    assert ui.service.reconcile_for_operation.await_count == 2
    ui.service.confirm.assert_not_awaited()


@pytest.mark.asyncio
async def test_still_protected_previous_install_blocks_selection_before_catalog(ui):
    ui.service.reconcile_for_operation.side_effect = ui.commands.MinekuaiError("安装任务仍受维护保护")
    handlers = ui.registry["change"].handlers
    await handlers["begin"](ui.matcher, ui.bot, ui.event, Message("test Test Pack"))
    with pytest.raises(Finished, match="仍受维护保护"):
        await handlers["choose_server"](ui.matcher, ui.bot, ui.event, "test")
    ui.service.preflight.assert_not_awaited()
    ui.service.search.assert_not_awaited()
    ui.service.prepare.assert_not_awaited()


@pytest.mark.asyncio
async def test_guard_appearing_during_selection_blocks_before_new_confirmation(ui):
    await choose_project(ui)
    ui.service.reconcile_for_operation.side_effect = ui.commands.MinekuaiError("仍有活动安装")
    with pytest.raises(Finished, match="仍有活动安装"):
        await ui.registry["change"].handlers["choose_version"](ui.matcher, ui.bot, ui.event, "0")
    ui.service.prepare.assert_not_awaited()
    ui.service.confirm.assert_not_awaited()


@pytest.mark.asyncio
async def test_client_download_command_is_readonly_and_allowed_for_regular_member(ui):
    ui.allowed[0] = False  # No administrator privileges.
    message = await invoke(ui, "client", "test")
    ui.service.client_download.assert_awaited_once_with(ui.server, refresh=None)
    assert "下载入口" in message and "未上传群文件" in message
    assert "根据最近一次整合包选择记录提供，不代表已识别当前文件或安装成功" in message
    assert "官方免费客户端目录（非该版本直链）" in message
    assert "目录不保证提供该版本" in message
    assert "该版本客户端下载链接：" not in message
    ui.service.prepare.assert_not_awaited()
    ui.service.confirm.assert_not_awaited()
    ui.service.reconcile_maintenance.assert_not_awaited()
    ui.service.reconcile_for_operation.assert_not_awaited()
    ui.service.maintenance.begin.assert_not_called()


@pytest.mark.asyncio
async def test_client_download_denied_access_never_queries_metadata(ui):
    ui.read_allowed[0] = False
    assert "没有机器人访问权限" in await invoke(ui, "client", "test")
    ui.service.client_download.assert_not_awaited()


@pytest.mark.asyncio
async def test_client_download_optional_permission_defaults_to_admin(ui):
    ui.allowed[0] = False
    registry = ui.commands.register_modpack_commands(
        servers=SimpleNamespace(list_servers=lambda: [ui.server], get_server=lambda name: ui.server),
        service=ui.service, check_admin=ui.check_admin,
        refresh_factory=lambda *args: None, audit=ui.audit, display_name=lambda event: "tester",
    )
    matcher = Matcher()
    with pytest.raises(Finished, match="没有管理员权限"):
        await registry["client"].handlers["read_client_download"](matcher, ui.event, Message("test"))
    ui.service.client_download.assert_not_awaited()


@pytest.mark.asyncio
async def test_exact_client_link_includes_version_runtime_and_extraction_code(ui):
    ui.service.client_download.return_value.update(
        url="https://downloads.example.test/client-v2.zip", exact=True, code="abcd",
        detail="官方目录提供的该版本客户端地址。",
    )
    message = await invoke(ui, "client")
    assert "该版本客户端下载链接：" in message
    assert "官方免费客户端目录" not in message
    for expected in ("Test Pack", "v2", "1.21.1", "21", "提取码：abcd", "未上传群文件"):
        assert expected in message
    assert "https://downloads.example.test/client-v2.zip" in message.splitlines()


@pytest.mark.asyncio
async def test_missing_client_link_is_explicit_not_claimed_as_upload(ui):
    ui.service.client_download.return_value.update(url="", exact=False, detail="未找到可用客户端信息。")
    message = await invoke(ui, "client", "test")
    assert "暂未提供可用的客户端下载链接" in message
    assert "未找到可用客户端信息" in message
    assert "已上传" not in message


@pytest.mark.asyncio
async def test_client_metadata_failure_is_generic_and_never_exposes_exception(ui):
    ui.service.client_download.side_effect = RuntimeError("private-token private-account response body")
    message = await invoke(ui, "client", "test")
    assert "客户端下载信息暂不可用" in message and "整合包客户端 test" in message
    assert "private-token" not in message and "RuntimeError" not in message
    ui.service.confirm.assert_not_awaited()


@pytest.mark.asyncio
async def test_client_read_permission_revoked_during_metadata_lookup_suppresses_url(ui):
    info = dict(ui.service.client_download.return_value)

    async def download(*args, **kwargs):
        ui.read_allowed[0] = False
        return info

    ui.service.client_download.side_effect = download
    message = await invoke(ui, "client", "test")
    assert "没有机器人访问权限" in message and info["url"] not in message


@pytest.mark.asyncio
async def test_client_read_changed_server_identity_suppresses_old_url(ui):
    ui.service.current.side_effect = ui.commands.MinekuaiError("private binding details")
    message = await invoke(ui, "client", "test")
    assert "服务器绑定已变化" in message
    assert "https://" not in message and "private binding details" not in message


@pytest.mark.asyncio
async def test_install_submission_sends_selected_release_to_original_matcher(ui):
    selected = await choose_release(ui, "1")
    matcher = Matcher()
    result = await invoke(ui, "confirm", current_code(ui), matcher=matcher)
    ui.service.client_download.assert_awaited_once_with(ui.server, choice=selected, refresh=None)
    assert matcher.sent[:3] == list(PROGRESS_MESSAGES)
    assert len(matcher.sent) == 4
    notice = matcher.sent[-1]
    assert "所选版本，不代表安装已成功" in notice
    assert "官方免费客户端目录（非该版本直链）" in notice
    assert "下载入口" in notice and "未上传群文件" in notice
    assert "尚未确认完成" in result
    assert callable(ui.service.confirm.await_args.kwargs["on_submitted"])
    # Fake bot has no arbitrary-group send or upload API; all output is through this matcher.
    assert vars(ui.bot) == {"self_id": "100"}


@pytest.mark.asyncio
@pytest.mark.parametrize("override", [{"user_id": 201}, {"group_id": 301}, {"group_id": None}])
async def test_invalid_confirmation_scope_never_sends_client_information(ui, override):
    await choose_release(ui)
    event = SimpleNamespace(**{**vars(ui.event), **override})
    await invoke(ui, "confirm", current_code(ui), event=event)
    ui.service.client_download.assert_not_awaited()
    assert not ui.last_matcher.sent


@pytest.mark.asyncio
async def test_confirmation_failure_before_submission_has_no_client_notice(ui):
    await choose_release(ui)
    ui.service.confirm.side_effect = ui.commands.MinekuaiError("实例未就绪，未提交安装")
    await invoke(ui, "confirm", current_code(ui))
    ui.service.client_download.assert_not_awaited()
    assert not ui.last_matcher.sent


@pytest.mark.asyncio
async def test_missing_client_url_does_not_change_install_submission(ui):
    await choose_release(ui)
    ui.service.client_download.return_value.update(url="", detail="该目录暂未提供客户端。")
    message = await invoke(ui, "confirm", current_code(ui))
    assert "暂未提供可用的客户端下载链接" in ui.last_matcher.sent[-1]
    assert "尚未确认完成" in message
    ui.service.confirm.assert_awaited_once()
    ui.service.maintenance.begin.assert_called_once()


@pytest.mark.asyncio
async def test_client_metadata_exception_does_not_cancel_or_repeat_install(ui):
    await choose_release(ui)
    ui.service.client_download.side_effect = RuntimeError("private JWT response secret")
    message = await invoke(ui, "confirm", current_code(ui))
    notice = ui.last_matcher.sent[-1]
    assert "客户端下载信息暂不可用" in notice
    assert "不影响安装，请勿重复安装" in notice
    assert "private JWT" not in notice
    assert "尚未确认完成" in message
    ui.service.confirm.assert_awaited_once()
    ui.service.maintenance.begin.assert_called_once()


@pytest.mark.asyncio
async def test_client_notice_send_failure_does_not_change_install_result(ui):
    await choose_release(ui)
    matcher = Matcher()

    async def send(text):
        if "下载入口" in str(text):
            raise RuntimeError("private QQ error")
        matcher.sent.append(str(text))

    matcher.send = send
    message = await invoke(ui, "confirm", current_code(ui), matcher=matcher)
    assert matcher.sent == list(PROGRESS_MESSAGES)
    assert "尚未确认完成" in message and "private QQ" not in message
    ui.service.confirm.assert_awaited_once()
    ui.service.maintenance.begin.assert_called_once()


@pytest.mark.asyncio
async def test_client_metadata_timeout_gives_bounded_fallback_and_continues(ui, monkeypatch):
    await choose_release(ui)
    monkeypatch.setattr(ui.commands, "CLIENT_METADATA_TIMEOUT", .01)

    async def download(*args, **kwargs):
        await asyncio.Event().wait()

    ui.service.client_download.side_effect = download
    message = await asyncio.wait_for(invoke(ui, "confirm", current_code(ui)), timeout=1)
    assert "客户端下载信息暂不可用" in ui.last_matcher.sent[-1]
    assert "尚未确认完成" in message
    ui.service.confirm.assert_awaited_once()


@pytest.mark.asyncio
async def test_client_notice_timeout_does_not_block_install_observation(ui, monkeypatch):
    await choose_release(ui)
    monkeypatch.setattr(ui.commands, "CLIENT_NOTICE_TIMEOUT", .01)
    matcher = Matcher()

    async def send(text):
        if "下载入口" in str(text):
            await asyncio.Event().wait()
        matcher.sent.append(str(text))

    matcher.send = send
    message = await asyncio.wait_for(invoke(ui, "confirm", current_code(ui), matcher=matcher), timeout=1)
    assert "尚未确认完成" in message
    assert matcher.sent == list(PROGRESS_MESSAGES)
    ui.service.confirm.assert_awaited_once()
    ui.service.maintenance.begin.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["permission", "identity", "scope"])
async def test_auto_client_notice_rechecks_identity_permission_and_original_scope(ui, changed):
    await choose_release(ui)
    info = dict(ui.service.client_download.return_value)

    async def download(*args, **kwargs):
        if changed == "permission":
            ui.allowed[0] = False
        elif changed == "identity":
            ui.service.current.side_effect = ui.commands.MinekuaiError("binding changed")
        else:
            ui.event.group_id = 999
        return info

    ui.service.client_download.side_effect = download
    await invoke(ui, "confirm", current_code(ui))
    assert ui.last_matcher.sent == list(PROGRESS_MESSAGES)
    ui.service.confirm.assert_awaited_once()


@pytest.mark.asyncio
async def test_repeated_submission_callback_does_not_duplicate_notice(ui):
    await choose_release(ui)

    async def confirm(scope, code, *, on_submitted, **kwargs):
        pending = ui.service.confirms.consume(scope, code)
        ui.service.maintenance.begin(pending.server, pending.choice)
        await on_submitted(pending)
        await on_submitted(pending)
        return pending

    ui.service.confirm.side_effect = confirm
    await invoke(ui, "confirm", current_code(ui))
    ui.service.client_download.assert_awaited_once()
    assert len(ui.last_matcher.sent) == 1
    assert "下载入口" in ui.last_matcher.sent[0]


def test_production_registration_uses_normal_access_for_client_download():
    tree = ast.parse((PLUGIN / "__init__.py").read_text(encoding="utf-8"))
    registration = next(call for call in ast.walk(tree) if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name) and call.func.id == "register_modpack_commands")
    permission = next(keyword.value for keyword in registration.keywords if keyword.arg == "check_permission")
    assert isinstance(permission, ast.Name) and permission.id == "_check_perm"
