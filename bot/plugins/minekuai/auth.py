"""自动登录刷 token

用 Playwright 跑无头 Chromium 在 minekuai.com 上模拟人工登录，登录
完成后从前端的 localStorage（或 cookies）取出新 token + clientid。

我们没有反编译麦块联机的 RSA+AES 加密 JS bundle，所以直接用浏览器跑——
JS 自己会做加密，我们只负责填表 + 取结果。

适用场景：
- 用户在群里『添加账号』把手机号+密码存进 DB
- 用户『绑定账号 <服务器> <手机号>』把账号关联到某台服务器
- 之后服务器的 token 一旦失效，bot 自动调本模块用账号重新登录，无感续期

代价：
- 镜像里需要装 Chromium（+~300 MB）
- 每次登录 5-10 秒（浏览器冷启动）
- 麦块联机改登录页 UI 后选择器可能失效
"""
import asyncio
import base64
import json
from typing import Any

from loguru import logger


LOGIN_URL = "https://minekuai.com/login"
DEFAULT_TIMEOUT_MS = 30_000
# 实际登录响应路径——『账号登录』(密码) 和 『手机登录』(SMS) 走不同端点，
# minekuai 历史上在 pterodactylLogin / pterodactylSMSLogin 之间反复横跳——
# 同一个端点接受手机+密码 / 手机+验证码两种登录方式，路径偶尔改名。
# 这里同时匹配两种路径，免得他们再翻就废了。
LOGIN_API_PATHS = ("/auth/pterodactylLogin", "/auth/pterodactylSMSLogin")


class LoginError(Exception):
    """自动登录失败的统一异常"""


def _import_playwright():
    """懒加载 playwright——没装的话报清晰的 LoginError，而不是 bot 整个挂掉"""
    try:
        from playwright.async_api import (  # noqa: F401
            BrowserContext,
            Page,
            TimeoutError as PWTimeoutError,
            async_playwright,
        )
    except ImportError as e:
        raise LoginError(
            "playwright 未安装，无法自动登录。"
            "请确认 Dockerfile 里有 `pip install playwright` 和 "
            "`playwright install chromium`，并重建镜像。"
        ) from e
    import playwright.async_api as _pw
    return _pw


async def refresh_token(
    phone: str,
    password: str,
    *,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> tuple[str, str, str, str]:
    """用账号密码自动登录 minekuai.com。

    返回 4 元组：(token, client_id, session_cookie, xsrf_token)
      - token / client_id: 调 api.minekuai.com 计时卡接口用（JWT Bearer 认证）
      - session_cookie / xsrf_token: 调 minekuai.com/api/client/... 面板接口用
        （Laravel session + CSRF 认证，用来开关服务器实例）

    失败抛 LoginError，调用方根据消息提示用户。
    """
    logger.info(f"[auth] 启动 Chromium 登录账号 {_mask_phone(phone)}")
    pw_mod = _import_playwright()

    async with pw_mod.async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        try:
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/148.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
            )
            return await _do_login(context, phone, password, timeout_ms)
        finally:
            await browser.close()


# ============================================================
# 内部实现
# ============================================================

async def _do_login(
    context: "BrowserContext",
    phone: str,
    password: str,
    timeout_ms: int,
) -> tuple[str, str, str, str]:
    page = await context.new_page()

    # 用 asyncio.Event 做请求-响应同步
    login_response: dict[str, Any] = {}
    response_ready = asyncio.Event()

    async def on_response(resp) -> None:
        if not any(p in resp.url for p in LOGIN_API_PATHS):
            return
        try:
            body = await resp.json()
        except Exception as e:
            logger.warning(f"[auth] 登录响应不是 JSON: {e}")
            return
        login_response["status"] = resp.status
        login_response["body"] = body
        response_ready.set()

    page.on("response", on_response)

    # 打开登录页
    pw_mod = _import_playwright()
    try:
        await page.goto(LOGIN_URL, timeout=timeout_ms, wait_until="domcontentloaded")
    except pw_mod.TimeoutError as e:
        raise LoginError(f"打不开登录页（超时）: {e}") from e

    # 给前端一点时间渲染（vue/react 异步挂载）
    await asyncio.sleep(1.0)

    # 切到密码登录（如果当前是验证码登录 tab）
    await _switch_to_password_tab(page)

    # 填账号
    await _fill_phone(page, phone)

    # 填密码
    await _fill_password(page, password)

    # 点登录按钮
    await _click_login_button(page)

    # 等响应（最多 timeout_ms 毫秒）
    try:
        await asyncio.wait_for(response_ready.wait(), timeout=timeout_ms / 1000)
    except asyncio.TimeoutError as e:
        raise LoginError(
            "等待登录响应超时——可能：网络慢、登录按钮没点到、minekuai 改了 UI"
        ) from e

    # 解析响应
    status = login_response.get("status")
    body = login_response.get("body") or {}

    if status != 200:
        raise LoginError(f"登录 HTTP {status}: {body}")

    code = body.get("code")
    if code not in (200, 0, "200", "0", None):
        msg = body.get("msg") or body.get("message") or "未知错误"
        raise LoginError(f"登录业务码失败 [{code}]: {msg}")

    # 找 token + clientid（在 data 字段里，常见的几种命名都试一下）
    data = body.get("data") or body
    token = (
        data.get("access_token")
        or data.get("token")
        or data.get("tokenValue")
        or data.get("accessToken")
    )
    client_id = (
        data.get("clientid")
        or data.get("clientId")
        or data.get("client_id")
    )

    if not token:
        # 兜底：尝试从 localStorage 拿
        try:
            token = await page.evaluate(
                "() => localStorage.getItem('Admin-Token') "
                "|| localStorage.getItem('token') "
                "|| localStorage.getItem('access_token')"
            )
        except Exception:
            pass

    if not token:
        raise LoginError(
            f"登录成功但响应里没找到 token 字段: {json.dumps(data)[:300]}"
        )

    # clientid 没有单独字段——它嵌在 JWT payload 里
    # 服务端 校验时 header 的 clientid 必须跟 JWT 里的 clientid 一致
    if not client_id:
        client_id = _extract_clientid_from_jwt(token) or ""

    # Pterodactyl 面板还需要 session cookies + XSRF——
    # 等几秒让前端跑完登录后的初始化（写 pterodactyl_session 等 cookie）
    await asyncio.sleep(2)
    session_cookie, xsrf_token = await _extract_panel_auth(context)

    logger.info(f"[auth] 账号 {_mask_phone(phone)} 登录成功")
    return token, client_id, session_cookie, xsrf_token


async def _extract_panel_auth(context) -> tuple[str, str]:
    """从浏览器 context 里取出 Pterodactyl 面板需要的认证材料。

    返回 (cookie_header, xsrf_token)，两者都能为空字符串（如果某些 cookie 缺失）。
      cookie_header 形如 "key1=value1; key2=value2; ..."（直接给 httpx.headers 用）
      xsrf_token 是 X-XSRF-TOKEN 头的值（来自 XSRF-TOKEN cookie 但 URL 解码后）
    """
    from urllib.parse import unquote

    cookies = await context.cookies()
    # 只保留 minekuai.com 域下的 cookies
    relevant = [
        c for c in cookies
        if c.get("domain", "").endswith("minekuai.com")
    ]
    cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in relevant)

    xsrf_raw = ""
    for c in relevant:
        if c["name"] == "XSRF-TOKEN":
            # cookie 值是 URL 编码的；X-XSRF-TOKEN header 期望解码后的
            xsrf_raw = unquote(c["value"])
            break

    if not cookie_header:
        logger.warning("[auth] 没拿到任何 minekuai cookie，面板 API 调用会失败")
    if not xsrf_raw:
        logger.warning("[auth] 没找到 XSRF-TOKEN cookie，面板 API 调用会失败")
    return cookie_header, xsrf_raw


def _extract_clientid_from_jwt(token: str) -> str | None:
    """从 JWT 的 payload 里抽 clientid 字段。

    JWT 格式: header.payload.signature，三段都是 base64url 编码。
    我们只 decode payload（中间那段），找 clientid。
    """
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload_b64 = parts[1]
        # base64url 需要补齐 padding 才能 b64decode
        pad = "=" * (-len(payload_b64) % 4)
        payload_bytes = base64.urlsafe_b64decode(payload_b64 + pad)
        payload = json.loads(payload_bytes)
        cid = payload.get("clientid") or payload.get("clientId")
        if cid:
            logger.debug(f"[auth] 从 JWT payload 抽到 clientid: {cid}")
        return cid
    except Exception as e:
        logger.warning(f"[auth] 解 JWT payload 失败: {e}")
        return None


async def _switch_to_password_tab(page) -> None:
    """确认在『手机登录』tab——新版默认就是手机+密码，一般不用切。

    如果碰到旧版默认是别的 tab，再尝试切回来。
    """
    # 默认 tab 就有 phone+password input，不切也行
    # 仅当 phone input 不可见时尝试切 tab
    try:
        phone_visible = await page.locator("input[name='phone']").first.is_visible(
            timeout=500
        )
        if phone_visible:
            return
    except Exception:
        pass
    candidates = ["手机登录", "密码登录", "账号登录"]
    for label in candidates:
        try:
            await page.get_by_text(label, exact=True).first.click(timeout=2_000)
            logger.debug(f"[auth] 切到 tab: {label}")
            await asyncio.sleep(0.3)
            return
        except Exception:
            continue
    logger.debug("[auth] 没切 tab，按默认状态继续")


async def _fill_phone(page, phone: str) -> None:
    """填手机号——尝试几种常见的 input 选择策略

    当前 minekuai (v3 UI) 的选择器：
      input[name='phone']  / #mkl-phone  / placeholder='请输入 11 位手机号码'
    保留旧的 fallback 以防换版本。
    """
    last_error: Exception | None = None
    strategies = [
        # 新版优先
        lambda: page.locator("input[name='phone']").first,
        lambda: page.locator("#mkl-phone").first,
        lambda: page.get_by_placeholder("请输入 11 位手机号", exact=False).first,
        # 旧版/通用 fallback
        lambda: page.get_by_placeholder("请输入手机号码", exact=False).first,
        lambda: page.get_by_placeholder("请输入手机号", exact=False).first,
        lambda: page.get_by_placeholder("手机号", exact=False).first,
        lambda: page.get_by_placeholder("账号", exact=False).first,
        lambda: page.locator("input[autocomplete='tel']").first,
        lambda: page.locator("input[type='tel']").first,
        lambda: page.locator("input[name='mobile']").first,
        lambda: page.locator("input[name='username']").first,
    ]
    for build in strategies:
        try:
            loc = build()
            await loc.fill(phone, timeout=3_000)
            return
        except Exception as e:
            last_error = e
            continue
    raise LoginError(f"找不到手机号输入框: {last_error}")


async def _fill_password(page, password: str) -> None:
    last_error: Exception | None = None
    strategies = [
        lambda: page.locator("input[name='password']").first,
        lambda: page.locator("#mkl-phone-password").first,
        lambda: page.get_by_placeholder("请输入密码", exact=False).first,
        lambda: page.get_by_placeholder("密码", exact=False).first,
        lambda: page.locator("input[type='password']").first,
        lambda: page.locator("input[autocomplete='current-password']").first,
    ]
    for build in strategies:
        try:
            loc = build()
            await loc.fill(password, timeout=3_000)
            return
        except Exception as e:
            last_error = e
            continue
    raise LoginError(f"找不到密码输入框: {last_error}")


async def _click_login_button(page) -> None:
    """点登录提交按钮。

    注意：『账号登录』/『手机登录』是 tab 按钮（含『登录』二字），不能用模糊匹配，
    否则会反复点 tab 而非真的提交表单。优先用 class 或精确文本匹配。
    """
    last_error: Exception | None = None
    strategies = [
        # 新版：button.mkl-submit 文本『立即登录』
        lambda: page.locator("button.mkl-submit").first,
        lambda: page.get_by_role("button", name="立即登录", exact=True).first,
        # 旧版 fallback
        lambda: page.locator("button[type='submit']").first,
        lambda: page.get_by_role("button", name="登录", exact=True).first,
        lambda: page.get_by_role("button", name="登 录", exact=True).first,
        lambda: page.locator(".login-btn").first,
        lambda: page.locator(".submit-btn").first,
    ]
    for build in strategies:
        try:
            loc = build()
            await loc.click(timeout=3_000)
            return
        except Exception as e:
            last_error = e
            continue
    raise LoginError(f"找不到登录按钮: {last_error}")


def _mask_phone(phone: str) -> str:
    """139****8110 这种打码格式，给日志用"""
    if len(phone) < 7:
        return "***"
    return phone[:3] + "****" + phone[-4:]
