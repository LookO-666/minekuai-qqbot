"""空闲自动关停 - 后台轮询每个开了自动关停的服务器的在线玩家数。

实现要点：
- 用 Minecraft 的 SLP (Server List Ping) 协议查在线玩家数——不依赖任何认证
- 每 60 秒查一遍所有 auto_close_idle_minutes > 0 的服务器
- 连续空闲达到设置的分钟数 → 群里广播倒计时 → COUNTDOWN_SECONDS 秒后真关
- 用户可以发『取消关停』在倒计时内阻止
- 用户可以发『暂停自动关停 N』临时停止 N 分钟
- 开服后给 GRACE_PERIOD_SECONDS 秒启动宽限期（避免还没启动的服务器被秒关）

需要 bot 引用才能给群里发消息——通过 register_bot() 在 bot 连接时注入。
"""
import asyncio
import json
import re
from dataclasses import dataclass, field
from time import localtime, strftime, time
from typing import TYPE_CHECKING, Awaitable, Callable, Iterable

from loguru import logger

try:
    import websockets  # uvicorn/nonebot 依赖里已有
except ImportError:  # pragma: no cover
    websockets = None

from . import servers

if TYPE_CHECKING:
    from nonebot.adapters.onebot.v11 import Bot
    from .client import PanelClient


# ============================================================
# 常量
# ============================================================

POLL_INTERVAL_SECONDS = 60       # 每分钟查一次
GRACE_PERIOD_SECONDS = 5 * 60    # 开服后 5 分钟启动宽限
COUNTDOWN_SECONDS = 60           # 关停前广播 + 倒计时窗口
SLP_TIMEOUT_SECONDS = 5          # SLP 单次查询超时
KEEPALIVE_AFTER_SECONDS = 6 * 24 * 60 * 60   # 6 天没启动就保活一次
KEEPALIVE_RUNTIME_SECONDS = 5 * 60           # 保活启动后运行 5 分钟再关


# ============================================================
# 模块状态（全部内存，重启清空）
# ============================================================

# 每个服务器最后一次查到非零玩家的时间戳（time.time()）
_last_active: dict[str, float] = {}

# 每个服务器最后一次被 mark_opened 的时间戳（用于 grace period）
_open_at: dict[str, float] = {}

# 正在倒计时关停的任务 {server_name: Task}
_pending_close: dict[str, asyncio.Task] = {}

# 正在等待"服务器可进入"的任务 {server_name: Task}（开服后轮询 SLP，ping 通就广播）
_ready_watchers: dict[str, asyncio.Task] = {}

# 正在执行 6 天保活启动/关停的任务 {server_name: Task}
_keepalive_tasks: dict[str, asyncio.Task] = {}

# 全局暂停截止时间戳；time() < _pause_until 时跳过所有检查
_pause_until: float = 0.0

# 玩家上下线追踪(SLP fallback,仅用于没配面板的服务器):
# 上一轮观察到的玩家名集合 / 在线人数(重启后第一轮只记录,不广播)
_last_player_names: dict[str, set[str]] = {}
_last_online_count: dict[str, int] = {}

# ---- 日志事件播报 + 在线时长 ----
# 每个服务器 latest.log 上一轮已处理到的行数(用于增量)
_log_seen_lines: dict[str, int] = {}
# 从日志解析出的当前在线玩家集合
_online_players: dict[str, set[str]] = {}
# 进行中的在线会话起点: {(server, mc_name): 进入时间戳}
_session_start: dict[tuple[str, str], float] = {}

# ---- 实时控制台 WebSocket ----
# 每个服务器的 WS 后台任务 {server_name: Task}
_ws_tasks: dict[str, asyncio.Task] = {}
# WS 当前是否已连上(决定轮询要不要兜底解析日志)
_ws_connected: dict[str, bool] = {}

# ---- 资源告警 ----
# 连续超阈值计数: {(server, "cpu"|"mem"): 连续次数}
_breach_count: dict[tuple[str, str], int] = {}
# 上次告警时间戳: {(server, kind): ts}
_last_alert_at: dict[tuple[str, str], float] = {}
# 缓存的内存配额(MB),来自 get_server_info,避免每轮多查
_mem_limit_mb: dict[str, int] = {}
_cpu_limit: dict[str, int] = {}

# 注入:配置对象 + 面板调用器(由 __init__ 提供,避免循环依赖)
_config = None
_panel_runner: "Callable[[servers.Server, Callable[[PanelClient], Awaitable]], Awaitable[tuple]] | None" = None
_start_callback: "Callable[[servers.Server], Awaitable[tuple[bool, str]]] | None" = None


# Minecraft 死亡消息关键词(子串匹配;配合"首词是在线玩家"双重判定降误报)
_DEATH_KEYWORDS = (
    "was slain", "was shot", "was killed", "was blown up", "was fireballed",
    "was pummeled", "was struck by lightning", "was burnt", "was roasted",
    "was squashed", "was impaled", "was skewered", "was poked", "was stung",
    "was frozen", "was doomed", "was speared", "was pricked", "was squished",
    "drowned", "blew up", "hit the ground too hard", "fell from",
    "fell off", "fell out of", "fell into", "was doomed to fall",
    "was blown from", "burned to death", "went up in flames",
    "walked into fire", "walked into a cactus", "walked into danger",
    "tried to swim in lava", "discovered the floor was lava",
    "got finished off", "starved to death", "suffocated", "withered away",
    "died", "was killed by", "froze to death", "experienced kinetic energy",
    "didn't want to live", "left the confines of this world",
    "was obliterated", "was killed trying to hurt",
)
_DEATH_RE = re.compile("|".join(re.escape(k) for k in _DEATH_KEYWORDS))

# 服务端日志行:开头一个或多个 [...] 前缀块,然后 ": " 接正文。
# 兼容:
#   原版/Paper  [09:52:16] [Server thread/INFO]: msg
#   Forge       [23Jun2026 09:52:16.192] [Server thread/INFO] [logger/]: msg
#   (中文 locale 日期如 [236月2026 ...] 也能匹配,日期内容不限字符)
_LOG_LINE_RE = re.compile(r"^(?:\[[^\]]*\]\s*)+:\s?(.*)$")
_JOIN_RE = re.compile(r"^(\w{1,16}) joined the game$")
_LEAVE_RE = re.compile(r"^(\w{1,16}) left the game$")
_ADV_RE = re.compile(
    r"^(\w{1,16}) has (?:made the advancement|completed the challenge|"
    r"reached the goal) (.+)$"
)
_NAME_HEAD_RE = re.compile(r"^(\w{1,16})\b")
# 聊天: <name> msg  或  [Not Secure] <name> msg
_CHAT_RE = re.compile(r"^(?:\[Not Secure\] )?<([^>]{1,16})> (.*)$")
# 终端 ANSI / 控制序列(WS 控制台输出里有颜色码和光标控制)
_ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_=>]|\[[0-?]*[ -/]*[@-~])")

# bot 引用（用来给群里发广播）
_bot: "Bot | None" = None

# 群号列表（用来广播）
_broadcast_groups: list[int] = []

# 触发实际关停时调用的协程；由 __init__ 注入，避免循环依赖
_close_callback = None


# ============================================================
# 注入 + 启动
# ============================================================

def register_bot(bot: "Bot", broadcast_groups: Iterable[int]) -> None:
    """bot 连接时调，让 watcher 能拿到 bot 引用和广播群"""
    global _bot, _broadcast_groups
    _bot = bot
    _broadcast_groups = list(broadcast_groups)


def register_close_callback(coro) -> None:
    """注入实际执行关服的协程。签名: async fn(server: Server) -> tuple[bool, str]"""
    global _close_callback
    _close_callback = coro


def register_start_callback(coro) -> None:
    """注入实际执行保活开服的协程。签名: async fn(server) -> tuple[bool, str]"""
    global _start_callback
    _start_callback = coro


def register_config(config) -> None:
    """注入插件 Config(读取播报/告警开关与阈值)。"""
    global _config
    _config = config


def register_panel_runner(runner) -> None:
    """注入面板调用器,签名:
        async fn(server, async fn(panel)->T) -> (T | None, err_msg)
    供日志读取 / 资源查询复用 __init__ 里的认证+自动刷新逻辑。
    """
    global _panel_runner
    _panel_runner = runner


def start_watcher() -> asyncio.Task:
    """启动后台循环。返回 Task 引用，调用方持有避免被 GC。"""
    task = asyncio.create_task(_watch_loop())
    logger.info(
        f"[idle-watcher] 已启动 (轮询 {POLL_INTERVAL_SECONDS}s "
        f"/ grace {GRACE_PERIOD_SECONDS}s / countdown {COUNTDOWN_SECONDS}s)"
    )
    return task


# ============================================================
# 状态钩子：开/关服时调
# ============================================================

def mark_opened(server_name: str) -> None:
    """记录服务器刚被开起来——启动 grace period，清空空闲计时"""
    now = time()
    _open_at[server_name] = now
    _last_active[server_name] = now  # 假设刚开还在活跃
    # 日志会随重启轮转,重置增量基线避免回放历史
    _log_seen_lines.pop(server_name, None)
    _online_players.pop(server_name, None)
    _breach_count.pop((server_name, "cpu"), None)
    _breach_count.pop((server_name, "mem"), None)
    # 如果它之前在等待关停，取消
    cancel_pending(server_name)


def mark_closed(server_name: str) -> None:
    """记录服务器被关——清空相关状态"""
    _stop_ws(server_name)               # 停掉实时控制台
    _flush_sessions(server_name)        # 结算在线时长
    _open_at.pop(server_name, None)
    _last_active.pop(server_name, None)
    _log_seen_lines.pop(server_name, None)
    _online_players.pop(server_name, None)
    _breach_count.pop((server_name, "cpu"), None)
    _breach_count.pop((server_name, "mem"), None)
    cancel_pending(server_name)
    # 也取消等待"可进入"的轮询（如果还在跑）
    rw = _ready_watchers.pop(server_name, None)
    if rw is not None and not rw.done():
        rw.cancel()


def cancel_pending(server_name: str) -> bool:
    """取消某个服务器的等待关停任务。返回 True=取消了，False=本来就没"""
    task = _pending_close.pop(server_name, None)
    if task is None or task.done():
        return False
    task.cancel()
    return True


def watch_for_ready(
    server: servers.Server,
    *,
    initial_delay: int = 20,
    poll_interval: int = 5,
    timeout: int = 300,
) -> None:
    """开服后启动一个后台 task 轮询 SLP，第一次 ping 通就在群里广播。

    - initial_delay: 先等多少秒再开始查（服务器刚启动 SLP 还没起来，先睡一下）
    - poll_interval: 每隔多少秒查一次
    - timeout: 最长等多久，超时放弃（不发消息）

    同一个 server 已经有 watcher 在跑时不重复起。
    """
    if not server.address:
        return
    existing = _ready_watchers.get(server.name)
    if existing is not None and not existing.done():
        return
    task = asyncio.create_task(
        _ready_loop(server.name, server.address, initial_delay, poll_interval, timeout)
    )
    _ready_watchers[server.name] = task


async def _ready_loop(
    name: str, address: str,
    initial_delay: int, poll_interval: int, timeout: int,
) -> None:
    """实际的 SLP 轮询循环"""
    try:
        # 启动期间 SLP 必然连不上,先等一段时间再查
        await asyncio.sleep(initial_delay)
        deadline = time() + timeout
        while time() < deadline:
            status = await query_status(address)
            if status is not None:
                tail = (
                    f" ({status.online}/{status.max} 在线)"
                    if status.max else ""
                )
                await _broadcast(
                    f"🎮 『{name}』已经可以进入了！\n"
                    f"📡 {address}{tail}"
                )
                return
            await asyncio.sleep(poll_interval)
        logger.info(
            f"[ready-watcher] {name} 等了 {timeout}s 还没 ping 通,放弃通知"
        )
    except asyncio.CancelledError:
        logger.debug(f"[ready-watcher] {name} 被取消")
        raise
    finally:
        _ready_watchers.pop(name, None)


def cancel_all_pending() -> list[str]:
    """取消所有等待关停的服务器。返回被取消的服务器名字列表"""
    names = []
    for name, task in list(_pending_close.items()):
        if not task.done():
            task.cancel()
            names.append(name)
    _pending_close.clear()
    return names


def list_pending() -> list[str]:
    """返回当前正在等待关停的服务器名字"""
    return [n for n, t in _pending_close.items() if not t.done()]


def pause_for(minutes: int) -> float:
    """全局暂停自动关停 N 分钟。返回截止时间戳"""
    global _pause_until
    _pause_until = max(_pause_until, time() + minutes * 60)
    return _pause_until


def get_pause_until() -> float:
    """返回当前暂停截止时间戳，0 = 未暂停"""
    return _pause_until if _pause_until > time() else 0.0


def get_state_for(server_name: str) -> dict:
    """给指令查询用的状态快照"""
    return {
        "open_at": _open_at.get(server_name, 0),
        "last_active": _last_active.get(server_name, 0),
        "pending": server_name in _pending_close,
    }


def has_online_players(server_name: str) -> bool:
    """该服务器当前(按日志解析)是否有人在线。QQ→MC 转发用来判断要不要发。"""
    return bool(_online_players.get(server_name))


def online_player_names(server_name: str) -> set[str]:
    return set(_online_players.get(server_name, set()))


# ============================================================
# 主循环
# ============================================================

async def _watch_loop() -> None:
    """每 POLL_INTERVAL_SECONDS 跑一次检查"""
    # 启动时给每个服务器一个 grace period 初始时间（按当前时间算），
    # 这样如果 bot 重启时正好有服务器在跑，至少 5 分钟后才开始算空闲
    now = time()
    for s in servers.list_servers():
        _open_at.setdefault(s.name, now)
        _last_active.setdefault(s.name, now)

    while True:
        try:
            await _tick()
        except Exception:
            logger.exception("[idle-watcher] 循环出错（继续下一轮）")
        interval = POLL_INTERVAL_SECONDS
        if _config is not None:
            interval = max(
                5, getattr(_config, "poll_interval_seconds", POLL_INTERVAL_SECONDS)
            )
        await asyncio.sleep(interval)


def _can_panel(s: servers.Server) -> bool:
    """该服务器是否具备走面板的条件(读日志/查资源)。"""
    return bool(
        s.instance_uuid and s.account_phone
        and _panel_runner is not None and _config is not None
    )


async def _tick() -> None:
    """跑一轮检查。每个服务器:
      1. 能走面板的 → 读 latest.log 增量做事件播报 + 在线时长 + 资源告警
      2. 不能走面板但有地址的 → SLP 玩家数 diff 做粗略上下线播报
      3. 有地址的 → SLP 查在线数喂给空闲关停
    """
    if time() < _pause_until:
        return

    for s in servers.list_servers():
        # 倒计时中的服务器不查(避免干扰用户的"取消关停"决策)
        if s.name in _pending_close and not _pending_close[s.name].done():
            continue

        panel_ok = _can_panel(s)

        # ---- 面板服务器:用面板自身 current_state(权威、稳定)决定拉不拉日志 ----
        # (不再用 SLP 当门控——SLP 偶发超时会被误判成"下线"导致基线反复重建、丢事件)
        if panel_ok:
            try:
                await _poll_panel(s)
            except Exception:
                logger.exception(f"[idle] 面板轮询 {s.name} 出错")

        if not s.address:
            continue

        # SLP:给空闲关停用;没法走面板的服务器还用它做粗略上下线播报
        try:
            status = await query_status(s.address)
        except Exception:
            logger.exception(f"[idle] 查 {s.name} SLP 时异常")
            status = None

        if not (panel_ok and _config and _config.event_broadcast):
            try:
                await _track_players(s, status)
            except Exception:
                logger.exception(f"[idle] 跟踪 {s.name} 玩家变动时异常")

        if s.auto_close_idle_minutes > 0:
            try:
                await _check_idle(s, status)
            except Exception:
                logger.exception(f"[idle] 检查 {s.name} 空闲时出错")

        try:
            await _check_keepalive(s)
        except Exception:
            logger.exception(f"[keepalive] 检查 {s.name} 保活时出错")


async def _check_keepalive(s: servers.Server) -> None:
    """6 天未启动的服务器自动短暂启动一次，避免服务商回收。"""
    if _start_callback is None or _close_callback is None:
        return
    if not (s.card_id and s.instance_uuid and s.account_phone):
        return

    task = _keepalive_tasks.get(s.name)
    if task is not None and not task.done():
        return

    now = time()
    last_started = getattr(s, "last_started_at", 0) or s.created_at or s.updated_at or now
    age = now - last_started
    if age < KEEPALIVE_AFTER_SECONDS:
        return

    if await _looks_running(s):
        servers.mark_server_started(s.name)
        logger.info(f"[keepalive] {s.name} 当前已经在运行，仅刷新 last_started_at")
        return

    logger.info(f"[keepalive] {s.name} 已 {int(age // 86400)} 天未启动，开始保活")
    _keepalive_tasks[s.name] = asyncio.create_task(
        _keepalive_cycle(s.name, int(age // 86400))
    )


async def _looks_running(s: servers.Server) -> bool:
    """尽量确认服务器是否已经在运行，避免保活流程误关正在运行的服务器。"""
    if _can_panel(s):
        async def _res(panel):
            return await panel.get_resources(s.instance_uuid)

        data, err = await _panel_runner(s, _res)  # type: ignore[misc]
        if isinstance(data, dict):
            state = data.get("attributes", {}).get("current_state")
            return state in {"running", "starting"}
        if err != "ok":
            logger.warning(f"[keepalive] 查询 {s.name} 面板状态失败: {err}")
            return False

    if s.address:
        status = await query_status(s.address)
        return bool(status and status.online > 0)
    return False


async def _keepalive_cycle(server_name: str, age_days: int) -> None:
    """执行一次保活: 开服 -> 等几分钟 -> 关服。"""
    try:
        fresh = servers.get_server(server_name)
        if fresh is None:
            return

        minutes = max(1, KEEPALIVE_RUNTIME_SECONDS // 60)
        await _broadcast(
            f"🛡️ 『{server_name}』已经约 {age_days} 天没有启动，"
            f"现在自动保活开机，约 {minutes} 分钟后会自动关机。"
        )

        ok, msg = await _start_callback(fresh)  # type: ignore[misc]
        if not ok:
            await _broadcast(f"❌ 『{server_name}』保活开机失败：{msg}")
            return
        await _broadcast(f"✅ 『{server_name}』保活开机成功：{msg}")

        await asyncio.sleep(KEEPALIVE_RUNTIME_SECONDS)

        fresh = servers.get_server(server_name)
        if fresh is None:
            await _broadcast(f"⚠️ 『{server_name}』已不存在，跳过保活关机")
            return
        ok, msg = await _close_callback(fresh)
        if ok:
            mark_closed(server_name)
            await _broadcast(f"💤 『{server_name}』保活完成，已自动关机。")
        else:
            await _broadcast(f"❌ 『{server_name}』保活关机失败：{msg}")
    except asyncio.CancelledError:
        logger.info(f"[keepalive] {server_name} 保活任务被取消")
        raise
    except Exception as e:
        logger.exception(f"[keepalive] {server_name} 保活任务异常")
        await _broadcast(f"❌ 『{server_name}』保活任务异常：{type(e).__name__}: {e}")
    finally:
        _keepalive_tasks.pop(server_name, None)


async def _check_idle(s: servers.Server, status: "SlpStatus | None") -> None:
    """空闲检查;复用调用方已查好的 status,避免重复 SLP。"""
    now = time()
    open_at = _open_at.get(s.name, now)
    if now - open_at < GRACE_PERIOD_SECONDS:
        return
    if status is None:
        logger.debug(f"[idle] {s.name} SLP 查询失败/不可达,跳过本轮")
        return
    if status.online > 0:
        _last_active[s.name] = now
        logger.debug(f"[idle] {s.name} 在线 {status.online} 人,刷新活跃时间")
        return

    last_active = _last_active.get(s.name, open_at)
    idle_seconds = now - last_active
    threshold = s.auto_close_idle_minutes * 60
    if idle_seconds >= threshold:
        logger.info(
            f"[idle] {s.name} 已空闲 {int(idle_seconds/60)} 分钟,触发关停倒计时"
        )
        task = asyncio.create_task(_countdown_close(s))
        _pending_close[s.name] = task


async def _track_players(
    s: servers.Server, status: "SlpStatus | None",
) -> None:
    """对比上一轮玩家集合,有变动就广播。

    SLP 的 players.sample 只是子集(默认 12 个),所以单看名字 diff 不
    可靠。这里以 status.online 总数变化为触发条件,名字 diff 仅用于
    填充播报内容。
    """
    if status is None:
        # 查询失败可能是临时不可达,清掉状态等下次重建
        # (不广播"所有人离开",避免误报)
        _last_player_names.pop(s.name, None)
        _last_online_count.pop(s.name, None)
        return

    current_names = set(status.players)
    current_count = status.online
    prev_count = _last_online_count.get(s.name)
    prev_names = _last_player_names.get(s.name, set())

    # 首次观测 → 建立基线,不广播
    if prev_count is None:
        _last_player_names[s.name] = current_names
        _last_online_count[s.name] = current_count
        return

    if current_count != prev_count:
        joined = current_names - prev_names
        left = prev_names - current_names
        parts: list[str] = []
        if joined:
            parts.append(f"➕ {', '.join(sorted(joined))}")
        if left:
            parts.append(f"➖ {', '.join(sorted(left))}")
        if not parts:
            # 总数变了但 sample 没列名字(可能 sample 已满,或玩家匿名)
            parts.append(
                "➕ 有人进入" if current_count > prev_count else "➖ 有人离开"
            )
        await _broadcast(
            f"🎮 『{s.name}』 ({current_count}/{status.max} 在线)\n"
            + "\n".join(parts)
        )

    _last_player_names[s.name] = current_names
    _last_online_count[s.name] = current_count


# ============================================================
# 面板轮询：日志事件播报 + 在线时长 + 资源告警
# ============================================================

def _day(ts: float) -> str:
    return strftime("%Y-%m-%d", localtime(ts))


def _at(mc_name: str) -> str:
    """若该 MC 名绑了 QQ,返回 ' [CQ:at,qq=...]',否则空串。"""
    qq = servers.get_qq_by_mc(mc_name)
    return f" [CQ:at,qq={qq}]" if qq else ""


# QQ 号 → 群名片/昵称 缓存(重启清空;昵称很少变)
_name_cache: dict[int, str] = {}


async def _qq_display(qq: int) -> str:
    """取 QQ 在播报群里的群名片(优先)/昵称,拿不到回退空串。带缓存。"""
    if qq in _name_cache:
        return _name_cache[qq]
    name = ""
    if _bot is not None and _broadcast_groups:
        try:
            info = await _bot.get_group_member_info(
                group_id=_broadcast_groups[0], user_id=qq, no_cache=False,
            )
            name = info.get("card") or info.get("nickname") or ""
        except Exception:
            try:
                info = await _bot.get_stranger_info(user_id=qq)
                name = info.get("nickname") or ""
            except Exception:
                name = ""
    if name:
        _name_cache[qq] = name
    return name


async def _speaker_name(mc_name: str) -> str:
    """绑了 QQ 且能取到昵称 → 用 QQ 群昵称;否则回退游戏名。"""
    qq = servers.get_qq_by_mc(mc_name)
    if qq:
        disp = await _qq_display(qq)
        if disp:
            return disp
    return mc_name


async def _poll_panel(s: servers.Server) -> None:
    """先查面板状态(便宜),仅在 running 时拉日志做播报/时长/告警。"""
    assert _panel_runner is not None and _config is not None

    # 1. 权威状态——current_state 稳定,不像 SLP 会偶发超时
    async def _res(panel):
        return await panel.get_resources(s.instance_uuid)

    data, err = await _panel_runner(s, _res)
    state = None
    if isinstance(data, dict):
        state = data.get("attributes", {}).get("current_state")

    if state != "running":
        # 离线 / 启动中 / 查询失败:停掉 WS、结算会话、清基线,不下载日志
        _stop_ws(s.name)
        _flush_sessions(s.name)
        _online_players.pop(s.name, None)
        _log_seen_lines.pop(s.name, None)
        _breach_count.pop((s.name, "cpu"), None)
        _breach_count.pop((s.name, "mem"), None)
        return

    # 2. running → 事件解析
    use_ws = (
        _config.event_broadcast
        and getattr(_config, "realtime_console", True)
        and websockets is not None
    )
    if use_ws:
        # 实时:确保 WS 任务在跑,事件由 WS 流处理(零延迟),这里不再轮询日志
        _ensure_ws(s)
    elif _config.event_broadcast:
        # 回退:周期性拉 latest.log 增量
        async def _read(panel):
            return await panel.read_file_text(s.instance_uuid, "/logs/latest.log")

        text, err2 = await _panel_runner(s, _read)
        if text is not None:
            try:
                await _process_log(s, text)
            except Exception:
                logger.exception(f"[event] 解析 {s.name} 日志出错")

    # 3. 资源告警(复用第 1 步已查到的 data,不再多查一次)
    if _config.resource_alert:
        try:
            await _check_resources(s, data)
        except Exception:
            logger.exception(f"[alert] {s.name} 资源告警出错")


# ============================================================
# 实时控制台 WebSocket(零延迟 MC→QQ)
# ============================================================

def _ensure_ws(s: servers.Server) -> None:
    """确保该服务器的 WS 实时任务在跑。"""
    t = _ws_tasks.get(s.name)
    if t is not None and not t.done():
        return
    _ws_tasks[s.name] = asyncio.create_task(_ws_loop(s))


def _stop_ws(name: str) -> None:
    t = _ws_tasks.pop(name, None)
    if t is not None and not t.done():
        t.cancel()
    _ws_connected.pop(name, None)


def _clean_console(raw: str) -> list[str]:
    """把一条 WS console output 清成若干"日志正文候选"。
    去掉 ANSI/光标控制 + 提示符前缀,按物理行切,取每行第一个 '[' 起的部分。
    """
    txt = _ANSI_RE.sub("", raw).replace("\r", "\n")
    out: list[str] = []
    for piece in txt.split("\n"):
        i = piece.find("[")
        if i < 0:
            continue
        out.append(piece[i:])
    return out


async def _seed_from_log(s: servers.Server) -> None:
    """WS 连上后,读一次 latest.log 算出当前在线玩家并开会话(只在还没基线时)。"""
    if s.name in _online_players:
        return
    if _panel_runner is None:
        return

    async def _read(panel):
        return await panel.read_file_text(s.instance_uuid, "/logs/latest.log")

    text, err = await _panel_runner(s, _read)
    if text is None:
        return
    raw = text.split("\n")
    if raw and raw[-1] == "":
        raw.pop()
    _seed_baseline(s.name, raw)


async def _ws_loop(s: servers.Server) -> None:
    """连接 Pterodactyl WS 控制台,实时把日志事件喂给 _handle_event。"""
    name = s.name
    if websockets is None or _panel_runner is None:
        return

    async def _creds():
        async def _fn(panel):
            return await panel.get_ws_credentials(s.instance_uuid)
        data, err = await _panel_runner(s, _fn)
        return data if isinstance(data, dict) else None

    try:
        creds = await _creds()
        if not creds or not creds.get("socket") or not creds.get("token"):
            logger.warning(f"[ws] {name} 拿不到 WS 凭据,放弃实时,改回轮询")
            return
        async with websockets.connect(
            creds["socket"], origin="https://minekuai.com", max_size=None,
        ) as ws:
            await ws.send(json.dumps({"event": "auth", "args": [creds["token"]]}))
            # 先用一次日志读出当前在线(已在线玩家也能计时长 + 死亡判定)
            await _seed_from_log(s)
            # 连上瞬间会回放最近历史,先忽略 2 秒避免把历史当新事件播报
            ignore_until = time() + 2.0
            _ws_connected[name] = True
            logger.info(f"[ws] {name} 实时控制台已连接")
            async for raw in ws:
                try:
                    m = json.loads(raw)
                except Exception:
                    continue
                ev = m.get("event")
                args = m.get("args") or []
                if ev == "console output":
                    if time() < ignore_until:
                        continue
                    for line in _clean_console(args[0] if args else ""):
                        lm = _LOG_LINE_RE.match(line)
                        if not lm:
                            continue
                        try:
                            await _handle_event(s, lm.group(1).strip())
                        except Exception:
                            logger.exception(f"[ws] {name} 处理事件出错")
                elif ev in ("token expiring", "token expired"):
                    fresh = await _creds()
                    if fresh and fresh.get("token"):
                        await ws.send(
                            json.dumps({"event": "auth", "args": [fresh["token"]]})
                        )
                elif ev == "status":
                    st = args[0] if args else ""
                    if st in ("stopping", "offline"):
                        _flush_sessions(name)
                        _online_players.pop(name, None)
                        break
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning(f"[ws] {name} 连接断开: {type(e).__name__}: {e}")
    finally:
        _ws_connected.pop(name, None)
        _ws_tasks.pop(name, None)


async def _process_log(s: servers.Server, text: str) -> None:
    """增量解析 latest.log,广播事件并维护在线时长会话。"""
    raw = text.split("\n")
    if raw and raw[-1] == "":
        raw.pop()  # 末尾换行产生的空串
    total = len(raw)

    seen = _log_seen_lines.get(s.name)
    if seen is None:
        # 首次观测:不回放广播,但扫一遍算出"当前在线"并给他们开会话,
        # 这样已经在线的玩家也能从现在开始计时长。
        _seed_baseline(s.name, raw)
        _log_seen_lines[s.name] = total
        return
    if total < seen:
        # 日志轮转/截断 → 从头处理新文件
        seen = 0
    new_lines = raw[seen:]
    _log_seen_lines[s.name] = total

    for line in new_lines:
        m = _LOG_LINE_RE.match(line)
        if not m:
            continue
        msg = m.group(1).strip()
        await _handle_event(s, msg)


def _seed_baseline(server_name: str, lines: list[str]) -> None:
    """扫描现有 latest.log,推算当前在线玩家(join 未配对 leave),
    给他们从现在开始开会话。Forge 每次启动会轮转 latest.log,
    所以净在线集合 = 本次运行当前在线。不发任何广播。
    """
    online: set[str] = set()
    for line in lines:
        m = _LOG_LINE_RE.match(line)
        if not m:
            continue
        msg = m.group(1).strip()
        if msg.startswith("Stopping the server") or msg.startswith("Stopping server"):
            online.clear()
            continue
        mj = _JOIN_RE.match(msg)
        if mj:
            online.add(mj.group(1))
            continue
        ml = _LEAVE_RE.match(msg)
        if ml:
            online.discard(ml.group(1))
    now = time()
    _online_players[server_name] = online
    for name in online:
        _session_start.setdefault((server_name, name), now)
    if online:
        logger.info(f"[event] {server_name} 基线在线玩家: {sorted(online)}")


async def _handle_event(s: servers.Server, msg: str) -> None:
    now = time()
    online = _online_players.setdefault(s.name, set())

    # 服务器关停 → 结算所有在线会话(关停日志不一定逐个打 left the game)
    if msg.startswith("Stopping the server") or msg.startswith("Stopping server"):
        _flush_sessions(s.name)
        online.clear()
        return

    mj = _JOIN_RE.match(msg)
    if mj:
        name = mj.group(1)
        online.add(name)
        _session_start[(s.name, name)] = now
        if _config and _config.broadcast_join_leave:
            await _broadcast(
                f"➕ 『{s.name}』 {await _speaker_name(name)} 进入了服务器"
            )
        return

    ml = _LEAVE_RE.match(msg)
    if ml:
        name = ml.group(1)
        online.discard(name)
        _end_session(s.name, name, now)
        if _config and _config.broadcast_join_leave:
            await _broadcast(
                f"➖ 『{s.name}』 {await _speaker_name(name)} 离开了服务器"
            )
        return

    # 聊天 → QQ(依赖服务器把聊天写进 latest.log)
    mc = _CHAT_RE.match(msg)
    if mc:
        if _config and getattr(_config, "chat_bridge", False) \
                and getattr(_config, "chat_mc_to_qq", False):
            name, content = mc.group(1), mc.group(2)
            await _broadcast(f"💬 {await _speaker_name(name)}: {content}")
        return

    ma = _ADV_RE.match(msg)
    if ma:
        name, adv = ma.group(1), ma.group(2)
        await _broadcast(f"🏆 『{s.name}』 {name} 达成了成就 {adv}{_at(name)}")
        return

    # 死亡:首词是在线玩家 + 命中死亡关键词(双重判定降误报)
    head = _NAME_HEAD_RE.match(msg)
    if head and head.group(1) in online and _DEATH_RE.search(msg):
        name = head.group(1)
        servers.add_death(name, _day(now))
        await _broadcast(f"💀 『{s.name}』 {msg}{_at(name)}")
        return


def _end_session(server_name: str, mc_name: str, now: float) -> None:
    """结算一个在线会话,把时长写进 DB。"""
    start = _session_start.pop((server_name, mc_name), None)
    if start is None:
        return
    secs = int(now - start)
    if secs > 0:
        servers.add_playtime(mc_name, _day(start), secs)


def _flush_sessions(server_name: str) -> None:
    """结算某服务器所有进行中的会话(服务器停了/不可达时调)。"""
    now = time()
    for (srv, name) in [k for k in _session_start if k[0] == server_name]:
        _end_session(server_name, name, now)


async def _check_resources(s: servers.Server, data: dict) -> None:
    """用已查到的 resources 数据做告警(持续超阈值 + 连击判定 + 冷却)。
    data 由 _poll_panel 传入,这里不再重复查询。调用时已确保 running。
    """
    assert _panel_runner is not None and _config is not None

    attrs = data.get("attributes", {}) if isinstance(data, dict) else {}
    res = attrs.get("resources", {}) or {}

    # 配额:内存来自 get_server_info(缓存);CPU 配额优先用缓存
    if s.name not in _mem_limit_mb:
        async def _info(panel):
            return await panel.get_server_info(s.instance_uuid)
        info, _e = await _panel_runner(s, _info)
        limits = (
            info.get("attributes", {}).get("limits", {})
            if isinstance(info, dict) else {}
        ) or {}
        _mem_limit_mb[s.name] = limits.get("memory") or 0
        _cpu_limit[s.name] = limits.get("cpu") or 0

    mem_limit_mb = _mem_limit_mb.get(s.name, 0)
    cpu_limit = _cpu_limit.get(s.name, 0)

    # CPU
    if _config.cpu_alert_percent > 0 and cpu_limit > 0:
        cpu_abs = float(res.get("cpu_absolute", 0.0) or 0.0)
        cpu_util = cpu_abs / cpu_limit * 100
        await _eval_breach(
            s, "cpu", cpu_util >= _config.cpu_alert_percent,
            f"🔥 『{s.name}』CPU 占用 {cpu_util:.0f}% "
            f"(已连续 {_config.alert_sustained_ticks} 次检测超阈值)",
        )

    # 内存
    if _config.mem_alert_percent > 0 and mem_limit_mb > 0:
        mem_used = res.get("memory_bytes", 0) or 0
        mem_util = mem_used / (mem_limit_mb * 1024 * 1024) * 100
        await _eval_breach(
            s, "mem", mem_util >= _config.mem_alert_percent,
            f"🔥 『{s.name}』内存占用 {mem_util:.0f}% "
            f"(已连续 {_config.alert_sustained_ticks} 次检测超阈值)",
        )


async def _eval_breach(
    s: servers.Server, kind: str, breaching: bool, alert_text: str,
) -> None:
    key = (s.name, kind)
    if not breaching:
        _breach_count[key] = 0
        return
    _breach_count[key] = _breach_count.get(key, 0) + 1
    if _breach_count[key] < _config.alert_sustained_ticks:
        return
    # 冷却,避免持续高负载时反复刷屏
    now = time()
    last = _last_alert_at.get(key, 0)
    if now - last < _config.alert_cooldown_minutes * 60:
        return
    _last_alert_at[key] = now
    at = ""
    if _config.allowed_users:
        at = " " + " ".join(f"[CQ:at,qq={u}]" for u in _config.allowed_users)
    await _broadcast(alert_text + at)


async def _countdown_close(server: servers.Server) -> None:
    """广播倒计时 → 等 COUNTDOWN_SECONDS → 真关"""
    msg = (
        f"⚠️ 『{server.name}』已空闲 {server.auto_close_idle_minutes} 分钟，"
        f"将在 {COUNTDOWN_SECONDS} 秒后自动关停。\n"
        f"如需保留请在群里回『取消关停』"
    )
    await _broadcast(msg)

    try:
        await asyncio.sleep(COUNTDOWN_SECONDS)
    except asyncio.CancelledError:
        logger.info(f"[idle] {server.name} 关停被取消")
        await _broadcast(f"✅ 『{server.name}』关停已取消")
        return
    finally:
        # 不管成不成都从 pending 字典里移除
        # 用 list_pending 之类的查询时会反映真实状态
        pass

    # 倒计时结束 → 真关
    if _close_callback is None:
        logger.error("[idle] close_callback 未注入，无法执行关停")
        return
    try:
        # 重新读最新 server（万一这期间有人改了配置）
        fresh = servers.get_server(server.name)
        if fresh is None:
            await _broadcast(f"⚠️ 『{server.name}』已不存在，跳过关停")
            return
        ok, msg = await _close_callback(fresh)
        if ok:
            mark_closed(server.name)
            await _broadcast(f"💤 『{server.name}』已自动关停（空闲超时）")
        else:
            await _broadcast(f"❌ 『{server.name}』自动关停失败：{msg}")
    except Exception as e:
        logger.exception(f"[idle] 关停 {server.name} 时异常")
        await _broadcast(f"❌ 『{server.name}』自动关停异常：{type(e).__name__}: {e}")
    finally:
        _pending_close.pop(server.name, None)


# ============================================================
# SLP 查询
# ============================================================

@dataclass
class SlpStatus:
    """SLP 查询返回的服务器状态"""
    online: int
    max: int
    latency_ms: int
    players: list[str] = field(default_factory=list)  # 当前在线玩家（最多 12 个，受限于协议）
    version: str = ""


async def query_status(address: str) -> SlpStatus | None:
    """查 Minecraft 服务器的完整状态。失败/不可达返回 None。

    这是公开的查询接口，给 `在线` 指令用。
    内部的轮询监视器走 _query_slp 即可（只要在线数）。
    """
    try:
        from mcstatus import JavaServer
    except ImportError:
        logger.error("[idle] mcstatus 未安装，无法查 SLP")
        return None

    addr = address.strip()
    if not addr:
        return None
    if ":" not in addr:
        addr = f"{addr}:25565"
    try:
        server = await asyncio.wait_for(
            JavaServer.async_lookup(addr),
            timeout=SLP_TIMEOUT_SECONDS,
        )
        status = await asyncio.wait_for(
            server.async_status(), timeout=SLP_TIMEOUT_SECONDS,
        )
        # 玩家名字列表（sample 可能为 None）
        names: list[str] = []
        if status.players.sample:
            for p in status.players.sample:
                if getattr(p, "name", None):
                    names.append(p.name)
        return SlpStatus(
            online=status.players.online,
            max=status.players.max,
            latency_ms=int(status.latency),
            players=names,
            version=getattr(status.version, "name", "") or "",
        )
    except asyncio.TimeoutError:
        return None
    except Exception as e:
        logger.debug(f"[idle] SLP 查询 {addr} 失败: {type(e).__name__}: {e}")
        return None


async def _query_slp(address: str) -> int | None:
    """轮询用的轻量版本——只要在线数，失败返回 None"""
    s = await query_status(address)
    return s.online if s is not None else None


# ============================================================
# 群广播
# ============================================================

async def _broadcast(text: str) -> None:
    """给所有 broadcast_groups 发消息"""
    if _bot is None:
        logger.warning(f"[idle] 没 bot 引用，无法广播: {text}")
        return
    for gid in _broadcast_groups:
        try:
            await _bot.send_group_msg(group_id=gid, message=text)
        except Exception as e:
            logger.warning(f"[idle] 广播到群 {gid} 失败: {e}")
