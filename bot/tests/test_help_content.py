"""Help stays short, accurate, and discoverable without importing NoneBot."""

import importlib.util
from pathlib import Path

import pytest


_SOURCE = Path(__file__).parents[1] / "plugins" / "minekuai" / "help_content.py"
_SPEC = importlib.util.spec_from_file_location("scenario_help_content", _SOURCE)
help_content = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(help_content)
render_help = help_content.render_help


@pytest.mark.parametrize(
    ("message", "topic"),
    [
        ("帮助", ""), ("help", ""), ("Help", ""), ("HELP", ""),
        ("hElP", ""), ("  帮助  ", ""), ("\tHeLp\n", ""),
        ("帮助 服务器", "服务器"), ("Help 玩家", "玩家"),
        ("帮助　整合包　", "整合包"), ("帮助\n管理", "管理"),
        ("\n HELP \t 服务器 \n", "服务器"),
        ("帮助 不存在", "不存在"), ("help unknown  topic", "unknown topic"),
    ],
)
def test_parse_help_topic_recognizes_only_explicit_help_commands(message, topic):
    assert help_content.parse_help_topic(message) == topic


@pytest.mark.parametrize(
    "message",
    [
        "", " ", "helpful", "帮助别人", "help管理", "管理", "服务器", "整合包",
        "1", "2", "3", "4", "请给我帮助", "我需要 help 服务器", "/帮助",
    ],
)
def test_parse_help_topic_does_not_swallow_plain_chat_or_menu_numbers(message):
    assert help_content.parse_help_topic(message) is None


def test_home_fits_one_short_message():
    text = render_help()
    assert len(text) <= 550
    assert len([line for line in text.splitlines() if line]) <= 18
    for command in (
        "开服 / 关服", "在线", "地址", "客户端", "更换整合包", "服务器列表",
    ):
        assert command in text
    assert "QQ ↔ 游戏聊天互通" in text
    assert "不用 @" in text
    assert "只有一台时可省略服务器名" in text


def test_home_contains_each_topic_once_as_its_own_command():
    lines = render_help().splitlines()
    for topic in help_content.TOPICS:
        assert sum(line.startswith(f"帮助 {topic}｜") for line in lines) == 1


def test_home_does_not_dump_rare_workflows_or_config():
    text = render_help(admin_all_group_members=True, command_cooldown=5)
    for detail in (
        "确认清空安装", "覆盖全部", "验证码", "添加账号", "更新token", "管理员",
        "维护保护", "先自行备份", "白名单", "指令冷却", "start", "stop",
    ):
        assert detail not in text


@pytest.mark.parametrize("topic", ["", *help_content.TOPICS, "不存在"])
@pytest.mark.parametrize("admin", [False, True])
@pytest.mark.parametrize("confirmation", [False, True])
def test_every_page_ends_in_standalone_github_url(topic, admin, confirmation):
    text = render_help(
        topic, admin_all_group_members=admin, stop_need_confirm=confirmation,
        command_cooldown=5,
    )
    assert text.splitlines()[-2:] == ["GitHub：", help_content.GITHUB_URL]
    assert text.count(help_content.GITHUB_URL) == 1
    assert not text.endswith("\n")
    assert "\r" not in text


@pytest.mark.parametrize("topic", help_content.TOPICS)
@pytest.mark.parametrize("admin", [False, True])
def test_category_page_is_short_and_has_home_navigation(topic, admin):
    text = render_help(topic, admin_all_group_members=admin, command_cooldown=5)
    assert len(text.splitlines()) <= 32
    assert len(text) < 1300
    assert text.startswith(f"🎮 帮助 · {topic}\n")
    assert text.count("返回首页：帮助") == 1


@pytest.mark.parametrize("topic", help_content.TOPICS)
def test_admin_scope_is_dynamic_and_not_a_permission_change(topic):
    restricted = render_help(topic)
    enabled = render_help(topic, admin_all_group_members=True)
    assert "管理指令仅管理员可用" in restricted
    assert "白名单群内所有可用成员也可使用管理指令" in enabled
    assert "管理指令仅管理员可用" not in enabled
    assert "ADMIN_USERS" not in restricted + enabled


def test_unknown_topic_returns_only_a_short_guide_not_echoed_input():
    unknown = "secret_token_" + "x" * 10000
    text = render_help(unknown)
    assert len(text) < 250
    assert unknown not in text
    assert "没有这个帮助分类" in text
    assert "开服 / 关服" not in text
    for topic in help_content.TOPICS:
        assert f"帮助 {topic}｜" in text


def test_surrounding_whitespace_is_ignored():
    assert render_help(" 服务器 \n") == render_help("服务器")
    assert render_help(" \t") == render_help()


@pytest.mark.parametrize("topic", ["服务器", "整合包"])
def test_stop_confirmation_only_shown_when_enabled(topic):
    assert render_help(topic).count("确认关服") == 1
    assert "确认关服" not in render_help(topic, stop_need_confirm=False)


@pytest.mark.parametrize("cooldown", [0, -1])
def test_nonpositive_cooldown_is_not_displayed(cooldown):
    assert "指令冷却" not in render_help("管理", command_cooldown=cooldown)


def test_cooldown_only_appears_in_administration_details():
    assert render_help("管理", command_cooldown=7).count("指令冷却：7 秒") == 1
    for topic in ("", "服务器", "玩家", "整合包"):
        assert "指令冷却" not in render_help(topic, command_cooldown=7)


def test_server_scene_covers_daily_controls_and_resource_queries():
    text = render_help("服务器")
    for command in (
        "开服 [服务器]", "关服 [服务器]", "在线 [服务器]", "地址 [服务器]",
        "服务器列表", "查服 [服务器]", "模组 [服务器]", "插件 [服务器]",
        "🔒 重启 [服务器]",
    ):
        assert command in text
    assert "等就绪通知再进入" in text
    assert "重启实例，不关闭计时卡" in text
    assert "帮助 管理" in text and "帮助 整合包" in text


def test_player_scene_covers_bridge_binding_profile_and_statistics():
    text = render_help("玩家")
    for command in (
        "绑定 <游戏名>", "解绑", "绑定列表", "今日榜", "本周榜", "在线时长",
        "死亡榜", "死亡次数", "mc <正版玩家名>", "绑定 <QQ>", "解绑 <QQ>",
    ):
        assert command in text
    for condition in (
        "开启聊天桥后", "每台当前有玩家的服务器", "允许群", "加入/离开",
        "死亡、成就", "可用面板认证", "标准游戏日志", "已绑定时",
    ):
        assert condition in text


def test_modpack_scene_preserves_destructive_billing_and_unknown_state_safety():
    text = render_help("整合包")
    assert help_content.MODPACK_HELP in text
    for command in (
        "更换整合包 [服务器] [关键词]", "确认清空安装 <确认码>", "取消更换整合包",
        "整合包状态 [服务器]", "整合包日志 [服务器]", "客户端 [服务器]",
        "结束整合包维护 [服务器]",
    ):
        assert command in text
    for warning in (
        "原人原会话 5 分钟内", "开卡计费", "覆盖全部文件和世界", "先自行备份",
        "先开启计时卡", "确认离线后才安装", "不强杀", "平台若自动启动则正常停服",
        "不撤销开卡或已提交安装", "安全结束后自动解除保护", "不能强制解除保护",
        "安装中或结果未知会保留保护", "请勿重复安装", "计费也可能继续",
        "不会自动启动游戏或关闭计时卡", "MCDR",
    ):
        assert warning in text


def test_client_help_explains_scope_and_fallback_without_duplicate_aliases():
    text = render_help("整合包")
    for detail in (
        "普通成员可用", "只发链接", "不上传文件", "不扣积分", "免费目录",
        "核对客户端版本", "提交后自动发送",
    ):
        assert detail in text
    assert "下载客户端" not in text
    assert text.count("客户端 [服务器]｜") == 1


def test_admin_scene_keeps_configuration_and_login_discoverable():
    text = render_help("管理")
    for command in (
        "指令 [服务器] <MC命令>", "日志 [服务器] [行数]", "自动关停",
        "暂停自动关停", "取消关停", "添加账号", "账号列表", "删除账号",
        "添加服务器", "删除服务器", "修改服务器名字", "修改地址", "修改uuid",
        "绑定账号", "更新token", "图形验证码", "短信验证码", "取消｜",
    ):
        assert command in text
    assert "登记已有实例，不会创建实例" in text
    assert "私聊" in text and "验证码按原会话提示提交" in text
    assert "自动保活" in text and "CPU/内存告警" in text
    assert "仅在登录提示后回复" in text
    assert "不撤销已提交的操作" in text
