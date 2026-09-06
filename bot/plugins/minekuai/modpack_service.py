"""Safe orchestration for destructive modpack changes; no QQ dependencies."""
from __future__ import annotations

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

    async def preflight(self, server, refresh=None):
        if not server.instance_uuid:
            raise ModpackError("该服务器未配置实例 ID，请先配置实例")
        info, server = await self.read(server,
            lambda p: p.get_server_info(server.instance_uuid), panel=True, refresh=refresh)
        attr = info.get("attributes")
        if not isinstance(attr, dict):
            raise ModpackError("实例详情格式异常，未提交安装")
        identifier = str(attr.get("identifier") or "")
        uuid = str(attr.get("uuid") or "")
        if server.instance_uuid.casefold() not in {identifier.casefold(), uuid.casefold()} or not identifier:
            raise ModpackError("官网实例与保存的绑定不一致，未提交安装")
        if str(attr.get("egg_id")) == "208":
            raise ModpackError("MCDR 实例使用不同的追加安装流程，请在官网操作；本指令不会覆盖它")
        if attr.get("is_installing") or attr.get("is_transferring") or attr.get("is_node_under_maintenance"):
            raise ModpackError("实例正在安装、迁移或节点维护，请结束后再试")
        if attr.get("status") not in (None, "", "install_failed", "reinstall_failed", "suspended"):
            raise ModpackError("实例状态暂不允许更换整合包，请在官网检查")
        if not attr.get("is_suspended") and attr.get("status") != "suspended":
            resources, server = await self.read(server,
                lambda p: p.get_resources(identifier), panel=True, refresh=refresh)
            if resources.get("attributes", {}).get("current_state") != "offline":
                raise ModpackError("请先停止实例再更换整合包；机器人不会替你强制杀进程")
        return server, identifier

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

    async def confirm(self, scope, code, *, authorized, refresh=None):
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
            if not authorized():
                raise ConfirmError("权限已变化，本次确认已取消")
            self.cancel_background(server.name)
            self.maintenance.begin(pending.server, pending.choice)
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
