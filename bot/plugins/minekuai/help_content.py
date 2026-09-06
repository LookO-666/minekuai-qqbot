"""Short, scenario-based QQ help, independent of the messaging adapter."""

import re


GITHUB_URL = "https://github.com/LookO-666/minekuai-qqbot"
TOPICS = ("服务器", "玩家", "整合包", "管理")
HELP_PATTERN = r"(?is)^\s*(?:帮助|help)(?:\s+(.*?))?\s*$"


def parse_help_topic(text: str) -> str | None:
    """Recognize a help command without consuming ordinary words or sentences."""
    match = re.fullmatch(HELP_PATTERN, text)
    if match is None:
        return None
    return " ".join((match.group(1) or "").split())

_HOME = """🎮 麦块机器人

开服 / 关服｜启动或关闭服务器
在线｜查看在线玩家
地址｜获取连接地址
客户端｜获取客户端下载入口
更换整合包｜按提示选择版本
服务器列表｜查看可用服务器

💬 支持 QQ ↔ 游戏聊天互通，详见「帮助 玩家」
帮助 服务器｜开关服与状态查询
帮助 玩家｜聊天、绑定与排行榜
帮助 整合包｜换包、下载与安装进度
帮助 管理｜运维与账号配置

直接发命令，不用 @；只有一台时可省略服务器名。
例如：开服 test；操作中发「取消」可退出。"""

_SERVER = """开服 [服务器]｜开启计时卡并启动实例，等就绪通知再进入
关服 [服务器]｜关闭计时卡，停止计费
在线 [服务器]｜人数、玩家、延迟与版本
地址 [服务器]｜连接地址
服务器列表｜已配置的服务器
查服 [服务器]｜实例状态、CPU、内存与磁盘
模组 [服务器]｜已安装的模组 JAR
插件 [服务器]｜已安装的插件 JAR
🔒 重启 [服务器]｜重启实例，不关闭计时卡

多台时可写名字，如「开服 test」；在线/地址不写名字会汇总。
日志、控制台与自动关停：帮助 管理
客户端和安装进度：帮助 整合包"""

_PLAYER = """绑定 <游戏名>｜绑定自己的 QQ，播报时可 @ 你
解绑｜解除自己的绑定
绑定列表｜查看全部玩家绑定
今日榜 / 本周榜｜今日 / 近 7 天在线时长排行
在线时长 [游戏名]｜个人在线时长
死亡榜｜累计死亡排行
死亡次数 [游戏名]｜今日与累计死亡次数
mc <正版玩家名>｜生成 Minecraft 玩家资料卡
🔒 绑定 <QQ> <游戏名> / 解绑 <QQ>｜管理员代操作

已绑定时，个人统计可省略游戏名。
💬 开启聊天桥后：群内普通文字 → 每台当前有玩家的服务器。
游戏聊天 → 允许群；加入/离开、死亡、成就可自动播报。
互通需配置实例和可用面板认证，并能读取标准游戏日志。"""

MODPACK_HELP = """更换整合包 [服务器] [关键词]｜搜索并按提示选择版本
确认清空安装 <确认码>｜原人原会话 5 分钟内确认
取消更换整合包｜取消未提交的选择，不撤销开卡或已提交安装
整合包状态 [服务器]｜查询安装与计费，安全结束后自动解除保护
整合包日志 [服务器]｜查看脱敏的安装日志
客户端 [服务器]｜查询下载链接，普通成员可用
结束整合包维护 [服务器]｜再次安全核对，不能强制解除保护

⚠️ 确认授权开卡计费（消耗时长），并覆盖全部文件和世界；请先自行备份。
先开启计时卡；平台若自动启动则正常停服，确认离线后才安装，不强杀。
提交后自动发送所选包/版本的客户端信息；只发链接，不上传文件、不扣积分。
没有版本直链时可能仅提供免费目录，请核对客户端版本。
安装中或结果未知会保留保护；先查状态/日志，请勿重复安装。
机器人不会自动启动游戏或关闭计时卡；报错后计费也可能继续。
成功后发「开服 [服务器]」；不再使用时发「关服 [服务器]」。
暂不支持 MCDR 实例的整合包更换。"""

_ADMIN = """指令 [服务器] <MC命令>｜发送控制台命令
日志 [服务器] [行数]｜最近日志，默认 30 行、最多 200 行
自动关停｜查看各服设置与倒计时
自动关停 <服务器> <分钟>｜设置空闲关停，0 为关闭
暂停自动关停 [分钟]｜全局暂停，默认 60 分钟
取消关停｜取消本次 60 秒关停倒计时

添加账号 / 账号列表 / 删除账号 <手机号>
添加服务器｜按提示登记已有实例，不会创建实例
删除服务器 <服务器>｜按提示确认后删除配置
修改服务器名字 [旧名] [新名]
修改地址 [服务器] [新地址]
修改uuid [服务器] [实例ID]
绑定账号 <服务器> <手机号>
更新token <服务器>｜自动续期失败时应急

图形验证码 <答案> / 短信验证码 <6位>｜仅在登录提示后回复
账号配置尽量在获准的私聊中发起；验证码按原会话提示提交，不要转发。
取消｜退出当前交互，不撤销已提交的操作
重启见「帮助 服务器」；安装管理见「帮助 整合包」。
后台可自动保活、空闲关停和 CPU/内存告警，受部署配置与状态限制。"""


def _footer(text: str, *, home: bool = False) -> str:
    navigation = "" if home else "\n\n返回首页：帮助"
    return f"{text}{navigation}\n\nGitHub：\n{GITHUB_URL}"


def render_help(
    topic: str = "", *, admin_all_group_members: bool = False,
    stop_need_confirm: bool = True, command_cooldown: int = 0,
) -> str:
    """Render one page without exposing private settings or changing permissions."""
    topic = topic.strip()
    if not topic:
        return _footer(_HOME, home=True)
    if topic not in TOPICS:
        return _footer(
            "没有这个帮助分类，请选择：\n"
            "帮助 服务器｜开关服与状态查询\n"
            "帮助 玩家｜聊天、绑定与排行榜\n"
            "帮助 整合包｜换包、下载与安装进度\n"
            "帮助 管理｜运维与账号配置"
        )

    scope = (
        "白名单群内所有可用成员也可使用管理指令"
        if admin_all_group_members else "管理指令仅管理员可用"
    )
    if topic in {"整合包", "管理"}:
        scope_line = f"🔒 {scope}。" + (
            "客户端下载除外。" if topic == "整合包" else ""
        )
    else:
        scope_line = f"🔒 标记为管理指令；{scope}。"

    content = {
        "服务器": _SERVER,
        "玩家": _PLAYER,
        "整合包": MODPACK_HELP,
        "管理": _ADMIN,
    }[topic]
    if topic in {"服务器", "整合包"} and stop_need_confirm:
        content += "\n关服收到提示后，5 分钟内回复「确认关服」。"
    if topic == "管理" and command_cooldown > 0:
        content += f"\n同一用户同一指令冷却：{command_cooldown} 秒。"
    return _footer(f"🎮 帮助 · {topic}\n{scope_line}\n\n{content}")
