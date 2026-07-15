"""机器人入口"""
import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

# 1. 初始化 nonebot
nonebot.init()

# 2. 注册 OneBot v11 适配器（用于和 NapCat 通信）
driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)

# 3. 加载我们写的麦块联机插件
nonebot.load_plugin("plugins.minekuai")


if __name__ == "__main__":
    nonebot.run()
