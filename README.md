# Minekuai QQ Bot

[![Tests](https://github.com/LookO-666/minekuai-qqbot/actions/workflows/tests.yml/badge.svg)](https://github.com/LookO-666/minekuai-qqbot/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

English | [简体中文](README.zh-CN.md)

Send `开服` (start server) in a QQ group and the bot will start both the [Minekuai](https://minekuai.com) time card **and** the Minecraft server instance. Once the server is ready, players can join immediately.

> **In short:** v1 only controlled time cards; v2 added automatic token renewal; v3 used a panel session to start instances; v4 switched to Pterodactyl Client API Keys, so panel control no longer depends on short-lived cookies. The entire start-up flow is now automated.

---

## Features

- **One-command server start:** starts the time card and server instance, then notifies the group when the server is ready
- **Two-way Minecraft ↔ QQ chat bridge:** relays in-game chat to QQ and sends ordinary QQ messages back to active servers with `tellraw`
- **Real-time event feed:** watches the Pterodactyl console over WebSocket for joins, leaves, deaths, and advancements; falls back to polling `latest.log`
- **Player linking and statistics:** links QQ users to Minecraft names, uses QQ display names, supports mentions, playtime leaderboards, and death leaderboards
- **Status and administration:** player count, player list, latency, version, CPU, memory, disk, mods, plugins, logs, console commands, and instance restarts
- **Smart background management:** idle shutdown, a cancellable 60-second countdown, temporary suspension, and automatic keepalive for servers not started for six days
- **Resource alerts:** notifies administrators only after CPU or memory stays above a threshold, with sustained checks and cooldowns to avoid spam
- **Multiple servers:** controls multiple instances from one bot; status queries can be aggregated while administrative actions target a specific server
- **Automatic authentication:** renews expired time-card JWTs with Playwright; prefers long-lived panel Client API Keys and retains session authentication as a compatibility fallback
- **Interactive setup in QQ:** administrators can add accounts and servers without editing the database or restarting the bot
- **Encrypted persistence:** stores configuration and statistics in SQLite; encrypts tokens, passwords, Client API Keys, and session credentials with Fernet
- **Role-based permissions:** regular users can start, stop, query, and use player features; administrators manage accounts, servers, and privileged operations

---

## Command reference

Commands have no prefix by default and can be sent directly in a QQ group. Commands marked 🔒 are limited to users in `ADMIN_USERS`, or to all allowed group members when `ADMIN_ALL_GROUP_MEMBERS=true`. All commands remain subject to `ALLOWED_GROUPS`, `ALLOWED_USERS`, and cooldown settings.

The command names are Chinese because the bot is designed for Chinese QQ groups; English aliases are available for several common operations.

### Everyday controls and queries

| Command | Description |
|---|---|
| `开服 [name]` / `开机` / `start` | Start the time card and instance. With multiple servers, omitting the name opens a selection prompt. A second message is sent when the server is ready. |
| `关服 [name]` / `关机` / `stop` | Stop the time card. A confirmation is required by default. |
| `确认关服` | Confirm your most recent stop request within five minutes. |
| `在线 [name]` / `人数` / `状态` | Show players, latency, version, and address. Without a name, summarizes every server. |
| `查服 [name]` / `info` | Show instance state, CPU, memory, disk, address, and mod/plugin counts. |
| `服务器列表` / `列表` | List configured servers, time-card IDs, and connection addresses. |
| `服务器地址 [name]` / `地址` | Show the connection address for one or all servers. |
| `模组 [name]` / `mods` | List JAR files under `/mods`. |
| `插件 [name]` / `plugins` | List JAR files under `/plugins`. |
| `mc <Java player name>` / `皮肤` | Look up a Minecraft Java player and render a profile card. |

### Chat bridge and player statistics

| Command or action | Description |
|---|---|
| Ordinary QQ group text | Relayed to every configured panel server where the bot has confirmed at least one online player. It appears in aqua as `[QQ] group nickname: message`. |
| In-game chat | Relayed from the live console to every `ALLOWED_GROUPS` group. Linked players use their QQ group card or nickname. |
| `绑定 <player>` | Link your QQ account to a Minecraft name for display names, mentions, and parameter-free statistics queries. |
| `解绑` / `绑定列表` | Remove your own link or list all links. Administrators can use `绑定 <QQ> <player>` and `解绑 <QQ>` on behalf of another user. |
| `今日榜` / `本周榜` | Show today's or the last seven days' playtime leaderboard, up to 15 players. |
| `在线时长 [player]` | Show today's and seven-day playtime. Linked users may omit the player name. |
| `死亡榜` | Show the all-time death leaderboard, up to 15 players. |
| `死亡次数 [player]` | Show today's and all-time deaths. Linked users may omit the player name. |

### Administrator operations

| Command | Description |
|---|---|
| 🔒 `指令 [name] <MC command>` / `cmd` | Send a command to the Minecraft server console. |
| 🔒 `日志 [name] [lines]` / `console` | Read the end of `latest.log`; defaults to 30 lines, maximum 200. |
| 🔒 `重启 [name]` / `restart` | Restart the server instance without stopping its time card, then notify the group when it is ready again. |
| 🔒 `自动关停` | Show idle-shutdown settings and active countdowns for all servers. |
| 🔒 `自动关停 <name> <minutes>` | Shut a server down after the specified idle period; `0` disables it. |
| 🔒 `暂停自动关停 [minutes]` | Globally suspend idle shutdown, defaulting to 60 minutes, and cancel active countdowns. |
| 🔒 `取消关停` / `保留` | Cancel the current automatic shutdown during its 60-second countdown. |

### Server and account configuration

| Command | Description |
|---|---|
| 🔒 `添加账号` / `加账号` | Store a Minekuai phone number and password; the password is encrypted. |
| 🔒 `账号列表` | Show masked phone numbers, linked servers, and panel authentication methods. |
| 🔒 `删除账号 <phone>` | Delete an account and unlink its servers after confirmation. |
| 🔒 `添加服务器` / `添加` | Five-step setup: name, time-card ID, address, instance UUID, and an existing account. Token, client ID, and session data are obtained automatically. |
| 🔒 `删除服务器 <name>` | Delete a server after replying `确认` (confirm). |
| 🔒 `修改服务器名字 [old [new]]` | Rename a server label. |
| 🔒 `修改地址 [name [address]]` | Change a player connection address. |
| 🔒 `修改uuid [name [instance ID]]` | Change or clear an instance ID. Clearing it returns to time-card-only mode. |
| 🔒 `绑定账号 <server> <phone>` | Change the account used for JWT renewal and panel operations. |
| 🔒 `更新token <name>` | Manually replace a JWT as an emergency fallback when automatic renewal fails. |

### General

| Command | Description |
|---|---|
| `取消` | Cancel the current multi-step interaction. |
| `帮助` / `help` | Show the built-in help message. |

> The chat bridge processes ordinary plain text in allowed groups. Recognized bot commands are intercepted by command handlers and are never relayed as chat. With multiple servers, one QQ message is sent to every server where online players have been detected.

---

## Automatic capabilities

| Capability | Behavior |
|---|---|
| Ready notification | Waits 20 seconds after starting an instance, then probes with Minecraft SLP every five seconds. It broadcasts the address and player count as soon as the server is reachable, for up to five minutes. |
| Real-time event listener | Prefers the Pterodactyl WebSocket and reconnects in the background. When real-time mode is disabled, polls `latest.log`. |
| Join, leave, death, and advancement events | Broadcasts events to every allowed group. Deaths and advancements mention the linked QQ user. |
| Playtime and death statistics | Maintains player sessions from server logs, persists them in SQLite, and settles sessions when a server stops or becomes unreachable. |
| Two-way chat bridge | Parses standard chat logs for MC → QQ. QQ → MC uses `tellraw` and only targets servers with confirmed online players. |
| Idle shutdown | Includes a five-minute grace period after start. When the configured idle period is reached, broadcasts a cancellable 60-second countdown. |
| Automatic keepalive | If a fully configured server has not started for about six days, starts it, keeps it running for about five minutes, then stops it, announcing each stage. |
| Resource alerts | Requires CPU or memory to remain above its threshold for several checks before alerting; repeat alerts are rate-limited and mention administrators. |
| SLP failure backoff | Background probes back off for 30, 60, 120, then 300 seconds after repeated failures. An explicit `在线` query still runs immediately. |
| Credential recovery | Automatically signs in to renew an expired JWT. Refreshes a compatibility panel session once after a 401/419. Reports a revoked Client API Key explicitly. |
| Update announcement | If the operator creates `bot-data/.changelog_to_send`, the bot sends its contents once after connecting and then removes the file. |

> Complete MC → QQ event and chat support requires an instance UUID, a linked account, and access to a standard-format `latest.log`. Modpacks or chat plugins that rewrite log lines may require additional regular expressions.

---

## Architecture

```text
QQ group message
   │
   ▼
NapCatQQ (QQ protocol client running a dedicated bot account)
   │  OneBot v11 reverse WebSocket
   ▼
nonebot2 + Minekuai plugin (application logic)
   │
   ├─ Step 1: start the time card
   │    HTTPS + JWT Bearer
   │    api.minekuai.com/system/timeBalance/...
   │       │
   │       ├─ 401 → is an account linked to this server?
   │       │       ↓ yes
   │       │      Playwright signs in to minekuai.com with headless Chromium
   │       │      → obtains a new JWT + client ID (and compatibility cookies)
   │       │      → writes them to the database → retries
   │       │
   │       └─ 200 → time card started
   │
   └─ Step 2: start the instance (when UUID + account are configured)
        HTTPS + Pterodactyl Client API Key (Authorization: Bearer ptlc_...)
        POST minekuai.com/api/client/servers/<id>/power {"signal":"start"}
           │
           ├─ no API Key → compatibility session cookies + X-XSRF-TOKEN
           │
           └─ 204 → start signal accepted; server is usually ready in 30–60 seconds

           SQLite (bot-data/operation_log.db)
           ├── servers       — server settings (card ID / instance UUID / linked account)
           ├── accounts      — phone / password / encrypted Client API Key / compatibility session
           └── operation_log — who performed each operation and when
```

Both processes run on the same machine as Docker containers orchestrated by [docker-compose.yml](docker-compose.yml):

| Container | Image | Purpose |
|---|---|---|
| `napcat` | `mlikiowa/napcat-docker:latest` | Runs the QQ account and translates QQ messages to OneBot v11. |
| `minekuai-bot` | Locally built from [bot/Dockerfile](bot/Dockerfile), including Chromium | Runs nonebot2, the application plugin, and Playwright. |

The image is about **1.88 GB**, largely due to Chromium. `network_mode: host` lets NapCat and the bot communicate through `127.0.0.1:8080` without Docker DNS configuration.

---

## Project layout

```text
minekuai-qqbot/
├── docker-compose.yml          # NapCat + bot orchestration
├── docs/
│   ├── SETUP.md                # non-Docker development deployment
│   └── DEPLOY-LINUX.md         # additional Linux Docker deployment notes
├── deploy/
│   └── minekuai-bot.service    # optional systemd unit without Compose
└── bot/
    ├── Dockerfile              # Python + Playwright + Chromium image
    ├── bot.py                  # nonebot entry point
    ├── pyproject.toml          # Python dependencies
    ├── .env.example            # configuration template
    └── plugins/minekuai/
        ├── __init__.py         # commands, workflows, and authentication retries
        ├── client.py           # MinekuaiClient (time card) + PanelClient (instance)
        ├── auth.py             # Playwright sign-in and token/cookie capture
        ├── config.py           # configuration schema
        ├── servers.py          # SQLite storage for servers and accounts
        ├── permission.py       # allowlists, cooldowns, and confirmations
        └── audit.py            # operation log
```

---

## Quick start (Linux + Docker)

Requirements: Docker 28+, Docker Compose v2, internet access, and at least 1 GB of free memory. Chromium is the largest runtime consumer.

### 1. Clone the repository

```bash
git clone https://github.com/LookO-666/minekuai-qqbot.git
cd minekuai-qqbot
```

### 2. Configure global permissions

```bash
cp bot/.env.example bot/.env
chmod 600 bot/.env
nano bot/.env
```

Only the permission-related fields are required initially:

| Field | Meaning |
|---|---|
| `ALLOWED_GROUPS` | QQ group IDs, for example `[123456789]` (required). |
| `ALLOWED_USERS` | Allowed QQ user IDs. Leave as `[]` to allow every member of an allowed group. |
| `ADMIN_USERS` | Administrator QQ user IDs. Administrative commands are denied when empty unless group-wide administration is enabled. |
| `ADMIN_ALL_GROUP_MEMBERS` | Set to `true` to grant administrative commands to every allowed member of an `ALLOWED_GROUPS` group. Defaults to `false`; enable only in a trusted group. |
| `CREDENTIAL_ENCRYPTION_KEY` | Fernet key used to encrypt sensitive SQLite fields (required; back it up securely). |
| `COMMAND_COOLDOWN` | Per-user command cooldown in seconds; defaults to 5. |
| `STOP_NEED_CONFIRM` | Whether stopping a server requires confirmation; defaults to `true`. |

> **Do not preconfigure servers or accounts in `.env`.** After the bot starts, an `ADMIN_USERS` administrator—or any allowed group member when `ADMIN_ALL_GROUP_MEMBERS=true`—can complete setup interactively with `添加账号` and `添加服务器` in QQ.
>
> Generate a key with `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Existing encrypted credentials cannot be recovered if the key is lost. Store key and database backups separately.

### 3. Start NapCat and sign in to QQ

```bash
docker compose up -d napcat
docker compose logs napcat | grep -iE "webui|token"
```

The logs will include a line similar to:

```text
[NapCat] [WebUi] WebUi User Panel Url: http://127.0.0.1:6099/webui?token=xxxxxxxx
```

> ⚠️ **Add a trailing slash to the URL:** use `http://127.0.0.1:6099/webui/?token=xxx`. Without it, a 301 redirect can hang in some browsers.

If the server is accessed over SSH, open another terminal on your local computer and forward the port:

```bash
ssh -L 6099:127.0.0.1:6099 user@your-server
```

Open the WebUI locally and complete two steps:

1. **Sign in to QQ:** scan the QR code with a dedicated bot account, not your primary account.
2. **Network Configuration → New → WebSocket Client:**

   | Field | Value |
   |---|---|
   | Name | `bot` |
   | URL | `ws://127.0.0.1:8080/onebot/v11/ws` |
   | Token | Leave empty |
   | Message format | `array` |
   | Enabled | ✅ |
   | Heartbeat interval | `30000` |

### 4. Start the bot

```bash
docker compose up -d --build bot
docker compose logs -f bot
```

The first build takes approximately **5–15 minutes** because pip installs Playwright and downloads about 150 MB of Chromium files. Subsequent source-only rebuilds are much faster.

Successful startup looks like:

```text
[INFO] uvicorn | Uvicorn running on http://127.0.0.1:8080
[INFO] uvicorn | "WebSocket /onebot/v11/ws" [accepted]
[INFO] nonebot | OneBot V11 | Bot xxxxxxxxxx connected
```

### 5. Add an account and server in QQ

Invite the bot to a group listed in `ALLOWED_GROUPS`.

First add an account for automatic renewal and instance control:

```text
添加账号
```

The bot asks for the Minekuai phone number and password. Passwords, tokens, API Keys, and session credentials are encrypted with `CREDENTIAL_ENCRYPTION_KEY` before being stored in the local SQLite database. Legacy plaintext records are migrated automatically and idempotently at startup.

Then add a server:

```text
添加服务器
```

The five prompts request:

- **Name:** the label used in bot commands, such as `GTNH`
- **Time-card ID:** the numeric suffix of an `api.minekuai.com/system/timeBalance/.../startTiming/XXX` request
- **Address:** the Minecraft address shown in the Minekuai console
- **Instance ID:** the value in `minekuai.com/server/XXX`, such as `420d4426`
- **Account:** choose an account already added to the bot; token, client ID, and panel authentication are obtained automatically

Now send:

```text
开服 GTNH
```

When the linked account has a Client API Key, panel control uses Bearer authentication and normally sends the instance start signal within two or three seconds. Playwright is only needed when the time-card JWT expires.

---

## Finding the token, client ID, time-card ID, and instance ID

Most new deployments only need the time-card ID and instance ID during interactive setup; token and client ID are obtained automatically. To inspect or recover them manually:

1. Sign in at <https://minekuai.com>.
2. Open the target server console, whose URL resembles `minekuai.com/server/420d4426`.
3. Open browser developer tools and select **Network**.
4. Click **Start** or **Stop** on the page to create requests.
5. Find the relevant value:

| Field | Where to find it |
|---|---|
| `token` | Any `api.minekuai.com` request → Request Headers → `authorization: Bearer eyJ...`. The bot removes the `Bearer ` prefix automatically. |
| `clientid` | The `clientid` header in the same request. |
| Time-card ID | The numeric suffix of a request URL containing `startTiming` or `stopTiming`. |
| Instance ID | The `XXX` in `minekuai.com/server/XXX`, or in `/api/client/servers/XXX/...`. |
| Address | The player-facing host and port shown in the web console. |

Expired tokens are normally renewed automatically. Use `更新token <name>` only as a manual fallback.

---

## Operations

```bash
docker compose ps                              # container status
docker compose logs -f bot                     # bot logs
docker compose logs -f napcat                  # QQ protocol client logs
docker compose restart bot                     # restart after changing .env
docker compose down && docker compose up -d    # restart the complete stack
```

Containers recover after a host reboot through `restart: unless-stopped` and Docker's startup service.

### Inspect the operation log

```bash
sqlite3 bot-data/operation_log.db \
  "SELECT ts, user_name, command, success, detail FROM operation_log ORDER BY id DESC LIMIT 20;"
```

### Inspect configured servers and accounts

```bash
sqlite3 bot-data/operation_log.db \
  "SELECT name, instance_uuid, account_phone FROM servers;"
sqlite3 bot-data/operation_log.db \
  "SELECT phone, length(panel_api_key), last_refresh_at FROM accounts;"
```

Using `服务器列表` and `账号列表` in QQ is usually more convenient.

### When automatic renewal fails

If `开服` reports:

```text
❌ token expired and automatic renewal also failed: xxx
```

Possible causes:

- The account password is wrong: run `删除账号 <phone>` and add it again with `添加账号`.
- Minekuai changed its sign-in UI: the Playwright selectors need to be updated.
- A risk-control challenge, slider, or CAPTCHA was triggered: use `更新token <name>` as a temporary manual fallback.

### When instance startup fails

If the bot reports `instance startup failed: xxx`:

- If `xxx` says the panel API Key is invalid or revoked, create a new Client API Key at <https://minekuai.com/account/api>.
- Accounts without an API Key fall back to a session; a session 401/419 triggers one automatic refresh.
- For other errors, inspect the `panel POST power` response in the Docker logs.

---

## Configuration reference (`bot/.env`)

```ini
# ===== nonebot basics =====
HOST=127.0.0.1
PORT=8080                                # also update the NapCat WS client URL
LOG_LEVEL=INFO                           # DEBUG enables detailed HTTP logs
ONEBOT_WS_URLS=["ws://127.0.0.1:8080/onebot/v11/ws"]
COMMAND_START=[""]                       # empty: send 开服; use ["/"] to require /开服

# ===== Minekuai legacy compatibility =====
# If the database is empty and all three are set, they are imported as "default".
# Manage servers with 添加服务器 afterward; these values are then ignored.
MINEKUAI_TOKEN=
MINEKUAI_CLIENT_ID=
MINEKUAI_CARD_ID=

# ===== permissions =====
ALLOWED_GROUPS=[group_id_1, group_id_2]   # allowed groups; empty allows all (not recommended)
ALLOWED_USERS=[]                          # allowed QQ users; empty allows all users in allowed groups
ADMIN_USERS=[admin_qq_id]                 # server/account/log/restart administration
ADMIN_ALL_GROUP_MEMBERS=false              # true grants admin rights to all allowed group members
CREDENTIAL_ENCRYPTION_KEY=                # Fernet key; do not change after data is encrypted

# ===== behavior =====
COMMAND_COOLDOWN=5                        # per-user, per-command cooldown in seconds
STOP_NEED_CONFIRM=true                    # require confirmation before stopping
POLL_INTERVAL_SECONDS=30                  # status/log/resource polling; minimum 5 seconds

# ===== chat bridge and event broadcast =====
EVENT_BROADCAST=true                      # master switch for events and log parsing
BROADCAST_JOIN_LEAVE=true                 # announce player joins and leaves
REALTIME_CONSOLE=true                     # WebSocket console; false polls logs
CHAT_BRIDGE=true                          # master switch for the two-way chat bridge
CHAT_MC_TO_QQ=true                        # in-game chat → QQ groups
CHAT_QQ_TO_MC=true                        # ordinary QQ text → active servers

# ===== resource alerts =====
RESOURCE_ALERT=true
CPU_ALERT_PERCENT=95                      # relative to panel CPU quota; 0 disables CPU alerts
MEM_ALERT_PERCENT=92                      # relative to panel memory quota; 0 disables memory alerts
ALERT_SUSTAINED_TICKS=3                   # consecutive checks above the threshold
ALERT_COOLDOWN_MINUTES=30                 # minimum interval between equivalent alerts
```

---

## Troubleshooting

**The bot does not respond to `开服`.**

Check in this order:

```bash
# 1. Are both containers running?
docker compose ps

# 2. Is NapCat connected to nonebot?
docker compose logs bot | grep -i "connected"
# Expect: "OneBot V11 | Bot xxx connected"

# 3. Is QQ online?
docker compose logs napcat | tail -20
# The latest status change should indicate online, not offline.

# 4. Is the group in the allowlist?
grep ALLOWED_GROUPS bot/.env
```

**The time card starts, but the server instance does not.**

The usual causes are:

- No linked account: run `绑定账号 GTNH <phone>` and retry.
- No instance ID: run `修改uuid GTNH <id>` and retry.

Use `服务器列表` to inspect the current configuration.

**The bot says the operation is too frequent.**

The time card was just started or stopped and the backend is rate-limiting requests. The bot recognizes this state and reports it with ℹ️ rather than ❌ because the desired state has already been reached.

**The image builds slowly.**

Chromium is about 150 MB. A proxy or a local Docker mirror may help in regions with slow international connectivity; see [docs/DEPLOY-LINUX.md](docs/DEPLOY-LINUX.md).

**NapCat WebUI does not open.**

Common causes:

1. The URL is missing its trailing slash. Use `http://127.0.0.1:6099/webui/?token=xxx`.
2. A remote SSH server needs the port forwarding described in Quick Start step 3.
3. A firewall is blocking port 6099; allow it temporarily or use SSH forwarding only.

**The NapCat container is running, but its WebUI port is unavailable.**

This is usually an X11/Xvfb conflict. The `entrypoint` override in `docker-compose.yml` creates `/tmp/.X11-unix` and moves Xvfb from display `:1` to `:99`, avoiding a host display collision under `network_mode: host`. After changing images, inspect `docker compose logs napcat` for messages such as `Missing X server` or `server already running`.

**The chat bridge or player events are not forwarded.**

Check the following:

1. The server has an instance UUID and linked account, and its panel Client API Key works.
2. `EVENT_BROADCAST=true`, `CHAT_BRIDGE=true`, and the relevant `CHAT_MC_TO_QQ` or `CHAT_QQ_TO_MC` direction is enabled.
3. Logs show `[ws] <server> real-time console connected`, or `latest.log` is readable when real-time mode is disabled.
4. The server emits standard English join, leave, death, advancement, and `<player> message` chat lines.
5. QQ → MC only relays after the bot has confirmed at least one player online from server logs.

With multiple servers, ordinary QQ messages go to every active server, while MC messages and events are broadcast to all `ALLOWED_GROUPS`. Set `CHAT_QQ_TO_MC=false` if ordinary group chat should never enter the game.

**I have multiple servers.**

Run `添加服务器` more than once and assign distinct names such as `GTNH` and `Vanilla`. Each server can use its own account and UUID. Operate them independently with commands such as `开服 GTNH` and `开服 Vanilla`.

---

## Development and testing

A non-Docker development environment requires `bot/requirements-security.txt` in addition to the main dependencies. Run the isolated test target before submitting or deploying changes:

```bash
docker compose --profile test run --rm test
```

---

## Security recommendations

1. **Use a secondary QQ account for the bot.** Risk controls should not affect your primary account.
2. **Do not sign that QQ account in elsewhere.** Another client may disconnect the protocol session silently.
3. **Close port 6099 after WebUI setup.** It is not needed during normal operation.
   ```bash
   sudo ufw deny 6099/tcp
   ```
4. **Restrict `bot/.env` permissions:** run `chmod 600 bot/.env`.
5. **Back up `CREDENTIAL_ENCRYPTION_KEY`.** Never commit it, and do not change it after storing encrypted data. Losing it makes credentials unrecoverable.
6. **Use a unique, strong Minekuai password.** Database encryption reduces exposure but does not eliminate host or backup compromise risks.
7. **Treat group-wide administration as full trust.** Keep `ADMIN_ALL_GROUP_MEMBERS=false` unless every member of every allowed group may manage credentials, run console commands, and stop or delete servers.
8. **Back up SQLite regularly:**
   ```bash
   cp bot-data/operation_log.db bot-data/operation_log.db.$(date +%F).bak
   ```

Never commit `.env`, the SQLite database, tokens, cookies, phone numbers, passwords, QQ/group IDs, or Client API Keys. If credentials are exposed, revoke them and rewrite Git history before publishing the repository.

---

## Documentation

- [docs/SETUP.md](docs/SETUP.md) — non-Docker development deployment
- [docs/DEPLOY-LINUX.md](docs/DEPLOY-LINUX.md) — additional Linux Docker deployment notes

---

## Technology stack

- [nonebot2](https://nonebot.dev/) — asynchronous QQ/IM bot framework
- [NapCatQQ](https://github.com/NapNeko/NapCatQQ) — QQ NT protocol client with a OneBot v11 interface
- [Playwright](https://playwright.dev/) — automatic sign-in when a time-card JWT expires
- [httpx](https://www.python-httpx.org/) — asynchronous HTTP client
- [websockets](https://websockets.readthedocs.io/) — Pterodactyl console and MC → QQ event stream
- [mcstatus](https://github.com/py-mine/mcstatus) — Minecraft SLP status, players, and latency
- [Pillow](https://python-pillow.org/) — Minecraft profile card rendering
- [pydantic](https://docs.pydantic.dev/) — configuration schema validation
- [SQLite](https://www.sqlite.org/) — configuration, accounts, statistics, and operation logs
- [cryptography](https://cryptography.io/) — Fernet encryption for sensitive credentials

---

## History

### v4 (current) — Client API Key panel authentication

- Prefers a Pterodactyl Client API Key created from the Minekuai account page.
- Encrypts API Keys with Fernet in SQLite and never prints or commits them.
- Retains session cookies as a compatibility fallback when an API Key is not configured.
- The time-card API still uses a JWT from `api.minekuai.com`, renewed by Playwright when necessary.

### v3 — fully automated startup

- Added headless Chromium sign-in with Playwright to renew expired JWTs transparently.
- Added Pterodactyl panel cookie management with automatic session renewal.
- Added automatic instance start after the time card: `POST /api/client/servers/<id>/power {"signal":"start"}`.
- Added `添加账号`, `账号列表`, `删除账号`, `绑定账号`, and `修改uuid`.
- Added the `accounts` table and `servers.instance_uuid` column.

### v2 — multiple servers and interactive configuration

- Added support for multiple servers in one bot.
- Added `添加服务器`, `删除服务器`, `修改服务器名字`, `修改地址`, and `更新token`.
- Moved server configuration from `.env` to SQLite.
- Added the `servers` table.

### v1 — one server and time-card control

- Added `开服` and `关服` to control the time card through `api.minekuai.com`.
- Starting the actual instance still required an administrator to use the Minekuai website.
- Stored all configuration in `.env`.

---

## Contributing

Issues and pull requests are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) before making changes. Report security issues privately according to [SECURITY.md](SECURITY.md), and never paste credentials into a public issue.

---

## License

[MIT](LICENSE)

---

## Credits

- This project calls Minekuai web endpoints and the Pterodactyl Client API. These interfaces may change; fixes are welcome when they do.
- Original author: [@klukeyuan](mailto:klukeyuan@gmail.com)
