"""Safe orchestration for destructive modpack changes; no QQ dependencies."""
from __future__ import annotations
import asyncio
import re

try:
    from .client import AuthError, MinekuaiError
    from .modpack_catalog import parse_catalog
    from .modpack_state import ServerIdentity, ConfirmError
except ImportError:
    from client import AuthError, MinekuaiError
    from modpack_catalog import parse_catalog
    from modpack_state import ServerIdentity, ConfirmError


class ModpackError(MinekuaiError):
    pass


READY_ATTEMPTS = 31
READY_INTERVAL = 2.0
READY_TIMEOUT = 60.0


class ModpackService:
    def __init__(self, *, get_server, build_client, build_panel, confirms,
                 maintenance, card_operation, cancel_background):
        self.get_server = get_server
        self.build_client = build_client
        self.build_panel = build_panel
        self.confirms = confirms
        self.maintenance = maintenance
        self.card_operation = card_operation
        self.cancel_background = cancel_background

    def current(self, identity):
        server = self.get_server(identity.name)
        if server is None or not identity.matches(server):
            raise ModpackError("服务器绑定已变化，请重新发起更换整合包")
        return server

    async def read(self, server, fn, *, panel=False, refresh=None):
        """Only read callbacks may use the one-time authentication refresh."""
        identity = ServerIdentity.from_server(server)
        for attempt in range(2):
            server = self.current(identity)
            try:
                factory = self.build_panel if panel else self.build_client
                async with factory(server) as client:
                    result = await fn(client)
                return result, self.current(identity)
            except AuthError:
                if attempt or refresh is None:
                    raise
                ok, message = await refresh(server)
                if not ok:
                    raise ModpackError(message)
        raise ModpackError("认证失败")

    async def search(self, server, query, page=1, refresh=None):
        data, _ = await self.read(server,
            lambda c: c.search_modpacks(query, page=page, page_size=9), refresh=refresh)
        return parse_catalog(data)

    async def versions(self, server, project_id, page=1, refresh=None):
        data, _ = await self.read(server,
            lambda c: c.list_modpack_versions(project_id, page=page, page_size=9), refresh=refresh)
        return parse_catalog(data)

    async def instance_details(self, server, refresh=None):
        if not server.instance_uuid:
            raise ModpackError("该服务器未配置实例 ID，请先配置实例")
        info, server = await self.read(server,
            lambda p: p.get_server_info(server.instance_uuid), panel=True, refresh=refresh)
        attr = info.get("attributes") if isinstance(info, dict) else None
        if not isinstance(attr, dict):
            raise ModpackError("实例详情格式异常，未提交安装")
        identifier = str(attr.get("identifier") or "")
        uuid = str(attr.get("uuid") or "")
        if (server.instance_uuid.casefold() not in {identifier.casefold(), uuid.casefold()}
                or re.fullmatch(r"[0-9a-fA-F]{8}", identifier) is None):
            raise ModpackError("官网实例与保存的绑定不一致，未提交安装")
        if str(attr.get("egg_id")) == "208":
            raise ModpackError("MCDR 实例使用不同的追加安装流程，请在官网操作；本指令不会覆盖它")
        if attr.get("is_installing") or attr.get("is_transferring") or attr.get("is_node_under_maintenance"):
            raise ModpackError("实例正在安装、迁移或节点维护，请结束后再试")
        if attr.get("status") not in (None, "", "install_failed", "reinstall_failed", "suspended"):
            raise ModpackError("实例状态暂不允许更换整合包，请在官网检查")
        return server, identifier, attr

    async def preflight(self, server, refresh=None, *, require_ready=False):
        server, identifier, attr = await self.instance_details(server, refresh)
        suspended = bool(attr.get("is_suspended") or attr.get("status") == "suspended")
        if require_ready and suspended:
            raise ModpackError("实例仍处于暂停状态，未提交安装")
        if not suspended:
            resources, server = await self.read(server,
                lambda p: p.get_resources(identifier), panel=True, refresh=refresh)
            if resources.get("attributes", {}).get("current_state") != "offline":
                raise ModpackError("请先停止实例再更换整合包；机器人不会替你强制杀进程")
        return server, identifier

    async def billing_active(self, server, identifier, refresh=None):
        """Only the exact card's exact instance can prove billing readiness."""
        payload, _ = await self.read(server, lambda c: c.get_user_packages(), refresh=refresh)
        cards = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(cards, list):
            raise ModpackError("计时卡列表格式异常，无法确认计费状态")
        matches = [card for card in cards if isinstance(card, dict)
                   and str(card.get("balanceId")) == server.card_id]
        if len(matches) != 1 or not isinstance(matches[0].get("instances"), list):
            raise ModpackError("计时卡绑定缺失或重复，无法确认计费状态")
        instances = [item for item in matches[0]["instances"] if isinstance(item, dict)
                     and str(item.get("serverId") or "").casefold() == identifier.casefold()]
        if len(instances) != 1:
            raise ModpackError("计时卡中的实例绑定缺失或重复，无法确认计费状态")
        status = instances[0].get("timingStatus")
        if type(status) not in (int, str) or status not in (0, 1, "0", "1"):
            raise ModpackError("目标实例的计费状态未知，未提交安装")
        return str(status) == "1"

    async def _wait_install_ready(self, identity, identifier, *, opened_billing,
                                  authorized, refresh=None):
        # Enabling billing can schedule a delayed game start. Initial offline
        # alone isn't proof it has settled; observe that transition and stop it.
        saw_active = not opened_billing
        stop_sent = False
        offline_reads = 0
        for attempt in range(READY_ATTEMPTS):
            server = self.current(identity)
            if not authorized():
                raise ConfirmError("权限已变化，已停止安装流程")
            billing = await self.billing_active(server, identifier, refresh)
            server, found, attr = await self.instance_details(server, refresh)
            if found != identifier:
                raise ModpackError("官网实例标识已变化，未提交安装")
            if not billing or attr.get("is_suspended") or attr.get("status") == "suspended":
                offline_reads = 0
            else:
                resources, server = await self.read(server,
                    lambda p: p.get_resources(identifier), panel=True, refresh=refresh)
                resource_attr = resources.get("attributes") if isinstance(resources, dict) else None
                state = resource_attr.get("current_state") if isinstance(resource_attr, dict) else None
                if state not in {"offline", "starting", "running", "stopping"}:
                    raise ModpackError("实例运行状态未知，未提交安装")
                if state != "offline":
                    offline_reads = 0
                    if not opened_billing:
                        raise ModpackError("实例已被启动，请先停服后重新确认更换")
                    saw_active = True
                    if state in {"starting", "running"} and not stop_sent:
                        server = self.current(identity)
                        if not authorized():
                            raise ConfirmError("权限已变化，未发送停服指令")
                        stop_sent = True
                        # Keep billing on. This is a single graceful process
                        # stop, never a billing stop, start, kill, or retry.
                        async with self.build_panel(server) as panel:
                            await panel.power(identifier, "stop")
                elif saw_active:
                    offline_reads += 1
                    if offline_reads >= 2:
                        return self.current(identity)
            if attempt + 1 < READY_ATTEMPTS:
                await asyncio.sleep(READY_INTERVAL)
        raise ModpackError("未确认开卡后的实例启动/停服已结束或持续离线，未提交安装")

    async def validate_choice(self, server, choice, refresh=None):
        projects, _ = await self.search(server, choice.search_query, choice.search_page, refresh)
        project = next((p for p in projects if p.item_id == choice.project_id), None)
        if project is None:
            raise ModpackError("整合包列表已变化，请重新搜索选择")
        candidates = (project,)
        if choice.version_page:
            candidates, _ = await self.versions(server, choice.project_id, choice.version_page, refresh)
        candidate = next((p for p in candidates if p.item_id == choice.item_id), None)
        fields = ("project_id", "item_id", "name", "version", "game_version", "java_version", "file_name")
        if candidate is None or not candidate.installable or any(
            getattr(candidate, field) != getattr(choice, field) for field in fields
        ):
            raise ModpackError("所选版本或下载文件已变化，请重新选择并确认")

    async def prepare(self, scope, server, choice, refresh=None):
        self.maintenance.ensure_card_available(server.card_id)
        server, _ = await self.preflight(server, refresh)
        await self.validate_choice(server, choice, refresh)
        return self.confirms.issue(scope, ServerIdentity.from_server(server), choice)

    async def confirm(self, scope, code, *, authorized, refresh=None, progress=None):
        # Synchronous consume happens before the first await. Failed attempts
        # never restore confirmation, and submitted POSTs never get replayed.
        pending = self.confirms.consume(scope, code)
        if not authorized():
            raise ConfirmError("你当前没有管理员权限，本次确认已取消")
        async with self.card_operation(pending.server.card_id):
            server = self.current(pending.server)
            self.maintenance.ensure_card_available(server.card_id)
            server, identifier = await self.preflight(server, refresh)
            await self.validate_choice(server, pending.choice, refresh)
            server = self.current(pending.server)
            active = await self.billing_active(server, identifier, refresh)
            server = self.current(pending.server)
            if not authorized():
                raise ConfirmError("权限已变化，本次确认已取消")
            self.cancel_background(server.name)
            self.maintenance.begin(pending.server, pending.choice)
            try:
                if progress:
                    await progress("正在开启计时卡（开始消耗时长），随后核对实例并正常停服..."
                                   if not active else "计时卡已经开启，正在核对实例是否可安装...")
                server = self.current(pending.server)
                if not authorized():
                    raise ConfirmError("权限已变化，未开启计时卡或安装")
                if not active:
                    # Billing may start the game too. Neither this request nor
                    # the subsequent graceful stop may use the read-retry path.
                    async with self.build_client(server) as client:
                        await client.start_timing(server.card_id, instance_id=identifier)
                server = await asyncio.wait_for(self._wait_install_ready(
                    pending.server, identifier, opened_billing=not active,
                    authorized=authorized, refresh=refresh,
                ), timeout=READY_TIMEOUT)
                if progress:
                    await progress("计时卡已开启且实例离线，正在复核并提交更换整合包...")
                await self.validate_choice(server, pending.choice, refresh)
                server, found = await self.preflight(server, refresh, require_ready=True)
                if found != identifier or not await self.billing_active(server, identifier, refresh):
                    raise ModpackError("实例标识或计费状态已变化，未提交安装")
                server = self.current(pending.server)
                if not authorized():
                    raise ConfirmError("权限已变化，未提交安装")
            except BaseException as exc:
                self.maintenance.mark(pending.server.instance_uuid, "unknown")
                if isinstance(exc, asyncio.CancelledError):
                    raise
                reason = str(exc) if isinstance(exc, (MinekuaiError, ConfirmError)) else type(exc).__name__
                raise ModpackError(
                    f"开计时卡或安装前检查未完成：{reason}。未提交更换整合包；"
                    "计费可能已经开启，维护保护保留，请先到官网核对计费和实例状态；机器人不会自动关卡"
                ) from None
            try:
                # The destructive request is deliberately OUTSIDE read().
                async with self.build_client(server) as client:
                    await client.switch_modpack(identifier, pending.choice.file_name, pending.choice.item_id)
            except BaseException:
                self.maintenance.mark(pending.server.instance_uuid, "unknown")
                raise
            self.maintenance.mark(pending.server.instance_uuid, "submitted")
            return pending

    async def finish_maintenance(self, server, *, authorized, refresh=None):
        identity = ServerIdentity.from_server(server)
        async with self.card_operation(server.card_id, allow_maintenance=True):
            if not authorized():
                raise ConfirmError("你当前没有管理员权限")
            server = self.current(identity)
            entry = self.maintenance.get(server.instance_uuid)
            if not entry:
                raise ModpackError("该实例没有整合包维护保护")
            # This command is an explicit human acknowledgment, not an inferred
            # successful install. Reject active install/transfer states anyway.
            info, server = await self.read(server,
                lambda p: p.get_server_info(server.instance_uuid), panel=True, refresh=refresh)
            attr = info.get("attributes") if isinstance(info, dict) else None
            if not isinstance(attr, dict) or not attr.get("identifier"):
                raise ModpackError("实例详情不完整，不能解除维护保护")
            if server.instance_uuid.casefold() not in {
                str(attr.get("identifier") or "").casefold(),
                str(attr.get("uuid") or "").casefold(),
            }:
                raise ModpackError("实例详情与绑定不一致，不能解除维护保护")
            if attr.get("is_installing") or attr.get("is_transferring") or attr.get("status") in ("installing", "reinstalling", "restoring_backup"):
                raise ModpackError("官网仍显示安装或迁移中，不能解除维护保护")
            if attr.get("is_node_under_maintenance") or attr.get("status") not in (
                None, "", "install_failed", "reinstall_failed", "suspended",
            ):
                raise ModpackError("官网状态未知或节点维护中，不能解除维护保护")
            self.current(identity)
            if not authorized():
                raise ConfirmError("权限已变化，未解除保护")
            return self.maintenance.finish(server.instance_uuid)
