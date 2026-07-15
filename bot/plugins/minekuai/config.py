"""配置模型 - 从 .env 加载并校验"""
from pydantic import BaseModel
from typing import List


class Config(BaseModel):
    """麦块联机插件配置"""

    # 计时卡接口（api.minekuai.com，JWT Bearer 认证）
    minekuai_token: str = ""
    minekuai_client_id: str = ""
    minekuai_card_id: str = ""

    # 权限
    allowed_groups: List[int] = []   # 允许使用的群号（空=不限制）
    allowed_users: List[int] = []    # 允许使用的 QQ 号（空=群里所有人）
    admin_users: List[int] = []      # 管理员 QQ；高权限命令仅这些账号可用

    # 行为
    command_cooldown: int = 30       # 同一用户的指令冷却（秒）
    stop_need_confirm: bool = True   # 关服是否需要二次确认

    # 后台轮询周期（秒）。日志/资源只在服务器在线时拉,所以可以调小。
    poll_interval_seconds: int = 30

    # 事件播报（需要服务器配了 uuid + 绑定账号）
    event_broadcast: bool = True     # 玩家加入/离开/死亡/成就 播报总开关
    broadcast_join_leave: bool = True   # 加入/离开是否播报（死亡/成就始终播报）
    # 实时控制台:用 Pterodactyl WebSocket 流式接收日志(几乎零延迟),
    # 关掉则回退到 poll_interval_seconds 周期轮询 latest.log。
    realtime_console: bool = True

    # 聊天桥（游戏 ↔ QQ 群 双向转发）
    chat_bridge: bool = True          # 总开关
    chat_mc_to_qq: bool = True        # 游戏内聊天 → QQ 群（依赖服务器把聊天写进 latest.log）
    chat_qq_to_mc: bool = True        # QQ 群消息 → 游戏内（tellraw，仅在有人在线时转发）

    # 资源告警（持续超阈值才报，避免抖动刷屏）
    resource_alert: bool = True
    cpu_alert_percent: int = 95      # CPU 超过此百分比（0=不监控）
    mem_alert_percent: int = 92      # 内存占用超过配额此百分比（0=不监控）
    alert_sustained_ticks: int = 3   # 连续多少个轮询周期超阈值才报
    alert_cooldown_minutes: int = 30 # 同一告警的最短重复间隔
