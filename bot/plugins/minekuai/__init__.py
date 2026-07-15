"""
麦块联机 QQ Bot 插件主入口（多服务器版）

注册的指令（默认无前缀）：

【开关】
  开服 [<名字>] / 开机 / start    → 打开计时卡（多台时不带名字会列表询问）
  关服 [<名字>] / 关机 / stop     → 进入二次确认（默认开启）
  确认关服                        → 确认上一条关服请求

【查询】
  在线 [<名字>] / 状态            → SLP ping 查在线玩家数
  查服 [<名字>] / info            → 状态 / CPU / 内存 / 模组数 / 端口
  模组 [<名字>] / mods            → 列出 /mods 下的 .jar
  插件 [<名字>] / plugins         → 列出 /plugins 下的 .jar
  日志 [<名字>] [<行数>]          → 最近 N 行控制台日志(默认 30,上限 200)

【运维】
  重启 [<名字>] / restart         → 实例重启(不动计时卡)
  指令 [<名字>] <命令>            → 发到游戏控制台(如 op X)

【玩家绑定 + 统计】
  绑定 <游戏名>                   → 绑 QQ↔游戏名(死亡/成就播报会 @ 你)
  解绑 / 绑定列表
  今日榜 / 本周榜                 → 在线时长排行
  在线时长 [<游戏名>]             → 查个人时长(绑过可省略游戏名)
  死亡榜 / 死亡次数 [<游戏名>]     → 死亡统计

【自动播报(无需指令)】
  玩家加入/离开/死亡/成就 → 自动播报到群(死亡/成就 @ 绑定的 QQ)
  游戏内聊天 ↔ QQ 群     → 双向转发(聊天桥)
  CPU/内存 持续超阈值     → 自动告警 @ 管理员

【账号管理（必须先有账号才能加服务器）】
  添加账号 / 加账号               → 多步交互：手机号 + 密码
  账号列表                        → 列出所有账号（密码不显示）
  删除账号 <手机号>               → 删

【服务器管理】
  服务器列表 / 列表 / list        → 列出所有已配置的
  服务器地址 [<名字>]             → 查询连接地址
  添加服务器 / 添加               → 5 步：名字/卡 ID/地址/UUID/账号
                                    bot 自动登录获取 token/clientid
  删除服务器 <名字> / 删除 <名字> → 删（需要回复『确认』再生效）
  修改服务器名字 / 重命名 / 改名  → 改名（支持 inline: 重命名 旧名 新名）
  修改地址 [<名字> [<地址>]]      → 改地址
  修改uuid [<名字> [<UUID>]]      → 改实例 ID
  绑定账号 <服务器> <手机号>      → 改绑账号
  更新token <名字>                → 自动续期失效时手动应急

【其它】
  取消                            → 中止当前正在进行的多步交互
  帮助 / help                     → 显示指令列表

token 失效时：如果服务器绑了账号，bot 会用 Playwright 自动登录刷新；
没绑账号则提示用户『更新token <名字>』手动更新。
"""
import re
from pathlib import Path

from nonebot import (
    get_driver, get_plugin_config, on_command, on_fullmatch, on_message,
)
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
)
from nonebot.exception import MatcherException
from nonebot.matcher import Matcher
from nonebot.params import ArgPlainText, CommandArg
from nonebot.plugin import PluginMetadata
from loguru import logger

from . import auth, idle_watcher, mc_profile, servers
from .audit import init_db as init_audit_db, log_operation
from .client import (
    AuthError,
    MinekuaiClient,
    MinekuaiError,
    PanelClient,
    RateLimitError,
)
from .config import Config
from .permission import (
    check_cooldown,
    consume_pending_confirm,
    is_user_allowed,
    mark_pending_confirm,
    update_cooldown,
)


__plugin_meta__ = PluginMetadata(
    name="麦块联机控制",
    description="多服务器 QQ Bot：开关计时卡 + 运行时管理",
    usage="发送 帮助 查看指令",
    type="application",
)

config = get_plugin_config(Config)
init_audit_db()
servers.init_db()
# 兼容：从 .env 旧字段自动迁移单台配置（仅在数据库为空时生效）
servers.maybe_migrate_from_env(
    config.minekuai_token,
    config.minekuai_client_id,
    config.minekuai_card_id,
)


CANCEL_WORDS = {"取消", "cancel", "Cancel", "CANCEL"}


# ============================================================
# bot 连接时启动空闲监视器
# ============================================================

_driver = get_driver()
_idle_task = None  # 持有 task 避免被 GC


@_driver.on_bot_connect
async def _on_bot_connect(bot: Bot):
    """bot 连上后启动 idle_watcher（每次重连都重置一下）"""
    global _idle_task
    idle_watcher.register_bot(bot, config.allowed_groups)
    idle_watcher.register_close_callback(_auto_close_callback)
    idle_watcher.register_start_callback(_auto_start_callback)
    idle_watcher.register_config(config)
    idle_watcher.register_panel_runner(_panel_run_bg)
    if _idle_task is None or _idle_task.done():
        _idle_task = idle_watcher.start_watcher()
    await _maybe_send_changelog(bot)


async def _maybe_send_changelog(bot: Bot) -> None:
    """如果存在待发公告文件,发到允许的群一次,然后删除(避免重启重发)。"""
    import os
    path = "/app/data/.changelog_to_send"
    try:
        if not os.path.isfile(path):
            return
        with open(path, encoding="utf-8") as f:
            text = f.read().strip()
        if text:
            for gid in config.allowed_groups:
                await bot.send_group_msg(group_id=gid, message=text)
            logger.info(f"已发送更新公告到 {len(config.allowed_groups)} 个群")
        os.remove(path)
    except Exception:
        logger.exception("发送更新公告失败")


async def _auto_close_callback(server: servers.Server) -> tuple[bool, str]:
    """被 idle_watcher 调用执行实际关停。返回 (成功?, 状态消息)"""
    try:
        async with _build_client(server) as client:
            await client.close_server(card_id=server.card_id)
        log_operation(
            0, "idle_watcher", None,
            f"auto_close {server.name}", True, "idle timeout",
        )
        return True, "已关闭计时卡"
    except AuthError as e:
        # 自动续期一次再试
        if server.account_phone:
            ok, msg = await _refresh_token_for(server)
            if ok:
                fresh = servers.get_server(server.name)
                if fresh:
                    try:
                        async with _build_client(fresh) as client:
                            await client.close_server(card_id=fresh.card_id)
                        log_operation(
                            0, "idle_watcher", None,
                            f"auto_close {server.name}", True,
                            "after refresh",
                        )
                        return True, "已关闭计时卡（刷新 token 后）"
                    except Exception as e2:
                        return False, f"刷新后仍失败: {e2}"
            return False, f"token 失效，刷新也失败：{msg}"
        return False, f"token 失效: {e}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


async def _auto_start_callback(server: servers.Server) -> tuple[bool, str]:
    """被 idle_watcher 调用执行保活启动。返回 (成功?, 状态消息)。"""
    try:
        if (not server.token or not server.client_id) and server.account_phone:
            ok, msg = await _refresh_token_for(server)
            if not ok:
                log_operation(
                    0, "keepalive", None,
                    f"keepalive_start {server.name}", False,
                    f"initial refresh failed: {msg}",
                )
                return False, f"自动获取 token 失败: {msg}"
            server = servers.get_server(server.name) or server

        if not server.token or not server.client_id:
            return False, "缺少 token/client_id，且未绑定可自动登录的账号"

        refresh_attempted = False
        while True:
            try:
                async with _build_client(server) as client:
                    await client.open_timing_only(card_id=server.card_id)
                break
            except AuthError as e:
                if refresh_attempted or not server.account_phone:
                    raise e
                refresh_attempted = True
                ok, msg = await _refresh_token_for(server)
                if not ok:
                    log_operation(
                        0, "keepalive", None,
                        f"keepalive_start {server.name}", False,
                        f"refresh failed: {msg}",
                    )
                    return False, f"token 刷新失败: {msg}"
                server = servers.get_server(server.name) or server

        instance_msg = ""
        if server.instance_uuid and server.account_phone:
            async def _start(panel):
                return await panel.start_instance(server.instance_uuid)

            _result, err = await _panel_run_bg(server, _start)
            if err != "ok":
                try:
                    async with _build_client(server) as client:
                        await client.close_server(card_id=server.card_id)
                except Exception:
                    logger.exception(f"保活启动『{server.name}』实例失败后关闭计时卡也失败")
                log_operation(
                    0, "keepalive", None,
                    f"keepalive_start {server.name}", False,
                    f"计时卡 OK, 实例失败: {err}",
                )
                return False, f"计时卡已开启，但实例启动失败: {err}"
            instance_msg = "，实例启动指令已下达"
        elif server.instance_uuid and not server.account_phone:
            return False, "有实例 UUID 但未绑定账号，无法自动启动实例"

        servers.mark_server_started(server.name)
        idle_watcher.mark_opened(server.name)
        log_operation(
            0, "keepalive", None, f"keepalive_start {server.name}", True
        )
        return True, f"计时卡已开启{instance_msg}"

    except RateLimitError as e:
        servers.mark_server_started(server.name)
        idle_watcher.mark_opened(server.name)
        log_operation(
            0, "keepalive", None,
            f"keepalive_start {server.name}", True, f"限流: {e}",
        )
        return True, f"计时卡可能已开启（{e}）"
    except AuthError as e:
        log_operation(
            0, "keepalive", None,
            f"keepalive_start {server.name}", False, str(e),
        )
        return False, f"认证失败: {e}"
    except MinekuaiError as e:
        log_operation(
            0, "keepalive", None,
            f"keepalive_start {server.name}", False, str(e),
        )
        return False, str(e)
    except Exception as e:
        logger.exception(f"保活启动『{server.name}』时发生未预期异常")
        log_operation(
            0, "keepalive", None,
            f"keepalive_start {server.name}", False, f"未知: {e}",
        )
        return False, f"意外错误 {type(e).__name__}: {e}"


# ============================================================
# 工具函数
# ============================================================

def _strip_bearer(token: str) -> str:
    """从用户输入里提取干净的 JWT。

    支持几种粘贴形式：
      eyJ...                            （纯 token）
      Bearer eyJ...                     （带前缀）
      authorization: Bearer eyJ...      （整行 header）
      'Bearer eyJ...'                   （带引号）
    """
    token = token.strip().strip("'\"").strip()
    if ":" in token:
        prefix, rest = token.split(":", 1)
        if prefix.strip().lower() in ("authorization", "auth"):
            token = rest.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


def _check_perm(event: MessageEvent) -> tuple[bool, str]:
    """权限校验，返回 (是否允许, 拒绝消息)"""
    user_id = event.user_id
    group_id = event.group_id if isinstance(event, GroupMessageEvent) else None
    return is_user_allowed(
        user_id, group_id,
        config.allowed_groups, config.allowed_users,
    )


def _user_display_name(event: MessageEvent) -> str:
    if isinstance(event, GroupMessageEvent):
        return event.sender.card or event.sender.nickname or str(event.user_id)
    return event.sender.nickname or str(event.user_id)


def _build_client(server: servers.Server) -> MinekuaiClient:
    return MinekuaiClient(token=server.token, client_id=server.client_id)


def _format_server_names(servers_list: list[servers.Server]) -> str:
    return "、".join(f"『{s.name}』" for s in servers_list)


def _mask_phone(phone: str) -> str:
    """139****8110 这种打码格式，给群里的提示用，避免泄漏完整号码"""
    if len(phone) < 7:
        return "***"
    return phone[:3] + "****" + phone[-4:]


async def _refresh_token_for(server: servers.Server) -> tuple[bool, str]:
    """尝试用绑定账号刷新指定服务器的 token + 面板 cookies。

    一次登录搞定 4 样：token（JWT）/ clientid / session_cookie / xsrf_token。
    返回 (成功?, 状态消息)。成功时 DB 已被更新；调用方应重新读 server / account。
    """
    if not server.account_phone:
        return False, "未绑定账号，无法自动续期"

    account = servers.get_account(server.account_phone)
    if not account:
        return False, f"绑定的账号 {server.account_phone} 在数据库里找不到了"

    try:
        new_token, new_client_id, session_cookie, xsrf_token = (
            await auth.refresh_token(account.phone, account.password)
        )
    except auth.LoginError as e:
        return False, f"自动登录失败：{e}"
    except Exception as e:
        logger.exception("自动登录时发生未预期异常")
        return False, f"自动登录异常 ({type(e).__name__}): {e}"

    if not servers.update_credentials(server.name, new_token, new_client_id):
        return False, "DB 更新失败（服务器可能已被删）"
    # 顺手把面板 cookies 也写回账号——所有绑了这个账号的服务器都能复用
    if session_cookie and xsrf_token:
        servers.update_account_session(
            account.phone, session_cookie, xsrf_token
        )
    else:
        logger.warning("[refresh] 没拿到面板 cookies，下次开服可能要再登一次")
    servers.mark_account_refreshed(account.phone)
    return True, "已刷新"


async def _ensure_panel_auth(
    server: servers.Server,
) -> tuple[bool, str, servers.Server]:
    """确保账号有可用的面板 cookies；没有就主动登录一次。

    返回 (成功?, 状态消息, 重新读取后的 server)。
    """
    if not server.account_phone:
        return False, "未绑定账号，没办法走面板接口", server
    account = servers.get_account(server.account_phone)
    if not account:
        return False, f"绑定的账号 {server.account_phone} 不存在", server
    if account.session_cookie and account.xsrf_token:
        return True, "已有 cookies", server
    ok, msg = await _refresh_token_for(server)
    return ok, msg, servers.get_server(server.name) or server


async def _start_instance(
    matcher: Matcher,
    server: servers.Server,
) -> tuple[bool, str]:
    """通过 panel POST /power signal=start 启动实例。
    cookies 失效时自动刷一次再重试。
    """
    if not server.instance_uuid:
        return False, "未配置 instance_uuid，跳过实例启动"
    ok, msg, server = await _ensure_panel_auth(server)
    if not ok:
        return False, msg

    refresh_attempted = False
    while True:
        account = servers.get_account(server.account_phone)
        if not account or not account.session_cookie or not account.xsrf_token:
            return False, "面板 cookies 丢失"

        try:
            async with PanelClient(
                account.session_cookie, account.xsrf_token,
            ) as panel:
                await panel.start_instance(server.instance_uuid)
            return True, "start 信号已下达"

        except AuthError as e:
            if refresh_attempted:
                return False, f"刷新后仍认证失败：{e}"
            refresh_attempted = True
            await matcher.send(
                f"⏳ 面板 cookies 失效，正在用账号 "
                f"{_mask_phone(server.account_phone)} 重新登录..."
            )
            ok, msg = await _refresh_token_for(server)
            if not ok:
                return False, f"刷新失败：{msg}"
            continue

        except RateLimitError as e:
            return True, f"实例可能已在启动（{e}）"

        except MinekuaiError as e:
            return False, str(e)
        except Exception as e:
            logger.exception("调 panel power 时发生未预期异常")
            return False, f"意外错误 {type(e).__name__}: {e}"


# ============================================================
# 指令: 开服
# ============================================================

start_cmd = on_command(
    "开服",
    aliases={"开机", "start", "Start", "START"},
    priority=10, block=True,
)


@start_cmd.handle()
async def _start_init(
    matcher: Matcher,
    event: MessageEvent,
    arg: Message = CommandArg(),
):
    ok, reason = _check_perm(event)
    if not ok:
        if reason:
            await matcher.finish(reason)
        return  # 静默忽略

    cooling, remaining = check_cooldown(
        event.user_id, "start", config.command_cooldown
    )
    if cooling:
        await matcher.finish(f"操作太频繁，请 {remaining} 秒后再试")

    name_arg = arg.extract_plain_text().strip()
    all_servers = servers.list_servers()
    if not all_servers:
        await matcher.finish(
            "还没配置任何服务器。发『添加服务器』开始配置。"
        )

    if name_arg:
        matcher.set_arg("server_name", Message(name_arg))
        return
    if len(all_servers) == 1:
        matcher.set_arg("server_name", Message(all_servers[0].name))
        return

    await matcher.send(
        f"现在有这些服务器：{_format_server_names(all_servers)}\n"
        f"回复要开的名字（或『取消』）"
    )


@start_cmd.got("server_name")
async def _start_step(
    matcher: Matcher,
    event: MessageEvent,
    name: str = ArgPlainText("server_name"),
):
    name = name.strip()
    if name in CANCEL_WORDS:
        await matcher.finish("已取消")

    server = servers.get_server(name)
    if not server:
        await matcher.reject(
            f"找不到服务器『{name}』。请重输或发『取消』。"
        )

    user_id = event.user_id
    group_id = event.group_id if isinstance(event, GroupMessageEvent) else None
    user_name = _user_display_name(event)

    # 若服务器还没有有效 token/clientid 但绑了账号，先自动登录补齐
    if (not server.token or not server.client_id) and server.account_phone:
        await matcher.send(
            f"⏳ 『{server.name}』还没有 token，正在用账号 "
            f"{_mask_phone(server.account_phone)} 自动获取..."
        )
        ok, msg = await _refresh_token_for(server)
        if not ok:
            log_operation(
                user_id, user_name, group_id,
                f"start {server.name}", False, f"initial refresh failed: {msg}",
            )
            await matcher.finish(
                f"❌ 『{server.name}』自动获取 token 失败：{msg}\n"
                f"请用『更新token {server.name}』手动填一份"
            )
        server = servers.get_server(server.name)
    if not server.token or not server.client_id:
        await matcher.finish(
            f"❌ 『{server.name}』没有 token 也没绑账号。\n"
            f"发『绑定账号 {server.name} <手机号>』后重试，或发"
            f"『更新token {server.name}』手动填"
        )

    await matcher.send(f"正在开启『{server.name}』的计时卡...")

    refresh_attempted = False
    while True:
        try:
            # 第 1 步：开计时卡（Bearer JWT, api.minekuai.com）
            async with _build_client(server) as client:
                await client.open_timing_only(card_id=server.card_id)

            # 第 2 步：实例启动（如果配了 uuid + 账号）
            instance_msg = ""
            instance_started = False
            if server.instance_uuid and server.account_phone:
                # 计时卡刚开，给后端 2 秒同步
                import asyncio as _asyncio
                await _asyncio.sleep(2)
                ok, msg = await _start_instance(matcher, server)
                if ok:
                    instance_msg = " + 实例启动指令已下达"
                    instance_started = True
                else:
                    instance_msg = f"\n⚠️ 但实例启动失败：{msg}"
                    log_operation(
                        user_id, user_name, group_id,
                        f"start {server.name}", True,
                        f"计时卡 OK, 实例失败: {msg}",
                    )
            elif server.instance_uuid and not server.account_phone:
                instance_msg = (
                    f"\n实例 UUID 已配但未绑定账号，无法自动启动实例 "
                    f"(发『绑定账号 {server.name} <手机号>』)"
                )
            else:
                instance_msg = (
                    f"\n实例 UUID 未配置，需手动启动 "
                    f"(发『修改uuid {server.name} <id>』)"
                )

            update_cooldown(user_id, "start")
            log_operation(
                user_id, user_name, group_id, f"start {server.name}", True
            )
            servers.mark_server_started(server.name)
            idle_watcher.mark_opened(server.name)
            # 实例真的启动了 + 有地址 → 起后台 task 轮询 SLP，ping 通就发"可以进入"
            if instance_started and server.address:
                idle_watcher.watch_for_ready(server)
                tail = "正在启动，ping 通后会再通知一次"
            else:
                tail = "等 30-60 秒玩家可进入"
            await matcher.finish(
                f"✅ 『{server.name}』计时卡已开启{instance_msg}\n{tail}"
            )

        except AuthError as e:
            # 第一次 401 + 绑了账号 → 尝试自动刷新一次再重试
            if not refresh_attempted and server.account_phone:
                refresh_attempted = True
                await matcher.send(
                    f"⏳ token 失效，正在用账号 "
                    f"{_mask_phone(server.account_phone)} 自动登录刷新..."
                )
                ok, msg = await _refresh_token_for(server)
                if ok:
                    server = servers.get_server(server.name)
                    continue
                log_operation(
                    user_id, user_name, group_id,
                    f"start {server.name}", False, f"refresh failed: {msg}",
                )
                await matcher.finish(
                    f"❌ 『{server.name}』token 失效，自动续期也失败：{msg}\n"
                    f"请用『更新token {server.name}』手动更新"
                )
            log_operation(
                user_id, user_name, group_id,
                f"start {server.name}", False, str(e),
            )
            await matcher.finish(
                f"❌ 『{server.name}』认证失败，token 已过期或冻结。\n"
                f"方案 A：发『更新token {server.name}』手动更新\n"
                f"方案 B：发『添加账号』然后『绑定账号 {server.name} <手机号>』开启自动续期"
            )

        except RateLimitError as e:
            update_cooldown(user_id, "start")
            log_operation(
                user_id, user_name, group_id,
                f"start {server.name}", True, f"限流: {e}",
            )
            await matcher.finish(
                f"ℹ️ 『{server.name}』计时卡可能已开启（操作太频繁）。\n"
                f"如果服务器还没运行，请联系管理员手动启动。"
            )

        except MinekuaiError as e:
            log_operation(
                user_id, user_name, group_id,
                f"start {server.name}", False, str(e),
            )
            await matcher.finish(f"❌ 『{server.name}』开启计时卡失败：{e}")

        except MatcherException:
            raise

        except Exception as e:
            logger.exception(f"开启『{server.name}』时发生未预期的异常")
            log_operation(
                user_id, user_name, group_id,
                f"start {server.name}", False, f"未知: {e}",
            )
            await matcher.finish(f"❌ 意外错误：{type(e).__name__}")


# ============================================================
# 指令: 关服
# ============================================================

stop_cmd = on_command(
    "关服",
    aliases={"关机", "stop", "Stop", "STOP"},
    priority=10, block=True,
)


@stop_cmd.handle()
async def _stop_init(
    matcher: Matcher,
    event: MessageEvent,
    arg: Message = CommandArg(),
):
    ok, reason = _check_perm(event)
    if not ok:
        if reason:
            await matcher.finish(reason)
        return

    cooling, remaining = check_cooldown(
        event.user_id, "stop", config.command_cooldown
    )
    if cooling:
        await matcher.finish(f"操作太频繁，请 {remaining} 秒后再试")

    name_arg = arg.extract_plain_text().strip()
    all_servers = servers.list_servers()
    if not all_servers:
        await matcher.finish("还没配置任何服务器。")

    if name_arg:
        matcher.set_arg("server_name", Message(name_arg))
        return
    if len(all_servers) == 1:
        matcher.set_arg("server_name", Message(all_servers[0].name))
        return

    await matcher.send(
        f"现在有这些服务器：{_format_server_names(all_servers)}\n"
        f"回复要关的名字（或『取消』）"
    )


@stop_cmd.got("server_name")
async def _stop_step(
    matcher: Matcher,
    event: MessageEvent,
    name: str = ArgPlainText("server_name"),
):
    name = name.strip()
    if name in CANCEL_WORDS:
        await matcher.finish("已取消")

    server = servers.get_server(name)
    if not server:
        await matcher.reject(
            f"找不到服务器『{name}』。请重输或发『取消』。"
        )

    user_id = event.user_id
    user_name = _user_display_name(event)

    if config.stop_need_confirm:
        mark_pending_confirm(user_id, server.name)
        await matcher.finish(
            f"⚠️ 关闭『{server.name}』会断开所有玩家！\n"
            f"如确认关服，请在 5 分钟内回复：确认关服"
        )

    await _do_stop(matcher, event, user_name, server)


async def _do_stop(
    matcher: Matcher,
    event: MessageEvent,
    user_name: str,
    server: servers.Server,
):
    user_id = event.user_id
    group_id = event.group_id if isinstance(event, GroupMessageEvent) else None

    await matcher.send(f"正在关闭『{server.name}』计时卡...")

    refresh_attempted = False
    while True:
        try:
            async with _build_client(server) as client:
                await client.close_server(card_id=server.card_id)
            update_cooldown(user_id, "stop")
            log_operation(
                user_id, user_name, group_id, f"stop {server.name}", True
            )
            idle_watcher.mark_closed(server.name)
            await matcher.finish(f"✅ 『{server.name}』关服成功，计时卡已关闭")

        except AuthError as e:
            if not refresh_attempted and server.account_phone:
                refresh_attempted = True
                await matcher.send(
                    f"⏳ token 失效，正在用账号 "
                    f"{_mask_phone(server.account_phone)} 自动登录刷新..."
                )
                ok, msg = await _refresh_token_for(server)
                if ok:
                    server = servers.get_server(server.name)
                    continue
                log_operation(
                    user_id, user_name, group_id,
                    f"stop {server.name}", False, f"refresh failed: {msg}",
                )
                await matcher.finish(
                    f"❌ 『{server.name}』token 失效，自动续期也失败：{msg}\n"
                    f"请用『更新token {server.name}』手动更新"
                )
            log_operation(
                user_id, user_name, group_id,
                f"stop {server.name}", False, str(e),
            )
            await matcher.finish(
                f"❌ 『{server.name}』认证失败，token 已过期或冻结。\n"
                f"方案 A：发『更新token {server.name}』手动更新\n"
                f"方案 B：发『绑定账号 {server.name} <手机号>』开启自动续期"
            )

        except RateLimitError as e:
            update_cooldown(user_id, "stop")
            log_operation(
                user_id, user_name, group_id,
                f"stop {server.name}", True, f"限流: {e}",
            )
            await matcher.finish(
                f"ℹ️ 『{server.name}』计时卡可能已关闭（操作太频繁）"
            )

        except MinekuaiError as e:
            log_operation(
                user_id, user_name, group_id,
                f"stop {server.name}", False, str(e),
            )
            await matcher.finish(f"❌ 『{server.name}』关服失败：{e}")

        except MatcherException:
            raise

        except Exception as e:
            logger.exception(f"关闭『{server.name}』时发生未预期的异常")
            log_operation(
                user_id, user_name, group_id,
                f"stop {server.name}", False, f"未知: {e}",
            )
            await matcher.finish(f"❌ 意外错误：{type(e).__name__}")


# ============================================================
# 指令: 确认关服
# ============================================================

confirm_stop_cmd = on_fullmatch("确认关服", priority=9, block=True)


@confirm_stop_cmd.handle()
async def _confirm_stop(matcher: Matcher, event: MessageEvent):
    ok, reason = _check_perm(event)
    if not ok:
        if reason:
            await matcher.finish(reason)
        return

    server_name = consume_pending_confirm(event.user_id)
    if not server_name:
        await matcher.finish("没有待确认的关服请求，请先发『关服』")

    server = servers.get_server(server_name)
    if not server:
        await matcher.finish(
            f"服务器『{server_name}』已不存在（可能被删了）"
        )

    user_name = _user_display_name(event)
    await _do_stop(matcher, event, user_name, server)


# ============================================================
# 指令: 服务器列表
# ============================================================

list_cmd = on_command(
    "服务器列表",
    aliases={"列表", "list", "List", "LIST"},
    priority=10, block=True,
)


@list_cmd.handle()
async def _list(matcher: Matcher, event: MessageEvent):
    ok, reason = _check_perm(event)
    if not ok:
        if reason:
            await matcher.finish(reason)
        return

    all_servers = servers.list_servers()
    if not all_servers:
        await matcher.finish(
            "还没配置任何服务器。发『添加服务器』开始配置。"
        )

    lines = ["📋 已配置的服务器："]
    for s in all_servers:
        addr_part = f" | 📡 {s.address}" if s.address else " | 📡 (未设置)"
        lines.append(f"  ▸ {s.name} (卡 {s.card_id}){addr_part}")
    lines.append("")
    lines.append("发『开服 <名字>』/『关服 <名字>』操作")
    await matcher.finish("\n".join(lines))


# ============================================================
# 指令: 服务器地址（查询）
# ============================================================

addr_query_cmd = on_command(
    "服务器地址",
    aliases={"查询地址", "地址", "address"},
    priority=10, block=True,
)


@addr_query_cmd.handle()
async def _addr_query(
    matcher: Matcher,
    event: MessageEvent,
    arg: Message = CommandArg(),
):
    ok, reason = _check_perm(event)
    if not ok:
        if reason:
            await matcher.finish(reason)
        return

    name = arg.extract_plain_text().strip()
    all_servers = servers.list_servers()
    if not all_servers:
        await matcher.finish(
            "还没配置任何服务器。发『添加服务器』开始配置。"
        )

    if name:
        server = servers.get_server(name)
        if not server:
            await matcher.finish(f"找不到服务器『{name}』")
        targets = [server]
    else:
        targets = all_servers

    lines = []
    for s in targets:
        if s.address:
            lines.append(f"📡 『{s.name}』: {s.address}")
        else:
            lines.append(
                f"📡 『{s.name}』: 未设置（发『修改地址 {s.name}』来填）"
            )
    await matcher.finish("\n".join(lines))


# ============================================================
# 指令: 修改地址（写入）
# ============================================================

addr_update_cmd = on_command(
    "修改地址",
    aliases={"更新地址", "修改服务器地址"},
    priority=10, block=True,
)


@addr_update_cmd.handle()
async def _addr_update_init(
    matcher: Matcher,
    event: MessageEvent,
    arg: Message = CommandArg(),
):
    ok, reason = _check_perm(event)
    if not ok:
        if reason:
            await matcher.finish(reason)
        return

    text = arg.extract_plain_text().strip()
    parts = text.split(maxsplit=1)
    all_servers = servers.list_servers()
    if not all_servers:
        await matcher.finish("还没配置任何服务器")

    # inline 完整：修改地址 名字 地址
    if len(parts) >= 2:
        if not servers.get_server(parts[0]):
            await matcher.finish(f"找不到服务器『{parts[0]}』")
        matcher.set_arg("name", Message(parts[0]))
        matcher.set_arg("address", Message(parts[1]))
        return

    # 只给名字
    if len(parts) == 1:
        if not servers.get_server(parts[0]):
            await matcher.finish(f"找不到服务器『{parts[0]}』")
        matcher.set_arg("name", Message(parts[0]))
        return

    # 没参数
    if len(all_servers) == 1:
        matcher.set_arg("name", Message(all_servers[0].name))
        return

    await matcher.send(
        f"现在有这些服务器：{_format_server_names(all_servers)}\n"
        f"回复要改地址的服务器名字（或『取消』）"
    )


@addr_update_cmd.got("name")
async def _addr_update_step_name(
    matcher: Matcher,
    name: str = ArgPlainText("name"),
):
    n = name.strip()
    if n in CANCEL_WORDS:
        await matcher.finish("已取消")
    if not servers.get_server(n):
        await matcher.reject(
            f"找不到服务器『{n}』。请重输或发『取消』"
        )
    matcher.set_arg("name", Message(n))


@addr_update_cmd.got(
    "address",
    prompt=(
        "请输入新地址（玩家连接用的 IP:端口）\n"
        "\n"
        "📌 例子：mc.example.com:25565、123.45.67.89:25565\n"
        "\n"
        "发『取消』中止"
    ),
)
async def _addr_update_finish(
    matcher: Matcher,
    event: MessageEvent,
    name: str = ArgPlainText("name"),
    address: str = ArgPlainText("address"),
):
    addr = address.strip()
    if addr in CANCEL_WORDS:
        await matcher.finish("已取消")
    if not addr:
        await matcher.reject("地址不能为空。请重输（或『取消』）")

    if not servers.update_address(name, addr):
        await matcher.finish(f"❌ 更新失败：服务器『{name}』不存在")

    user_name = _user_display_name(event)
    group_id = event.group_id if isinstance(event, GroupMessageEvent) else None
    log_operation(
        event.user_id, user_name, group_id, f"update_address {name}", True
    )
    await matcher.finish(f"✅ 已更新『{name}』的地址：{addr}")


# ============================================================
# 指令: 添加服务器（多步交互）
# ============================================================

add_cmd = on_command(
    "添加服务器",
    aliases={"添加"},
    priority=10, block=True,
)


@add_cmd.handle()
async def _add_init(matcher: Matcher, event: MessageEvent):
    ok, reason = _check_perm(event)
    if not ok:
        if reason:
            await matcher.finish(reason)
        return
    # 必须有账号才能添加服务器（token / clientid 由账号自动获取）
    if not servers.list_accounts():
        await matcher.finish(
            "❌ 还没添加任何账号。\n"
            "添加服务器需要先绑定账号（自动登录获取 token / clientid）。\n"
            "请先发『添加账号』把账号加进来，然后再发『添加服务器』。"
        )


@add_cmd.got(
    "name",
    prompt=(
        "【1/5】请输入服务器名字（你自己起的标签）\n"
        "\n"
        "📌 用途：之后『开服 名字』『关服 名字』就用这个\n"
        "📌 要求：不能为空、不能含空格、不能跟已有的重名\n"
        "📌 例子：GTNH、原版、服务器1\n"
        "\n"
        "发『取消』中止"
    ),
)
async def _add_step_name(
    matcher: Matcher,
    name: str = ArgPlainText("name"),
):
    name = name.strip()
    if name in CANCEL_WORDS:
        await matcher.finish("已取消")
    if not name:
        await matcher.reject("名字不能为空。请重输（或『取消』）")
    if len(name) > 50:
        await matcher.reject("名字不能超过 50 字。请重输")
    if " " in name:
        await matcher.reject(
            "名字里不能包含空格（不然『开服 X』会出错）。请重输"
        )
    if servers.get_server(name):
        await matcher.reject(
            f"名字『{name}』已存在。换一个（或『取消』）"
        )
    matcher.set_arg("name", Message(name))


@add_cmd.got(
    "card_id",
    prompt=(
        "【2/5】请输入计时卡 ID（纯数字）\n"
        "\n"
        "🔍 怎么找：\n"
        "1. 浏览器登录 minekuai.com 打开服务器控制台\n"
        "2. F12 → Network 标签\n"
        "3. 在网页上点『启动』或『关闭』按钮\n"
        "4. 找 URL 含 startTiming 或 stopTiming 的请求\n"
        "5. URL 末尾那串数字就是（如 1940712535355367435）\n"
        "\n"
        "发『取消』中止"
    ),
)
async def _add_step_card(
    matcher: Matcher,
    card_id: str = ArgPlainText("card_id"),
):
    card_id = card_id.strip()
    if card_id in CANCEL_WORDS:
        await matcher.finish("已取消")
    if not card_id.isdigit():
        await matcher.reject(
            "计时卡 ID 应该是纯数字。请重输（或『取消』）"
        )
    matcher.set_arg("card_id", Message(card_id))


@add_cmd.got(
    "address",
    prompt=(
        "【3/5】请输入服务器地址（玩家连接用的 IP:端口）\n"
        "\n"
        "📌 例子：mc.example.com:25565、123.45.67.89:25565\n"
        "📌 在 minekuai.com 服务器控制台页面顶部能看到\n"
        "\n"
        "如果暂时不知道，发『跳过』；之后可以用『修改地址 <名字>』补上\n"
        "发『取消』中止"
    ),
)
async def _add_step_address(
    matcher: Matcher,
    address: str = ArgPlainText("address"),
):
    address = address.strip()
    if address in CANCEL_WORDS:
        await matcher.finish("已取消")
    if address.lower() in ("跳过", "skip", "无"):
        address = ""
    matcher.set_arg("address", Message(address))


@add_cmd.got(
    "instance_uuid",
    prompt=(
        "【4/5】请输入实例 ID（开服会用来启动服务器实例）\n"
        "\n"
        "📌 控制台 URL 里那段：minekuai.com/server/XXX 的 XXX\n"
        "    例如 420d4426 或 e65b9139\n"
        "📌 配了就能开计时卡后自动启动服务器实例；不配只开计时卡\n"
        "\n"
        "如果暂时不填，发『跳过』；之后用『修改uuid <名字>』补上\n"
        "发『取消』中止"
    ),
)
async def _add_step_uuid(
    matcher: Matcher,
    instance_uuid: str = ArgPlainText("instance_uuid"),
):
    instance_uuid = instance_uuid.strip()
    if instance_uuid in CANCEL_WORDS:
        await matcher.finish("已取消")
    if instance_uuid.lower() in ("跳过", "skip", "无"):
        instance_uuid = ""
    elif " " in instance_uuid or len(instance_uuid) > 80:
        await matcher.reject(
            "实例 ID 看起来格式不对（不应有空格、不该超过 80 字）。"
            "请重输或『跳过』"
        )
    matcher.set_arg("instance_uuid", Message(instance_uuid))


@add_cmd.got(
    "account_phone",
    prompt=(
        "【5/5】请输入绑定的账号手机号\n"
        "\n"
        "📌 必须是已经『添加账号』添加进来的账号\n"
        "📌 bot 会用这个账号自动登录获取 token、clientid 和 cookies\n"
        "    省去手填 token / clientid 的麻烦，token 失效也能自动刷新\n"
        "\n"
        "如果还没添加账号，先发『取消』，然后『添加账号』完成后再回来\n"
        "发『取消』中止"
    ),
)
async def _add_finish(
    matcher: Matcher,
    event: MessageEvent,
    name: str = ArgPlainText("name"),
    card_id: str = ArgPlainText("card_id"),
    address: str = ArgPlainText("address"),
    instance_uuid: str = ArgPlainText("instance_uuid"),
    account_phone: str = ArgPlainText("account_phone"),
):
    account_phone = account_phone.strip()
    if account_phone in CANCEL_WORDS:
        await matcher.finish("已取消")
    if not account_phone.isdigit() or len(account_phone) != 11:
        await matcher.reject(
            "手机号应该是 11 位纯数字。请重输（或『取消』）"
        )
    account = servers.get_account(account_phone)
    if not account:
        accs = servers.list_accounts()
        existing = (
            "、".join(_mask_phone(a.phone) for a in accs)
            if accs else "（暂无）"
        )
        await matcher.reject(
            f"找不到账号『{_mask_phone(account_phone)}』。\n"
            f"现有账号：{existing}\n"
            f"请输已存在的手机号，或『取消』后先发『添加账号』"
        )

    user_name = _user_display_name(event)
    group_id = event.group_id if isinstance(event, GroupMessageEvent) else None

    # 自动登录拿 token / clientid / cookies
    await matcher.send(
        f"⏳ 正在用账号 {_mask_phone(account_phone)} 自动登录获取 token..."
    )
    try:
        token, client_id, session_cookie, xsrf = await auth.refresh_token(
            account.phone, account.password
        )
    except auth.LoginError as e:
        log_operation(
            event.user_id, user_name, group_id,
            f"add_server {name} (login failed)", False, str(e),
        )
        await matcher.finish(
            f"❌ 自动登录失败：{e}\n"
            f"可能账号密码错了，或 minekuai 改了登录页。\n"
            f"先排查（删账号重加 / 检查密码），然后重新发『添加服务器』"
        )
    except Exception as e:
        logger.exception("自动登录时未预期的异常")
        await matcher.finish(
            f"❌ 自动登录异常：{type(e).__name__}: {e}\n"
            f"请重试或排查日志"
        )

    # 写入 DB
    try:
        servers.add_server(
            name, card_id, token, client_id,
            address=address,
            account_phone=account_phone,
            instance_uuid=instance_uuid,
        )
    except ValueError as e:
        await matcher.finish(f"❌ 添加失败：{e}")

    # 把刚拿到的 cookies 也写回账号
    if session_cookie and xsrf:
        servers.update_account_session(account_phone, session_cookie, xsrf)

    log_operation(
        event.user_id, user_name, group_id, f"add_server {name}", True
    )
    addr_hint = f"地址：{address}" if address else "地址：未设置"
    uuid_hint = (
        "实例 UUID：已配置（开服会自动启动实例）"
        if instance_uuid else "实例 UUID：未配置（只开计时卡）"
    )
    await matcher.finish(
        f"✅ 已添加服务器『{name}』，并绑定账号 {_mask_phone(account_phone)}！\n"
        f"{addr_hint}\n"
        f"{uuid_hint}\n"
        f"token / clientid / cookies 都已自动获取。\n"
        f"现在可以发『开服 {name}』测试。"
    )


# ============================================================
# 指令: 修改uuid
# ============================================================

uuid_update_cmd = on_command(
    "修改uuid",
    aliases={"修改UUID", "更新uuid", "修改实例"},
    priority=10, block=True,
)


@uuid_update_cmd.handle()
async def _uuid_update_init(
    matcher: Matcher,
    event: MessageEvent,
    arg: Message = CommandArg(),
):
    ok, reason = _check_perm(event)
    if not ok:
        if reason:
            await matcher.finish(reason)
        return

    text = arg.extract_plain_text().strip()
    parts = text.split(maxsplit=1)
    all_servers = servers.list_servers()
    if not all_servers:
        await matcher.finish("还没配置任何服务器")

    if len(parts) >= 2:
        if not servers.get_server(parts[0]):
            await matcher.finish(f"找不到服务器『{parts[0]}』")
        matcher.set_arg("name", Message(parts[0]))
        matcher.set_arg("instance_uuid", Message(parts[1]))
        return

    if len(parts) == 1:
        if not servers.get_server(parts[0]):
            await matcher.finish(f"找不到服务器『{parts[0]}』")
        matcher.set_arg("name", Message(parts[0]))
        return

    if len(all_servers) == 1:
        matcher.set_arg("name", Message(all_servers[0].name))
        return

    await matcher.send(
        f"现有服务器：{_format_server_names(all_servers)}\n"
        f"回复要改 UUID 的服务器名字（或『取消』）"
    )


@uuid_update_cmd.got("name")
async def _uuid_update_step_name(
    matcher: Matcher,
    name: str = ArgPlainText("name"),
):
    n = name.strip()
    if n in CANCEL_WORDS:
        await matcher.finish("已取消")
    if not servers.get_server(n):
        await matcher.reject(f"找不到服务器『{n}』。请重输或发『取消』")
    matcher.set_arg("name", Message(n))


@uuid_update_cmd.got(
    "instance_uuid",
    prompt=(
        "请输入新的实例 UUID\n"
        "\n"
        "📌 形如 e65b9139-938d-47fd-b7ab-8b59a6824c61\n"
        "📌 从 minekuai.com F12 抓 /api/client/servers/XXX/ 那段\n"
        "\n"
        "发『清空』把 UUID 留空（回到只开计时卡模式）\n"
        "发『取消』中止"
    ),
)
async def _uuid_update_finish(
    matcher: Matcher,
    event: MessageEvent,
    name: str = ArgPlainText("name"),
    instance_uuid: str = ArgPlainText("instance_uuid"),
):
    val = instance_uuid.strip()
    if val in CANCEL_WORDS:
        await matcher.finish("已取消")
    if val in ("清空", "clear", "无"):
        val = ""
    elif " " in val or len(val) > 80:
        await matcher.reject(
            "实例 ID 看起来格式不对（不应有空格、不该超过 80 字）。"
            "请重输（或『清空』/『取消』）"
        )

    if not servers.update_instance_uuid(name, val):
        await matcher.finish(f"❌ 更新失败：服务器『{name}』不存在")

    user_name = _user_display_name(event)
    group_id = event.group_id if isinstance(event, GroupMessageEvent) else None
    log_operation(
        event.user_id, user_name, group_id,
        f"update_uuid {name}", True,
    )

    if val:
        await matcher.finish(
            f"✅ 已更新『{name}』的实例 UUID。\n"
            f"以后开服会自动启动服务器实例。"
        )
    else:
        await matcher.finish(
            f"✅ 已清空『{name}』的实例 UUID。\n"
            f"开服时只开计时卡，需手动启动实例。"
        )


# ============================================================
# 指令: 删除服务器
# ============================================================

del_cmd = on_command(
    "删除服务器",
    aliases={"删除"},
    priority=10, block=True,
)


@del_cmd.handle()
async def _del_init(
    matcher: Matcher,
    event: MessageEvent,
    arg: Message = CommandArg(),
):
    ok, reason = _check_perm(event)
    if not ok:
        if reason:
            await matcher.finish(reason)
        return

    name = arg.extract_plain_text().strip()
    if not name:
        all_servers = servers.list_servers()
        if not all_servers:
            await matcher.finish("没有任何服务器可删")
        await matcher.finish(
            f"用法：删除服务器 <名字>\n"
            f"现有：{_format_server_names(all_servers)}"
        )

    if not servers.get_server(name):
        await matcher.finish(f"找不到服务器『{name}』")

    matcher.set_arg("name", Message(name))


@del_cmd.got(
    "confirm",
    prompt="⚠️ 真要删吗？回复『确认』继续，其它任意内容取消",
)
async def _del_finish(
    matcher: Matcher,
    event: MessageEvent,
    name: str = ArgPlainText("name"),
    confirm: str = ArgPlainText("confirm"),
):
    if confirm.strip() != "确认":
        await matcher.finish("已取消")

    if not servers.remove_server(name):
        await matcher.finish(f"❌ 删除失败：『{name}』不存在")

    user_name = _user_display_name(event)
    group_id = event.group_id if isinstance(event, GroupMessageEvent) else None
    log_operation(
        event.user_id, user_name, group_id, f"remove_server {name}", True
    )
    await matcher.finish(f"✅ 已删除服务器『{name}』")


# ============================================================
# 指令: 更新 token
# ============================================================

update_token_cmd = on_command(
    "更新token",
    aliases={"更新Token", "更新TOKEN"},
    priority=10, block=True,
)


@update_token_cmd.handle()
async def _update_token_init(
    matcher: Matcher,
    event: MessageEvent,
    arg: Message = CommandArg(),
):
    ok, reason = _check_perm(event)
    if not ok:
        if reason:
            await matcher.finish(reason)
        return

    name = arg.extract_plain_text().strip()
    all_servers = servers.list_servers()
    if not all_servers:
        await matcher.finish("还没配置任何服务器")

    if not name:
        if len(all_servers) == 1:
            name = all_servers[0].name
        else:
            await matcher.finish(
                f"用法：更新token <名字>\n"
                f"现有：{_format_server_names(all_servers)}"
            )

    if not servers.get_server(name):
        await matcher.finish(f"找不到服务器『{name}』")

    matcher.set_arg("name", Message(name))


@update_token_cmd.got(
    "token",
    prompt=(
        "请发新的 token（一长串以 eyJ 开头）\n"
        "\n"
        "🔍 怎么找：\n"
        "1. 浏览器重新登录 minekuai.com（旧 token 已失效）\n"
        "2. F12 → Network → 在页面上随便点点\n"
        "3. 找任意 api.minekuai.com 请求 → Headers → Request Headers\n"
        "4. 整行复制 authorization 那行（『Bearer 』前缀会自动去掉）\n"
        "\n"
        "发『取消』中止"
    ),
)
async def _update_token_finish(
    matcher: Matcher,
    event: MessageEvent,
    name: str = ArgPlainText("name"),
    token: str = ArgPlainText("token"),
):
    if token.strip() in CANCEL_WORDS:
        await matcher.finish("已取消")

    cleaned = _strip_bearer(token)
    if not cleaned.startswith("eyJ"):
        await matcher.reject(
            "看起来不像 JWT token（应以 eyJ 开头）。请重发（或『取消』）"
        )

    if not servers.update_token(name, cleaned):
        await matcher.finish(f"❌ 更新失败：服务器『{name}』不存在")

    user_name = _user_display_name(event)
    group_id = event.group_id if isinstance(event, GroupMessageEvent) else None
    log_operation(
        event.user_id, user_name, group_id, f"update_token {name}", True
    )
    await matcher.finish(f"✅ 已更新『{name}』的 token")


# ============================================================
# 指令: 修改服务器名字
# ============================================================

rename_cmd = on_command(
    "修改服务器名字",
    aliases={"重命名", "改名"},
    priority=10, block=True,
)


@rename_cmd.handle()
async def _rename_init(
    matcher: Matcher,
    event: MessageEvent,
    arg: Message = CommandArg(),
):
    ok, reason = _check_perm(event)
    if not ok:
        if reason:
            await matcher.finish(reason)
        return

    parts = arg.extract_plain_text().split()
    all_servers = servers.list_servers()
    if not all_servers:
        await matcher.finish("还没配置任何服务器")

    # 用户内联给了 旧名 + 新名（修改服务器名字 default GTNH）
    if len(parts) >= 2:
        matcher.set_arg("old_name", Message(parts[0]))
        matcher.set_arg("new_name", Message(parts[1]))
        return

    # 只给了旧名（修改服务器名字 default）
    if len(parts) == 1:
        old = parts[0]
        if not servers.get_server(old):
            await matcher.finish(f"找不到服务器『{old}』")
        matcher.set_arg("old_name", Message(old))
        return

    # 没给任何参数
    if len(all_servers) == 1:
        # 只有一台直接选它
        matcher.set_arg("old_name", Message(all_servers[0].name))
        return

    # 多台，列出来让用户选
    await matcher.send(
        f"现在有这些服务器：{_format_server_names(all_servers)}\n"
        f"回复要改名的服务器名字（或『取消』）"
    )


@rename_cmd.got("old_name")
async def _rename_step_old(
    matcher: Matcher,
    old_name: str = ArgPlainText("old_name"),
):
    old = old_name.strip()
    if old in CANCEL_WORDS:
        await matcher.finish("已取消")
    if not servers.get_server(old):
        await matcher.reject(
            f"找不到服务器『{old}』。请重输或发『取消』。"
        )
    matcher.set_arg("old_name", Message(old))


@rename_cmd.got(
    "new_name",
    prompt=(
        "请输入新名字\n"
        "\n"
        "📌 要求：不能为空、不能含空格、不能跟已有的重名\n"
        "📌 例子：GTNH、原版\n"
        "\n"
        "发『取消』中止"
    ),
)
async def _rename_finish(
    matcher: Matcher,
    event: MessageEvent,
    old_name: str = ArgPlainText("old_name"),
    new_name: str = ArgPlainText("new_name"),
):
    new = new_name.strip()
    if new in CANCEL_WORDS:
        await matcher.finish("已取消")
    if not new:
        await matcher.reject("新名字不能为空。请重输（或『取消』）")
    if " " in new:
        await matcher.reject("新名字不能含空格。请重输（或『取消』）")
    if len(new) > 50:
        await matcher.reject("新名字不能超过 50 字。请重输")
    if new == old_name:
        await matcher.finish("新旧名字相同，没改")
    if servers.get_server(new):
        await matcher.reject(
            f"名字『{new}』已被占用。请换一个（或『取消』）"
        )

    if not servers.rename_server(old_name, new):
        await matcher.finish(f"❌ 改名失败：服务器『{old_name}』不存在")

    user_name = _user_display_name(event)
    group_id = event.group_id if isinstance(event, GroupMessageEvent) else None
    log_operation(
        event.user_id, user_name, group_id,
        f"rename_server {old_name}->{new}", True,
    )
    await matcher.finish(f"✅ 已将『{old_name}』改名为『{new}』")


# ============================================================
# 指令: 添加账号（多步交互）
# ============================================================

add_account_cmd = on_command(
    "添加账号",
    aliases={"加账号"},
    priority=10, block=True,
)


@add_account_cmd.handle()
async def _add_account_init(matcher: Matcher, event: MessageEvent):
    ok, reason = _check_perm(event)
    if not ok:
        if reason:
            await matcher.finish(reason)
        return


@add_account_cmd.got(
    "phone",
    prompt=(
        "【1/2】请输入账号手机号（11 位）\n"
        "\n"
        "📌 这是你登录 minekuai.com 用的手机号\n"
        "📌 之后 token 失效时，bot 会用这个账号自动登录刷新\n"
        "\n"
        "发『取消』中止"
    ),
)
async def _add_account_step_phone(
    matcher: Matcher,
    phone: str = ArgPlainText("phone"),
):
    phone = phone.strip()
    if phone in CANCEL_WORDS:
        await matcher.finish("已取消")
    if not phone.isdigit() or len(phone) != 11:
        await matcher.reject(
            "手机号应该是 11 位纯数字。请重输（或『取消』）"
        )
    if servers.get_account(phone):
        await matcher.reject(
            f"账号『{_mask_phone(phone)}』已存在。"
            f"如需改密码，请先『删除账号 {phone}』再重新添加。"
        )
    matcher.set_arg("phone", Message(phone))


@add_account_cmd.got(
    "password",
    prompt=(
        "【2/2】请输入登录密码\n"
        "\n"
        "⚠️ 密码会以明文存在本机的 SQLite 数据库里。\n"
        "    跟现在 token 在 DB 里是同样的敏感级别。\n"
        "    建议给 minekuai 设一个独立密码，别跟其它地方共用。\n"
        "\n"
        "发『取消』中止"
    ),
)
async def _add_account_finish(
    matcher: Matcher,
    event: MessageEvent,
    phone: str = ArgPlainText("phone"),
    password: str = ArgPlainText("password"),
):
    password = password.strip()
    if password in CANCEL_WORDS:
        await matcher.finish("已取消")
    if not password:
        await matcher.reject("密码不能为空。请重输（或『取消』）")

    try:
        servers.add_account(phone, password)
    except ValueError as e:
        await matcher.finish(f"❌ 添加失败：{e}")

    user_name = _user_display_name(event)
    group_id = event.group_id if isinstance(event, GroupMessageEvent) else None
    log_operation(
        event.user_id, user_name, group_id,
        f"add_account {_mask_phone(phone)}", True,
    )
    await matcher.finish(
        f"✅ 已添加账号『{_mask_phone(phone)}』\n"
        f"现在可以发『绑定账号 <服务器名字> {phone}』把它绑给某台服务器。"
    )


# ============================================================
# 指令: 账号列表
# ============================================================

list_accounts_cmd = on_command(
    "账号列表",
    priority=10, block=True,
)


@list_accounts_cmd.handle()
async def _list_accounts(matcher: Matcher, event: MessageEvent):
    ok, reason = _check_perm(event)
    if not ok:
        if reason:
            await matcher.finish(reason)
        return

    accounts = servers.list_accounts()
    if not accounts:
        await matcher.finish("还没添加任何账号。发『添加账号』开始。")

    # 计算每个账号被多少台服务器引用
    all_servers = servers.list_servers()
    usage: dict[str, list[str]] = {}
    for s in all_servers:
        if s.account_phone:
            usage.setdefault(s.account_phone, []).append(s.name)

    lines = ["📱 已配置的账号："]
    for acc in accounts:
        bound = usage.get(acc.phone, [])
        bound_str = "、".join(bound) if bound else "无绑定服务器"
        lines.append(f"  ▸ {_mask_phone(acc.phone)} → {bound_str}")
    await matcher.finish("\n".join(lines))


# ============================================================
# 指令: 删除账号
# ============================================================

del_account_cmd = on_command(
    "删除账号",
    priority=10, block=True,
)


@del_account_cmd.handle()
async def _del_account_init(
    matcher: Matcher,
    event: MessageEvent,
    arg: Message = CommandArg(),
):
    ok, reason = _check_perm(event)
    if not ok:
        if reason:
            await matcher.finish(reason)
        return

    phone = arg.extract_plain_text().strip()
    if not phone:
        accounts = servers.list_accounts()
        if not accounts:
            await matcher.finish("没有任何账号可删")
        phones = "、".join(_mask_phone(a.phone) for a in accounts)
        await matcher.finish(f"用法：删除账号 <手机号>\n现有：{phones}")

    if not servers.get_account(phone):
        await matcher.finish(f"找不到账号『{_mask_phone(phone)}』")
    matcher.set_arg("phone", Message(phone))


@del_account_cmd.got(
    "confirm",
    prompt=(
        "⚠️ 删除账号会同时解绑所有引用此账号的服务器（这些服务器以后失效就不能自动续期了）。\n"
        "回复『确认』继续，其它任意内容取消"
    ),
)
async def _del_account_finish(
    matcher: Matcher,
    event: MessageEvent,
    phone: str = ArgPlainText("phone"),
    confirm: str = ArgPlainText("confirm"),
):
    if confirm.strip() != "确认":
        await matcher.finish("已取消")

    if not servers.remove_account(phone):
        await matcher.finish(f"❌ 删除失败：『{_mask_phone(phone)}』不存在")

    user_name = _user_display_name(event)
    group_id = event.group_id if isinstance(event, GroupMessageEvent) else None
    log_operation(
        event.user_id, user_name, group_id,
        f"remove_account {_mask_phone(phone)}", True,
    )
    await matcher.finish(f"✅ 已删除账号『{_mask_phone(phone)}』及其绑定")


# ============================================================
# 指令: 绑定账号 <服务器> <手机号>
# ============================================================

bind_account_cmd = on_command(
    "绑定账号",
    aliases={"绑账号"},
    priority=10, block=True,
)


@bind_account_cmd.handle()
async def _bind_account(
    matcher: Matcher,
    event: MessageEvent,
    arg: Message = CommandArg(),
):
    ok, reason = _check_perm(event)
    if not ok:
        if reason:
            await matcher.finish(reason)
        return

    parts = arg.extract_plain_text().split()
    if len(parts) != 2:
        all_servers = servers.list_servers()
        accounts = servers.list_accounts()
        hint = "用法：绑定账号 <服务器名字> <手机号>"
        if all_servers:
            hint += f"\n服务器：{_format_server_names(all_servers)}"
        if accounts:
            hint += "\n账号：" + "、".join(_mask_phone(a.phone) for a in accounts)
        await matcher.finish(hint)

    server_name, phone = parts
    server = servers.get_server(server_name)
    if not server:
        await matcher.finish(f"找不到服务器『{server_name}』")
    if not servers.get_account(phone):
        await matcher.finish(
            f"找不到账号『{_mask_phone(phone)}』。"
            f"先发『添加账号』把它加进来。"
        )

    if not servers.bind_server_account(server_name, phone):
        await matcher.finish("❌ 绑定失败")

    user_name = _user_display_name(event)
    group_id = event.group_id if isinstance(event, GroupMessageEvent) else None
    log_operation(
        event.user_id, user_name, group_id,
        f"bind {server_name} → {_mask_phone(phone)}", True,
    )
    await matcher.finish(
        f"✅ 已把账号『{_mask_phone(phone)}』绑给『{server_name}』。\n"
        f"以后这台 token 失效时会自动登录刷新。"
    )




# ============================================================

# 指令: mc <玩家名> - 查询 Minecraft 玩家资料并生成图片卡

# ============================================================



mc_profile_cmd = on_command(

    "mc",

    aliases={"查mc", "查MC", "皮肤", "skin", "Skin"},

    priority=10, block=True,

)





@mc_profile_cmd.handle()

async def _mc_profile(

    matcher: Matcher,

    event: MessageEvent,

    arg: Message = CommandArg(),

):

    ok, reason = _check_perm(event)

    if not ok:

        if reason:

            await matcher.finish(reason)

        return



    name = arg.extract_plain_text().strip()

    if not name:

        await matcher.finish("用法: mc <正版玩家名>\n例: mc XiaoRanTwT")

    if " " in name:

        await matcher.finish("玩家名里不能有空格。用法: mc <正版玩家名>")



    user_name = _user_display_name(event)

    group_id = event.group_id if isinstance(event, GroupMessageEvent) else None

    await matcher.send(f"正在查询玩家 {name} 的信息，请稍候...")



    try:

        profile = await mc_profile.fetch_profile(name)

        card = mc_profile.render_card(profile)

        log_operation(

            event.user_id, user_name, group_id,

            f"mc_profile {name}", True,

            f"name={profile.name} uuid={profile.uuid}",

        )

        await matcher.finish(MessageSegment.image(card.read_bytes()))

    except mc_profile.ProfileError as e:

        log_operation(

            event.user_id, user_name, group_id,

            f"mc_profile {name}", False, str(e),

        )

        await matcher.finish(f"❌ 查询失败：{e}")

    except MatcherException:

        raise

    except Exception as e:

        logger.exception(f"查询 Minecraft 玩家资料失败: {name}")

        log_operation(

            event.user_id, user_name, group_id,

            f"mc_profile {name}", False, f"未知: {e}",

        )

        await matcher.finish(f"❌ 查询异常：{type(e).__name__}")



# ============================================================
# 指令: 指令 [<服务器名>] <MC 命令>
#       把命令发到 minecraft 服务器控制台（等于网页面板那个指令框）
# ============================================================

mc_cmd_cmd = on_command(
    "指令",
    aliases={"cmd", "命令", "MC"},
    priority=10, block=True,
)


@mc_cmd_cmd.handle()
async def _mc_cmd(
    matcher: Matcher,
    event: MessageEvent,
    arg: Message = CommandArg(),
):
    ok, reason = _check_perm(event)
    if not ok:
        if reason:
            await matcher.finish(reason)
        return

    text = arg.extract_plain_text().strip()
    if not text:
        await matcher.finish(
            "用法：\n"
            "  指令 <命令>            (单台服务器时)\n"
            "  指令 <服务器名> <命令> (多台时)\n"
            "例：指令 op PpPpPp"
        )

    all_servers = servers.list_servers()
    if not all_servers:
        await matcher.finish("还没配置任何服务器")

    # 解析：第一个 token 是服务器名 → 用它；否则全当命令（单台场景）
    parts = text.split(maxsplit=1)
    if len(parts) == 2 and servers.get_server(parts[0]):
        server_name = parts[0]
        command = parts[1]
    elif len(all_servers) == 1:
        server_name = all_servers[0].name
        command = text
    else:
        names = "、".join(s.name for s in all_servers)
        await matcher.finish(
            f"有多台服务器（{names}），需要指定：指令 <服务器名> <命令>"
        )

    server = servers.get_server(server_name)
    if not server.instance_uuid:
        await matcher.finish(
            f"『{server_name}』没配实例 UUID,无法发指令。"
            f"发『修改uuid {server_name} <id>』先配上"
        )
    if not server.account_phone:
        await matcher.finish(
            f"『{server_name}』没绑账号,无法发指令。"
            f"发『绑定账号 {server_name} <手机号>』"
        )

    # 确保有 cookies
    perm_ok, perm_msg, server = await _ensure_panel_auth(server)
    if not perm_ok:
        await matcher.finish(f"❌ 鉴权失败：{perm_msg}")

    user_id = event.user_id
    user_name = _user_display_name(event)
    group_id = event.group_id if isinstance(event, GroupMessageEvent) else None

    refresh_attempted = False
    while True:
        account = servers.get_account(server.account_phone)
        if not account or not account.session_cookie or not account.xsrf_token:
            await matcher.finish("❌ 面板 cookies 丢失")

        try:
            async with PanelClient(
                account.session_cookie, account.xsrf_token,
            ) as panel:
                await panel.send_command(server.instance_uuid, command)
            log_operation(
                user_id, user_name, group_id,
                f"mc_cmd {server.name}: {command[:100]}", True,
            )
            await matcher.finish(
                f"✅ 已发送到『{server.name}』:\n{command}"
            )

        except AuthError as e:
            if refresh_attempted:
                await matcher.finish(f"❌ 鉴权后仍失败：{e}")
            refresh_attempted = True
            await matcher.send(
                f"⏳ cookies 失效,用账号 "
                f"{_mask_phone(server.account_phone)} 刷新中..."
            )
            ok, msg = await _refresh_token_for(server)
            if not ok:
                await matcher.finish(f"❌ 刷新失败：{msg}")
            continue

        except MatcherException:
            raise

        except MinekuaiError as e:
            log_operation(
                user_id, user_name, group_id,
                f"mc_cmd {server.name}", False, str(e),
            )
            await matcher.finish(f"❌ 发送失败：{e}")

        except Exception as e:
            logger.exception(f"发指令到 {server.name} 时异常")
            log_operation(
                user_id, user_name, group_id,
                f"mc_cmd {server.name}", False,
                f"{type(e).__name__}: {e}",
            )
            await matcher.finish(f"❌ 意外错误：{type(e).__name__}: {e}")


# ============================================================
# 指令: 在线（查服务器人数）
# ============================================================

status_cmd = on_command(
    "在线",
    aliases={"人数", "状态", "status"},
    priority=10, block=True,
)


@status_cmd.handle()
async def _status(
    matcher: Matcher,
    event: MessageEvent,
    arg: Message = CommandArg(),
):
    ok, reason = _check_perm(event)
    if not ok:
        if reason:
            await matcher.finish(reason)
        return

    name_arg = arg.extract_plain_text().strip()
    all_servers = servers.list_servers()
    if not all_servers:
        await matcher.finish("还没配置任何服务器")

    targets: list[servers.Server]
    if name_arg:
        s = servers.get_server(name_arg)
        if not s:
            await matcher.finish(f"找不到服务器『{name_arg}』")
        targets = [s]
    else:
        targets = all_servers

    # 并发查所有
    import asyncio as _asyncio

    async def _q(s):
        if not s.address:
            return s, None
        try:
            return s, await idle_watcher.query_status(s.address)
        except Exception:
            return s, None

    results = await _asyncio.gather(*(_q(s) for s in targets))

    def _names(s, st) -> list[str]:
        """优先用控制台/日志跟踪到的真实玩家名;
        服务器在 SLP 里把名字匿名成 'Anonymous Player' 时尤其有用。
        拿不到就退回 SLP 列表(过滤掉匿名占位)。"""
        tracked = idle_watcher.online_player_names(s.name)
        if tracked:
            return sorted(tracked)
        sample = (st.players if st else []) or []
        return [p for p in sample if p and p.lower() != "anonymous player"]

    if len(targets) == 1:
        s, st = results[0]
        if not s.address:
            await matcher.finish(
                f"🎮 {s.name}\n没填地址,无法查询\n发『修改地址 {s.name}』填一下"
            )
        if st is None:
            await matcher.finish(
                f"🎮 {s.name}\n🔴 离线 / 不可达\n"
                f"地址: {s.address}\n"
                f"(开服后 30-60 秒才能 ping 到)"
            )
        names = _names(s, st)
        lines = [
            f"🎮 {s.name}",
            f"🟢 {st.online}/{st.max} 在线  ({st.latency_ms}ms)",
        ]
        if names:
            lines.append(f"玩家: {', '.join(names)}")
        lines.append(f"地址: {s.address}")
        if st.version:
            lines.append(f"版本: {st.version}")
        await matcher.finish("\n".join(lines))

    # 多服务器汇总
    lines = ["🎮 服务器在线状态"]
    for s, st in results:
        if not s.address:
            lines.append(f"  ▸ {s.name}: ⚠️ 未填地址")
        elif st is None:
            lines.append(f"  ▸ {s.name}: 🔴 离线")
        else:
            names = _names(s, st)
            tail = f"  ({', '.join(names)})" if names else ""
            lines.append(
                f"  ▸ {s.name}: 🟢 {st.online}/{st.max}{tail}"
            )
    await matcher.finish("\n".join(lines))


# ============================================================
# 指令: 自动关停 <名字> <分钟数>
# ============================================================

auto_close_cmd = on_command(
    "自动关停",
    aliases={"自动关服"},
    priority=10, block=True,
)


@auto_close_cmd.handle()
async def _auto_close(
    matcher: Matcher,
    event: MessageEvent,
    arg: Message = CommandArg(),
):
    ok, reason = _check_perm(event)
    if not ok:
        if reason:
            await matcher.finish(reason)
        return

    text = arg.extract_plain_text().strip()

    # 无参 / 列表关键字 → 显示所有设置
    if not text or text in ("列表", "list"):
        all_servers = servers.list_servers()
        if not all_servers:
            await matcher.finish("还没配置任何服务器")
        lines = ["⏱ 自动关停设置："]
        for s in all_servers:
            if s.auto_close_idle_minutes <= 0:
                lines.append(f"  ▸ {s.name}：未启用")
            else:
                lines.append(
                    f"  ▸ {s.name}：空闲 {s.auto_close_idle_minutes} 分钟自动关"
                )
        pause = idle_watcher.get_pause_until()
        if pause > 0:
            from time import time as _t
            mins = int((pause - _t()) / 60) + 1
            lines.append(f"\n⏸ 全局暂停中（还剩约 {mins} 分钟）")
        pending = idle_watcher.list_pending()
        if pending:
            lines.append(f"\n⏳ 倒计时中: {', '.join(pending)}")
        lines.append("\n用法：自动关停 <名字> <分钟数>（0=关闭）")
        await matcher.finish("\n".join(lines))

    parts = text.split()
    if len(parts) != 2:
        await matcher.finish(
            "用法：\n"
            "自动关停                  - 查看所有设置\n"
            "自动关停 <名字> <分钟数>  - 设置（0=关闭）\n"
            "例：自动关停 GTNH 10"
        )

    name, mins_str = parts
    if not servers.get_server(name):
        await matcher.finish(f"找不到服务器『{name}』")
    if not mins_str.isdigit():
        await matcher.finish("分钟数必须是非负整数")
    mins = int(mins_str)
    if mins > 24 * 60:
        await matcher.finish("分钟数太大（不超过 24 小时）")

    if not servers.update_auto_close(name, mins):
        await matcher.finish(f"❌ 更新失败")

    user_name = _user_display_name(event)
    group_id = event.group_id if isinstance(event, GroupMessageEvent) else None
    log_operation(
        event.user_id, user_name, group_id,
        f"auto_close {name} = {mins}min", True,
    )

    if mins == 0:
        # 关掉的话也取消可能正在跑的倒计时
        idle_watcher.cancel_pending(name)
        await matcher.finish(f"✅ 『{name}』自动关停已关闭")
    else:
        s = servers.get_server(name)
        addr_hint = (
            f"" if s and s.address
            else f"\n⚠️ 该服务器没填地址，无法 SLP 查询，需要先『修改地址 {name}』"
        )
        await matcher.finish(
            f"✅ 『{name}』空闲 {mins} 分钟后自动关停{addr_hint}"
        )


# ============================================================
# 指令: 暂停自动关停 [分钟数]
# ============================================================

pause_close_cmd = on_command(
    "暂停自动关停",
    aliases={"暂停关停"},
    priority=10, block=True,
)


@pause_close_cmd.handle()
async def _pause_close(
    matcher: Matcher,
    event: MessageEvent,
    arg: Message = CommandArg(),
):
    ok, reason = _check_perm(event)
    if not ok:
        if reason:
            await matcher.finish(reason)
        return

    text = arg.extract_plain_text().strip()
    if not text:
        mins = 60
    elif text.isdigit():
        mins = int(text)
    else:
        await matcher.finish("用法：暂停自动关停 [分钟数]（默认 60）")
    if mins <= 0 or mins > 24 * 60:
        await matcher.finish("分钟数应该在 1-1440 之间")

    until_ts = idle_watcher.pause_for(mins)
    cancelled = idle_watcher.cancel_all_pending()
    extra = (
        f"\n并取消了正在倒计时的关停: {', '.join(cancelled)}"
        if cancelled else ""
    )

    from datetime import datetime
    until_str = datetime.fromtimestamp(until_ts).strftime("%H:%M")
    user_name = _user_display_name(event)
    group_id = event.group_id if isinstance(event, GroupMessageEvent) else None
    log_operation(
        event.user_id, user_name, group_id,
        f"pause auto_close {mins}min", True,
    )
    await matcher.finish(
        f"⏸ 自动关停已暂停 {mins} 分钟（至 {until_str}）{extra}"
    )


# ============================================================
# 指令: 取消关停（取消所有正在倒计时的自动关停）
# ============================================================

cancel_close_cmd = on_fullmatch(
    ("取消关停", "保留"),
    priority=9, block=True,
)


@cancel_close_cmd.handle()
async def _cancel_close(matcher: Matcher, event: MessageEvent):
    ok, reason = _check_perm(event)
    if not ok:
        if reason:
            await matcher.finish(reason)
        return

    cancelled = idle_watcher.cancel_all_pending()
    if not cancelled:
        await matcher.finish("当前没有正在倒计时的自动关停")

    user_name = _user_display_name(event)
    group_id = event.group_id if isinstance(event, GroupMessageEvent) else None
    log_operation(
        event.user_id, user_name, group_id,
        f"cancel auto_close {','.join(cancelled)}", True,
    )
    await matcher.finish(
        f"✅ 已取消关停: {', '.join(cancelled)}"
    )


# ============================================================
# 指令: 查服 / 模组 / 插件（只读面板查询）
# ============================================================

_STATE_ZH = {
    "running": "运行中",
    "offline": "已关停",
    "starting": "启动中",
    "stopping": "关停中",
}


def _fmt_size(n_bytes: float | int | None) -> str:
    """字节数 → 人类可读（B/K/M/G）。"""
    if not n_bytes or n_bytes <= 0:
        return "0"
    n = float(n_bytes)
    for u in ("B", "K", "M", "G"):
        if n < 1024:
            return f"{n:.0f}{u}" if u in ("B", "K") else f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}T"


def _fmt_mb(mb: float | int | None) -> str:
    """面板配额单位是 MB,转人类可读。"""
    if not mb or mb <= 0:
        return "无限"
    return f"{mb}M" if mb < 1024 else f"{mb/1024:.1f}G"


async def _with_panel_refresh(matcher: Matcher, server, fn):
    """调 PanelClient 操作,401/419 自动刷一次 token 再重试。
    返回 (result | None, err_msg, server)。
    """
    ok, msg, server = await _ensure_panel_auth(server)
    if not ok:
        return None, msg, server

    refresh_attempted = False
    while True:
        account = servers.get_account(server.account_phone)
        if not account or not account.session_cookie or not account.xsrf_token:
            return None, "面板 cookies 丢失", server
        try:
            async with PanelClient(
                account.session_cookie, account.xsrf_token,
            ) as panel:
                result = await fn(panel)
            return result, "ok", server
        except AuthError as e:
            if refresh_attempted:
                return None, f"刷新后仍认证失败: {e}", server
            refresh_attempted = True
            await matcher.send(
                f"⏳ 面板 cookies 失效,正在用账号 "
                f"{_mask_phone(server.account_phone)} 重新登录..."
            )
            ok, msg = await _refresh_token_for(server)
            if not ok:
                return None, f"刷新失败: {msg}", server
            server = servers.get_server(server.name) or server
            continue
        except MinekuaiError as e:
            return None, str(e), server
        except Exception as e:
            logger.exception("[panel] 查询时发生未预期异常")
            return None, f"意外错误 {type(e).__name__}: {e}", server


async def _panel_run_bg(server, fn):
    """后台(无 matcher)版的面板调用器,供 idle_watcher 注入使用。
    401/419 自动刷一次 token。返回 (result | None, err_msg)。
    """
    ok, msg, server = await _ensure_panel_auth(server)
    if not ok:
        return None, msg
    refresh_attempted = False
    while True:
        account = servers.get_account(server.account_phone)
        if not account or not account.session_cookie or not account.xsrf_token:
            return None, "面板 cookies 丢失"
        try:
            async with PanelClient(
                account.session_cookie, account.xsrf_token,
            ) as panel:
                result = await fn(panel)
            return result, "ok"
        except AuthError as e:
            if refresh_attempted:
                return None, f"刷新后仍认证失败: {e}"
            refresh_attempted = True
            logger.info(f"[panel-bg] {server.name} cookies 失效,自动刷新")
            ok, msg = await _refresh_token_for(server)
            if not ok:
                return None, f"刷新失败: {msg}"
            server = servers.get_server(server.name) or server
            continue
        except MinekuaiError as e:
            return None, str(e)
        except Exception as e:
            logger.exception("[panel-bg] 查询时发生未预期异常")
            return None, f"意外错误 {type(e).__name__}: {e}"


def _pick_target_server(matcher: Matcher, raw: str, usage_hint: str):
    """通用:无参时若只有一台直接选,否则提示用法。返回 Server 或抛 finish。"""
    all_servers = servers.list_servers()
    if not all_servers:
        # 协程外抛会被吃掉,所以由调用方做 await matcher.finish
        return None, "还没配置任何服务器"
    if not raw:
        if len(all_servers) == 1:
            return all_servers[0], None
        names = ", ".join(s.name for s in all_servers)
        return None, f"{usage_hint}\n已配置: {names}"
    s = servers.get_server(raw)
    if not s:
        return None, f"找不到服务器『{raw}』"
    return s, None


info_cmd = on_command(
    "查服",
    aliases={"服务器信息", "服务器详情", "info"},
    priority=10, block=True,
)


@info_cmd.handle()
async def _info(
    matcher: Matcher,
    event: MessageEvent,
    arg: Message = CommandArg(),
):
    ok, reason = _check_perm(event)
    if not ok:
        if reason:
            await matcher.finish(reason)
        return

    raw = arg.extract_plain_text().strip()
    server, err = _pick_target_server(matcher, raw, "用法: 查服 <名字>")
    if err:
        await matcher.finish(err)
    if not server.instance_uuid:
        await matcher.finish(
            f"🛠 {server.name}\n"
            f"未配置实例 UUID(仅计时卡服务器),无法查询面板信息\n"
            f"如需配置: 『修改uuid {server.name} <UUID>』"
        )

    import asyncio as _asyncio

    async def _fetch(panel: PanelClient):
        return await _asyncio.gather(
            panel.get_server_info(server.instance_uuid),
            panel.get_resources(server.instance_uuid),
            panel.list_directory(server.instance_uuid, "/mods"),
            panel.list_directory(server.instance_uuid, "/plugins"),
            return_exceptions=True,
        )

    result, msg, server = await _with_panel_refresh(matcher, server, _fetch)
    if result is None:
        await matcher.finish(f"❌ 查询失败: {msg}")

    info_resp, res_resp, mods_r, plugins_r = result
    if isinstance(info_resp, Exception):
        await matcher.finish(f"❌ 查询失败: {info_resp}")
    if isinstance(res_resp, Exception):
        await matcher.finish(f"❌ 查询资源失败: {res_resp}")

    info_attrs = info_resp.get("attributes", {}) if isinstance(info_resp, dict) else {}
    res_attrs = res_resp.get("attributes", {}) if isinstance(res_resp, dict) else {}
    state_raw = res_attrs.get("current_state", "") or ""
    state = _STATE_ZH.get(state_raw, state_raw or "未知")

    limits = info_attrs.get("limits", {}) or {}
    mem_limit_mb = limits.get("memory") or 0
    disk_limit_mb = limits.get("disk") or 0
    cpu_limit = limits.get("cpu") or 0

    res = res_attrs.get("resources", {}) or {}
    mem_used = res.get("memory_bytes", 0) or 0
    cpu_used = float(res.get("cpu_absolute", 0.0) or 0.0)
    disk_used = res.get("disk_bytes", 0) or 0

    # 端口分配 fallback:服务器配置里没填 address 时,从面板拿
    addr_line = server.address or ""
    if not addr_line:
        allocs = (
            info_attrs.get("relationships", {})
            .get("allocations", {})
            .get("data", [])
        ) or []
        if allocs:
            default = next(
                (a.get("attributes", {}) for a in allocs
                 if a.get("attributes", {}).get("is_default")),
                allocs[0].get("attributes", {}),
            )
            host = default.get("ip_alias") or default.get("ip")
            port = default.get("port")
            if host and port:
                addr_line = f"{host}:{port}"
    if not addr_line:
        addr_line = "(未填)"

    def _count_jars(listing):
        if isinstance(listing, Exception):
            return None
        return sum(
            1 for f in listing
            if f.get("is_file") and f.get("name", "").lower().endswith(".jar")
        )

    mod_count = _count_jars(mods_r)
    plugin_count = _count_jars(plugins_r)

    cpu_part = f"CPU {cpu_used:.1f}%"
    if cpu_limit:
        cpu_part += f"/{cpu_limit}%"
    mem_part = f"内存 {_fmt_size(mem_used)}/{_fmt_mb(mem_limit_mb)}"
    disk_part = f"磁盘 {_fmt_size(disk_used)}/{_fmt_mb(disk_limit_mb)}"

    lines = [
        f"🛠 {server.name} [{state}]",
        f"地址: {addr_line}",
        f"{cpu_part} | {mem_part} | {disk_part}",
    ]
    if mod_count is not None:
        lines.append(f"模组: {mod_count} 个  (用『模组 {server.name}』看列表)")
    if plugin_count is not None:
        lines.append(f"插件: {plugin_count} 个  (用『插件 {server.name}』看列表)")
    if mod_count is None and plugin_count is None:
        lines.append("(没找到 /mods 也没找到 /plugins)")

    await matcher.finish("\n".join(lines))


async def _do_list_jars(
    matcher: Matcher,
    event: MessageEvent,
    raw: str,
    directory: str,
    kind: str,
):
    ok, reason = _check_perm(event)
    if not ok:
        if reason:
            await matcher.finish(reason)
        return
    server, err = _pick_target_server(matcher, raw, f"用法: {kind} <名字>")
    if err:
        await matcher.finish(err)
    if not server.instance_uuid:
        await matcher.finish(f"🛠 {server.name}\n未配置实例 UUID,无法查询")

    async def _do(panel: PanelClient):
        return await panel.list_directory(server.instance_uuid, directory)

    result, msg, server = await _with_panel_refresh(matcher, server, _do)
    if result is None:
        await matcher.finish(f"❌ {msg}")

    jars = sorted(
        (f.get("name", "") for f in result
         if f.get("is_file") and f.get("name", "").lower().endswith(".jar")),
        key=str.lower,
    )
    if not jars:
        await matcher.finish(
            f"🛠 {server.name}\n{directory} 下没找到 .jar 文件"
        )

    header = f"🛠 {server.name} {kind}列表 ({len(jars)} 个)"
    max_chars = 4500
    out = [header]
    cur = len(header)
    shown = 0
    for i, name in enumerate(jars, 1):
        line = f"{i}. {name}"
        if cur + len(line) + 1 > max_chars:
            break
        out.append(line)
        cur += len(line) + 1
        shown = i
    if shown < len(jars):
        out.append(f"...还有 {len(jars) - shown} 个未显示")
    await matcher.finish("\n".join(out))


mods_cmd = on_command(
    "模组", aliases={"mods"}, priority=10, block=True,
)


@mods_cmd.handle()
async def _mods(
    matcher: Matcher,
    event: MessageEvent,
    arg: Message = CommandArg(),
):
    await _do_list_jars(
        matcher, event, arg.extract_plain_text().strip(),
        "/mods", "模组",
    )


plugins_cmd = on_command(
    "插件", aliases={"plugins"}, priority=10, block=True,
)


@plugins_cmd.handle()
async def _plugins(
    matcher: Matcher,
    event: MessageEvent,
    arg: Message = CommandArg(),
):
    await _do_list_jars(
        matcher, event, arg.extract_plain_text().strip(),
        "/plugins", "插件",
    )


# ============================================================
# 指令: 绑定 / 解绑 / 绑定列表（QQ ↔ MC 玩家名）
# ============================================================

bind_cmd = on_command(
    "绑定",
    aliases={"绑定游戏名", "bind"},
    priority=10, block=True,
)


@bind_cmd.handle()
async def _bind(
    matcher: Matcher,
    event: MessageEvent,
    arg: Message = CommandArg(),
):
    ok, reason = _check_perm(event)
    if not ok:
        if reason:
            await matcher.finish(reason)
        return

    parts = arg.extract_plain_text().split()
    # 管理员可 “绑定 <QQ> <MC名>” 替别人绑;普通 “绑定 <MC名>” 绑自己
    if len(parts) == 2 and parts[0].isdigit():
        target_qq = int(parts[0])
        mc_name = parts[1]
    elif len(parts) == 1:
        target_qq = event.user_id
        mc_name = parts[0]
    else:
        await matcher.finish(
            "用法: 绑定 <游戏名>\n(管理员可: 绑定 <QQ号> <游戏名>)"
        )

    if not re.fullmatch(r"[A-Za-z0-9_]{1,16}", mc_name):
        await matcher.finish("游戏名只能是字母/数字/下划线,1-16 位")

    try:
        servers.bind_player(target_qq, mc_name)
    except ValueError as e:
        await matcher.finish(f"❌ {e}")

    log_operation(
        event.user_id, _user_display_name(event),
        event.group_id if isinstance(event, GroupMessageEvent) else None,
        f"bind {target_qq} -> {mc_name}", True,
    )
    await matcher.finish(
        f"✅ 已绑定 QQ {target_qq} ↔ 游戏名『{mc_name}』\n"
        f"以后死亡/成就播报会 @ 你"
    )


unbind_cmd = on_command(
    "解绑", aliases={"取消绑定", "unbind"}, priority=10, block=True,
)


@unbind_cmd.handle()
async def _unbind(matcher: Matcher, event: MessageEvent, arg: Message = CommandArg()):
    ok, reason = _check_perm(event)
    if not ok:
        if reason:
            await matcher.finish(reason)
        return
    parts = arg.extract_plain_text().split()
    target_qq = int(parts[0]) if (parts and parts[0].isdigit()) else event.user_id
    if servers.unbind_player(target_qq):
        await matcher.finish(f"✅ 已解绑 QQ {target_qq}")
    await matcher.finish(f"QQ {target_qq} 当前没有绑定")


bindings_cmd = on_command(
    "绑定列表", aliases={"绑定情况"}, priority=10, block=True,
)


@bindings_cmd.handle()
async def _bindings(matcher: Matcher, bot: Bot, event: MessageEvent):
    ok, reason = _check_perm(event)
    if not ok:
        if reason:
            await matcher.finish(reason)
        return
    rows = servers.list_bindings()
    if not rows:
        await matcher.finish("还没有人绑定游戏名。发『绑定 <游戏名>』绑定")

    group_id = event.group_id if isinstance(event, GroupMessageEvent) else None

    async def _name(qq: int) -> str:
        if group_id is not None:
            try:
                info = await bot.get_group_member_info(
                    group_id=group_id, user_id=qq, no_cache=False,
                )
                return info.get("card") or info.get("nickname") or str(qq)
            except Exception:
                pass
        try:
            info = await bot.get_stranger_info(user_id=qq)
            return info.get("nickname") or str(qq)
        except Exception:
            return str(qq)

    lines = [f"🔗 绑定列表 ({len(rows)})"]
    for qq, mc in rows:
        lines.append(f"  ▸ {mc}  ←  {await _name(qq)}")
    await matcher.finish("\n".join(lines))


# ============================================================
# 指令: 今日榜 / 本周榜 / 在线时长（玩家在线时长统计）
# ============================================================

def _fmt_duration(seconds: int) -> str:
    h, rem = divmod(int(seconds), 3600)
    m = rem // 60
    if h > 0:
        return f"{h}h{m:02d}m"
    return f"{m}m"


def _day_offset(days_back: int) -> str:
    from datetime import datetime, timedelta
    return (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")


async def _do_leaderboard(matcher: Matcher, event: MessageEvent, days: int, title: str):
    ok, reason = _check_perm(event)
    if not ok:
        if reason:
            await matcher.finish(reason)
        return
    since = _day_offset(days - 1)
    until = _day_offset(0)
    rows = servers.playtime_leaderboard(since, until, limit=15)
    if not rows:
        await matcher.finish(f"📊 {title}\n(暂无记录)")
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"📊 {title}"]
    for i, (mc, secs) in enumerate(rows):
        rank = medals[i] if i < 3 else f"{i+1}."
        bound = "🔗" if servers.get_qq_by_mc(mc) else ""
        lines.append(f"{rank} {mc}{bound}  {_fmt_duration(secs)}")
    await matcher.finish("\n".join(lines))


today_rank_cmd = on_command(
    "今日榜", aliases={"今日时长", "今天榜"}, priority=10, block=True,
)


@today_rank_cmd.handle()
async def _today_rank(matcher: Matcher, event: MessageEvent):
    await _do_leaderboard(matcher, event, 1, "今日在线时长榜")


week_rank_cmd = on_command(
    "本周榜", aliases={"周榜", "本周时长", "时长榜"}, priority=10, block=True,
)


@week_rank_cmd.handle()
async def _week_rank(matcher: Matcher, event: MessageEvent):
    await _do_leaderboard(matcher, event, 7, "近 7 天在线时长榜")


playtime_cmd = on_command(
    "在线时长", aliases={"我的时长", "playtime"}, priority=10, block=True,
)


@playtime_cmd.handle()
async def _playtime(matcher: Matcher, event: MessageEvent, arg: Message = CommandArg()):
    ok, reason = _check_perm(event)
    if not ok:
        if reason:
            await matcher.finish(reason)
        return
    raw = arg.extract_plain_text().strip()
    mc_name = raw or servers.get_mc_by_qq(event.user_id)
    if not mc_name:
        await matcher.finish(
            "用法: 在线时长 <游戏名>\n(或先『绑定 <游戏名>』后直接发『在线时长』)"
        )
    today = servers.get_playtime_total(mc_name, _day_offset(0), _day_offset(0))
    week = servers.get_playtime_total(mc_name, _day_offset(6), _day_offset(0))
    await matcher.finish(
        f"⏱ {mc_name} 在线时长\n"
        f"今日: {_fmt_duration(today)}\n"
        f"近 7 天: {_fmt_duration(week)}"
    )


# ============================================================
# 指令: 死亡榜 / 死亡次数（死亡统计）
# ============================================================

death_rank_cmd = on_command(
    "死亡榜", aliases={"死亡排行", "送人头榜"}, priority=10, block=True,
)


@death_rank_cmd.handle()
async def _death_rank(matcher: Matcher, event: MessageEvent):
    ok, reason = _check_perm(event)
    if not ok:
        if reason:
            await matcher.finish(reason)
        return
    # 全量(从有记录以来)
    rows = servers.death_leaderboard("0000-00-00", "9999-99-99", limit=15)
    if not rows:
        await matcher.finish("📊 死亡榜\n(暂无记录,死给我看看?)")
    medals = ["💀", "☠️", "⚰️"]
    lines = ["📊 死亡榜（累计）"]
    for i, (mc, n) in enumerate(rows):
        rank = medals[i] if i < 3 else f"{i+1}."
        bound = "🔗" if servers.get_qq_by_mc(mc) else ""
        lines.append(f"{rank} {mc}{bound}  {n} 次")
    await matcher.finish("\n".join(lines))


death_cnt_cmd = on_command(
    "死亡次数", aliases={"我死了几次", "deaths"}, priority=10, block=True,
)


@death_cnt_cmd.handle()
async def _death_cnt(matcher: Matcher, event: MessageEvent, arg: Message = CommandArg()):
    ok, reason = _check_perm(event)
    if not ok:
        if reason:
            await matcher.finish(reason)
        return
    raw = arg.extract_plain_text().strip()
    mc_name = raw or servers.get_mc_by_qq(event.user_id)
    if not mc_name:
        await matcher.finish(
            "用法: 死亡次数 <游戏名>\n(或先『绑定 <游戏名>』后直接发『死亡次数』)"
        )
    today = servers.get_death_total(mc_name, _day_offset(0), _day_offset(0))
    total = servers.get_death_total(mc_name, "0000-00-00", "9999-99-99")
    await matcher.finish(
        f"💀 {mc_name} 死亡次数\n今日: {today} 次\n累计: {total} 次"
    )


# ============================================================
# 聊天桥: QQ 群消息 → 游戏内（tellraw，仅在有人在线时转发）
# ============================================================

chat_relay = on_message(priority=99, block=False)


@chat_relay.handle()
async def _chat_relay(bot: Bot, event: MessageEvent):
    if not isinstance(event, GroupMessageEvent):
        return
    if not (config.chat_bridge and config.chat_qq_to_mc):
        return
    ok, _ = _check_perm(event)
    if not ok:
        return
    text = event.get_plaintext().strip()
    if not text:
        return
    # 收敛长度 + 去掉换行(tellraw 单行)
    text = text.replace("\n", " ")
    if len(text) > 200:
        text = text[:200] + "…"
    sender = _user_display_name(event)

    import json
    payload = json.dumps(
        {"text": f"[QQ] {sender}: {text}", "color": "aqua"},
        ensure_ascii=False,
    )

    for s in servers.list_servers():
        if not (s.instance_uuid and s.account_phone):
            continue
        # 没人在线就不发,省请求也避免对离线服发指令
        if not idle_watcher.has_online_players(s.name):
            continue

        async def _send(panel, _uuid=s.instance_uuid, _p=payload):
            await panel.send_command(_uuid, f"tellraw @a {_p}")

        await _panel_run_bg(s, _send)


# ============================================================
# 指令: 日志（拿服务器控制台最近 N 行)
# ============================================================

log_cmd = on_command(
    "日志",
    aliases={"log", "console", "控制台"},
    priority=10, block=True,
)


@log_cmd.handle()
async def _log(
    matcher: Matcher,
    event: MessageEvent,
    arg: Message = CommandArg(),
):
    ok, reason = _check_perm(event)
    if not ok:
        if reason:
            await matcher.finish(reason)
        return

    # 参数:可有可无的服务器名 + 可有可无的行数(数字)
    parts = arg.extract_plain_text().split()
    name_part: str | None = None
    n_lines = 30
    for p in parts:
        if p.isdigit():
            n_lines = max(1, min(int(p), 200))
        else:
            name_part = p

    server, err = _pick_target_server(
        matcher, name_part or "", "用法: 日志 [名字] [行数=30]",
    )
    if err:
        await matcher.finish(err)
    if not server.instance_uuid:
        await matcher.finish(
            f"🛠 {server.name}\n未配置实例 UUID,无法读日志"
        )

    async def _fetch(panel: PanelClient):
        return await panel.read_file_text(
            server.instance_uuid, "/logs/latest.log",
        )

    result, msg, server = await _with_panel_refresh(matcher, server, _fetch)
    if result is None:
        await matcher.finish(f"❌ 读日志失败: {msg}")

    lines = result.splitlines()
    if not lines:
        await matcher.finish(f"📋 {server.name} 日志为空")
    tail = lines[-n_lines:]
    text = "\n".join(tail)
    # 控制单条 QQ 消息长度
    max_chars = 4500
    truncated = ""
    if len(text) > max_chars:
        text = text[-max_chars:]
        truncated = "...(已截断)\n"
    await matcher.finish(
        f"📋 {server.name} latest.log 最后 {len(tail)} 行:\n"
        f"{truncated}{text}"
    )


# ============================================================
# 指令: 重启（实例重启,不动计时卡)
# ============================================================

restart_cmd = on_command(
    "重启",
    aliases={"restart", "Restart", "RESTART"},
    priority=10, block=True,
)


@restart_cmd.handle()
async def _restart(
    matcher: Matcher,
    event: MessageEvent,
    arg: Message = CommandArg(),
):
    ok, reason = _check_perm(event)
    if not ok:
        if reason:
            await matcher.finish(reason)
        return

    raw = arg.extract_plain_text().strip()
    server, err = _pick_target_server(matcher, raw, "用法: 重启 <名字>")
    if err:
        await matcher.finish(err)
    if not server.instance_uuid:
        await matcher.finish(
            f"🛠 {server.name}\n未配置实例 UUID,无法重启"
        )

    async def _do(panel: PanelClient):
        await panel.power(server.instance_uuid, "restart")
        return True

    result, msg, server = await _with_panel_refresh(matcher, server, _do)
    if result is None:
        await matcher.finish(f"❌ 重启失败: {msg}")

    # 重启后:重置空闲计时 + 起 ready 监视(等真正能进入再播报)
    servers.mark_server_started(server.name)
    idle_watcher.mark_opened(server.name)
    if server.address:
        idle_watcher.watch_for_ready(server)

    user_name = _user_display_name(event)
    group_id = event.group_id if isinstance(event, GroupMessageEvent) else None
    log_operation(
        event.user_id, user_name, group_id,
        f"restart {server.name}", True,
    )

    await matcher.finish(
        f"🔄 『{server.name}』重启指令已下达\n"
        f"30-60 秒后会通知就绪状态"
    )


# ============================================================
# 指令: 帮助
# ============================================================

help_cmd = on_fullmatch(
    ("帮助", "help", "Help", "HELP"),
    priority=10, block=True,
)


HELP_IMG_PATH = Path(__file__).parent / "assets" / "help.png"


@help_cmd.handle()
async def _help(matcher: Matcher, event: MessageEvent):
    group_id = event.group_id if isinstance(event, GroupMessageEvent) else None
    if config.allowed_groups and group_id not in config.allowed_groups:
        return

    # 优先发图片
    try:
        if HELP_IMG_PATH.is_file():
            await matcher.finish(
                MessageSegment.image(HELP_IMG_PATH.read_bytes())
            )
    except MatcherException:
        raise
    except Exception:
        logger.exception("帮助图发送失败,回退到文本版")

    # fallback: 文本版（图不存在 / 发图失败 / 协议端有问题时用）
    confirm_line = "关服 → 确认关服\n" if config.stop_need_confirm else ""
    text = (
        "🎮 麦块联机 · 张鹤杰\n"
        "\n"
        "━ 日常\n"
        "开服 [名字]          开卡 + 启动\n"
        "关服 [名字]          关闭(防误关)\n"
        f"{confirm_line}"
        "在线 [名字]          查在线 / 延迟\n"
        "查服 [名字]          状态/CPU/内存/模组数\n"
        "模组 [名字]          列出 /mods 下 .jar\n"
        "插件 [名字]          列出 /plugins 下 .jar\n"
        "日志 [名字] [行数]   最近 N 行控制台\n"
        "\n"
        "━ 运维\n"
        "重启 [名字]          实例重启(不动计时卡)\n"
        "指令 [名字] <命令>   发到游戏(如 op X)\n"
        "mc <玩家名>           查询正版玩家资料卡\n"
        "\n"
        "━ 玩家绑定 + 统计\n"
        "绑定 <游戏名>        绑 QQ↔游戏名\n"
        "解绑 / 绑定列表\n"
        "今日榜 / 本周榜      在线时长排行\n"
        "在线时长 [游戏名]    查个人时长\n"
        "死亡榜 / 死亡次数    死亡统计\n"
        "(加入/离开/死亡/成就 自动播报)\n"
        "(游戏聊天 ↔ QQ 群 双向转发)\n"
        "\n"
        "━ 服务器\n"
        "服务器列表 / 服务器地址 [名字]\n"
        "添加服务器 / 删除服务器 <名字>\n"
        "修改服务器名字 [旧 [新]] / 修改地址 / 修改uuid\n"
        "绑定账号 <服务器> <手机号>\n"
        "更新token <名字>  应急(一般无需)\n"
        "\n"
        "━ 账号(用于自动登录拿 token)\n"
        "添加账号 / 账号列表 / 删除账号 <手机号>\n"
        "\n"
        "━ 空闲自动关停\n"
        "自动关停 <名字> <分钟>  0=关\n"
        "自动关停                看当前设置\n"
        "暂停自动关停 [分钟]     全局暂停\n"
        "取消关停 / 保留         阻止本次\n"
        "\n"
        "━ 其它\n"
        "取消 · 帮助\n"
        "\n"
        f"群里直接发 · 不用 @ 不用前缀 · 冷却 {config.command_cooldown}s"
    )
    await matcher.finish(text)
