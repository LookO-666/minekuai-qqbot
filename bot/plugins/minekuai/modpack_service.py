"""Safe orchestration for destructive modpack changes; no QQ dependencies."""
from __future__ import annotations
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from loguru import logger

try:
    from .client import AuthError, MinekuaiError, _safe_error_text
    from .modpack_catalog import CatalogError, normalize_item, parse_catalog, sanitize_display
    from .modpack_download import build_client_download
    from .modpack_state import ServerIdentity, ConfirmError
except ImportError:
    from client import AuthError, MinekuaiError, _safe_error_text
    from modpack_catalog import CatalogError, normalize_item, parse_catalog, sanitize_display
    from modpack_download import build_client_download
    from modpack_state import ServerIdentity, ConfirmError


class ModpackError(MinekuaiError):
    pass


READY_ATTEMPTS = 31
READY_INTERVAL = 2.0
READY_TIMEOUT = 60.0
INSTALL_TIMEOUT = 600.0
INSTALL_INTERVAL = 5.0
INSTALL_LOG_LIMIT = 256 * 1024


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

    async def client_download(self, server, choice=None, refresh=None):
        """Return free client links only; never call a points-charging resolver.

        fileName is the server installer, not a client. A child release must
        have its own matching catalog row; a parent's latest client is never
        substituted. Old history can still offer the explicitly labelled free
        collection without guessing a release ID or rewriting the installation.
        """
        identity = ServerIdentity.from_server(server)
        server = self.current(identity)
        entry = None
        if choice is None:
            entry = self.maintenance.latest(server.instance_uuid)
            if not entry:
                raise ModpackError("没有整合包选择记录，暂时无法确定要下载哪个客户端")
            if any(entry.get(key) != value for key, value in (
                ("server_name", identity.name), ("card_id", identity.card_id),
                ("instance_uuid", identity.instance_uuid),
                ("server_created_at", identity.created_at),
            )):
                raise ModpackError("安装记录与当前服务器绑定不一致，未提供其他实例的客户端下载信息")
            target = {
                "project_id": entry.get("pack_project_id", ""),
                "item_id": entry.get("pack_item_id", ""),
                "name": entry.get("pack_name", ""),
                "version": entry.get("pack_version", ""),
                "game_version": entry.get("pack_game_version", ""),
                "java_version": entry.get("pack_java_version", ""),
            }
        else:
            target = {field: getattr(choice, field) for field in (
                "project_id", "item_id", "name", "version", "game_version", "java_version",
            )}
        info = {key: sanitize_display(target[key], 160 if key == "name" else 80)
                for key in ("name", "version", "game_version", "java_version")}
        selected = None
        ids_valid = all(isinstance(target[key], str) and
                        re.fullmatch(r"[A-Za-z0-9_-]{1,128}", target[key])
                        for key in ("project_id", "item_id"))
        if ids_valid:
            try:
                # A pending selection already carries its exact catalog page.
                # History uses bounded, read-only paging; missing entries fall
                # back to the free collection, never another release's URL.
                is_project = target["project_id"] == target["item_id"]
                pages = (choice.search_page if is_project else choice.version_page,) if choice else range(1, 4)
                page_size = 9 if choice else 50
                for page in pages:
                    if is_project:
                        query = choice.search_query if choice else target["name"]
                        payload, _ = await self.read(server, lambda c: c.search_modpacks(
                            query, page=page, page_size=page_size), refresh=refresh)
                    else:
                        payload, _ = await self.read(server, lambda c: c.list_modpack_versions(
                            target["project_id"], page=page, page_size=page_size), refresh=refresh)
                    _, total = parse_catalog(payload)
                    matches = [raw for raw in payload["rows"]
                               if str(raw.get("id")) == target["item_id"]]
                    if len(matches) == 1:
                        item = normalize_item(matches[0])
                        if all(getattr(item, field) == target[field] for field in target):
                            selected = matches[0]
                        break
                    if matches or page * page_size >= total:
                        break
            except (MinekuaiError, CatalogError, ValueError):
                # This ancillary read cannot change the installation's outcome.
                # Never expose an API body, auth material or paid download URL.
                logger.info("[modpack] free client catalog metadata unavailable")
        server = self.current(identity)
        if entry is not None:
            latest = self.maintenance.latest(server.instance_uuid)
            if not latest or any(latest.get(key) != entry.get(key) for key in (
                "attempt_id", "created_at", "pack_item_id", "pack_version",
            )):
                raise ModpackError("整合包选择记录已变化，请重新查询客户端")
        return build_client_download(info, selected, secrets=(server.token, server.client_id))

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
            if require_ready:
                live = await self.live_state(server, identifier, refresh)
                if live["state"] != "offline" or not live["stable_offline"]:
                    raise ModpackError("实时控制台尚未确认持续离线，未提交安装")
        return server, identifier

    async def live_state(self, server, identifier, refresh=None):
        payload, _ = await self.read(server,
            lambda p: p.get_live_state(identifier, stable_offline_seconds=3.0),
            panel=True, refresh=refresh)
        if (not isinstance(payload, dict)
                or not isinstance(payload.get("state"), str)
                or payload.get("state") not in {"offline", "starting", "running", "stopping"}
                or type(payload.get("stable_offline")) is not bool
                or (payload["state"] != "offline" and payload["stable_offline"])):
            raise ModpackError("实时控制台运行状态格式异常，未提交安装")
        return payload

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

    async def _log_snapshot(self, server, refresh=None, *, read_content=True):
        """Read only the known installer log, with a bounded file-size check."""
        files, server = await self.read(server,
            lambda p: p.list_directory(server.instance_uuid, "/"), panel=True, refresh=refresh)
        if not isinstance(files, list):
            raise ModpackError("安装日志目录格式异常")
        found = [item for item in files if isinstance(item, dict)
                 and item.get("name") == "installserverlogs.log"]
        if not found:
            return None
        if len(found) != 1:
            raise ModpackError("安装日志文件记录重复")
        item = found[0]
        if (item.get("is_file") is not True or item.get("is_symlink") is not False
                or type(item.get("size")) is not int or item["size"] < 0):
            raise ModpackError("安装日志不是普通文件，不能用于确认安装")
        try:
            modified = datetime.fromisoformat(item["modified_at"].replace("Z", "+00:00"))
            if modified.tzinfo is None:
                raise ValueError
        except (ValueError, TypeError, AttributeError, KeyError):
            raise ModpackError("安装日志时间格式未知，不能确认本次安装") from None
        text = ""
        if read_content and 0 < item["size"] <= INSTALL_LOG_LIMIT:
            text, server = await self.read(server,
                lambda p: p.read_file_text(server.instance_uuid, "/installserverlogs.log"),
                panel=True, refresh=refresh)
            if not isinstance(text, str) or len(text.encode("utf-8")) > INSTALL_LOG_LIMIT:
                raise ModpackError("安装日志过大或格式异常，未确认完成")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""
        stamp = json.dumps([item.get("created_at"), item["modified_at"], item["size"], digest],
                           ensure_ascii=True, separators=(",", ":"))
        return {"stamp": stamp, "modified_at": modified.timestamp(), "text": text,
                "content_digest": digest}

    @staticmethod
    def _new_install_log(entry, snapshot):
        if not entry or entry.get("write_started_at") == 0 or not snapshot:
            return False
        # NULL marks a pre-migration guard. Its creation time bounds the
        # maintenance window but does not independently identify a catalog pack.
        boundary = entry.get("write_started_at") or entry["created_at"]
        baseline = entry.get("baseline_log_stamp")
        if snapshot["modified_at"] < boundary or snapshot["stamp"] == baseline:
            return False
        if baseline:
            try:
                fields = json.loads(baseline)
                if not isinstance(fields, list) or len(fields) != 4:
                    return False
                if fields[3] and fields[3] == snapshot["content_digest"]:
                    return False
            except (ValueError, TypeError):
                return False
        return True

    async def install_status(self, server, refresh=None, *, include_billing=True):
        """Reconcile uncertain receipts using fresh installer evidence; never replay writes."""
        identity = ServerIdentity.from_server(server)
        active_entry = self.maintenance.get(server.instance_uuid)
        entry = active_entry or self.maintenance.latest(server.instance_uuid)
        info, server = await self.read(server,
            lambda p: p.get_server_info(server.instance_uuid), panel=True, refresh=refresh)
        attr = info.get("attributes") if isinstance(info, dict) else None
        if (not isinstance(attr, dict) or server.instance_uuid.casefold() not in {
                str(attr.get("identifier") or "").casefold(), str(attr.get("uuid") or "").casefold()}):
            raise ModpackError("安装查询的实例标识与绑定不一致")
        outcome, detail = "unknown", "尚无足够证据确认本次安装结果，请勿重复安装"
        if attr.get("is_installing") or attr.get("status") in {"installing", "reinstalling"}:
            outcome, detail = "installing", "官网显示安装仍在进行中，请等待"
        elif attr.get("status") in {"install_failed", "reinstall_failed"}:
            # A pre-existing failure flag may remain briefly after a new POST.
            # Do not end observation until this task has new installation evidence.
            fresh_failure = not entry or entry.get("install_outcome") in {"installing", "failed"}
            if fresh_failure:
                outcome, detail = "failed", "官网显示安装失败，请检查安装日志；不会自动重复安装"
            else:
                detail = "官网仍显示失败状态，但尚未确认属于本次请求，继续观察新安装日志"
        elif (entry and entry.get("write_started_at") != 0
              and attr.get("status") in (None, "", "suspended")
              and attr.get("is_installing") is False
              and not any(attr.get(key) for key in (
                  "is_transferring", "is_node_under_maintenance"))):
            snapshot = await self._log_snapshot(server, refresh)
            if self._new_install_log(entry, snapshot):
                lines = snapshot["text"].strip().splitlines()
                last = lines[-1].strip() if lines else ""
                success = re.fullmatch(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] 整合包安装成功[!！]", last)
                if success:
                    try:
                        # Minekuai's installer uses China time; file metadata is
                        # ISO UTC. Validate both, including for old guards that
                        # have no pre-write content digest after migration.
                        finished_at = datetime.strptime(success[1], "%Y-%m-%d %H:%M:%S").replace(
                            tzinfo=timezone(timedelta(hours=8))).timestamp()
                        boundary = entry.get("write_started_at") or entry["created_at"]
                        if not boundary <= finished_at <= snapshot["modified_at"] + 5:
                            success = None
                    except ValueError:
                        success = None
                if success:
                    # Recheck after reading the log: an active/new task always
                    # takes precedence over an earlier success line.
                    latest, server = await self.read(server,
                        lambda p: p.get_server_info(server.instance_uuid), panel=True, refresh=refresh)
                    current = latest.get("attributes", {})
                    if (current.get("identifier") == attr.get("identifier")
                            and current.get("status") in (None, "", "suspended")
                            and current.get("is_installing") is False
                            and not any(current.get(key) for key in (
                                "is_transferring", "is_node_under_maintenance"))):
                        outcome, detail = "completed", "本次维护期间的新安装日志确认成功，官网已无进行中的安装任务"
                    else:
                        detail = "读取日志期间实例状态发生变化，暂不确认完成"
        self.current(identity)
        if active_entry:
            self.maintenance.observe(active_entry, outcome)
        billing = None
        if include_billing:
            try:
                billing = await self.billing_active(server, str(attr.get("identifier") or ""), refresh)
            except MinekuaiError:
                pass
        return {"outcome": outcome, "detail": detail, "billing_active": billing,
                "maintenance": active_entry is not None, "released": False}

    async def install_log(self, server, refresh=None):
        identity = ServerIdentity.from_server(server)
        snapshot = await self._log_snapshot(server, refresh)
        if not snapshot:
            return "尚未发现安装日志；请用『整合包状态』继续核对，勿重复安装。"
        if not snapshot["text"]:
            return "安装日志为空或超出读取上限；请用『整合包状态』核对任务状态。"
        text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", snapshot["text"])
        text = _safe_error_text(text, *(getattr(server, field, "") or "" for field in (
            "token", "panel_api_key", "session_cookie", "xsrf_token", "panel_session", "panel_xsrf")))
        # Legacy credentials live on the bound Account rather than Server.
        # Reuse the actual client's redactor, including individual cookie values.
        async with self.build_panel(self.current(identity)) as panel:
            text = panel._error_text(text)
        self.current(identity)
        text = re.sub(r"https?://\S+", "[下载链接已隐藏]", text)
        return "\n".join(text.splitlines()[-30:])[-3000:]

    async def _reconcile_locked(self, identity, *, authorized, refresh=None):
        """Caller owns the normal per-card/instance lock; no remote mutations."""
        if not authorized():
            raise ConfirmError("你当前没有管理员权限")
        server = self.current(identity)
        entry = self.maintenance.get(server.instance_uuid)
        if entry and (entry["server_name"] != server.name or entry["card_id"] != server.card_id
                      or entry["server_created_at"] != server.created_at
                      or entry["instance_uuid"].casefold() != server.instance_uuid.casefold()):
            raise ModpackError("维护记录的原服务器绑定已变化，保护保留，不能自动解除")
        report = await self.install_status(server, refresh)
        if entry is None:
            return report
        # Always recheck terminal state immediately before releasing protection.
        info, server = await self.read(server,
            lambda p: p.get_server_info(server.instance_uuid), panel=True, refresh=refresh)
        attr = info.get("attributes") if isinstance(info, dict) else None
        terminal = (isinstance(attr, dict)
            and server.instance_uuid.casefold() in {
                str(attr.get("identifier") or "").casefold(), str(attr.get("uuid") or "").casefold()}
            and attr.get("is_installing") is False
            and not attr.get("is_transferring") and not attr.get("is_node_under_maintenance"))
        if entry.get("write_started_at") == 0 and terminal and attr.get("status") in (
                None, "", "suspended", "install_failed", "reinstall_failed"):
            report.update(outcome="not_submitted", detail="本次未发送安装请求，已确认没有活跃安装任务")
        outcome = report["outcome"]
        allowed = (
            terminal and (
                (outcome == "completed" and attr.get("status") in (None, "", "suspended"))
                or (outcome == "failed" and attr.get("status") in ("install_failed", "reinstall_failed"))
                or outcome == "not_submitted"
            )
        )
        self.current(identity)
        if not authorized():
            raise ConfirmError("权限已变化，未解除维护保护")
        if not allowed:
            report.update(maintenance=True, released=False)
            return report
        self.maintenance.observe(entry, outcome)
        self.maintenance.finish(server.instance_uuid, expected=entry, reason=outcome)
        report.update(maintenance=False, released=True)
        logger.info("[modpack] instance={} maintenance released outcome={}",
                    server.instance_uuid[:8], outcome)
        return report

    async def reconcile_maintenance(self, server, *, authorized, refresh=None):
        identity = ServerIdentity.from_server(server)
        async with self.card_operation(server.card_id, allow_maintenance=True):
            return await self._reconcile_locked(identity, authorized=authorized, refresh=refresh)

    async def reconcile_for_operation(self, server, *, authorized, refresh=None):
        """Resolve the guard's original owner, never use an alias's credentials on it."""
        identity = ServerIdentity.from_server(server)
        if not authorized():
            raise ConfirmError("你当前没有管理员权限")
        entry = self.maintenance.find_blocking(server.card_id, server.instance_uuid)
        if entry is None:
            return {"maintenance": False, "released": False}
        owner = self.get_server(entry["server_name"])
        if (owner is None or owner.card_id != entry["card_id"]
                or owner.instance_uuid.casefold() != entry["instance_uuid"].casefold()
                or owner.created_at != entry["server_created_at"]):
            raise ModpackError("维护记录的原服务器绑定已变化，无法自动解除；请用『整合包状态』核对")
        report = await self.reconcile_maintenance(owner, authorized=authorized, refresh=refresh)
        self.current(identity)
        if not authorized():
            raise ConfirmError("权限已变化，操作已停止")
        if report["maintenance"]:
            raise ModpackError(
                f"安装任务仍受维护保护：{report['detail']}。"
                f"请发『整合包状态 {owner.name}』或『整合包日志 {owner.name}』；确认结束后会自动解除"
            )
        return report

    async def _wait_install_finished(self, identity, refresh=None):
        """Observation expiry is not a failed install, and never causes a new POST."""
        deadline = asyncio.get_running_loop().time() + INSTALL_TIMEOUT
        while True:
            server = self.current(identity)
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return "unknown"
            try:
                result = await asyncio.wait_for(self.install_status(
                    server, refresh, include_billing=False), timeout=min(45.0, remaining))
                if result["outcome"] in {"completed", "failed"}:
                    return result["outcome"]
            except (MinekuaiError, asyncio.TimeoutError):
                # Read-only polling can recover after a transient API outage.
                # No response text or credentials enter the diagnostic log.
                logger.info("[modpack] instance={} installation observation unavailable",
                            identity.instance_uuid[:8])
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return "unknown"
            await asyncio.sleep(min(INSTALL_INTERVAL, remaining))

    async def _wait_install_ready(self, identity, identifier, *, opened_billing,
                                  authorized, refresh=None, diagnostics=None):
        # A start transition can be absent or too brief for HTTP polling.
        # Require authenticated live offline observations instead of demanding
        # that the instance must have been seen running first.
        observed = diagnostics if diagnostics is not None else {}
        stop_sent = False
        offline_reads = 0
        for attempt in range(READY_ATTEMPTS):
            observed.update(stage="读取计费状态", http=None, live=None, stable=None)
            server = self.current(identity)
            if not authorized():
                raise ConfirmError("权限已变化，已停止安装流程")
            billing = await self.billing_active(server, identifier, refresh)
            observed.update(billing=billing, stage="读取实例详情")
            server, found, attr = await self.instance_details(server, refresh)
            if found != identifier:
                raise ModpackError("官网实例标识已变化，未提交安装")
            suspended = bool(attr.get("is_suspended") or attr.get("status") == "suspended")
            observed["suspended"] = suspended
            if not billing or suspended:
                offline_reads = 0
            else:
                observed["stage"] = "读取 HTTP 运行状态"
                resources, server = await self.read(server,
                    lambda p: p.get_resources(identifier), panel=True, refresh=refresh)
                resource_attr = resources.get("attributes") if isinstance(resources, dict) else None
                state = resource_attr.get("current_state") if isinstance(resource_attr, dict) else None
                if not isinstance(state, str) or state not in {"offline", "starting", "running", "stopping"}:
                    raise ModpackError("实例运行状态未知，未提交安装")
                observed.update(http=state, stage="核对实时控制台")
                live = await self.live_state(server, identifier, refresh)
                observed.update(live=live["state"], stable=live["stable_offline"])
                logger.info("[modpack] instance={} billing={} http={} live={} stable_offline={}",
                            identifier, billing, state, live["state"], live["stable_offline"])
                if live["state"] != "offline":
                    offline_reads = 0
                    if not opened_billing:
                        raise ModpackError("实例已被启动，请先停服后重新确认更换")
                    if live["state"] in {"starting", "running"} and not stop_sent:
                        server = self.current(identity)
                        if not authorized():
                            raise ConfirmError("权限已变化，未发送停服指令")
                        stop_sent = True
                        observed["stage"] = "正常停服"
                        # Keep billing on. This is a single graceful process
                        # stop, never a billing stop, start, kill, or retry.
                        async with self.build_panel(server) as panel:
                            await panel.power(identifier, "stop")
                elif state == "offline" and live["stable_offline"]:
                    offline_reads += 1
                    if offline_reads >= 2:
                        return self.current(identity)
                else:
                    # A stale/disagreeing HTTP snapshot must not bypass the
                    # live check, nor should it trigger a stop on an offline WS.
                    offline_reads = 0
                observed["stage"] = "等待 HTTP 与实时状态连续一致离线"
            if attempt + 1 < READY_ATTEMPTS:
                await asyncio.sleep(READY_INTERVAL)
        raise ModpackError("未确认 HTTP 与实时控制台持续一致离线，未提交安装")

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

    async def confirm(self, scope, code, *, authorized, refresh=None, progress=None,
                      on_submitted=None):
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
            diagnostics = {"billing": active, "stage": "开启计时卡"}
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
                    authorized=authorized, refresh=refresh, diagnostics=diagnostics,
                ), timeout=READY_TIMEOUT)
                if progress:
                    await progress("计时卡已开启且实例离线，正在复核并提交更换整合包...")
                await self.validate_choice(server, pending.choice, refresh)
                if not await self.billing_active(server, identifier, refresh):
                    raise ModpackError("实例标识或计费状态已变化，未提交安装")
                server, found = await self.preflight(server, refresh, require_ready=True)
                if found != identifier:
                    raise ModpackError("实例标识已变化，未提交安装")
                server = self.current(pending.server)
                if not authorized():
                    raise ConfirmError("权限已变化，未提交安装")
                baseline = await self._log_snapshot(server, refresh)
                self.current(pending.server)
                if not authorized():
                    raise ConfirmError("权限已变化，未提交安装")
                self.maintenance.start_write(server.instance_uuid, baseline["stamp"] if baseline else "")
            except BaseException as exc:
                self.maintenance.mark(pending.server.instance_uuid, "unknown")
                if isinstance(exc, asyncio.CancelledError):
                    raise
                if isinstance(exc, asyncio.TimeoutError):
                    reason = (f"等待实例就绪超过 {READY_TIMEOUT:g} 秒；停在{diagnostics['stage']}，"
                              f"计费={'已开启' if diagnostics.get('billing') else '未确认开启'}，"
                              f"HTTP={diagnostics.get('http') or '未取得'}，"
                              f"实时={diagnostics.get('live') or '未取得'}")
                else:
                    reason = str(exc) if isinstance(exc, (MinekuaiError, ConfirmError)) else type(exc).__name__
                raise ModpackError(
                    f"开计时卡或安装前检查未完成：{reason}。未提交更换整合包；"
                    "计费可能已经开启，请发『整合包状态』自动核对并恢复操作；机器人不会自动关卡"
                ) from None
            receipt_error = None
            try:
                # The destructive request is deliberately OUTSIDE read().
                async with self.build_client(server) as client:
                    await client.switch_modpack(identifier, pending.choice.file_name, pending.choice.item_id)
            except BaseException as exc:
                self.maintenance.mark(pending.server.instance_uuid, "unknown")
                if isinstance(exc, asyncio.CancelledError):
                    raise
                receipt_error = str(exc) if isinstance(exc, MinekuaiError) else type(exc).__name__
            else:
                self.maintenance.mark(pending.server.instance_uuid, "submitted")
            if on_submitted:
                try:
                    # Client download metadata is optional and independent of
                    # the destructive operation. Never replay the installer or
                    # skip its observation because a QQ notification failed.
                    await asyncio.wait_for(on_submitted(pending), timeout=20.0)
                except Exception:
                    logger.warning("[modpack] optional client download notification unavailable")
            if progress:
                try:
                    await progress("安装请求已发出，正在核对安装日志，可能需要几分钟。请勿重复安装。"
                                   if receipt_error is None else
                                   "安装接口未返回可确认的结果，但请求可能已受理。正在只读核对安装日志，请勿重复安装。")
                except Exception:
                    # A QQ send failure must not prevent observing the write.
                    pass
            outcome = await self._wait_install_finished(pending.server, refresh)
            report = None
            if outcome in {"completed", "failed"}:
                report = await self._reconcile_locked(pending.server, authorized=authorized, refresh=refresh)
            if outcome == "failed":
                raise ModpackError(
                    "安装失败，任务已结束且保护已自动解除；可发『整合包日志』查看原因，或发『关服』停止计费；不会自动重试"
                    if report and report["released"] else
                    "平台显示安装失败，仍在核对任务是否结束；请发『整合包状态』或『整合包日志』，不会自动重试"
                )
            if outcome != "completed" and receipt_error is not None:
                raise ModpackError(
                    f"安装请求已发出，但结果未确认：{receipt_error}。"
                    "只读观察暂未确认完成；请用『整合包状态』继续查询，不要重复安装"
                )
            return pending

    async def finish_maintenance(self, server, *, authorized, refresh=None):
        report = await self.reconcile_maintenance(server, authorized=authorized, refresh=refresh)
        if report["maintenance"]:
            raise ModpackError("尚未确认安装结束，维护保护保留；请发『整合包状态』或『整合包日志』继续核对")
        return True
