"""
麦块联机 API 客户端

主要走 MinekuaiClient（api.minekuai.com，Bearer 认证）：
- start_timing / stop_timing：开关计时卡（控制扣费）
- verify_eula：触发实例启动（同一套 Bearer 认证，不需要 cookies）

PanelClient（minekuai.com/api/client/...，cookies + XSRF）保留作为备选——
万一某个 endpoint 只在 Pterodactyl 路径上能用，可以走这边。

设计原则:
- 所有 HTTP 调用都封装在这里，业务逻辑不直接碰 httpx
- 失败抛 MinekuaiError，调用方根据异常类型给出友好提示
- 异步实现，配合 nonebot2 的事件循环
"""

import asyncio

from typing import Any
import httpx
from loguru import logger


class MinekuaiError(Exception):
    """麦块联机 API 异常基类"""


class AuthError(MinekuaiError):
    """认证失败 - token 过期或无效"""


class APIError(MinekuaiError):
    """业务接口返回非成功状态"""


class RateLimitError(MinekuaiError):
    """请求被麦块联机后端限流。
    通常意味着同一个计时卡刚做过开/关操作，新请求被拒绝。
    实际状态大概率已经是请求想要的状态。"""


class MinekuaiClient:
    """麦块联机计时卡 API 客户端（异步）"""

    BASE_URL = "https://api.minekuai.com"
    DEFAULT_TIMEOUT = 15.0

    def __init__(self, token: str, client_id: str):
        if not token or not client_id:
            raise ValueError("token 和 client_id 不能为空")

        self._token = token
        self._client_id = client_id
        self._http: httpx.AsyncClient | None = None

    async def __aenter__(self):
        self._http = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers=self._build_headers(),
            timeout=self.DEFAULT_TIMEOUT,
        )
        return self

    async def __aexit__(self, *exc_info):
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def _build_headers(self) -> dict[str, str]:
        """构造和浏览器一致的请求头，避免被风控"""
        return {
            "Authorization": f"Bearer {self._token}",
            "clientid": self._client_id,
            "Origin": "https://minekuai.com",
            "Referer": "https://minekuai.com/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/147.0.0.0 Safari/537.36 Edg/147.0.0.0"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Content-Language": "zh_CN",
        }

    async def _request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> dict:
        if self._http is None:
            raise RuntimeError("Client 未初始化，请用 async with 进入上下文")

        logger.debug(f"→ {method} {path}")
        try:
            r = await self._http.request(method, path, **kwargs)
        except httpx.TimeoutException as e:
            raise APIError(f"请求超时: {path}") from e
        except httpx.HTTPError as e:
            raise APIError(f"网络错误: {e}") from e

        if r.status_code == 401:
            raise AuthError("token 已过期或无效，请更新配置中的 MINEKUAI_TOKEN")

        if r.status_code >= 400:
            raise APIError(f"HTTP {r.status_code}: {r.text[:200]}")

        try:
            data = r.json()
        except ValueError:
            return {"raw": r.text}

        if isinstance(data, dict) and "code" in data:
            code = data.get("code")
            if code not in (200, 0, "200", "0", None):
                msg = data.get("msg") or data.get("message") or "未知错误"
                # 业务码 401 也认作认证失败（HTTP 200 + body code=401，
                # 麦块联机 token 过期/冻结时是这个形式）
                if code in (401, "401"):
                    raise AuthError(f"token 已过期或被冻结，请更新 MINEKUAI_TOKEN: {msg}")
                # 500 + "操作太频繁" = 限流，通常意味着前一次操作刚完成
                if code in (500, "500") and ("频繁" in msg or "稍后" in msg):
                    raise RateLimitError(msg)
                raise APIError(f"接口业务失败 [{code}]: {msg}")

        logger.debug(f"← {r.status_code} {path}")
        return data

    # ============================================================
    # 计时卡接口
    # ============================================================

    async def start_timing(self, card_id: str) -> dict:
        """打开计时卡（开始计时扣费）"""
        return await self._request(
            "POST", f"/system/timeBalance/user/startTiming/{card_id}"
        )

    async def stop_timing(self, card_id: str) -> dict:
        """关闭计时卡（停止计时）"""
        return await self._request(
            "POST", f"/system/timeBalance/user/stopTiming/{card_id}"
        )

    # ============================================================
    # 组合接口（业务流程）
    # ============================================================

    async def open_timing_only(self, card_id: str) -> None:
        """只开计时卡。实例启动由 PanelClient.start_instance 负责。"""
        logger.info(f"[开服] 打开计时卡 {card_id}")
        try:
            await self.start_timing(card_id)
        except APIError as e:
            raise APIError(f"打开计时卡失败: {e}") from e
        logger.info("[开服] 计时卡已开启")

    # 向后兼容别名
    open_server = open_timing_only

    async def close_server(self, card_id: str) -> None:
        """关服流程：关闭计时卡（关闭计时卡后实例自动停止）"""
        logger.info(f"[关服] 关闭计时卡 {card_id}")
        await self.stop_timing(card_id)
        logger.info("[关服] 流程完成")


# ============================================================
# Pterodactyl 面板客户端 - 控制服务器实例启停
# ============================================================

class PanelClient:
    """麦块联机 Pterodactyl 面板 API 客户端（异步）

    通过 4 个 GET 预检查触发服务器实例启动。认证用 Laravel session cookies +
    X-XSRF-TOKEN，需要由调用方先经 auth.refresh_token 拿到。
    """

    BASE_URL = "https://minekuai.com"
    DEFAULT_TIMEOUT = 30.0   # 面板调用比计时卡慢，容差大一些

    def __init__(self, session_cookie: str, xsrf_token: str):
        if not session_cookie or not xsrf_token:
            raise ValueError("session_cookie 和 xsrf_token 不能为空")
        self._cookie = session_cookie
        self._xsrf = xsrf_token
        self._http: httpx.AsyncClient | None = None

    async def __aenter__(self):
        self._http = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers=self._build_headers(),
            timeout=self.DEFAULT_TIMEOUT,
        )
        return self

    async def __aexit__(self, *exc_info):
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def _build_headers(self) -> dict[str, str]:
        return {
            "Cookie": self._cookie,
            "X-XSRF-TOKEN": self._xsrf,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Origin": "https://minekuai.com",
            "Referer": "https://minekuai.com/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0"
            ),
            "X-Requested-With": "XMLHttpRequest",
        }

    async def _request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> dict:
        if self._http is None:
            raise RuntimeError("PanelClient 未初始化，请用 async with 进入上下文")

        logger.debug(f"→ [panel] {method} {path}")
        try:
            r = await self._http.request(method, path, **kwargs)
        except httpx.TimeoutException as e:
            raise APIError(f"面板请求超时: {path}") from e
        except httpx.HTTPError as e:
            raise APIError(f"面板网络错误: {e}") from e

        # 401: session 失效；419: CSRF 过期（Laravel 风格）
        if r.status_code in (401, 419):
            raise AuthError(
                f"面板 session/CSRF 失效 (HTTP {r.status_code})，"
                f"需要重新登录刷新 cookies"
            )

        if r.status_code >= 400:
            raise APIError(f"面板 HTTP {r.status_code}: {r.text[:200]}")

        try:
            data = r.json()
        except ValueError:
            return {"raw": r.text}

        if isinstance(data, dict) and "code" in data:
            code = data.get("code")
            if code not in (200, 0, "200", "0", None):
                msg = data.get("msg") or data.get("message") or "未知错误"
                if code in (401, 419, "401", "419"):
                    raise AuthError(f"面板业务码 {code}: {msg}")
                if code in (500, "500") and ("频繁" in msg or "稍后" in msg):
                    raise RateLimitError(msg)
                raise APIError(f"面板业务失败 [{code}]: {msg}")

        logger.debug(f"← [panel] {r.status_code} {path}")
        return data

    # ------------------------------------------------------------
    # 启动/停止/重启实例 - Pterodactyl 标准 power endpoint
    # ------------------------------------------------------------

    async def power(self, instance_id: str, signal: str) -> dict:
        """发送电源信号给实例。signal: start / stop / restart / kill。

        实测响应是 HTTP 204 No Content（成功），失败返回 4xx + JSON 错误。
        instance_id 短 ID（如 420d4426）和完整 UUID 都接受。
        """
        return await self._request(
            "POST",
            f"/api/client/servers/{instance_id}/power",
            json={"signal": signal},
        )

    async def start_instance(self, instance_id: str) -> None:
        """启动服务器实例。"""
        logger.info(f"[panel] POST power signal=start for {instance_id}")
        try:
            await self.power(instance_id, "start")
        except (AuthError, RateLimitError):
            raise
        except APIError as e:
            raise APIError(f"实例启动指令下达失败: {e}") from e
        logger.info("[panel] start 信号已发，服务器应该正在启动")

    async def stop_instance(self, instance_id: str) -> None:
        """停止服务器实例（备用，目前 bot 不主动调用）。"""
        await self.power(instance_id, "stop")

    async def send_command(self, instance_id: str, command: str) -> None:
        """发指令到服务器控制台（Pterodactyl 标准端点）。

        命令前面不加 /——跟 minekuai 面板里的指令框一致。
        服务器执行后返回 204 No Content；命令的输出不在 HTTP 响应里，
        要看的话得订阅 WebSocket 控制台（暂不实现）。
        """
        cmd = command.lstrip("/").strip()
        logger.info(f"[panel] POST command to {instance_id}: {cmd[:60]}")
        await self._request(
            "POST", f"/api/client/servers/{instance_id}/command",
            json={"command": cmd},
        )

    # ------------------------------------------------------------
    # 只读查询：基本信息、实时资源、目录列表（用于 "查服" / "模组"）
    # ------------------------------------------------------------

    async def get_server_info(self, instance_id: str) -> dict:
        """实例基本信息：名字、端口分配、CPU/内存/磁盘配额等。"""
        return await self._request(
            "GET", f"/api/client/servers/{instance_id}"
        )

    async def get_resources(self, instance_id: str) -> dict:
        """实例当前状态 + 实时资源占用。

        attributes.current_state ∈ {running, offline, starting, stopping}
        """
        return await self._request(
            "GET", f"/api/client/servers/{instance_id}/resources"
        )

    async def list_directory(
        self, instance_id: str, directory: str = "/",
    ) -> list[dict]:
        """列目录。返回每个文件/目录的 attributes（含 name/is_file/size）。
        目录不存在时面板返回 404 → APIError。
        """
        data = await self._request(
            "GET", f"/api/client/servers/{instance_id}/files/list",
            params={"directory": directory},
        )
        return [item.get("attributes", {}) for item in data.get("data", [])]

    async def get_ws_credentials(self, instance_id: str) -> dict:
        """拿 WebSocket 控制台的 socket URL + 短期 token(用于实时日志流)。
        返回 {"socket": "wss://...", "token": "JWT"}。
        """
        d = await self._request(
            "GET", f"/api/client/servers/{instance_id}/websocket"
        )
        return d.get("data", {}) if isinstance(d, dict) else {}

    async def read_file_text(self, instance_id: str, file_path: str) -> str:
        """读取实例内文件的原始文本内容（用于看日志、读 server.properties 等）。
        Pterodactyl 把整文件一次性返回,大文件调用方自己截尾。
        """
        if self._http is None:
            raise RuntimeError("PanelClient 未初始化,请用 async with 进入上下文")
        logger.debug(f"→ [panel] GET file {file_path} ({instance_id})")
        try:
            r = await self._http.get(
                f"/api/client/servers/{instance_id}/files/contents",
                params={"file": file_path},
            )
        except httpx.TimeoutException as e:
            raise APIError(f"读取文件超时: {file_path}") from e
        except httpx.HTTPError as e:
            raise APIError(f"网络错误: {e}") from e
        if r.status_code in (401, 419):
            raise AuthError(
                f"面板 session/CSRF 失效 (HTTP {r.status_code})"
            )
        if r.status_code >= 400:
            raise APIError(f"面板 HTTP {r.status_code}: {r.text[:200]}")
        return r.text
