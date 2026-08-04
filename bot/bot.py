"""机器人入口"""
import re

import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
from nonebot.log import logger

# 1. 初始化 nonebot
nonebot.init()


_VERIFICATION_CODE_RE = re.compile(
    r"((?:图形|图片|短信)?验证码\s+)-?\d{1,8}"
)


def _redact_verification_codes(record: dict) -> None:
    """避免 NoneBot 的入站消息日志留下登录验证码。"""
    record["message"] = _VERIFICATION_CODE_RE.sub(
        r"\1[REDACTED]", str(record["message"])
    )


logger.configure(patcher=_redact_verification_codes)

# 2. 注册 OneBot v11 适配器（用于和 NapCat 通信）
driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)

# 3. 加载我们写的麦块联机插件
nonebot.load_plugin("plugins.minekuai")


if __name__ == "__main__":
    nonebot.run()
