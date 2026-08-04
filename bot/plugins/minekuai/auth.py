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
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal

from loguru import logger


LOGIN_URL = "https://minekuai.com/login"
DEFAULT_TIMEOUT_MS = 30_000
# 实际登录响应路径——『账号登录』(密码) 和 『手机登录』(SMS) 走不同端点，
# minekuai 历史上在 pterodactylLogin / pterodactylSMSLogin 之间反复横跳——
# 同一个端点接受手机+密码 / 手机+验证码两种登录方式，路径偶尔改名。
# 这里同时匹配两种路径，免得他们再翻就废了。
LOGIN_API_PATHS = ("/auth/pterodactylLogin", "/auth/pterodactylSMSLogin")
SMS_CODE_API_PATH = "/resource/sms/code"
MAX_IMAGE_CODE_ATTEMPTS = 3


class LoginError(Exception):
    """自动登录失败的统一异常"""


@dataclass(frozen=True)
class LoginChallenge:
    """需要由发起登录的用户回答的一次性验证问题。"""

    kind: Literal["image", "sms"]
    image_base64: str = ""


VerificationProvider = Callable[[LoginChallenge], Awaitable[str]]


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
    verification_provider: VerificationProvider | None = None,
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
            return await _do_login(
                context,
                phone,
                password,
                timeout_ms,
                verification_provider=verification_provider,
            )
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
    *,
    verification_provider: VerificationProvider | None = None,
) -> tuple[str, str, str, str]:
    page = await context.new_page()

    login_responses: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    sms_code_responses: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def on_response(resp) -> None:
        target = None
        if any(path in resp.url for path in LOGIN_API_PATHS):
            target = login_responses
        elif SMS_CODE_API_PATH in resp.url:
            target = sms_code_responses
        if target is None:
            return
        try:
            body = await resp.json()
        except Exception as e:
            logger.warning(f"[auth] 验证响应不是 JSON: {e}")
            return
        target.put_nowait({"status": resp.status, "body": body})

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

    login_response = await _wait_api_response(
        login_responses, timeout_ms, "等待登录响应"
    )
    status = login_response.get("status")
    body = login_response.get("body") or {}

    if status != 200:
        raise LoginError(f"登录 HTTP {status}")

    code = body.get("code")
    if code not in (200, 0, "200", "0", None):
        msg = body.get("msg") or body.get("message") or "未知错误"
        if _is_sms_required(code, msg):
            if verification_provider is None:
                raise LoginError(
                    "登录需要手机号验证码；请从群里的交互指令重新触发登录"
                )
            body = await _complete_sms_login(
                page,
                phone,
                verification_provider,
                login_responses,
                sms_code_responses,
                timeout_ms,
            )
        else:
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


async def _wait_api_response(
    queue: asyncio.Queue[dict[str, Any]],
    timeout_ms: int,
    action: str,
) -> dict[str, Any]:
    try:
        return await asyncio.wait_for(queue.get(), timeout=timeout_ms / 1000)
    except asyncio.TimeoutError as e:
        raise LoginError(
            f"{action}超时——可能是网络慢或 minekuai 改了登录页"
        ) from e


def _is_sms_required(code: Any, message: str) -> bool:
    """识别密码登录触发的异地/风控短信验证提示。"""
    text = f"{code} {message}".lower()
    markers = (
        "非常用地点",
        "手机号验证码登录",
        "手机验证码登录",
        "短信验证码登录",
        "使用验证码登录",
    )
    return any(marker in text for marker in markers)


def _business_succeeded(response: dict[str, Any]) -> bool:
    if response.get("status") != 200:
        return False
    body = response.get("body") or {}
    return body.get("code") in (200, 0, "200", "0", None)


def _response_message(response: dict[str, Any], fallback: str) -> str:
    body = response.get("body") or {}
    return str(body.get("msg") or body.get("message") or fallback)


async def _complete_sms_login(
    page,
    phone: str,
    verification_provider: VerificationProvider,
    login_responses: asyncio.Queue[dict[str, Any]],
    sms_code_responses: asyncio.Queue[dict[str, Any]],
    timeout_ms: int,
) -> dict[str, Any]:
    """切换到验证码登录，完成人机计算题、发短信并提交短信码。"""
    logger.info(f"[auth] 账号 {_mask_phone(phone)} 需要交互式短信验证")
    await _switch_to_sms_tab(page)
    await _fill_sms_phone(page, phone)
    await _click_send_sms_button(page)

    captcha_image = page.locator(".mkl-captcha-img img").first
    try:
        await captcha_image.wait_for(state="visible", timeout=timeout_ms)
    except Exception as e:
        raise LoginError("短信验证已触发，但没有显示图片计算题") from e

    sms_sent = False
    for attempt in range(1, MAX_IMAGE_CODE_ATTEMPTS + 1):
        image_src = await captcha_image.get_attribute("src") or ""
        image_base64 = _extract_image_base64(image_src)
        if not image_base64:
            raise LoginError("读取图片验证码失败")

        answer = (
            await verification_provider(
                LoginChallenge(kind="image", image_base64=image_base64)
            )
        ).strip()
        if not re.fullmatch(r"-?\d{1,6}", answer):
            raise LoginError("图片验证码答案格式不正确")

        await _fill_image_code(page, answer)
        await _click_confirm_send_sms(page)
        send_response = await _wait_api_response(
            sms_code_responses, timeout_ms, "发送短信验证码"
        )
        if _business_succeeded(send_response):
            sms_sent = True
            break

        message = _response_message(send_response, "发送失败")
        if attempt >= MAX_IMAGE_CODE_ATTEMPTS or not _is_image_code_error(message):
            raise LoginError(f"发送短信验证码失败：{message}")

        logger.info(
            f"[auth] 账号 {_mask_phone(phone)} 图片验证码错误，等待重新输入"
        )
        try:
            await page.wait_for_function(
                "oldSrc => document.querySelector('.mkl-captcha-img img')?.src !== oldSrc",
                image_src,
                timeout=timeout_ms,
            )
        except Exception:
            await captcha_image.click(timeout=3_000)
            await asyncio.sleep(0.5)

    if not sms_sent:
        raise LoginError("发送短信验证码失败")

    sms_code = (
        await verification_provider(LoginChallenge(kind="sms"))
    ).strip()
    if not re.fullmatch(r"\d{6}", sms_code):
        raise LoginError("短信验证码必须是 6 位数字")

    await _fill_sms_code(page, sms_code)
    await _click_login_button(page)
    login_response = await _wait_api_response(
        login_responses, timeout_ms, "等待短信登录响应"
    )
    if login_response.get("status") != 200:
        raise LoginError(f"短信登录 HTTP {login_response.get('status')}")
    if not _business_succeeded(login_response):
        body = login_response.get("body") or {}
        code = body.get("code")
        message = _response_message(login_response, "未知错误")
        raise LoginError(f"短信登录业务码失败 [{code}]: {message}")
    return login_response.get("body") or {}


def _extract_image_base64(src: str) -> str:
    if not src:
        return ""
    if src.startswith("data:") and "," in src:
        return src.split(",", 1)[1]
    return src


def _is_image_code_error(message: str) -> bool:
    return any(
        marker in message
        for marker in ("图形验证码", "图片验证码", "计算结果", "验证码错误")
    )


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
    # localStorage 会记住上次的登录方式，必须确认密码框而不只是手机号框可见。
    try:
        password_visible = await page.locator(
            "#mkl-phone-password, input[name='password']"
        ).first.is_visible(
            timeout=500
        )
        if password_visible:
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


async def _switch_to_sms_tab(page) -> None:
    strategies = [
        lambda: page.get_by_role("tab", name="验证码", exact=True).first,
        lambda: page.get_by_text("验证码", exact=True).first,
        lambda: page.get_by_text("短信登录", exact=True).first,
        lambda: page.get_by_text("验证码登录", exact=True).first,
    ]
    last_error: Exception | None = None
    for build in strategies:
        try:
            await build().click(timeout=3_000)
            await page.locator("#mkl-sms-phone").wait_for(
                state="visible", timeout=3_000
            )
            return
        except Exception as e:
            last_error = e
    raise LoginError(f"找不到验证码登录入口: {last_error}")


async def _fill_sms_phone(page, phone: str) -> None:
    try:
        await page.locator("#mkl-sms-phone").fill(phone, timeout=3_000)
    except Exception as e:
        raise LoginError(f"找不到短信登录手机号输入框: {e}") from e


async def _click_send_sms_button(page) -> None:
    strategies = [
        lambda: page.locator("button.mkl-sms-btn").first,
        lambda: page.get_by_role("button", name="获取验证码", exact=True).first,
    ]
    last_error: Exception | None = None
    for build in strategies:
        try:
            await build().click(timeout=3_000)
            return
        except Exception as e:
            last_error = e
    raise LoginError(f"找不到获取验证码按钮: {last_error}")


async def _fill_image_code(page, answer: str) -> None:
    strategies = [
        lambda: page.locator(".mkl-captcha-inline input[type='text']").first,
        lambda: page.get_by_placeholder("计算结果", exact=False).first,
    ]
    last_error: Exception | None = None
    for build in strategies:
        try:
            await build().fill(answer, timeout=3_000)
            return
        except Exception as e:
            last_error = e
    raise LoginError(f"找不到图片验证码输入框: {last_error}")


async def _click_confirm_send_sms(page) -> None:
    strategies = [
        lambda: page.locator("button.mkl-captcha-send").first,
        lambda: page.get_by_role("button", name="发送验证码", exact=True).first,
    ]
    last_error: Exception | None = None
    for build in strategies:
        try:
            await build().click(timeout=3_000)
            return
        except Exception as e:
            last_error = e
    raise LoginError(f"找不到发送验证码按钮: {last_error}")


async def _fill_sms_code(page, code: str) -> None:
    try:
        await page.locator("#mkl-sms-code").fill(code, timeout=3_000)
    except Exception as e:
        raise LoginError(f"找不到短信验证码输入框: {e}") from e


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
