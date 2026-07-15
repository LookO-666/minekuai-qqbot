# 开发模式部署（非 Docker）

> 大多数人应该走 [README.md](../README.md) 里的 Docker 流程，省心。
>
> 这篇是给想本地跑、改代码、调试的人看的。

---

## 一、装 NapCatQQ（QQ 协议端）

NapCat 负责挂 QQ 账号、收发消息。它是一个独立程序。

### Windows

1. 去 https://github.com/NapNeko/NapCatQQ/releases 下载 `NapCat.Shell.zip`
2. 解压到任意目录
3. 双击 `NapCatQQ.bat` 启动
4. 第一次会让你扫码登录（用机器人小号，**不要主号**）
5. 登录后浏览器打开它的 WebUI（一般是 `http://127.0.0.1:6099`），加一个**反向 WebSocket 客户端**：
   - URL: `ws://127.0.0.1:8080/onebot/v11/ws`
   - 启用心跳，Token 留空

### Linux / macOS

推荐用 Docker（即使你的 bot 不用 Docker，napcat 用 Docker 最省事）：

```bash
docker run -d --name napcat --restart=always \
  -e NAPCAT_UID=$(id -u) -e NAPCAT_GID=$(id -g) \
  -e QT_QPA_PLATFORM=offscreen \
  --network host \
  -v ./napcat-data/QQ:/app/.config/QQ \
  -v ./napcat-data/config:/app/napcat/config \
  mlikiowa/napcat-docker:latest
```

> 在某些 host 网络环境下 Xvfb 会因为 `:1` 显示号被宿主机占用而启动失败。看 [docker-compose.yml](../docker-compose.yml) 里的 `entrypoint:` 覆盖（用 `:99`）即可解决。

---

## 二、装 Python 依赖

```bash
cd bot
python -m venv .venv

# Windows
.venv\Scripts\activate
# Linux/macOS
source .venv/bin/activate

pip install -e .

# 如果要用 token 自动续期功能，还要装 Chromium：
playwright install chromium
```

> 需要 Python ≥ 3.10。
>
> Chromium 下载约 150 MB。如果不用自动续期可以跳过，token 失效时手动『更新token』。

---

## 三、写配置

```bash
cp .env.example .env
nano .env   # 或者你喜欢的编辑器
```

只需要填权限相关字段（白名单等）：

```ini
ALLOWED_GROUPS=[你的QQ群号]
ALLOWED_USERS=[]              # 空=群里所有人都能用
COMMAND_COOLDOWN=5
STOP_NEED_CONFIRM=true
```

> **服务器配置（token / clientid / 计时卡 ID / 地址）启动后在 QQ 群里发『添加服务器』搞定**，不用预先填进 `.env`。

数据库会写到当前目录下的 `operation_log.db`。如果想换地方：

```bash
export OPERATION_LOG_DB=/path/to/operation_log.db
```

---

## 四、启动

```bash
cd bot
python bot.py
```

正常启动后能看到：

```
[SUCCESS] nonebot | Loaded adapters: OneBot V11
[INFO] minekuai | 服务器配置数据库就绪: ...
[INFO] uvicorn | Uvicorn running on http://127.0.0.1:8080
```

然后看 NapCat 那边能不能连过来——成功的话日志会有 `OneBot V11 | Bot xxx connected`。

---

## 五、跑测试

```bash
cd bot
pip install pytest pytest-asyncio
pytest tests/ -v
```

测试覆盖 [client.py](../bot/plugins/minekuai/client.py)（HTTP 调用 + 错误处理）和 [permission.py](../bot/plugins/minekuai/permission.py)（白名单/冷却/二次确认）。

---

## 六、调试技巧

### 看详细 HTTP 日志

`.env` 里改 `LOG_LEVEL=DEBUG`，重启 bot。所有麦块联机 API 调用的 URL / 状态码会打印出来。

### 直接读数据库

```bash
sqlite3 bot/operation_log.db
sqlite> .tables
operation_log  servers
sqlite> SELECT name, address, card_id FROM servers;
sqlite> SELECT ts, user_name, command, success FROM operation_log ORDER BY id DESC LIMIT 10;
```

### 重置所有服务器配置

```bash
sqlite3 bot/operation_log.db "DELETE FROM servers;"
```

---

## 测试 Bot 是否正常工作

在配置好的 QQ 群里发：

```
帮助
```

应该回复指令列表。然后 `添加服务器` 走完一遍，`开服` 测试。

排查顺序见 [README.md 常见问题](../README.md#常见问题)。
