"""
麦块联机 API 客户端

主要走 MinekuaiClient（api.minekuai.cn，Bearer 认证）：
- start_timing / stop_timing：开关计时卡（控制扣费）
- get_user_packages：只读查询当前账号的计时卡套餐

PanelClient 优先使用新版 api.minekuai.cn/panel/... 的 JWT + clientid；
旧版 Client API Key、cookies + XSRF 认证保留为显式兼容回退。

设计原则:
- 所有 HTTP 调用都封装在这里，业务逻辑不直接碰 httpx
- 失败抛 MinekuaiError，调用方根据异常类型给出友好提示
- 异步实现，配合 nonebot2 的事件循环
"""

import asyncio
import json
import re
import unicodedata

from typing import Any
from urllib.parse import quote, quote_plus, unquote
import httpx
from loguru import logger

try:
    from .panel_power import send_power, PreSendAuthError, PowerTransportError
except ImportError:
    from panel_power import send_power, PreSendAuthError, PowerTransportError


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


_JWT_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\."
    r"[A-Za-z0-9_-]*(?![A-Za-z0-9_-])"
)


def _safe_error_text(value: Any, *secrets: str) -> str:
    """Keep useful API errors without forwarding credentials echoed by a backend."""
    text = _JWT_PATTERN.sub("[REDACTED]", str(value))
    variants = set()
    for secret in secrets:
        if secret:
            decoded = unquote(secret)
            variants.update((
                secret, decoded, quote(secret, safe=""), quote_plus(secret),
                json.dumps(secret)[1:-1], json.dumps(secret, ensure_ascii=False)[1:-1],
            ))
    for secret in sorted(variants, key=len, reverse=True):
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


def _timeout_message(error: httpx.TimeoutException, service: str) -> str:
    if isinstance(error, httpx.ConnectTimeout):
        return f"无法连接{service}（连接超时），请检查机器人服务器网络或接口可用性"
    if isinstance(error, httpx.PoolTimeout):
        return f"{service}连接繁忙，请稍后重试"
    return (
        f"等待{service}响应超时，操作结果尚未确认；"
        "请先在网页确认状态，勿连续重复开关"
    )


def _json_response(response: httpx.Response, service: str, *, allow_empty: bool = False) -> dict:
    if allow_empty and response.status_code == 204 and not response.content:
        return {}
    try:
        data = response.json()
    except ValueError:
        raise APIError(
            f"{service}返回了网页或无效响应，可能遇到网站安全验证；未确认操作成功"
        ) from None
    if not isinstance(data, dict):
        raise APIError(f"{service}响应格式异常；未确认操作成功")
    return data


class MinekuaiClient:
    """麦块联机计时卡 API 客户端（异步）"""

    BASE_URL = "https://api.minekuai.cn"
    DEFAULT_TIMEOUT = 15.0
    MODPACK_MAX_PAGE = 1000
    MODPACK_MAX_PAGE_SIZE = 50

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
            raise APIError(_timeout_message(e, "计时卡 API（api.minekuai.cn）")) from None
        except httpx.HTTPError as e:
            raise APIError(f"网络错误: {_safe_error_text(e, self._token)}") from None

        if r.status_code in (401, 419):
            raise AuthError("token 已过期或无效，请更新配置中的 MINEKUAI_TOKEN")

        if r.status_code >= 400:
            detail = _safe_error_text(r.text, self._token)[:200]
            raise APIError(f"HTTP {r.status_code}: {detail}")

        data = _json_response(r, "计时卡 API")

        if isinstance(data, dict) and "code" in data:
            code = data.get("code")
            if code not in (200, 0, "200", "0", None):
                msg = _safe_error_text(
                    data.get("msg") or data.get("message") or "未知错误", self._token,
                )
                # 业务码 401 也认作认证失败（HTTP 200 + body code=401，
                # 麦块联机 token 过期/冻结时是这个形式）
                if code in (401, 419, "401", "419"):
                    raise AuthError("token 已过期或被冻结，请更新 MINEKUAI_TOKEN")
                # 500 + "操作太频繁" = 限流，通常意味着前一次操作刚完成
                if code in (500, "500") and ("频繁" in msg or "稍后" in msg):
                    raise RateLimitError(msg)
                safe_code = _safe_error_text(code, self._token)
                raise APIError(f"接口业务失败 [{safe_code}]: {msg}")

        logger.debug(f"← {r.status_code} {path}")
        return data

    # ============================================================
    # 计时卡接口
    # ============================================================

    async def get_user_packages(self) -> dict:
        """只读查询当前账号的计时卡套餐，不改变计时卡状态。"""
        return await self._request("GET", "/system/timeBalance/user/userPackages")

    @classmethod
    def _modpack_pagination(cls, page: int, page_size: int) -> None:
        if type(page) is not int or not 1 <= page <= cls.MODPACK_MAX_PAGE:
            raise ValueError("整合包页码必须是 1 到 1000 的整数")
        if type(page_size) is not int or not 1 <= page_size <= cls.MODPACK_MAX_PAGE_SIZE:
            raise ValueError("整合包每页数量必须是 1 到 50 的整数")

    @staticmethod
    def _modpack_text(value: Any, label: str, max_length: int) -> str:
        if (
            not isinstance(value, str) or not value.strip()
            or len(value) > max_length
            or any(unicodedata.category(char).startswith("C") for char in value)
        ):
            raise ValueError(f"{label}必须是有效的非空文本，最多 {max_length} 个字符")
        return value

    @classmethod
    def _modpack_id(cls, value: Any) -> str:
        if type(value) is int and value > 0:
            value = str(value)
        value = cls._modpack_text(value, "整合包 ID", 128)
        if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ValueError("整合包 ID 格式无效")
        return value

    async def search_modpacks(
        self, keyword: str, page: int = 1, page_size: int = 9,
    ) -> dict:
        """只读搜索整合包；返回根层 rows/total，不进行 data 解包。"""
        self._modpack_pagination(page, page_size)
        keyword = self._modpack_text(keyword, "搜索关键词", 100).strip()
        return await self._request("GET", "/system/modpacks/list", params={
            "name": keyword, "primaryId": "", "pageNum": page,
            "pageSize": page_size, "orderByColumn": "download_count", "isAsc": "desc",
        })

    async def list_modpack_versions(
        self, primary_id: str, page: int = 1, page_size: int = 9,
    ) -> dict:
        """只读列出指定整合包项目的版本，保持接口的根层 rows/total。"""
        self._modpack_pagination(page, page_size)
        primary_id = self._modpack_id(primary_id)
        return await self._request("GET", "/system/modpacks/list", params={
            "primaryId": primary_id, "pageNum": page, "pageSize": page_size,
            "orderByColumn": "createTime", "isAsc": "desc",
        })

    async def switch_modpack(
        self, instance_id: str, file_name: str, modpack_id: str,
    ) -> dict:
        """覆盖实例全部文件并更换整合包；调用方必须事先取得明确确认。

        只发送一次写请求，认证失败或超时均不在此处自动重试。
        file_name 必须来自选中的目录记录，保持原值，不改写下载参数。
        """
        if not isinstance(instance_id, str) or not re.fullmatch(r"[0-9a-fA-F]{8}", instance_id):
            raise ValueError("实例 ID 必须是 8 位短 identifier")
        file_name = self._modpack_text(file_name, "整合包文件名", 2048)
        modpack_id = self._modpack_id(modpack_id)
        result = await self._request(
            "POST", "/system/mineKuaiMinecraft/v2/switchModpack",
            json={"instanceId": instance_id, "fileName": file_name,
                  "id": modpack_id, "useExternalUrl": True},
        )
        if result.get("code") not in (200, "200", 0, "0"):
            raise APIError("更换整合包响应缺少成功业务码；结果未确认，请先在官网检查，勿重复安装")
        return result

    async def start_timing(self, card_id: str, *, instance_id: str = "") -> dict:
        """新版按实例开启计费；无实例 ID 时保留旧计时卡操作。"""
        path = (f"/system/timeBalance/user/instance/{instance_id}/start" if instance_id
                else f"/system/timeBalance/user/startTiming/{card_id}")
        return await self._request(
            "POST", path
        )

    async def stop_timing(self, card_id: str, *, instance_id: str = "") -> dict:
        """新版暂停指定实例计费；无实例 ID 时保留旧计时卡操作。"""
        path = (f"/system/timeBalance/user/instance/{instance_id}/stop" if instance_id
                else f"/system/timeBalance/user/stopTiming/{card_id}")
        return await self._request(
            "POST", path
        )

    # ============================================================
    # 组合接口（业务流程）
    # ============================================================

    async def open_timing_only(self, card_id: str, *, instance_id: str = "") -> None:
        """开启计费。实例电源状态由 PanelClient.start_instance 另行确认。"""
        logger.info(f"[开服] 打开计时卡 {card_id}")
        try:
            await self.start_timing(card_id, instance_id=instance_id)
        except APIError as e:
            raise APIError(f"打开计时卡失败: {e}") from e
        logger.info("[开服] 计时卡已开启")

    # 向后兼容别名
    open_server = open_timing_only

    async def close_server(self, card_id: str, *, instance_id: str = "") -> None:
        """关服流程：关闭计时卡（关闭计时卡后实例自动停止）"""
        logger.info(f"[关服] 关闭计时卡 {card_id}")
        await self.stop_timing(card_id, instance_id=instance_id)
        logger.info("[关服] 流程完成")


# ============================================================
# Pterodactyl 面板客户端 - 控制服务器实例启停
# ============================================================

class PanelClient:
    """麦块联机 Pterodactyl 面板 API 客户端（异步）

    新版网关与计时卡共用 JWT + clientid，并解包外层 code/data。
    未提供 JWT 时兼容旧版 Client API Key / Laravel session。
    """

    BASE_URL = "https://minekuai.com"
    GATEWAY_URL = "https://api.minekuai.cn"
    DEFAULT_TIMEOUT = 30.0   # 面板调用比计时卡慢，容差大一些

    def __init__(
        self,
        api_key: str = "",
        session_cookie: str = "",
        xsrf_token: str = "",
        *,
        token: str = "",
        client_id: str = "",
    ):
        if bool(token) != bool(client_id):
            raise ValueError("新版面板 token 和 client_id 必须同时配置")
        if not token and not api_key and not (session_cookie and xsrf_token):
            raise ValueError("请配置 token + client_id，或旧版面板 API 凭据")
        self._token = token
        self._client_id = client_id
        self.BASE_URL = self.GATEWAY_URL if token else type(self).BASE_URL
        self._api_key = api_key
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

    def _error_text(self, value: Any) -> str:
        cookie_values = (
            part.partition("=")[2].strip().strip('"')
            for part in self._cookie.split(";")
            if "=" in part
        )
        return _safe_error_text(
            value, self._token, self._api_key, self._cookie, self._xsrf, *cookie_values,
        )

    def _build_headers(self) -> dict[str, str]:
        headers = {
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
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
            headers["clientid"] = self._client_id
            headers["Content-Language"] = "zh_CN"
        elif self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        else:
            headers["Cookie"] = self._cookie
            headers["X-XSRF-TOKEN"] = self._xsrf
        return headers

    def _server_path(self, instance_id: str, suffix: str = "") -> str:
        prefix = "/panel" if self._token else "/api/client"
        return f"{prefix}/servers/{instance_id}{suffix}"

    def _auth_type(self) -> str:
        return "JWT" if self._token else ("API Key" if self._api_key else "session/CSRF")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        _allow_uncoded_gateway: bool = False,
        **kwargs: Any,
    ) -> dict:
        if self._http is None:
            raise RuntimeError("PanelClient 未初始化，请用 async with 进入上下文")

        logger.debug(f"→ [panel] {method} {path}")
        try:
            r = await self._http.request(method, path, **kwargs)
        except httpx.TimeoutException as e:
            raise APIError(_timeout_message(e, f"面板 API（{self.BASE_URL}）")) from None
        except httpx.HTTPError as e:
            raise APIError(f"面板网络错误: {self._error_text(e)}") from None

        if r.status_code in (401, 419):
            auth_type = self._auth_type()
            raise AuthError(
                f"面板 {auth_type} 认证失败 (HTTP {r.status_code})"
            )

        if r.status_code >= 400:
            raise APIError(f"面板 HTTP {r.status_code}: {self._error_text(r.text)[:200]}")

        data = _json_response(r, "面板 API", allow_empty=True)

        if isinstance(data, dict) and "code" in data:
            code = data.get("code")
            if code not in (200, 0, "200", "0", None):
                msg = self._error_text(data.get("msg") or data.get("message") or "未知错误")
                if code in (401, 419, "401", "419"):
                    raise AuthError(f"面板认证失败（业务码 {code}），请更新认证信息")
                if code in (500, "500") and ("频繁" in msg or "稍后" in msg):
                    raise RateLimitError(msg)
                raise APIError(f"面板业务失败 [{self._error_text(code)}]: {msg}")

        if self._token:
            if data.get("code") not in (200, "200", 0, "0") and not (
                _allow_uncoded_gateway and "code" not in data and "data" in data
            ):
                raise APIError("新版面板响应缺少成功业务码；未确认操作成功")
            payload = data.get("data")
            if payload is None:
                data = {}
            elif isinstance(payload, dict):
                data = payload
            else:
                raise APIError("新版面板响应数据格式异常；未确认操作成功")

        logger.debug(f"← [panel] {r.status_code} {path}")
        return data

    # ------------------------------------------------------------
    # 新版使用官网 WebSocket 电源协议，旧版保留 HTTP power endpoint
    # ------------------------------------------------------------

    async def power(self, instance_id: str, signal: str) -> dict:
        """发送电源信号给实例。signal: start / stop / restart / kill。

        新版：认证后发送一次 set state，等待兼容状态变化，超时不重发。
        旧版：HTTP 204 No Content。短 ID 和完整 UUID 都接受。
        """
        if self._token:
            credentials = await self.get_ws_credentials(instance_id)
            try:
                return await send_power(credentials["socket"], credentials["token"], signal)
            except PreSendAuthError as error:
                raise AuthError(str(error)) from None
            except PowerTransportError as error:
                raise APIError(str(error)) from None
        return await self._request(
            "POST",
            self._server_path(instance_id, "/power"),
            json={"signal": signal},
        )

    async def start_instance(self, instance_id: str) -> None:
        """启动服务器实例。"""
        logger.info(f"[panel] power signal=start for {instance_id}")
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
            "POST", self._server_path(instance_id, "/command"),
            json={"command": cmd},
        )

    # ------------------------------------------------------------
    # 只读查询：基本信息、实时资源、目录列表（用于 "查服" / "模组"）
    # ------------------------------------------------------------

    async def get_server_info(self, instance_id: str) -> dict:
        """实例基本信息：名字、端口分配、CPU/内存/磁盘配额等。"""
        return await self._request(
            "GET", self._server_path(instance_id)
        )

    async def get_resources(self, instance_id: str) -> dict:
        """实例当前状态 + 实时资源占用。

        attributes.current_state ∈ {running, offline, starting, stopping}
        """
        return await self._request(
            "GET", self._server_path(instance_id, "/resources")
        )

    async def list_directory(
        self, instance_id: str, directory: str = "/",
    ) -> list[dict]:
        """列目录。返回每个文件/目录的 attributes（含 name/is_file/size）。
        目录不存在时面板返回 404 → APIError。
        """
        data = await self._request(
            "GET", self._server_path(instance_id, "/files/list"),
            params={"directory": directory},
        )
        return [item.get("attributes", {}) for item in data.get("data", [])]

    async def get_ws_credentials(self, instance_id: str) -> dict:
        """拿 WebSocket 控制台的 socket URL + 短期 token(用于实时日志流)。
        返回 {"socket": "wss://...", "token": "JWT"}。
        """
        d = await self._request(
            "GET", self._server_path(instance_id, "/websocket"),
            _allow_uncoded_gateway=True,
        )
        if self._token:
            if not isinstance(d.get("token"), str) or not isinstance(d.get("socket"), str):
                raise APIError("新版面板 WebSocket 凭据格式异常")
            return d
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
                self._server_path(instance_id, "/files/contents"),
                params={"file": file_path},
            )
        except httpx.TimeoutException as e:
            raise APIError(f"读取文件超时: {self._error_text(file_path)}") from None
        except httpx.HTTPError as e:
            raise APIError(f"网络错误: {self._error_text(e)}") from None
        if r.status_code in (401, 419):
            auth_type = self._auth_type()
            raise AuthError(
                f"面板 {auth_type} 认证失败 (HTTP {r.status_code})"
            )
        if r.status_code >= 400:
            raise APIError(f"面板 HTTP {r.status_code}: {self._error_text(r.text)[:200]}")
        if self._token:
            # The gateway can report business errors with HTTP 200 even
            # though a successful file response is raw text, not a JSON envelope.
            try:
                failure = r.json()
            except ValueError:
                failure = None
            if isinstance(failure, dict) and failure.get("code") in (401, "401", 419, "419"):
                raise AuthError("面板 JWT 认证失败，请更新认证信息")
            if (
                isinstance(failure, dict) and "code" in failure
                and failure["code"] not in (200, "200", 0, "0", None)
                and ("msg" in failure or "message" in failure)
            ):
                code = self._error_text(failure["code"])
                detail = self._error_text(
                    failure.get("msg") or failure.get("message") or "未知错误"
                )[:200]
                raise APIError(f"面板文件读取失败 [{code}]: {detail}")
        return r.text
