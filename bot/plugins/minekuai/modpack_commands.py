"""QQ modpack selection UI. Installation is a separate, scoped command."""
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


def register_modpack_commands(*, servers, service, check_admin, refresh_factory,
                              audit, display_name):
    """Register once from the plugin after its shared helpers are defined."""
    change = on_command("更换整合包", aliases={"切换整合包"}, priority=5, block=True)
    confirm = on_command("确认清空安装", priority=4, block=True)
    cancel = on_command("取消更换整合包", priority=4, block=True)
    status = on_command("整合包状态", priority=5, block=True)
    finish = on_command("结束整合包维护", priority=5, block=True)

    async def send_end(matcher, text):
        await matcher.finish(MessageSegment.text(text))

    async def admin(matcher, event):
        ok, reason = check_admin(event)
        if not ok:
            await send_end(matcher, reason)

    async def failed(matcher, exc, *, installation=False):
        if isinstance(exc, EXPECTED_ERRORS):
            message = str(exc)
        else:
            logger.error("整合包操作异常: {}", type(exc).__name__)
            message = "操作异常，请查看机器人日志或官网状态"
        if installation:
            warnings = []
            if not any(text in message for text in (
                "维护保护保留", "维护保护会保留", "维护保护仍保留", "维护保护仍然保留",
            )):
                warnings.append("若已开始开卡或安装，维护保护会保留。")
            if "计费" not in message and "消耗时长" not in message:
                warnings.append("计时卡可能已开启并继续消耗时长。")
            if "不会自动关卡" not in message and "不自动关卡" not in message:
                warnings.append("机器人不会自动关卡。")
            if warnings:
                message += "\n" + "".join(warnings)
            message += ("\n先用『整合包状态 <服务器>』并到官网核对安装与计费；"
                        "任务结束后手动关卡，再发『结束整合包维护 <服务器> 我已核对』。"
                        "请勿重复安装。")
        await send_end(matcher, "❌ " + message)

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
            "请在官网确认任务结束后手动关闭计时卡、解除保护。\n"
            "仅原发起人在本会话 5 分钟内发送：\n"
            f"确认清空安装 {pending.code}\n"
            "不更换请发：取消更换整合包")

    @confirm.handle()
    async def install(matcher: Matcher, bot: Bot, event: MessageEvent, args: Message = CommandArg()):
        await admin(matcher, event)
        try:
            pending = await service.confirm(scope_of(bot, event), args.extract_plain_text().strip(),
                authorized=lambda: check_admin(event)[0], refresh=refresh_factory(matcher, event),
                progress=lambda text: matcher.send(MessageSegment.text(text)))
        except Exception as exc:
            audit(event.user_id, display_name(event), getattr(event, "group_id", None),
                  "switch_modpack", False, type(exc).__name__)
            await failed(matcher, exc, installation=True)
        audit(event.user_id, display_name(event), getattr(event, "group_id", None),
              f"switch_modpack {pending.server.name}", True, f"submitted item={pending.choice.item_id}")
        await send_end(matcher, f"✅ 『{sanitize_display(pending.server.name)}』安装请求已提交，尚未确认完成。\n"
            "维护保护已开启：机器人暂停该实例的开关服、重启、控制台和聊天桥写入。\n"
            "本次流程先开启计时卡，平台若自动启动则正常停服后再安装；没有主动发送游戏启动指令或强杀。\n"
            "计费可能仍在继续，机器人不会自动关卡，请到官网核对。\n"
            f"查看：整合包状态 {pending.server.name}\n"
            "请在官网确认安装已结束（完成或失败），检查文件与 Java 版本，按需手动关闭计时卡后，再发送：\n"
            f"结束整合包维护 {pending.server.name} 我已核对")

    @cancel.handle()
    async def cancel_install(matcher: Matcher, bot: Bot, event: MessageEvent):
        await admin(matcher, event)
        removed = service.confirms.cancel(scope_of(bot, event))
        await send_end(matcher, "已取消待确认的整合包更换。" if removed else "没有待确认的安装。已执行的开卡或安装不能用此指令撤销；请到官网核对计费和安装状态。")

    @status.handle()
    async def inspect(matcher: Matcher, event: MessageEvent, args: Message = CommandArg()):
        await admin(matcher, event)
        server = await resolve(matcher, args.extract_plain_text().strip())
        lines = [f"『{sanitize_display(server.name)}』整合包状态"]
        try:
            entry = service.maintenance.get(server.instance_uuid)
            if entry:
                phase = {"preparing": "开卡或提交前/流程可能中断", "submitted": "请求已提交", "unknown": "请求结果不确定"}.get(entry["phase"], "未知")
                lines.extend([f"维护保护：开启（{phase}，不代表安装成功）",
                              f"本次选择：{entry['pack_name']} · {entry['pack_version']}"])
            else:
                lines.append("维护保护：未开启（不代表当前安装状态）")
            info, _ = await service.read(server, lambda p: p.get_server_info(server.instance_uuid),
                panel=True, refresh=refresh_factory(matcher, event))
            attr = info.get("attributes", {})
            lines.append(f"官网实例状态：{sanitize_display(attr.get('status')) or '未标记安装任务'}")
            lines.append(f"官网安装中：{'是' if attr.get('is_installing') else '否/未标记'}")
        except Exception as exc:
            lines.append("官网查询未完成：" + (str(exc) if isinstance(exc, EXPECTED_ERRORS) else type(exc).__name__))
        lines.append("这不是已安装整合包的识别结果；请在官网核对安装结果与文件。")
        lines.append("计时卡可能已开启并继续计费；机器人不会自动关卡。请在官网核对，确认任务结束后手动关卡、解除维护保护。")
        await send_end(matcher, "\n".join(lines))

    @finish.handle()
    async def release(matcher: Matcher, event: MessageEvent, args: Message = CommandArg()):
        await admin(matcher, event)
        parts = args.extract_plain_text().strip().rsplit(maxsplit=1)
        if len(parts) != 2 or parts[1] != "我已核对":
            await send_end(matcher, "请先在官网确认安装已完成、失败或未发生，且已结束。核对文件、Java 配置与计费状态，按需手动关闭计时卡后发送：\n结束整合包维护 <服务器> 我已核对")
        server = await resolve(matcher, parts[0])
        try:
            await service.finish_maintenance(server, authorized=lambda: check_admin(event)[0],
                                             refresh=refresh_factory(matcher, event))
        except Exception as exc:
            await failed(matcher, exc)
        audit(event.user_id, display_name(event), getattr(event, "group_id", None),
              f"finish_modpack_maintenance {server.name}", True, "user acknowledged website check")
        await send_end(matcher, "维护保护已解除；机器人没有自动开服，也未据此判定安装成功。计时卡没有被自动关闭，计费可能继续，请到官网核对并按需手动关卡。")

    return {"change": change, "confirm": confirm, "cancel": cancel, "status": status, "finish": finish}
