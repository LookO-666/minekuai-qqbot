# 麦块联机 QQ Bot

群里发一句 `开服`，bot 自动开 [麦块联机](https://minekuai.com) 的计时卡 **+** 启动服务器实例，全自动，玩家直接进。

> **简短历程**：v1 只开计时卡，服务器还要管理员手动点；v2 加了 token 自动续期；v3 用 Playwright 无头浏览器登录拿 Pterodactyl 面板 cookies，调标准 `power` 端点直接启动实例。**现在全自动**。

---

## 功能特性

- **真·一键开服**：群里发 `开服 <名字>`，bot 同时开计时卡 + 启动实例，30-60 秒玩家可进
- **多服务器**：一个 bot 同时控制任意多台麦块联机实例
- **token 自动续期**：JWT 失效 / 被冻结 → bot 用绑定账号 Playwright 模拟登录拿新 token，群友无感
- **面板 cookies 自动管理**：调 `power` 端点用的 Pterodactyl session 同样自动刷新
- **聊天交互式配置**：群里指令就能加 / 删 / 改服务器，不用编辑 `.env` + 重启
- **持久化**：所有配置存 SQLite；所有操作有日志
- **白名单 + 冷却 + 二次确认**：误操作 / 滥用 / 误关服都被挡住

---

## 指令速查

### 开关

| 指令 | 说明 |
|---|---|
| `开服 [<名字>]` / `开机` / `start` | 开计时卡 + 启动实例（多台时不带名字会列表询问） |
| `关服 [<名字>]` / `关机` / `stop` | 进入二次确认 |
| `确认关服` | 确认上一条关服请求（关闭计时卡，实例会自动停） |

### 服务器管理

| 指令 | 说明 |
|---|---|
| `服务器列表` / `列表` | 列出所有服务器 |
| `服务器地址 [<名字>]` / `地址` | 查询连接 IP:端口 |
| `添加服务器` / `添加` | 6 步交互：名字 / 卡 ID / token / clientid / 地址 / 实例 ID |
| `删除服务器 <名字>` | 删除（需要『确认』） |
| `修改服务器名字 [旧 [新]]` | 改名 |
| `修改地址 [<名字> [<地址>]]` | 改地址 |
| `修改uuid [<名字> [<实例 ID>]]` | 改实例 ID（控制服务器实例启停） |
| `更新token <名字>` | 手动更新 token（自动续期都有了一般不需要） |
| `绑定账号 <服务器> <手机号>` | 把账号绑给服务器，开启自动续期 + 实例自动启动 |

### 账号管理（自动续期 token + 启动实例用）

| 指令 | 说明 |
|---|---|
| `添加账号` / `加账号` | 多步交互：手机号 + 密码 |
| `账号列表` | 列出已配置账号（手机号打码显示） |
| `删除账号 <手机号>` | 删除（同时解绑相关服务器） |

### 通用

| 指令 | 说明 |
|---|---|
| `取消` | 中止当前正在进行的多步交互 |
| `帮助` / `help` | 显示指令列表 |

> 默认无前缀（群里直接发『开服』触发）。可在 `.env` 里改 `COMMAND_START`。

---

## 系统架构

```
QQ 群消息
   │
   ▼
NapCatQQ（QQ 协议端，挂机器人小号）
   │  OneBot v11 反向 WebSocket
   ▼
nonebot2 + minekuai 插件（业务逻辑）
   │
   ├─ 第 1 步：开计时卡
   │    HTTPS + JWT Bearer
   │    api.minekuai.com/system/timeBalance/...
   │       │
   │       ├─ 401 → 服务器绑了账号？
   │       │       ↓ 是
   │       │      Playwright 无头 Chromium 登录 minekuai.com
   │       │      → 拿到新 JWT + clientid + cookies + xsrf
   │       │      → 写回 DB → 重试
   │       │
   │       └─ 200 → 计时卡开启成功
   │
   └─ 第 2 步：启动服务器实例（如果配了 uuid + 账号）
        HTTPS + Pterodactyl session cookies + X-XSRF-TOKEN
        POST minekuai.com/api/client/servers/<id>/power {"signal":"start"}
           │
           ├─ 401/419 → cookies 失效 → 同一套 Playwright 流程刷一遍
           │
           └─ 204 → 启动信号下达，30-60 秒后服务器可进

           SQLite (bot-data/operation_log.db)
           ├── servers 表       — 服务器配置（card_id / instance_uuid / 绑定账号）
           ├── accounts 表      — 手机号 / 密码 / session cookies / xsrf
           └── operation_log 表 — 谁在何时操作了什么
```

两个进程跑在一台机器上，都是 Docker 容器，由 [docker-compose.yml](docker-compose.yml) 编排：

| 容器 | 镜像 | 作用 |
|---|---|---|
| `napcat` | `mlikiowa/napcat-docker:latest` | 挂 QQ 账号，把 QQ 消息翻译成 OneBot v11 协议 |
| `minekuai-bot` | 本地构建（[bot/Dockerfile](bot/Dockerfile)），含 Chromium | nonebot2 主程序 + Playwright |

镜像约 **1.88 GB**（Chromium 占大头）。`network_mode: host` 让 napcat ↔ bot 走 `127.0.0.1:8080`，免去 Docker DNS 复杂度。

---

## 目录结构

```
minekuai-qqbot/
├── docker-compose.yml          # napcat + bot 编排
├── docs/
│   ├── SETUP.md                # 非 Docker 开发部署
│   └── DEPLOY-LINUX.md         # Linux 服务器 Docker 部署补充
├── deploy/
│   └── minekuai-bot.service    # systemd 单元（不用 docker-compose 时备用）
└── bot/
    ├── Dockerfile              # bot 镜像（含 Python + Playwright + Chromium）
    ├── bot.py                  # nonebot 入口
    ├── pyproject.toml          # Python 依赖
    ├── .env.example            # 配置模板
    └── plugins/minekuai/
        ├── __init__.py         # 指令注册 + 业务流程 + 自动续期重试逻辑
        ├── client.py           # MinekuaiClient (计时卡) + PanelClient (实例)
        ├── auth.py             # Playwright 自动登录（捕获 token + cookies）
        ├── config.py           # 配置 schema
        ├── servers.py          # SQLite 数据层：servers + accounts
        ├── permission.py       # 白名单 + 冷却 + 待确认状态
        └── audit.py            # 操作日志
```

---

## 快速开始（Linux + Docker）

要求：Docker 28+ / Docker Compose v2 / 能访问外网 / 至少 1GB 空闲内存（Chromium 跑起来用得多）。

### 1. 克隆 + 进目录

```bash
git clone https://github.com/<你>/minekuai-qqbot.git
cd minekuai-qqbot
```

### 2. 全局配置（白名单等）

```bash
cp bot/.env.example bot/.env
chmod 600 bot/.env
nano bot/.env
```

只用填权限字段：

| 字段 | 含义 |
|---|---|
| `ALLOWED_GROUPS` | 你的 QQ 群号列表，如 `[123456789]`（必填） |
| `ALLOWED_USERS` | 允许的 QQ 号列表，留空 `[]` = 群内所有人都能用 |
| `COMMAND_COOLDOWN` | 同一用户的指令冷却（秒），默认 5 |
| `STOP_NEED_CONFIRM` | 关服是否需要二次确认，默认 `true` |

> **服务器/账号配置一律不用预先写 `.env`**——bot 启动后在 QQ 群里发 `添加服务器` / `添加账号` 全交互式完成。

### 3. 拉起 napcat + 扫码登录 QQ

```bash
docker compose up -d napcat
docker compose logs napcat | grep -iE "webui|token"
```

会看到一行：

```
[NapCat] [WebUi] WebUi User Panel Url: http://127.0.0.1:6099/webui?token=xxxxxxxx
```

> ⚠️ **URL 末尾必须带斜杠**：`http://127.0.0.1:6099/webui/?token=xxx`，不然 301 跳转会让某些浏览器卡住。

**SSH 用户**：服务器没图形界面，在本地电脑新开终端做端口转发：

```bash
ssh -L 6099:127.0.0.1:6099 user@your-server
```

本地浏览器打开 WebUI 链接后两步：

1. **登录 QQ**：扫码登录机器人小号（不要主号）
2. **网络配置 → 新建 → WebSocket 客户端**：

   | 字段 | 值 |
   |---|---|
   | 名称 | `bot` |
   | URL | `ws://127.0.0.1:8080/onebot/v11/ws` |
   | Token | 留空 |
   | 消息格式 | `array` |
   | 启用 | ✅ |
   | 心跳间隔 | `30000` |

### 4. 拉起机器人

```bash
docker compose up -d --build bot
docker compose logs -f bot
```

镜像首次构建要 **5-15 分钟**（pip 装 Playwright + 下载 Chromium 约 150MB）。后续修改代码重建只要几秒。

成功标志：

```
[INFO] uvicorn | Uvicorn running on http://127.0.0.1:8080
[INFO] uvicorn | "WebSocket /onebot/v11/ws" [accepted]
[INFO] nonebot | OneBot V11 | Bot xxxxxxxxxx connected
```

### 5. 在 QQ 群里加账号 + 加服务器

把机器人加到配好的 QQ 群里。

**先加账号**（用于自动续期 + 实例启动）：

```
添加账号
```

bot 会问 2 个问题：手机号（minekuai 登录用的）+ 密码。**密码以明文存在本机 SQLite 里**，建议给 minekuai 设个独立密码。

**再加服务器**：

```
添加服务器
```

6 步交互式询问。其中：

- **计时卡 ID**：F12 → 任意 `api.minekuai.com/system/timeBalance/.../startTiming/XXX` 那串纯数字
- **token / clientid**：F12 → 任意 `api.minekuai.com` 请求的 Request Headers 里
- **实例 ID**：浏览器控制台 URL `minekuai.com/server/XXX` 里的 XXX（如 `420d4426`）

**最后绑账号**：

```
绑定账号 GTNH 13xxxxxxxx
```

群里发：

```
开服 GTNH
```

第一次会触发一次 Playwright 登录（~20 秒），把 cookies 存进 DB。之后开服都 2-3 秒到位（cookies 没过期的话）。

---

## token / clientid / 计时卡 ID / 实例 ID 怎么抓

1. 浏览器登录 https://minekuai.com
2. 进想要控制的服务器控制台页面（URL 形如 `minekuai.com/server/420d4426`）
3. F12 → **Network**（网络）标签
4. 点页面上的"启动"或"关闭"按钮（让网络面板出现请求）
5. 找需要的字段：

| 字段 | 怎么找 |
|---|---|
| `token` | 任意 `api.minekuai.com` 请求 → Request Headers → `authorization: Bearer eyJ...`（bot 会自动去掉 `Bearer ` 前缀） |
| `clientid` | 同上请求 Headers 里的 `clientid` 字段 |
| `计时卡 ID` | URL 含 `startTiming` 或 `stopTiming` 的请求，URL 末尾的纯数字 |
| `实例 ID` | 浏览器地址栏 `minekuai.com/server/XXX` 中的 XXX，或任意 `/api/client/servers/XXX/...` 请求的 XXX |
| `地址` | 网页控制台右上角显示（玩家用的 IP:端口） |

> token 失效之后**有自动续期，平时不用管**。需要的话用 `更新token <名字>` 手动重粘。

---

## 日常运维

```bash
docker compose ps                              # 容器状态
docker compose logs -f bot                     # 机器人日志
docker compose logs -f napcat                  # QQ 协议端日志
docker compose restart bot                     # 改了 .env 后只重启 bot
docker compose down && docker compose up -d    # 全部重起
```

服务器开机后自动恢复（`restart: unless-stopped` + Docker 自启）。

### 查看操作日志

```bash
sqlite3 bot-data/operation_log.db \
  "SELECT ts, user_name, command, success, detail FROM operation_log ORDER BY id DESC LIMIT 20;"
```

### 查看已配置的服务器 + 账号

```bash
sqlite3 bot-data/operation_log.db \
  "SELECT name, instance_uuid, account_phone FROM servers;"
sqlite3 bot-data/operation_log.db \
  "SELECT phone, length(session_cookie), last_refresh_at FROM accounts;"
```

> 直接群里 `服务器列表` / `账号列表` 更方便。

### 自动续期失败时

如果某次 `开服` 报：

```
❌ token 失效，自动续期也失败：xxx
```

可能原因：
- 账号密码错了 → `删除账号 <手机号>` + `添加账号` 重输
- minekuai 改了登录页 UI → 我（bot 作者）需要调 Playwright 选择器
- 触发了风控（滑块 / 验证码） → 暂时只能 `更新token <名字>` 手动应急

### 实例启动失败时

报 `实例启动失败: xxx`：
- `xxx` 是 `面板 cookies 丢失` / `401` → 自动会触发刷新，无需手动；若反复失败说明 cookies 不可用
- `xxx` 是其他错误 → 看 docker logs 里 `panel POST power` 那行的具体响应

---

## 配置详解（bot/.env）

```ini
# ===== nonebot 基础 =====
HOST=127.0.0.1
PORT=8080                                # 改这里也要同步改 napcat WS 客户端 URL
LOG_LEVEL=INFO                           # DEBUG 看详细 HTTP 日志
ONEBOT_WS_URLS=["ws://127.0.0.1:8080/onebot/v11/ws"]
COMMAND_START=[""]                       # 空=直接发『开服』触发；填 "/" 要 "/开服"

# ===== 麦块联机（兼容字段，新部署可全留空） =====
# 启动时若数据库为空 + 这三个都填了，会自动导入为名为『default』的服务器
# 之后请用『添加服务器』等指令管理；这些字段会被忽略
MINEKUAI_TOKEN=
MINEKUAI_CLIENT_ID=
MINEKUAI_CARD_ID=

# ===== 权限 =====
ALLOWED_GROUPS=[群号1, 群号2]              # 哪些群能用，空=不限群（不推荐）
ALLOWED_USERS=[]                          # 哪些 QQ 能用，空=群内所有人都能用

# ===== 行为 =====
COMMAND_COOLDOWN=5                        # 同一用户同一指令冷却（秒）
STOP_NEED_CONFIRM=true                    # 关服是否需要二次确认
```

---

## 常见问题

**Q: 群里发"开服"机器人没反应**

按这个顺序排查：

```bash
# 1. 容器都活着吗？
docker compose ps

# 2. NapCat 连上 nonebot 了吗？
docker compose logs bot | grep -i "connected"
# 应有 "OneBot V11 | Bot xxx connected"

# 3. QQ 在线吗？
docker compose logs napcat | tail -20
# 找最后一条状态变更，应该是"上线"，不是"离线"

# 4. 群号在白名单里吗？
grep ALLOWED_GROUPS bot/.env
```

**Q: 开了计时卡但服务器实例没启动**

通常两种原因：
- **服务器没绑账号** → `绑定账号 GTNH <手机号>` 后重试
- **服务器没填实例 ID** → `修改uuid GTNH <id>` 后重试

`服务器列表` 能看到当前每台的配置。

**Q: 报错 "操作太频繁，请稍后再试"**

计时卡刚被开/关过，后端限流。bot 识别这种情况后用 ℹ️ 而非 ❌ 显示（状态实际已经对了）。

**Q: 镜像构建很慢**

Chromium 下载约 150MB。国内服务器最好挂个代理或者用国内 Docker 镜像源（见 [docs/DEPLOY-LINUX.md](docs/DEPLOY-LINUX.md)）。

**Q: WebUI 打不开**

最常见三种：
1. URL 末尾少了斜杠 → 用 `http://127.0.0.1:6099/webui/?token=xxx`
2. SSH 远程没做端口转发 → 见上文第 3 步
3. 防火墙 → 开 6099 或者只走 SSH 转发

**Q: napcat 容器 Up 但 WebUI 端口没起来**

X11/Xvfb 的坑。`docker-compose.yml` 里有一段 `entrypoint:` 覆盖（创建 `/tmp/.X11-unix`，把 Xvfb 显示号从 `:1` 改成 `:99`），避开 `network_mode: host` 下宿主机已占用 `:1` 显示号的冲突。如果换镜像后又起不来，先看 `docker compose logs napcat` 里有没有 `Missing X server`、`server already running` 这类报错。

**Q: 我有多台服务器**

直接 `添加服务器` 加几台，起不同名字（如 `GTNH` / `原版`）。每台都可以独立绑账号、配 UUID。`开服 GTNH` / `开服 原版` 分别操作。

---

## 安全建议

1. **机器人 QQ 用小号**：万一被风控不影响主号
2. **QQ 小号别在其它地方登录**：协议端会被踢导致 bot 静默失效
3. **WebUI 端口（6099）登录后关掉**：日常不需要外露
   ```bash
   sudo ufw deny 6099/tcp
   ```
4. **`bot/.env` 文件权限收紧**：`chmod 600 bot/.env`
5. **minekuai 密码用独立的**：DB 里存明文，给它一个跟其它服务不一样的强密码
6. **定期备份 SQLite**：
   ```bash
   cp bot-data/operation_log.db bot-data/operation_log.db.$(date +%F).bak
   ```

---

## 文档

- [docs/SETUP.md](docs/SETUP.md) — 非 Docker 开发部署
- [docs/DEPLOY-LINUX.md](docs/DEPLOY-LINUX.md) — Linux 服务器 Docker 部署补充

---

## 技术栈

- [nonebot2](https://nonebot.dev/) — 异步 QQ/IM 机器人框架
- [NapCatQQ](https://github.com/NapNeko/NapCatQQ) — QQ NT 协议端，OneBot v11 接口
- [Playwright](https://playwright.dev/) — 自动登录拿 token + cookies
- [httpx](https://www.python-httpx.org/) — 异步 HTTP 客户端
- [pydantic](https://docs.pydantic.dev/) — 配置 schema 校验
- [SQLite](https://www.sqlite.org/) — 配置 + 账号 + 操作日志

---

## 更新历史

### v3 (current) — 全自动开服

- 加 **Playwright 无头 Chromium 自动登录**：JWT token 失效时用账号密码自动刷新，群友无感
- 加 **Pterodactyl 面板 cookies 管理**：调 `power` 端点用的 session 同步自动续期
- 加 **实例自动启动**：开计时卡之后直接 `POST /api/client/servers/<id>/power {"signal":"start"}`，30-60 秒玩家就能进
- 新指令：`添加账号` / `账号列表` / `删除账号` / `绑定账号` / `修改uuid`
- DB 加 `accounts` 表 + `servers.instance_uuid` 列

### v2 — 多服务器 + 聊天交互式配置

- 一个 bot 同时控制多台
- 群里指令 `添加服务器` / `删除服务器` / `修改服务器名字` / `修改地址` / `更新token`
- 服务器配置从 `.env` 移到 SQLite
- DB 加 `servers` 表

### v1 — 单台 + 计时卡

- 群里 `开服` / `关服` 调 `api.minekuai.com` 开关计时卡
- 服务器实例启动还需要管理员手动到 minekuai.com 点
- 配置全部在 `.env` 里

---

## License

[MIT](LICENSE)

---

## 致谢

- 麦块联机不开放公开 API，所有接口路径都是从浏览器 F12 抓出来的。如果哪天接口又改了，欢迎 PR 修正。
- 作者：[@klukeyuan](mailto:klukeyuan@gmail.com)
