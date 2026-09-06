"""QQ modpack selection UI. Installation is a separate, scoped command."""
import asyncio
from dataclasses import asdict
import time

from loguru import logger
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent, MessageSegment
from nonebot.matcher import Matcher
from nonebot.params import ArgPlainText, CommandArg

from .client import MinekuaiError
from .modpack_catalog import CatalogError, sanitize_display
from .modpack_state import ConfirmError, InstallChoice, MaintenanceError, ServerIdentity
from .operations import OperationBusyError


EXPECTED_ERRORS = (MinekuaiError, CatalogError, ConfirmError, MaintenanceError, OperationBusyError)
CANCEL = {"取消", "cancel", "取消更换整合包"}
INSTALL_OUTCOME_LABELS = {
    "completed": "已确认完成", "failed": "平台报告失败",
    "installing": "仍在安装中", "unknown": "尚未确认结果", "not_submitted": "未提交安装",
}
CLIENT_METADATA_TIMEOUT = 12.0
CLIENT_NOTICE_TIMEOUT = 5.0


def scope_of(bot, event):
    return int(bot.self_id), int(event.user_id), getattr(event, "group_id", None)


def pack_line(number, item):
    unavailable = "（无可安装文件）" if not item.installable else ""
    return (f"{number}. {item.name or '未命名'} · {item.version or '未标注版本'}"
            f" | MC {item.game_version or '?'} / Java {item.java_version or '?'}{unavailable}")


def catalog_prompt(items, total, page, *, project=None):
    lines = [f"{'版本' if project else '整合包'}列表 · 第 {page} 页 / 共 {max(1, (total + 8) // 9)} 页"]
    if project:
        lines.append(pack_line(0, project) + "（主目录版本）")
    lines.extend(pack_line(i, item) for i, item in enumerate(items, 1))
    if not items:
        lines.append("本页没有条目。")
    lines.append("回复编号选择；下一页 / 上一页；取消退出。")
    return "\n".join(lines)


def client_download_text(server, info, *, selected=False):
    """Render service-validated download metadata as text, never as a file upload."""
    if not isinstance(info, dict) or not isinstance(info.get("url", ""), str):
        raise MinekuaiError("客户端下载信息格式异常")
    name = sanitize_display(info.get("name", ""), 100) or "未标注整合包"
    version = sanitize_display(info.get("version", ""), 80) or "未标注版本"
    lines = [f"『{sanitize_display(server.name)}』客户端下载信息（下载入口，未上传群文件）",
             f"整合包：{name}\n版本：{version}",
             f"MC：{sanitize_display(info.get('game_version', ''), 40) or '?'} / "
             f"Java：{sanitize_display(info.get('java_version', ''), 40) or '?'}"]
    if selected:
        lines.append("所选版本，不代表安装已成功。")
    else:
        lines.append("根据最近一次整合包选择记录提供，不代表已识别当前文件或安装成功。")
    url = info.get("url", "")
    if url:
        if info.get("exact") is True:
            lines.append("该版本客户端下载链接：")
        else:
            lines.extend(["官方免费客户端目录（非该版本直链）：",
                          "请按上方整合包名称和版本在目录内查找，目录不保证提供该版本。"])
        lines.append(url)
        if info.get("code"):
            lines.append("提取码：" + sanitize_display(info["code"], 80))
    else:
        lines.append("暂未提供可用的客户端下载链接。")
    if info.get("detail"):
        lines.append(sanitize_display(info["detail"], 320))
    return "\n".join(lines)


def register_modpack_commands(*, servers, service, check_admin, refresh_factory,
                              audit, display_name, check_permission=None):
    """Register once from the plugin after its shared helpers are defined."""
    change = on_command("更换整合包", aliases={"切换整合包"}, priority=5, block=True)
    confirm = on_command("确认清空安装", priority=4, block=True)
    cancel = on_command("取消更换整合包", priority=4, block=True)
    status = on_command("整合包状态", priority=5, block=True)
    finish = on_command("结束整合包维护", priority=5, block=True)
    logs = on_command("整合包日志", priority=5, block=True)
    client_download = on_command("整合包客户端", aliases={"客户端", "下载客户端"}, priority=5, block=True)
    check_permission = check_admin if check_permission is None else check_permission

    async def send_end(matcher, text):
        await matcher.finish(MessageSegment.text(text))

    async def admin(matcher, event):
        ok, reason = check_admin(event)
        if not ok:
            await send_end(matcher, reason)

    async def reader(matcher, event):
        ok, reason = check_permission(event)
        if not ok:
            await send_end(matcher, reason or "没有使用该指令的权限")

    def client_unavailable(server):
        return (f"客户端下载信息暂不可用，可发『整合包客户端 {sanitize_display(server.name)}』重试；"
                "不影响安装，请勿重复安装。")

    async def failed(matcher, exc, *, installation=False):
        if isinstance(exc, EXPECTED_ERRORS):
            message = str(exc)
        else:
            logger.error("整合包操作异常: {}", type(exc).__name__)
            message = "操作异常，请稍后查询整合包状态和日志"
        uncertain_install = (
            installation and not isinstance(exc, ConfirmError)
            and "未提交" not in message and "安装失败" not in message
        )
        if installation:
            if uncertain_install and "可能已受理" not in message:
                message += "\n安装请求可能已受理，请用状态查询确认，不要再次提交。"
            warnings = []
            if not any(text in message for text in (
                "维护保护保留", "维护保护会保留", "维护保护仍保留", "维护保护仍然保留",
            )):
                warnings.append("结果未确认时，维护保护会保留。")
            if "计费" not in message and "消耗时长" not in message:
                warnings.append("计时卡可能已开启并继续消耗时长，请查询计费状态。")
            if "不会自动关卡" not in message and "不自动关卡" not in message:
                warnings.append("机器人不会自动关卡。")
            if warnings:
                message += "\n" + "".join(warnings)
            message += ("\n请在群内查询『整合包状态 <服务器>』『整合包日志 <服务器>』；"
                        "确认任务安全结束后会自动解除保护，也可发『结束整合包维护 <服务器>』重新核对。"
                        "请勿重复安装。")
        await send_end(matcher, ("⚠️ " if uncertain_install else "❌ ") + message)

    def billing_line(billing):
        if billing is True:
            return "计费：已开启，正在消耗时长。"
        if billing is False:
            return "计费：本次查询显示未开启。"
        return "计费：尚未确认，可能仍在消耗时长，请稍后用整合包状态查询。"

    def next_steps(server, protected, outcome):
        name = sanitize_display(server.name)
        lines = ["机器人不会自动关卡，也不会自动启动游戏。",
                 f"查询：整合包状态 {name}｜整合包日志 {name}"]
        if protected:
            lines.append("维护保护仍在：暂停开关服、重启、控制台和聊天桥写入。继续查询即可，结果未知时不要重新安装。")
        else:
            if outcome == "completed":
                lines.append(f"要进入游戏请发：开服 {name}（安装完成不代表游戏已经启动或可以进入）。")
            lines.append(f"不需要继续计费请发：关服 {name}；若提示确认，再发：确认关服。")
        return lines

    async def maintenance_report(matcher, event, server):
        report = await service.reconcile_maintenance(
            server, authorized=lambda: check_admin(event)[0], refresh=refresh_factory(matcher, event))
        if (not isinstance(report, dict) or type(report.get("maintenance")) is not bool
                or type(report.get("released")) is not bool
                or (report["released"] and report["maintenance"])):
            raise MinekuaiError("维护核对结果格式异常，请重新查询；未确认解除保护")
        return report

    def report_lines(server, report, entry):
        lines = []
        if report["released"]:
            lines.append("维护保护：已自动解除，核对结果已归档。")
        elif report["maintenance"]:
            lines.append("维护保护：保留，尚未确认任务安全结束。")
        else:
            lines.append("维护保护：未开启；已有结束记录仍可查询。")
        if entry:
            lines.append(f"记录选择：{sanitize_display(entry.get('pack_name', ''))} · {sanitize_display(entry.get('pack_version', ''))}")
        outcome = report.get("outcome", "unknown")
        lines.append("本次安装观察：" + INSTALL_OUTCOME_LABELS.get(outcome, INSTALL_OUTCOME_LABELS["unknown"]))
        if report.get("detail"):
            lines.append(sanitize_display(report["detail"], 320))
        lines.append(billing_line(report.get("billing_active")))
        lines.append("“记录选择”只是安装记录，不代表已识别当前整合包。")
        lines.extend(next_steps(server, report["maintenance"], outcome))
        return lines

    async def session(matcher, bot, event, answer=""):
        await admin(matcher, event)
        state = matcher.state
        if state.get("mp_scope") != scope_of(bot, event):
            await send_end(matcher, "操作会话不匹配，请在原会话继续或重新发起")
        if time.monotonic() - state.get("mp_started", 0) > 300:
            await send_end(matcher, "选择已超时，请重新发送『更换整合包』")
        if answer.strip().casefold() in CANCEL:
            service.confirms.cancel(scope_of(bot, event))
            await send_end(matcher, "已取消更换整合包，未提交安装。")

    async def selected_server(matcher):
        try:
            return service.current(matcher.state["mp_identity"])
        except Exception as exc:
            await failed(matcher, exc)

    async def resolve(matcher, name):
        configured = servers.list_servers()
        if not name and len(configured) == 1:
            return configured[0]
        server = servers.get_server(name) if name else None
        if server is None:
            names = "、".join(sanitize_display(s.name) for s in configured) or "暂无"
            await send_end(matcher, f"请填写服务器名字。已配置：{names}")
        return server

    @change.handle()
    async def begin(matcher: Matcher, bot: Bot, event: MessageEvent, args: Message = CommandArg()):
        await admin(matcher, event)
        service.confirms.cancel(scope_of(bot, event))
        matcher.state["mp_scope"] = scope_of(bot, event)
        matcher.state["mp_started"] = time.monotonic()
        parts = args.extract_plain_text().strip().split(maxsplit=1)
        if parts:
            matcher.set_arg("mp_name", Message(MessageSegment.text(parts[0])))
            if len(parts) == 2:
                matcher.set_arg("mp_query", Message(MessageSegment.text(parts[1])))
        else:
            configured = servers.list_servers()
            if len(configured) == 1:
                matcher.set_arg("mp_name", Message(MessageSegment.text(configured[0].name)))
            else:
                await matcher.send(MessageSegment.text("已配置：" + "、".join(sanitize_display(s.name) for s in configured)))

    @change.got("mp_name", prompt="要更换哪台服务器的整合包？回复服务器名字，或取消。")
    async def choose_server(matcher: Matcher, bot: Bot, event: MessageEvent, name: str = ArgPlainText("mp_name")):
        await session(matcher, bot, event, name)
        server = await resolve(matcher, name.strip())
        try:
            await service.reconcile_for_operation(
                server, authorized=lambda: check_admin(event)[0], refresh=refresh_factory(matcher, event))
            service.maintenance.ensure_card_available(server.card_id)
            server, _ = await service.preflight(server, refresh_factory(matcher, event))
        except Exception as exc:
            await failed(matcher, exc)
        matcher.state["mp_identity"] = ServerIdentity.from_server(server)

    @change.got("mp_query", prompt="输入整合包关键词，例如 ATM、机械动力。确认更换将先开启计时卡并消耗时长，再清空安装；全部文件会被覆盖，请先自行备份；取消可退出。")
    async def search(matcher: Matcher, bot: Bot, event: MessageEvent, query: str = ArgPlainText("mp_query")):
        await session(matcher, bot, event, query)
        query = query.strip()
        if not 1 <= len(query) <= 100:
            await change.reject("请输入 1—100 字的整合包关键词，或取消。")
        server = await selected_server(matcher)
        try:
            items, total = await service.search(server, query, refresh=refresh_factory(matcher, event))
        except Exception as exc:
            await failed(matcher, exc)
        if not items:
            await change.reject("没找到整合包，请换一个关键词，或取消。")
        matcher.state.update(mp_query_text=query, mp_page=1, mp_items=items, mp_total=total)
        await matcher.send(MessageSegment.text(catalog_prompt(items, total, 1)))

    @change.got("mp_pack")
    async def choose_pack(matcher: Matcher, bot: Bot, event: MessageEvent, answer: str = ArgPlainText("mp_pack")):
        await session(matcher, bot, event, answer)
        answer, state = answer.strip(), matcher.state
        server = await selected_server(matcher)
        if answer in {"下一页", "上一页"}:
            page = state["mp_page"] + (1 if answer == "下一页" else -1)
            if not 1 <= page <= min(1000, max(1, (state["mp_total"] + 8) // 9)):
                await change.reject("已经到边界了，请回复本页编号，或取消。")
            try:
                items, total = await service.search(server, state["mp_query_text"], page, refresh_factory(matcher, event))
            except Exception as exc:
                await failed(matcher, exc)
            state.update(mp_page=page, mp_items=items, mp_total=total)
            await change.reject(MessageSegment.text(catalog_prompt(items, total, page)))
        if answer not in {str(i) for i in range(1, len(state["mp_items"]) + 1)}:
            await change.reject("请回复本页整合包编号、下一页、上一页或取消。")
        project = state["mp_items"][int(answer) - 1]
        try:
            items, total = await service.versions(server, project.item_id, refresh=refresh_factory(matcher, event))
        except Exception as exc:
            await failed(matcher, exc)
        state.update(mp_project=project, mp_vpage=1, mp_versions=items, mp_vtotal=total)
        await matcher.send(MessageSegment.text(catalog_prompt(items, total, 1, project=project)))

    @change.got("mp_version")
    async def choose_version(matcher: Matcher, bot: Bot, event: MessageEvent, answer: str = ArgPlainText("mp_version")):
        await session(matcher, bot, event, answer)
        answer, state = answer.strip(), matcher.state
        server = await selected_server(matcher)
        if answer in {"下一页", "上一页"}:
            page = state["mp_vpage"] + (1 if answer == "下一页" else -1)
            if not 1 <= page <= min(1000, max(1, (state["mp_vtotal"] + 8) // 9)):
                await change.reject("已经到边界了，请回复本页版本编号，或取消。")
            try:
                items, total = await service.versions(server, state["mp_project"].item_id, page, refresh_factory(matcher, event))
            except Exception as exc:
                await failed(matcher, exc)
            state.update(mp_vpage=page, mp_versions=items, mp_vtotal=total)
            await change.reject(MessageSegment.text(catalog_prompt(items, total, page, project=state["mp_project"])))
        if answer not in {str(i) for i in range(len(state["mp_versions"]) + 1)}:
            await change.reject("请回复版本编号（0 为主目录版本）、下一页、上一页或取消。")
        index = int(answer)
        item = state["mp_project"] if index == 0 else state["mp_versions"][index - 1]
        if not item.installable:
            await change.reject("该版本没有可安装文件，请选择其他版本，或取消。")
        choice = InstallChoice(**asdict(item), search_query=state["mp_query_text"],
                               search_page=state["mp_page"], version_page=0 if index == 0 else state["mp_vpage"])
        try:
            await service.reconcile_for_operation(
                server, authorized=lambda: check_admin(event)[0], refresh=refresh_factory(matcher, event))
            pending = await service.prepare(scope_of(bot, event), server, choice, refresh_factory(matcher, event))
        except Exception as exc:
            await failed(matcher, exc)
        await send_end(matcher,
            f"⚠️ 更换整合包 · 最终确认\n服务器『{sanitize_display(server.name)}』 / {server.instance_uuid}\n"
            f"整合包：{choice.name}\n版本：{choice.version or '未标注'}\n"
            f"MC：{choice.game_version or '未标注'} / Java：{choice.java_version or '未标注'}\n"
            "将覆盖全部文件，包括世界存档！机器人不会自动备份。请先自行备份。\n"
            "确认同时授权：开启计时卡（消耗时长）＋清空安装。\n"
            "先建立维护保护并开卡；若平台自动启动实例，会尝试一次正常停服。\n"
            "只读确认实例已解冻且离线后，才提交一次覆盖安装请求。\n"
            "开卡或就绪检查失败不会提交安装，但维护保护会保留。\n"
            "不会主动发送游戏启动指令或强杀；也不会自动关卡，计费可能持续。\n"
            "机器人会观察安装结果，确认任务安全结束后自动解除保护并保存记录。\n"
            "超时或结果不明可在群内查『整合包状态』『整合包日志』，不会重复安装。\n"
            "仅原发起人在本会话 5 分钟内发送：\n"
            f"确认清空安装 {pending.code}\n"
            "不更换请发：取消更换整合包")

    @confirm.handle()
    async def install(matcher: Matcher, bot: Bot, event: MessageEvent, args: Message = CommandArg()):
        await admin(matcher, event)
        notice_attempted = False

        async def on_submitted(pending):
            nonlocal notice_attempted
            if notice_attempted:
                return
            notice_attempted = True
            original_scope = scope_of(bot, event)

            def current_recipient():
                if pending.scope != original_scope or scope_of(bot, event) != original_scope or not check_admin(event)[0]:
                    raise ConfirmError("原会话或权限已变化")
                return service.current(pending.server)

            try:
                server = current_recipient()
            except Exception:
                return
            try:
                info = await asyncio.wait_for(service.client_download(
                    server, choice=pending.choice, refresh=None), timeout=CLIENT_METADATA_TIMEOUT)
                server = current_recipient()
                text = client_download_text(server, info, selected=True)
            except Exception:
                try:
                    server = current_recipient()
                except Exception:
                    return
                text = client_unavailable(server)
            try:
                await asyncio.wait_for(matcher.send(MessageSegment.text(text)), timeout=CLIENT_NOTICE_TIMEOUT)
            except Exception:
                logger.warning("客户端链接通知未发送；不影响安装结果观察")

        try:
            pending = await service.confirm(scope_of(bot, event), args.extract_plain_text().strip(),
                authorized=lambda: check_admin(event)[0], refresh=refresh_factory(matcher, event),
                progress=lambda text: matcher.send(MessageSegment.text(text)), on_submitted=on_submitted)
        except Exception as exc:
            audit(event.user_id, display_name(event), getattr(event, "group_id", None),
                  "switch_modpack", False, type(exc).__name__)
            await failed(matcher, exc, installation=True)
        audit(event.user_id, display_name(event), getattr(event, "group_id", None),
              f"switch_modpack {pending.server.name}", True, f"submitted item={pending.choice.item_id}")
        try:
            entry = service.maintenance.latest(pending.server.instance_uuid) or {}
            outcome = entry.get("install_outcome", "unknown")
            protected = not (type(entry.get("released_at")) is int and entry["released_at"] > 0)
        except Exception:
            outcome = "unknown"
            protected = True
            logger.warning("无法读取本次安装观察结果，请通过状态指令继续核对")
        result_line = {
            "completed": "✅ 本次维护期间的安装已确认完成；这不代表游戏已经启动或可以进入。",
            "failed": "❌ 平台报告本次安装失败，请查整合包日志，勿直接重复安装。",
            "installing": "⏳ 安装请求已提交，平台仍在安装中。",
        }.get(outcome, "⏳ 安装请求已提交，尚未确认完成，请用状态指令继续观察。")
        lines = [f"『{sanitize_display(pending.server.name)}』{result_line}",
            "维护保护已开启，等待安全确认。" if protected else "维护保护已自动解除，安装结果已归档。",
            "本次流程先开启计时卡，平台若自动启动则正常停服后再安装；没有主动发送游戏启动指令或强杀。\n"
            "计费可能仍在继续，机器人不会自动关卡。"]
        lines.extend(next_steps(pending.server, protected, outcome))
        await send_end(matcher, "\n".join(lines))

    @cancel.handle()
    async def cancel_install(matcher: Matcher, bot: Bot, event: MessageEvent):
        await admin(matcher, event)
        removed = service.confirms.cancel(scope_of(bot, event))
        await send_end(matcher, "已取消待确认的整合包更换。" if removed else "没有待确认的安装。已执行的开卡或安装不能用此指令撤销；请用『整合包状态』『整合包日志』查询。")

    @status.handle()
    async def inspect(matcher: Matcher, event: MessageEvent, args: Message = CommandArg()):
        await admin(matcher, event)
        server = await resolve(matcher, args.extract_plain_text().strip())
        lines = [f"『{sanitize_display(server.name)}』整合包状态"]
        try:
            report = await maintenance_report(matcher, event, server)
            entry = service.maintenance.latest(server.instance_uuid)
            lines.extend(report_lines(server, report, entry))
        except Exception as exc:
            lines.append("状态核对未完成：" + (str(exc) if isinstance(exc, EXPECTED_ERRORS) else type(exc).__name__))
            lines.append("尚未确认解除保护或计费状态；请稍后查询整合包状态和日志，不要重新安装。机器人不会自动关卡。")
        await send_end(matcher, "\n".join(lines))

    @finish.handle()
    async def release(matcher: Matcher, event: MessageEvent, args: Message = CommandArg()):
        await admin(matcher, event)
        name = args.extract_plain_text().strip()
        parts = name.rsplit(maxsplit=1)
        if parts and parts[-1] == "我已核对":
            name = parts[0] if len(parts) == 2 else ""
        server = await resolve(matcher, name)
        try:
            report = await maintenance_report(matcher, event, server)
            entry = service.maintenance.latest(server.instance_uuid)
        except Exception as exc:
            await failed(matcher, exc)
        audit(event.user_id, display_name(event), getattr(event, "group_id", None),
              f"finish_modpack_maintenance {server.name}", not report["maintenance"],
              f"reconciled outcome={report.get('outcome', 'unknown')}")
        await send_end(matcher, "\n".join([f"『{sanitize_display(server.name)}』维护核对",
                                         *report_lines(server, report, entry)]))

    @logs.handle()
    async def read_install_log(matcher: Matcher, event: MessageEvent, args: Message = CommandArg()):
        await admin(matcher, event)
        server = await resolve(matcher, args.extract_plain_text().strip())
        try:
            text = await service.install_log(server, refresh=refresh_factory(matcher, event))
            if not isinstance(text, str):
                raise MinekuaiError("安装日志格式异常，请稍后重试")
        except Exception as exc:
            await failed(matcher, exc)
        await send_end(matcher, f"『{sanitize_display(server.name)}』安装日志（已脱敏，最近片段）\n"
                       f"{text[:3000] or '暂未读取到安装日志。'}\n"
                       f"状态及维护保护请发：整合包状态 {sanitize_display(server.name)}")

    @client_download.handle()
    async def read_client_download(matcher: Matcher, event: MessageEvent, args: Message = CommandArg()):
        await reader(matcher, event)
        server = await resolve(matcher, args.extract_plain_text().strip())
        identity = ServerIdentity.from_server(server)
        try:
            info = await asyncio.wait_for(service.client_download(
                server, refresh=refresh_factory(matcher, event)), timeout=CLIENT_METADATA_TIMEOUT)
            server = service.current(identity)
            text = client_download_text(server, info)
        except Exception:
            text = client_unavailable(server)
        await reader(matcher, event)
        try:
            service.current(identity)
        except Exception:
            await send_end(matcher, "服务器绑定已变化，请重新查询整合包客户端")
        await send_end(matcher, text)

    return {"change": change, "confirm": confirm, "cancel": cancel, "status": status,
            "finish": finish, "logs": logs, "client": client_download}
