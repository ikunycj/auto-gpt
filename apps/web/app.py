# -*- coding: utf-8 -*-
"""
Flask 本地控制台。

复用现有后端：
    core.db                     —— 账号 / 邮箱池 / 任务的 SQLite 持久化与查询
    registration.application.job_service
                                —— 线程池批量注册 + 任务日志
    apps.web.config_editor      —— 安全读写 config/*.py

所有接口返回 JSON；前端由同级 web/ 下构建的 React SPA 提供。
默认绑定 127.0.0.1，仅本地访问。
"""
import logging
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, Response, jsonify, request, send_from_directory

from core import codex_retry_service, codex_relay_service, gpt_account_service, db, plan_check_service, extract_link_service, codex_agent_service, live_check_service, sqlite_store
from core.import_parser import (
    clean_import_value,
    is_email,
    is_http_url,
    looks_like_access_token,
    looks_like_totp,
    parse_account_material_line,
    split_import_line,
)
from registration.application import job_service as svc
from apps.web import config_editor

logger = logging.getLogger(__name__)

def _pool_source_arg(default: str = "outlook") -> str:
    src = (request.args.get("source") or "").strip()
    if not src and request.method == "POST":
        data = request.get_json(silent=True) or {}
        src = (data.get("source") or data.get("type") or "").strip()
    return src if src in ("all", "outlook", "generic_api", "cloudflare_domain") else default


def _with_pool_source(rows: list[dict], source: str) -> list[dict]:
    out = []
    for r in rows:
        x = dict(r)
        x["source"] = source
        if not x.get("copy_line"):
            x["copy_line"] = x.get("email") or ""
        out.append(x)
    return out




def _matches_query(row: dict, q: str | None) -> bool:
    q = str(q or "").strip().lower()
    if not q:
        return True
    try:
        return q in "\n".join(str(v) for v in row.values()).lower()
    except Exception:
        return False


def _paginate_items(items: list[dict], *, page: int, page_size: int) -> dict:
    page = max(1, int(page or 1))
    page_size = max(1, min(500, int(page_size or 50)))
    total = len(items)
    offset = (page - 1) * page_size
    return {
        "ok": True,
        "items": items[offset:offset + page_size],
        "total": total,
        "page": page,
        "page_size": page_size,
        "offset": offset,
        "limit": page_size,
    }


def _compact_account_for_list(row: dict) -> dict:
    """账号列表轻量对象：只返回当前表格渲染和按钮判断必需字段。

    原则：
    - 不返回完整 Token / Token 预览 / TOTP Secret / Agent Token。
    - 时间戳、错误原因、提链详情等只在前端确实要展示时返回；空值不返回。
    - 复制/下载敏感内容时再通过 /secret 接口按需读取。
    """
    out = {
        "id": row.get("id"),
        "email": row.get("email"),
        "has_access_token": bool(str(row.get("access_token") or "").strip()),
        "totp_enabled": bool(row.get("totp_secret")),
        "codex_agent_has_token": bool(str(row.get("codex_agent_token") or "").strip()),
    }

    # 这些是列表固定列直接展示字段。
    for key in (
        "user_name", "email_source", "note", "archived", "created_at",
        "plan_type", "current_plan_type", "plus_trial_eligible",
        "plan_check_status", "codex_status", "codex_agent_status",
    ):
        if key in row:
            out[key] = row.get(key)

    if row.get("plan_check_status") in ("queued", "running") or row.get("plan_check_ok") is False:
        out["plan_check_ok"] = row.get("plan_check_ok")

    # 下面字段仅在有值时返回，避免每行堆满 null/空字符串/内部状态。
    optional_keys = (
        # 套餐展示补充：付费到期/折扣/失败原因。
        "plan_check_error", "plan_expires_at", "plan_renews_at", "renews_at",
        "billing_period", "billing_currency", "discount_amount", "discount_type",
        "discount_expires_at", "discount_promo_campaign_id",
        "token_expired", "token_expires_at",
        # 查活状态。
        "live_check_status", "live_check_error", "live_checked_at",
        # 提链成功/失败时才需要。
        "extract_link_status", "extract_link_type", "extract_link_message", "extract_link_error",
        "extract_link_long_url", "extract_link_copy_paste", "extract_link_image_url_png",
        "extract_link_image_url_svg", "extract_link_expires_at",
        # Codex / Agent 状态提示。
        "codex_error", "codex_agent_message", "codex_agent_runtime_id",
        "codex_agent_sub2api_url", "codex_agent_sub2api_mode", "codex_agent_sub2api_total",
    )
    for key in optional_keys:
        value = row.get(key)
        if value is not None and value != "":
            out[key] = value
    plan = str(row.get("current_plan_type") or row.get("plan_type") or "").lower()
    if any(x in plan for x in ("plus", "pro", "team", "go")):
        expire = row.get("expires_at")
        if expire:
            out["expires_at"] = expire
    return out


def _account_secret_value(row: dict, field: str) -> str:
    field = (field or "").strip()
    if field == "access_token":
        return str(row.get("access_token") or "")
    if field == "copy_line":
        return str(row.get("copy_line") or "")
    if field == "codex_agent_token":
        return str(row.get("codex_agent_token") or "")
    raise ValueError("field 仅支持 access_token/copy_line/codex_agent_token")


def _compact_job_for_list(row: dict) -> dict:
    """注册任务列表轻量对象：只返回表格展示和按钮判断需要的字段。"""
    out = {
        "id": row.get("id"),
        "status": row.get("status"),
    }
    for key in (
        "parent_job_id", "retry_attempt", "email", "started_at", "completed_at",
        "display_status", "retryable", "retry_action", "retry_label",
        "manual_otp_required",
    ):
        value = row.get(key)
        if value is not None and value != "" and value is not False:
            out[key] = value
    err = str(row.get("error_message") or "").strip()
    if err:
        # 列表只需要摘要；完整错误和堆栈看“任务日志”。
        out["error_message"] = err[:240] + ("…" if len(err) > 240 else "")
    return out


def _job_status_counts(rows: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    counts["active"] = sum(int(counts.get(s, 0) or 0) for s in ("pending", "running", "stopping"))
    return counts

def create_app() -> Flask:
    project_root = Path(__file__).resolve().parents[2]
    react_dist = project_root / "web" / "dist"
    app = Flask(
        __name__,
        static_folder=str(react_dist / "assets") if (react_dist / "assets").is_dir() else None,
        static_url_path="/assets",
    )
    _prepared_downloads: dict[str, dict] = {}

    def _put_prepared_download(content: bytes, filename: str, mimetype: str = "application/zip") -> str:
        now = time.time()
        # 顺手清理 10 分钟前的临时下载，避免内存堆积。
        for k, v in list(_prepared_downloads.items()):
            if now - float(v.get("created_at") or 0) > 600:
                _prepared_downloads.pop(k, None)
        download_id = uuid.uuid4().hex
        _prepared_downloads[download_id] = {
            "content": bytes(content),
            "filename": filename,
            "mimetype": mimetype,
            "created_at": now,
        }
        return download_id

    @app.get("/api/downloads/<download_id>")
    def api_prepared_download(download_id: str):
        item = _prepared_downloads.pop(str(download_id or ""), None)
        if not item:
            return jsonify({"ok": False, "error": "下载已过期或不存在，请重新生成"}), 404
        content = item.get("content") or b""
        filename = item.get("filename") or "download.zip"
        mimetype = item.get("mimetype") or "application/octet-stream"
        return Response(
            content,
            mimetype=mimetype,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(len(content)),
                "Cache-Control": "no-store, max-age=0",
                "Pragma": "no-cache",
                "X-Content-Type-Options": "nosniff",
                "X-Download-Options": "noopen",
            },
        )

    # Import any still-present legacy input before recovery workers inspect it.
    # SQLite is authoritative after this idempotent migration; normal runtime
    # writes never create JSON/TXT/HTML mirrors in the repository.
    try:
        storage_info = db.initialize_storage()
        relay_storage_info = codex_relay_service.initialize_storage()
        logger.info(
            "SQLite 存储已就绪：db=%s collections=%s items=%s relay_items=%s",
            storage_info.get("path"),
            storage_info.get("collections"),
            storage_info.get("items"),
            relay_storage_info.get("items"),
        )
    except Exception:
        logger.exception("SQLite 存储初始化失败")
        raise
    recovered_plan_checks = db.recover_interrupted_plan_checks()
    if recovered_plan_checks:
        logger.warning("已恢复 %s 个因 WebUI 重启中断的套餐查询状态", recovered_plan_checks)
    recovered_extract_links = db.recover_interrupted_extract_links()
    if recovered_extract_links:
        logger.warning("已恢复 %s 个因 WebUI 重启中断的提链状态", recovered_extract_links)
    recovered_live_checks = db.recover_interrupted_live_checks()
    if recovered_live_checks:
        logger.warning("已恢复 %s 个因 WebUI 重启中断的查活状态", recovered_live_checks)
    recovered_codex_agents = db.recover_interrupted_codex_agents()
    if recovered_codex_agents:
        logger.warning("已恢复 %s 个因 WebUI 重启中断的 Codex Agent Token 状态", recovered_codex_agents)
    recovered_codex_relay = codex_relay_service.recover_interrupted_jobs()
    if recovered_codex_relay:
        logger.warning("已停止 %s 个因 WebUI 重启中断的 GPT 账号 Codex 接码任务", recovered_codex_relay)

    # ----------------------------------------------------------
    # 页面
    # ----------------------------------------------------------
    @app.get("/")
    def index():
        if not (react_dist / "index.html").is_file():
            return Response(
                "React WebUI build missing. Run `cd web && npm run build`.",
                status=503,
                mimetype="text/plain",
            )
        return send_from_directory(react_dist, "index.html")

    # ----------------------------------------------------------
    # 统计概览
    # ----------------------------------------------------------
    @app.get("/api/summary")
    def api_summary():
        from config import email as _email_cfg
        from core.email_provider import parse_email_sources, registration_email_status
        email_sources = parse_email_sources(_email_cfg.EMAIL_SOURCE)
        pool = {"total": 0, "available": 0, "used": 0, "failed": 0}
        for src in email_sources:
            # GPTMail/MailNest/CloudMail 地址按需生成，不属于本地邮箱池。
            if src in ("gptmail", "mailnest", "cloudmail", "cloudflare"):
                continue
            one = (
                db.generic_api_email_pool_summary() if src == "generic_api"
                else db.domain_email_pool_summary() if src == "cloudflare_domain"
                else db.outlook_pool_summary()
            )
            for k in pool:
                pool[k] += int(one.get(k, 0) or 0)
        domain_pool = db.domain_email_pool_summary()
        gpt_accounts = gpt_account_service.list_accounts()
        email_runtime = registration_email_status()
        # Kept for older WebUI bundles during a rolling local restart.
        email_runtime["mailnest_configured"] = any(
            item["id"] == "mailnest" and item["configured"]
            for item in email_runtime["channels"]
        )
        return jsonify({
            "accounts": len(gpt_accounts),
            "outlook_total": pool.get("total", 0),
            "outlook_available": pool.get("available", 0),
            "outlook_used": pool.get("used", 0),
            "outlook_failed": pool.get("failed", 0),
            "domain_total": domain_pool.get("total", 0),
            "domain_available": domain_pool.get("available", 0),
            "domain_used": domain_pool.get("used", 0),
            "domain_failed": domain_pool.get("failed", 0),
            "registration_email": email_runtime,
        })

    # ----------------------------------------------------------
    # 已注册账号
    # ----------------------------------------------------------
    @app.get("/api/accounts")
    def api_accounts():
        limit = request.args.get("limit", default=500, type=int)
        archived = str(request.args.get("archived", default="0") or "0").lower()
        plan_filter = str(request.args.get("plan", default="") or "").lower()
        q = str(request.args.get("q", default="") or "").strip()
        # 新分页接口：传 page/page_size 或 paged=1 时返回 {items,total,page,page_size,...}
        paged = str(request.args.get("paged", default="") or "").lower() in {"1", "true", "yes"}
        page_arg = request.args.get("page", default=None, type=int)
        page_size_arg = request.args.get("page_size", default=None, type=int)
        if paged or page_arg is not None or page_size_arg is not None:
            page = max(1, int(page_arg or 1))
            page_size = max(1, min(500, int(page_size_arg or limit or 50)))
            offset = (page - 1) * page_size
            result = db.list_accounts_page(limit=page_size, offset=offset, archived=archived, plan_filter=plan_filter, q=q)
            result["items"] = [_compact_account_for_list(r) for r in (result.get("items") or [])]
            result.update({"ok": True, "page": page, "page_size": page_size, "compact": True})
            return jsonify(result)
        return jsonify(db.list_accounts(limit=limit, archived=archived, plan_filter=plan_filter, q=q))

    @app.get("/api/accounts/plan-check-status")
    def api_account_plan_check_status():
        """套餐查询轻量状态，不返回 Token、邮箱密码等敏感字段。"""
        limit = request.args.get("limit", default=5000, type=int)
        archived = str(request.args.get("archived", default="0") or "0").lower()
        plan_filter = str(request.args.get("plan", default="") or "").lower()
        q = str(request.args.get("q", default="") or "").strip()
        page_arg = request.args.get("page", default=None, type=int)
        page_size_arg = request.args.get("page_size", default=None, type=int)
        if page_arg is not None or page_size_arg is not None:
            page = max(1, int(page_arg or 1))
            page_size = max(1, min(500, int(page_size_arg or limit or 50)))
            offset = (page - 1) * page_size
            snapshot = db.list_account_plan_check_statuses(limit=page_size, offset=offset, archived=archived, plan_filter=plan_filter, q=q)
            snapshot.update({"page": page, "page_size": page_size})
        else:
            snapshot = db.list_account_plan_check_statuses(limit=max(1, min(5000, limit)), archived=archived, plan_filter=plan_filter, q=q)
        snapshot["queue"] = plan_check_service.queue_settings()
        return jsonify(snapshot)


    @app.get("/api/accounts/<int:acc_id>/secret")
    def api_account_secret(acc_id: int):
        """按需读取单账号敏感值，避免账号列表一次性下发完整 Token/整行。"""
        field = str(request.args.get("field") or "").strip()
        acc = db.get_account(acc_id)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        try:
            value = _account_secret_value(acc, field)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "id": acc_id, "field": field, "value": value})

    @app.post("/api/accounts/secret-bulk")
    def api_accounts_secret_bulk():
        """按需批量读取账号敏感值。Body {account_ids:[...], field}."""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        field = str(data.get("field") or "").strip()
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 5000:
            return jsonify({"ok": False, "error": "单次最多读取 5000 个账号"}), 400
        values = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            try:
                value = _account_secret_value(acc, field)
            except ValueError as exc:
                return jsonify({"ok": False, "error": str(exc)}), 400
            if value:
                values.append({"id": acc_id, "email": acc.get("email"), "value": value})
            else:
                skipped.append({"id": acc_id, "email": acc.get("email"), "reason": "值为空"})
        return jsonify({"ok": True, "field": field, "values": values, "count": len(values), "skipped": skipped})

    @app.post("/api/accounts/<int:acc_id>/archive")
    def api_account_archive(acc_id: int):
        """归档/取消归档一个账号。Body {archived: true|false}。"""
        data = request.get_json(silent=True) or {}
        archived = bool(data.get("archived", True))
        updated = db.archive_account(acc_id=acc_id, archived=archived)
        if not updated:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        return jsonify({"ok": True, "updated": True, "id": acc_id, "archived": archived})

    @app.post("/api/accounts/archive-bulk")
    def api_accounts_archive_bulk():
        """批量归档/取消归档账号。Body {account_ids:[...], archived:true|false}。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        archived = bool(data.get("archived", True))
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 5000:
            return jsonify({"ok": False, "error": "单次最多归档 5000 个账号"}), 400
        account_ids = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            account_ids.append(acc_id)
        updated, db_skipped = db.archive_accounts(account_ids=account_ids, archived=archived)
        skipped.extend(db_skipped)
        return jsonify({"ok": True, "updated": updated, "updated_count": len(updated), "archived": archived, "skipped": skipped})

    @app.post("/api/accounts/<int:acc_id>/delete")
    def api_account_delete(acc_id: int):
        """删除一个已注册账号记录。只删除本地保存的账号/token记录，不改邮箱池状态。"""
        deleted = db.delete_account(acc_id=acc_id)
        if not deleted:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        return jsonify({"ok": True, "deleted": True})

    @app.post("/api/accounts/delete-bulk")
    def api_accounts_delete_bulk():
        """批量删除已注册账号记录。Body {account_ids: [...]} 或 {ids: [...]}。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 5000:
            return jsonify({"ok": False, "error": "单次最多删除 5000 个账号"}), 400
        account_ids = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            account_ids.append(acc_id)
        deleted, db_skipped = db.delete_accounts(account_ids=account_ids)
        skipped.extend(db_skipped)
        return jsonify({
            "ok": True,
            "deleted": deleted,
            "deleted_count": len(deleted),
            "skipped": skipped,
        })

    @app.post("/api/accounts/<int:acc_id>/note")
    def api_account_note(acc_id: int):
        """更新单个已注册账号备注。Body {note: "..."}，空字符串表示清空。"""
        data = request.get_json(silent=True) or {}
        note = str(data.get("note") or "")
        if len(note) > 2000:
            return jsonify({"ok": False, "error": "备注最多 2000 个字符"}), 400
        updated = db.update_account_note(acc_id=acc_id, note=note)
        if not updated:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        return jsonify({"ok": True, "updated": True, "id": acc_id, "note": note})

    @app.post("/api/accounts/note-bulk")
    def api_accounts_note_bulk():
        """批量更新已注册账号备注。Body {account_ids: [...], note: "..."}，空字符串表示清空。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        note = str(data.get("note") or "")
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 5000:
            return jsonify({"ok": False, "error": "单次最多备注 5000 个账号"}), 400
        if len(note) > 2000:
            return jsonify({"ok": False, "error": "备注最多 2000 个字符"}), 400

        account_ids = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            account_ids.append(acc_id)
        updated, db_skipped = db.update_accounts_note(account_ids=account_ids, note=note)
        skipped.extend(db_skipped)
        return jsonify({
            "ok": True,
            "updated": updated,
            "updated_count": len(updated),
            "skipped": skipped,
            "skipped_count": len(skipped),
        })

    @app.post("/api/accounts/check-live-bulk")
    def api_accounts_check_live_bulk():
        """批量查活：加入后台队列；协议 BrowserSession 指纹环境重新登录并刷新最新 AT。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多查活 500 个账号"}), 400

        account_ids: list[int] = []
        skipped: list[dict] = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            account_ids.append(acc_id)

        accounts = []
        for acc_id in account_ids:
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            email = str(acc.get("email") or "").strip()
            if not email:
                skipped.append({"id": acc_id, "reason": "邮箱为空"})
                continue
            accounts.append(acc)

        started = []
        busy_count = 0
        failed = []
        for acc in accounts:
            acc_id = int(acc.get("id") or 0)
            email = str(acc.get("email") or "")
            queued = live_check_service.enqueue_account_live_check(
                account_id=acc_id,
                email=email,
                trigger="manual",
                # 查活按“查套餐”同一套网络选路：
                # PLAN_CHECK_PROXY_MODE / PLAN_CHECK_PROXY / PROXY_POOL。
                # 不复用账号注册时的 proxy_used，避免旧注册出口被 CF 403 后一直失败。
                proxy=None,
            )
            if queued.get("accepted"):
                started.append({"id": acc_id, "email": email, "status": "queued"})
            elif queued.get("busy"):
                busy_count += 1
                skipped.append({"id": acc_id, "email": email, "reason": queued.get("error") or "正在查活"})
            else:
                failed.append({"id": acc_id, "email": email, "error": queued.get("error") or "入队失败"})

        return jsonify({
            "ok": True,
            "message": f"已入队 {len(started)} 个查活任务",
            "started": started,
            "started_count": len(started),
            "busy_count": busy_count,
            "failed": failed,
            "failed_count": len(failed),
            "skipped": skipped,
            "queue": live_check_service.queue_settings(),
        }), 202


    @app.post("/api/accounts/check-plan")
    def api_account_check_plan():
        """把单账号套餐查询加入后台队列。Body {account_id|email, proxy?, timezone_offset_min?}"""
        data = request.get_json(silent=True) or {}
        acc_id = data.get("account_id") or data.get("id")
        email = (data.get("email") or "").strip()
        acc = None
        if acc_id is not None:
            try:
                acc = db.get_account(int(acc_id))
            except Exception:
                acc = None
        if acc is None and email:
            acc = db.get_account_by_email(email)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        token = (acc.get("access_token") or "").strip()
        if not token:
            return jsonify({"ok": False, "error": "该账号没有 access_token"}), 400
        account_id = int(acc.get("id"))
        queued = plan_check_service.enqueue_account_plan_check(
            account_id=account_id,
            email=acc.get("email") or "",
            access_token=token,
            trigger="manual",
            proxy=data.get("proxy") if "proxy" in data else None,
            timezone_offset_min=str(data.get("timezone_offset_min") or "-"),
        )
        if queued.get("busy"):
            return jsonify({"ok": False, **queued}), 409
        if not queued.get("accepted"):
            return jsonify({"ok": False, **queued}), 503
        return jsonify({"ok": True, "started": True, **queued}), 202

    @app.post("/api/accounts/check-plan-bulk")
    def api_accounts_check_plan_bulk():
        """批量把套餐查询加入统一后台队列。Body {account_ids:[...], proxy?, timezone_offset_min?}"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多查询 500 个账号"}), 400
        # 与单账号查询保持一致：未传时使用独立网络策略。
        proxy = data.get("proxy") if "proxy" in data else None
        timezone_offset_min = str(data.get("timezone_offset_min") or "-")

        items = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except Exception:
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            if not (acc.get("access_token") or "").strip():
                skipped.append({"id": acc_id, "email": acc.get("email"), "reason": "缺少 access_token"})
                continue
            items.append(acc)

        started = []
        busy = []
        failed = []
        for acc in items:
            queued = plan_check_service.enqueue_account_plan_check(
                account_id=int(acc.get("id")),
                email=acc.get("email") or "",
                access_token=acc.get("access_token") or "",
                trigger="manual_bulk",
                proxy=proxy,
                timezone_offset_min=timezone_offset_min,
            )
            item = {"id": acc.get("id"), "email": acc.get("email"), **queued}
            if queued.get("accepted"):
                started.append(item)
            elif queued.get("busy"):
                busy.append(item)
            else:
                failed.append(item)
        return jsonify({
            "ok": True,
            "started": started,
            "started_count": len(started),
            "busy": busy,
            "busy_count": len(busy),
            "failed": failed,
            "failed_count": len(failed),
            "skipped": skipped,
            "skipped_count": len(skipped),
        }), 202

    @app.get("/api/extract-link/cdk")
    def api_extract_link_cdk():
        """查询当前配置或传入 CDK 的剩余次数。"""
        code = (request.args.get("code") or "").strip() or None
        try:
            return jsonify({"ok": True, **extract_link_service.query_cdk(cdk=code)})
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400

    def _is_extract_eligible(acc: dict) -> bool:
        plan = str(acc.get("current_plan_type") or acc.get("plan_type") or "").lower()
        return plan == "free" and bool(acc.get("plus_trial_eligible"))

    @app.post("/api/accounts/extract-link")
    def api_account_extract_link():
        """单账号提链。Body {account_id|id, link_type?, cdk?}。"""
        data = request.get_json(silent=True) or {}
        acc_id = data.get("account_id") or data.get("id")
        try:
            acc = db.get_account(int(acc_id))
        except Exception:
            acc = None
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        if not _is_extract_eligible(acc):
            return jsonify({"ok": False, "error": "仅支持 free(可Plus试用) 账号提链；请先查询套餐确认资格"}), 400
        token = (acc.get("access_token") or "").strip()
        if not token:
            return jsonify({"ok": False, "error": "该账号没有 access_token"}), 400
        try:
            queued = extract_link_service.enqueue_account_extract(
                account_id=int(acc.get("id")),
                email=acc.get("email") or "",
                access_token=token,
                trigger="manual",
                link_type=data.get("link_type"),
                cdk=data.get("cdk"),
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400
        if queued.get("busy"):
            return jsonify({"ok": False, **queued}), 409
        if not queued.get("accepted"):
            return jsonify({"ok": False, **queued}), 503
        return jsonify({"ok": True, "started": True, **{k: v for k, v in queued.items() if k != "future"}}), 202

    @app.post("/api/accounts/extract-link-bulk")
    def api_accounts_extract_link_bulk():
        """批量提链。Body {account_ids:[...], link_type?, cdk?}。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多提链 500 个账号"}), 400

        started = []
        busy = []
        failed = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except Exception:
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            email = acc.get("email")
            if not _is_extract_eligible(acc):
                skipped.append({"id": acc_id, "email": email, "reason": "不是 free(可Plus试用)"})
                continue
            token = (acc.get("access_token") or "").strip()
            if not token:
                skipped.append({"id": acc_id, "email": email, "reason": "缺少 access_token"})
                continue
            try:
                queued = extract_link_service.enqueue_account_extract(
                    account_id=acc_id,
                    email=email or "",
                    access_token=token,
                    trigger="manual_bulk",
                    link_type=data.get("link_type"),
                    cdk=data.get("cdk"),
                )
            except Exception as exc:
                failed.append({"id": acc_id, "email": email, "error": f"{type(exc).__name__}: {exc}"})
                continue
            item = {"id": acc_id, "email": email, **{k: v for k, v in queued.items() if k != "future"}}
            if queued.get("accepted"):
                started.append(item)
            elif queued.get("busy"):
                busy.append(item)
            else:
                failed.append(item)
        return jsonify({
            "ok": True,
            "started": started,
            "started_count": len(started),
            "busy": busy,
            "busy_count": len(busy),
            "failed": failed,
            "failed_count": len(failed),
            "skipped": skipped,
            "skipped_count": len(skipped),
        }), 202

    @app.post("/api/accounts/codex-agent")
    def api_account_codex_agent():
        """单账号生成 Codex Agent Token。Body {account_id|id, verify_task?}。"""
        data = request.get_json(silent=True) or {}
        acc_id = data.get("account_id") or data.get("id")
        try:
            acc = db.get_account(int(acc_id))
        except Exception:
            acc = None
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        token = (acc.get("access_token") or "").strip()
        if not token:
            return jsonify({"ok": False, "error": "该账号没有 access_token"}), 400
        try:
            queued = codex_agent_service.enqueue_account_codex_agent(
                account_id=int(acc.get("id")),
                email=acc.get("email") or "",
                access_token=token,
                trigger="manual",
                verify_task=bool(data.get("verify_task", True)),
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400
        if queued.get("busy"):
            return jsonify({"ok": False, **queued}), 409
        if not queued.get("accepted"):
            return jsonify({"ok": False, **queued}), 503
        return jsonify({"ok": True, "started": True, **{k: v for k, v in queued.items() if k != "future"}}), 202

    @app.post("/api/accounts/codex-agent-bulk")
    def api_accounts_codex_agent_bulk():
        """批量生成 Codex Agent Token。Body {account_ids:[...], verify_task?}。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多提交 500 个账号"}), 400

        started = []
        busy = []
        failed = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except Exception:
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            email = acc.get("email")
            token = (acc.get("access_token") or "").strip()
            if not token:
                skipped.append({"id": acc_id, "email": email, "reason": "缺少 access_token"})
                continue
            try:
                queued = codex_agent_service.enqueue_account_codex_agent(
                    account_id=acc_id,
                    email=email or "",
                    access_token=token,
                    trigger="manual_bulk",
                    verify_task=bool(data.get("verify_task", True)),
                )
            except Exception as exc:
                failed.append({"id": acc_id, "email": email, "error": f"{type(exc).__name__}: {exc}"})
                continue
            item = {"id": acc_id, "email": email, **{k: v for k, v in queued.items() if k != "future"}}
            if queued.get("accepted"):
                started.append(item)
            elif queued.get("busy"):
                busy.append(item)
            else:
                failed.append(item)
        return jsonify({
            "ok": True,
            "started": started,
            "started_count": len(started),
            "busy": busy,
            "busy_count": len(busy),
            "failed": failed,
            "failed_count": len(failed),
            "skipped": skipped,
            "skipped_count": len(skipped),
        }), 202

    def _codex_agent_auth_for_account(acc: dict) -> tuple[str, str]:
        """返回账号已生成的 Codex Agent auth.json 文本与下载文件名。"""
        import json as _json
        from pathlib import Path as _Path

        email = str(acc.get("email") or "").strip()
        safe_email = "".join(ch if ch.isalnum() or ch in ("@", ".", "-", "_") else "_" for ch in (email or f"account-{acc.get('id')}"))
        filename = f"codex-agent-{safe_email}.json"
        token_text = str(acc.get("codex_agent_token") or "").strip()
        if token_text:
            try:
                payload = _json.loads(token_text)
                token_text = _json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
            except Exception:
                token_text = token_text + ("\n" if not token_text.endswith("\n") else "")
            return token_text, filename

        auth_path = str(acc.get("codex_agent_auth_path") or "").strip()
        if auth_path:
            p = _Path(auth_path)
            if sqlite_store.file_exists(p, category="agent_credentials"):
                return sqlite_store.read_text_file(p, category="agent_credentials"), p.name or filename

        raise RuntimeError("该账号还没有生成 Codex Agent Token")

    def _join_sub2_url(base: str, path: str) -> str:
        base = str(base or "").strip().rstrip("/")
        path = str(path or "").strip()
        if not base or not path:
            return ""
        parsed = urlparse(path)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            return path
        return f"{base}/{path.lstrip('/')}"

    def _sub2_codex_session_import_url() -> str:
        from config import sub2api as sub2api_cfg
        api_base = str(getattr(sub2api_cfg, "SUB2API_API_BASE", "") or "").strip()
        if api_base:
            return _join_sub2_url(api_base, "/api/v1/admin/accounts/import/codex-session")
        # 兼容旧配置：之前 SUB2API_API_URL 是完整上传接口 URL。
        return str(getattr(sub2api_cfg, "SUB2API_API_URL", "") or "").strip()

    def _upload_account_codex_agent_to_sub2(acc: dict) -> dict:
        """把账号已生成的 Codex Agent auth.json 上传到 sub2api。"""
        import json as _json
        from config import sub2api as sub2api_cfg
        from core.codex_agent import upload_sub2api_account

        text, _filename = _codex_agent_auth_for_account(acc)
        try:
            auth_json = _json.loads(text)
        except Exception as exc:
            raise RuntimeError(f"Agent Token JSON 无效: {exc}") from exc

        api_url = _sub2_codex_session_import_url()
        api_token = str(getattr(sub2api_cfg, "SUB2API_API_KEY", "") or getattr(sub2api_cfg, "SUB2API_API_TOKEN", "") or "").strip()
        auth_header = str(getattr(sub2api_cfg, "SUB2API_API_AUTH_HEADER", "x-api-key") or "x-api-key").strip()
        auth_prefix = str(getattr(sub2api_cfg, "SUB2API_API_AUTH_PREFIX", "") or "").strip()
        payload_mode = "codex_session_import"
        proxy_key = str(getattr(sub2api_cfg, "SUB2API_PROXY_KEY", "") or "").strip() or None
        timeout = float(getattr(sub2api_cfg, "SUB2API_API_TIMEOUT", 20) or 20)

        result = upload_sub2api_account(
            auth_json,
            api_url,
            api_token=api_token,
            auth_header=auth_header,
            auth_prefix=auth_prefix,
            payload_mode=payload_mode,
            proxy_key=proxy_key,
            timeout=timeout,
        )
        try:
            db.update_account_codex_agent(int(acc.get("id")), {
                "ok": True,
                "status": "success",
                "message": "Agent Token 已上传 sub2api",
                "sub2api_url": result.get("url"),
                "sub2api_mode": result.get("payload_mode"),
                "sub2api_total": result.get("total"),
            })
        except Exception:
            logger.exception("更新账号 sub2api 上传状态失败: account_id=%s", acc.get("id"))
        return result

    @app.post("/api/accounts/<int:acc_id>/codex-agent/upload-sub2")
    def api_account_codex_agent_upload_sub2(acc_id: int):
        """单账号把已生成的 Codex Agent Token 上传到 sub2api。"""
        acc = db.get_account(acc_id)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        try:
            result = _upload_account_codex_agent_to_sub2(acc)
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400
        return jsonify({"ok": True, "account_id": acc_id, "email": acc.get("email"), "result": result})

    @app.post("/api/accounts/codex-agent/upload-sub2-bulk")
    def api_accounts_codex_agent_upload_sub2_bulk():
        """批量把已生成的 Codex Agent Token 上传到 sub2api。Body {account_ids:[...]}。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多提交 500 个账号"}), 400

        uploaded, failed, skipped = [], [], []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except Exception:
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            email = acc.get("email")
            if (acc.get("codex_agent_status") or "") != "success" and not (acc.get("codex_agent_token") or acc.get("codex_agent_auth_path")):
                skipped.append({"id": acc_id, "email": email, "reason": "未生成 Agent Token"})
                continue
            try:
                result = _upload_account_codex_agent_to_sub2(acc)
                uploaded.append({"id": acc_id, "email": email, "url": result.get("url"), "status_code": result.get("status_code")})
            except Exception as exc:
                failed.append({"id": acc_id, "email": email, "error": f"{type(exc).__name__}: {exc}"})
        return jsonify({
            "ok": True,
            "uploaded": uploaded,
            "uploaded_count": len(uploaded),
            "failed": failed,
            "failed_count": len(failed),
            "skipped": skipped,
            "skipped_count": len(skipped),
        })

    @app.get("/api/accounts/<int:acc_id>/codex-agent/download")
    def api_account_codex_agent_download(acc_id: int):
        """下载单个账号的 Codex Agent auth.json。"""
        acc = db.get_account(acc_id)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        try:
            content, filename = _codex_agent_auth_for_account(acc)
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 404
        data = content.encode("utf-8")
        return Response(
            data,
            mimetype="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(len(data)),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/api/accounts/codex-agent/download-bulk")
    def api_accounts_codex_agent_download_bulk():
        """下载选中账号已生成的 Codex Agent Token，打包 ZIP。"""
        import io
        import json as _json
        import zipfile
        from datetime import datetime as _dt

        data = request.get_json(silent=True) or {}
        if not data and request.form:
            ids_text = (request.form.get("account_ids") or request.form.get("ids") or "").strip()
            try:
                ids = _json.loads(ids_text) if ids_text else []
            except Exception:
                ids = [x.strip() for x in ids_text.split(",") if x.strip()]
        else:
            ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 1000:
            return jsonify({"ok": False, "error": "单次最多下载 1000 个账号"}), 400

        added = []
        errors = []
        used_names = set()
        seen = set()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for raw in ids:
                try:
                    acc_id = int(raw)
                except Exception:
                    errors.append({"id": raw, "error": "ID 非法"})
                    continue
                if acc_id in seen:
                    continue
                seen.add(acc_id)
                acc = db.get_account(acc_id)
                if not acc:
                    errors.append({"id": acc_id, "error": "账号不存在"})
                    continue
                try:
                    content, filename = _codex_agent_auth_for_account(acc)
                    arcname = filename
                    if arcname in used_names:
                        stem, dot, ext = arcname.rpartition(".")
                        arcname = f"{stem or arcname}-{len(used_names)+1}{dot}{ext}" if dot else f"{arcname}-{len(used_names)+1}"
                    used_names.add(arcname)
                    zf.writestr(arcname, content)
                    added.append({"id": acc_id, "email": acc.get("email"), "filename": arcname})
                except Exception as exc:
                    errors.append({"id": acc_id, "email": acc.get("email"), "error": f"{type(exc).__name__}: {exc}"})
            manifest = {
                "exported_at": _dt.now().isoformat(timespec="seconds"),
                "source": "accounts-codex-agent",
                "count": len(added),
                "files": added,
                "errors": errors,
            }
            zf.writestr("manifest.json", _json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

        if not added:
            return jsonify({"ok": False, "error": "没有可下载的 Codex Agent Token", "errors": errors}), 404
        now = _dt.now()
        dl_name = f"accounts-codex-agent-{now.strftime('%Y%m%d-%H%M%S')}.zip"
        buf.seek(0)
        zip_bytes = buf.getvalue()
        return Response(
            zip_bytes,
            mimetype="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{dl_name}"',
                "Content-Length": str(len(zip_bytes)),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/api/accounts/download-cpa-bulk")
    def api_accounts_download_cpa_bulk():
        """
        从账号列表选中的账号直接到 CPA auth-files 下载 Codex CPA JSON，并打包为 ZIP。
        Body: {"account_ids": [1,2,...]} 或 {"ids": [...]}
        """
        import io
        import json as _json
        import zipfile
        from datetime import datetime as _dt
        from core.codex_oauth import download_cpa_codex_auth_text, list_cpa_codex_auth_files

        data = request.get_json(silent=True) or {}
        if not data and request.form:
            ids_text = (request.form.get("account_ids") or request.form.get("ids") or "").strip()
            try:
                ids = _json.loads(ids_text) if ids_text else []
            except Exception:
                ids = [x.strip() for x in ids_text.split(",") if x.strip()]
        else:
            ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 1000:
            return jsonify({"ok": False, "error": "单次最多下载 1000 个账号"}), 400

        try:
            cpa_files = list_cpa_codex_auth_files()
        except Exception as exc:
            return jsonify({"ok": False, "error": f"读取 CPA auth-files 失败: {type(exc).__name__}: {exc}"}), 502

        def _match_cpa_file(email: str, local_filename: str = "") -> dict | None:
            """在已缓存的 CPA 文件列表中匹配，避免每个账号都重新请求 auth-files。"""
            email_l = str(email or "").strip().lower()
            local_name_l = str(local_filename or "").strip().lower()
            local_stem_l = local_name_l[:-5] if local_name_l.endswith(".json") else local_name_l

            def score(item: dict) -> int:
                name_l = str(item.get("name") or "").lower()
                item_email_l = str(item.get("email") or "").lower()
                s = 0
                if local_name_l and name_l == local_name_l:
                    s = max(s, 100)
                if local_stem_l and name_l.startswith(local_stem_l):
                    s = max(s, 80)
                if email_l and item_email_l == email_l:
                    s = max(s, 70)
                if email_l and email_l in name_l:
                    s = max(s, 60)
                if local_stem_l.endswith("-cpa-callback"):
                    base = local_stem_l[:-len("-cpa-callback")]
                    if base and name_l.startswith(base + "-"):
                        s = max(s, 75)
                return s

            ranked = sorted(((score(item), item) for item in cpa_files), key=lambda x: x[0], reverse=True)
            return ranked[0][1] if ranked and ranked[0][0] > 0 else None

        # 建立 email -> 本地 codex 文件名索引；有本地文件名时传给 CPA 匹配逻辑可提升命中率。
        local_by_email: dict[str, str] = {}
        try:
            for item in db.list_codex_accounts():
                email_key = str(item.get("email") or "").strip().lower()
                fname = str(item.get("filename") or "").strip()
                if email_key and fname and email_key not in local_by_email:
                    local_by_email[email_key] = fname
        except Exception:
            local_by_email = {}

        errors = []
        added = []
        used_names = set()
        seen_ids = set()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for raw_id in ids:
                try:
                    acc_id = int(raw_id)
                except (TypeError, ValueError):
                    errors.append({"id": raw_id, "error": "ID 非法"})
                    continue
                if acc_id in seen_ids:
                    continue
                seen_ids.add(acc_id)

                acc = db.get_account(acc_id)
                if not acc:
                    errors.append({"id": acc_id, "error": "账号不存在"})
                    continue
                email = str(acc.get("email") or "").strip()
                if not email:
                    errors.append({"id": acc_id, "error": "账号缺少 email"})
                    continue

                local_filename = local_by_email.get(email.lower(), "")
                try:
                    meta = _match_cpa_file(email=email, local_filename=local_filename)
                    cpa_name_hint = str((meta or {}).get("name") or "").strip()
                    if not cpa_name_hint:
                        raise RuntimeError(f"[Codex][CPA] 未在 CPA auth-files 中找到匹配的 Codex 凭证: {email}")
                    cpa_text, cpa_name, meta = download_cpa_codex_auth_text(
                        cpa_name=cpa_name_hint,
                    )
                    arcname = cpa_name
                    if arcname in used_names:
                        stem, dot, ext = arcname.rpartition(".")
                        arcname = f"{stem or arcname}-{len(used_names)+1}{dot}{ext}" if dot else f"{arcname}-{len(used_names)+1}"
                    used_names.add(arcname)
                    zf.writestr(arcname, cpa_text)
                    added.append({
                        "id": acc_id,
                        "email": email,
                        "local_filename": local_filename,
                        "cpa_filename": cpa_name,
                        "cpa_meta": meta,
                    })
                    if local_filename:
                        try:
                            db.mark_codex_exported(local_filename)
                        except Exception:
                            pass
                except Exception as exc:
                    errors.append({"id": acc_id, "email": email, "error": f"{type(exc).__name__}: {exc}"})

            manifest = {
                "exported_at": _dt.now().isoformat(timespec="seconds"),
                "source": "accounts-cpa",
                "count": len(added),
                "files": added,
                "errors": errors,
            }
            zf.writestr("manifest.json", _json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

        if not added:
            return jsonify({"ok": False, "error": "没有成功从 CPA 下载任何凭证", "errors": errors}), 502
        now = _dt.now()
        dl_name = f"accounts-cpa-bulk-{now.strftime('%Y%m%d-%H%M%S')}.zip"
        buf.seek(0)
        zip_bytes = buf.getvalue()
        if isinstance(data, dict) and data.get("prepare"):
            download_id = _put_prepared_download(zip_bytes, dl_name, "application/zip")
            return jsonify({
                "ok": True,
                "prepared": True,
                "download_id": download_id,
                "download_url": f"/api/downloads/{download_id}",
                "filename": dl_name,
                "added_count": len(added),
                "error_count": len(errors),
            })
        return Response(
            zip_bytes,
            mimetype="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{dl_name}"',
                "Content-Length": str(len(zip_bytes)),
                "Cache-Control": "no-store, max-age=0",
                "Pragma": "no-cache",
                "X-Content-Type-Options": "nosniff",
                "X-Download-Options": "noopen",
            },
        )

    # ----------------------------------------------------------
    # 邮箱池
    # ----------------------------------------------------------
    @app.get("/api/outlook")
    def api_outlook():
        status = request.args.get("status") or None
        limit = request.args.get("limit", default=500, type=int)
        source = _pool_source_arg()
        q = str(request.args.get("q", default="") or "").strip()
        paged = str(request.args.get("paged", default="") or "").lower() in {"1", "true", "yes"}
        page_arg = request.args.get("page", default=None, type=int)
        page_size_arg = request.args.get("page_size", default=None, type=int)
        fetch_limit = 1_000_000 if (paged or q) else limit
        if source == "all":
            rows = []
            rows += _with_pool_source(db.list_outlook_pool(status=status, limit=fetch_limit), "outlook")
            rows += _with_pool_source(db.list_generic_api_email_pool(status=status, limit=fetch_limit), "generic_api")
            rows += _with_pool_source(db.list_domain_email_pool(status=status, limit=fetch_limit), "cloudflare_domain")
            rows = sorted(rows, key=lambda x: str(x.get("created_at") or x.get("imported_at") or x.get("used_at") or ""), reverse=True)
        elif source == "generic_api":
            rows = _with_pool_source(db.list_generic_api_email_pool(status=status, limit=fetch_limit), "generic_api")
        elif source == "cloudflare_domain":
            rows = _with_pool_source(db.list_domain_email_pool(status=status, limit=fetch_limit), "cloudflare_domain")
        else:
            rows = _with_pool_source(db.list_outlook_pool(status=status, limit=fetch_limit), "outlook")
        if q:
            rows = [r for r in rows if _matches_query(r, q)]
        if paged or page_arg is not None or page_size_arg is not None:
            page = max(1, int(page_arg or 1))
            page_size = max(1, min(500, int(page_size_arg or limit or 50)))
            return jsonify(_paginate_items(rows, page=page, page_size=page_size))
        return jsonify(rows[:limit])

    @app.post("/api/outlook/import")
    def api_outlook_import():
        """
        粘贴文本导入邮箱素材。
        Outlook：email----password----clientId----refreshToken
        通用 API：按内容识别 email 与 code_url。
        分隔符读取 EMAIL_IMPORT_SEPARATORS 配置。
        """
        data = request.get_json(silent=True) or {}
        source = (data.get("source") or data.get("type") or "").strip()
        if source not in ("outlook", "generic_api"):
            return jsonify({"ok": False, "error": "导入时请选择具体类型：Outlook 或 通用 API"}), 400
        text = data.get("text") or ""
        as_registered = bool(data.get("as_registered", False))
        records = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if source == "generic_api":
                legacy_parts = split_import_line(line, max_fields=4)
                legacy_prefix = (
                    3 <= len(legacy_parts) <= 4
                    and is_email(legacy_parts[0])
                    and is_http_url(legacy_parts[1])
                )
                legacy_shape = (
                    legacy_prefix
                    and looks_like_access_token(legacy_parts[2])
                    and (len(legacy_parts) == 3 or looks_like_totp(legacy_parts[3]))
                )
                if legacy_shape:
                    # Former fixed-position API: email, code_url,
                    # access_token, optional totp_secret.
                    record = {
                        "email": clean_import_value(legacy_parts[0]).lower(),
                        "code_url": clean_import_value(legacy_parts[1]),
                        "access_token": clean_import_value(legacy_parts[2]),
                    }
                    if len(legacy_parts) == 4:
                        record["totp_secret"] = clean_import_value(legacy_parts[3])
                    records.append(record)
                    continue

                material = parse_account_material_line(line)
                if not material or not material.get("email_code_url"):
                    continue
                if not is_email(material.get("email")):
                    continue
                records.append({
                    "email": material["email"],
                    "code_url": material["email_code_url"],
                    "totp_secret": material.get("totp_secret") or "",
                })
                continue
            parts = split_import_line(line, max_fields=6)
            if len(parts) < 4:
                continue
            records.append({
                "email": clean_import_value(parts[0]),
                "password": clean_import_value(parts[1]),
                "client_id": clean_import_value(parts[2]),
                "refresh_token": clean_import_value(parts[3]),
                "access_token": clean_import_value(parts[4]) if len(parts) > 4 else "",
                "totp_secret": clean_import_value(parts[5]) if len(parts) > 5 else "",
            })
        if not records:
            need = "2 段：邮箱----取码地址" if source == "generic_api" else "4 段：email----password----clientId----refreshToken"
            return jsonify({"ok": False, "error": f"未解析到有效邮箱行（需 {need}，支持 ---、----、| 或 ==== 分隔）"}), 400
        if as_registered:
            inserted, skipped = db.import_registered_email_accounts(records, source=source)
        elif source == "generic_api":
            inserted, skipped = db.import_generic_api_emails(records)
        else:
            inserted, skipped = db.import_outlook_accounts(records)
        return jsonify({
            "ok": True,
            "inserted": inserted,
            "skipped": skipped,
            "parsed": len(records),
            "as_registered": as_registered,
        })

    @app.post("/api/outlook/status")
    def api_outlook_status():
        """手动改邮箱状态：body {email, status, note?, source?}。status ∈ available/used/failed/disabled。"""
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        status = (data.get("status") or "").strip()
        if not email or status not in ("available", "used", "failed", "disabled"):
            return jsonify({"ok": False, "error": "email 或 status 非法"}), 400
        source = (data.get("source") or _pool_source_arg()).strip()
        if source == "all":
            source = "outlook"
        if source == "generic_api":
            db.release_generic_api_email(email, status=status, note=data.get("note"))
        elif source == "cloudflare_domain":
            db.release_domain_email(email, status=status, note=data.get("note"))
        else:
            db.release_outlook(email, status=status, note=data.get("note"))
        return jsonify({"ok": True})

    @app.post("/api/outlook/status-bulk")
    def api_outlook_status_bulk():
        """批量修改邮箱状态。Body {items:[{email,source}], status, note?}。"""
        data = request.get_json(silent=True) or {}
        items = data.get("items") or data.get("emails") or []
        status = (data.get("status") or "").strip()
        note = data.get("note")
        default_source = (data.get("source") or _pool_source_arg()).strip()
        if status not in ("available", "used", "failed", "disabled"):
            return jsonify({"ok": False, "error": "status 非法"}), 400
        if not isinstance(items, list) or not items:
            return jsonify({"ok": False, "error": "items/emails 必须是非空数组"}), 400
        if len(items) > 5000:
            return jsonify({"ok": False, "error": "单次最多操作 5000 个邮箱"}), 400

        updated = []
        skipped = []
        seen = set()
        for raw_item in items:
            if isinstance(raw_item, dict):
                email = (str(raw_item.get("email") or "")).strip()
                item_source = (raw_item.get("source") or default_source or "outlook").strip()
            else:
                email = (str(raw_item or "")).strip()
                item_source = default_source
            if item_source == "all":
                item_source = "outlook"
            key = f"{item_source}:{email.lower()}"
            if not email:
                skipped.append({"email": raw_item, "reason": "邮箱为空"})
                continue
            if key in seen:
                continue
            seen.add(key)
            try:
                if item_source == "generic_api":
                    db.release_generic_api_email(email, status=status, note=note)
                elif item_source == "cloudflare_domain":
                    db.release_domain_email(email, status=status, note=note)
                else:
                    db.release_outlook(email, status=status, note=note)
                updated.append({"email": email, "source": item_source, "status": status})
            except Exception as exc:
                skipped.append({"email": email, "source": item_source, "reason": f"{type(exc).__name__}: {exc}"})
        return jsonify({
            "ok": True,
            "updated": updated,
            "updated_count": len(updated),
            "skipped": skipped,
        })

    @app.post("/api/outlook/delete")
    def api_outlook_delete():
        """从邮箱池彻底删除一个邮箱：body {email}。"""
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        source = (data.get("source") or _pool_source_arg()).strip()
        if source == "all":
            source = "outlook"
        deleted = (
            db.delete_generic_api_email(email)
            if source == "generic_api"
            else db.delete_domain_email(email)
            if source == "cloudflare_domain"
            else db.delete_outlook(email)
        )
        return jsonify({"ok": True, "deleted": deleted})

    @app.post("/api/outlook/delete-bulk")
    def api_outlook_delete_bulk():
        """从邮箱池批量彻底删除邮箱：body {emails: [...]}。"""
        data = request.get_json(silent=True) or {}
        source = _pool_source_arg()
        emails = data.get("items") or data.get("emails") or []
        if not isinstance(emails, list) or not emails:
            return jsonify({"ok": False, "error": "emails/items 必须是非空数组"}), 400
        if len(emails) > 5000:
            return jsonify({"ok": False, "error": "单次最多删除 5000 个邮箱"}), 400

        deleted: list[str] = []
        skipped: list[dict] = []
        seen: set[str] = set()
        for raw_item in emails:
            if isinstance(raw_item, dict):
                email = (str(raw_item.get("email") or "")).strip()
                item_source = (raw_item.get("source") or source or "outlook").strip()
            else:
                email = (str(raw_item or "")).strip()
                item_source = source
            if item_source == "all":
                item_source = "outlook"
            key = f"{item_source}:{email.lower()}"
            if not email:
                skipped.append({"email": raw_item, "reason": "邮箱为空"})
                continue
            if key in seen:
                continue
            seen.add(key)
            deleted_ok = (
                db.delete_generic_api_email(email)
                if item_source == "generic_api"
                else db.delete_domain_email(email)
                if item_source == "cloudflare_domain"
                else db.delete_outlook(email)
            )
            if deleted_ok:
                deleted.append({"email": email, "source": item_source})
            else:
                skipped.append({"email": email, "reason": "邮箱不存在"})

        return jsonify({
            "ok": True,
            "deleted": deleted,
            "deleted_count": len(deleted),
            "skipped": skipped,
        })

    # ----------------------------------------------------------
    # 域名邮箱池（Cloudflare 域名邮箱模式）
    # ----------------------------------------------------------
    @app.get("/api/domain-pool")
    def api_domain_pool():
        status = request.args.get("status") or None
        limit = request.args.get("limit", default=500, type=int)
        return jsonify(db.list_domain_email_pool(status=status, limit=limit))

    @app.post("/api/domain-pool/status")
    def api_domain_pool_status():
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        status = (data.get("status") or "").strip()
        if not email or status not in ("available", "used", "failed"):
            return jsonify({"ok": False, "error": "email 或 status 非法"}), 400
        db.release_domain_email(email, status=status, note=data.get("note"))
        return jsonify({"ok": True})

    @app.post("/api/domain-pool/delete")
    def api_domain_pool_delete():
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        deleted = db.delete_domain_email(email)
        return jsonify({"ok": True, "deleted": deleted})

    # ----------------------------------------------------------
    # Codex 授权账号（CPA 兼容凭证）
    # ----------------------------------------------------------
    @app.get("/api/codex")
    def api_codex_list():
        rows = db.list_codex_accounts()
        q = str(request.args.get("q", default="") or "").strip()
        if q:
            rows = [r for r in rows if _matches_query(r, q)]
        limit = request.args.get("limit", default=500, type=int)
        paged = str(request.args.get("paged", default="") or "").lower() in {"1", "true", "yes"}
        page_arg = request.args.get("page", default=None, type=int)
        page_size_arg = request.args.get("page_size", default=None, type=int)
        if paged or page_arg is not None or page_size_arg is not None:
            page = max(1, int(page_arg or 1))
            page_size = max(1, min(500, int(page_size_arg or limit or 50)))
            result = _paginate_items(rows, page=page, page_size=page_size)
            result["accounts"] = result.pop("items")
            result["summary"] = db.codex_accounts_summary()
            return jsonify(result)
        return jsonify({
            "summary": db.codex_accounts_summary(),
            "accounts": rows[:limit],
        })

    @app.get("/api/codex/download/<path:filename>")
    def api_codex_download(filename: str):
        """
        下载一个 CPA 兼容的 codex-*.json 文件，下载即标记为已导出（计数+1）。
        前端通过浏览器原生下载触发（a 标签 / window.location）。
        """
        try:
            content, fname = db.read_codex_credential(filename)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        db.mark_codex_exported(fname)
        return Response(
            content,
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    @app.get("/api/codex/download-from-cpa/<path:filename>")
    def api_codex_download_from_cpa(filename: str):
        """按本地 codex 文件/回执匹配 CPA auth-files，并从 CPA 下载实际 Codex JSON。"""
        try:
            content, fname = db.read_codex_credential(filename)
            import json as _json
            try:
                local = _json.loads(content)
            except Exception:
                local = {}
            email = str(local.get("email") or "").strip()
            from core.codex_oauth import download_cpa_codex_auth_text
            cpa_text, cpa_name, _meta = download_cpa_codex_auth_text(email=email, local_filename=fname)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 502
        db.mark_codex_exported(fname)
        return Response(
            cpa_text,
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{cpa_name}"'},
        )

    @app.post("/api/codex/download-bulk-from-cpa")
    def api_codex_download_bulk_from_cpa():
        """
        批量从 CPA 下载选中的 Codex 凭证，打包成 zip；zip 内每个文件都是 CPA 原始 JSON。
        Body: {"filenames": ["codex-xxx-cpa-callback.json", ...]}
        """
        import io
        import json as _json
        import zipfile
        from datetime import datetime as _dt
        from core.codex_oauth import download_cpa_codex_auth_text

        data = request.get_json(silent=True) or {}
        filenames = data.get("filenames") or []
        if not isinstance(filenames, list) or not filenames:
            return jsonify({"ok": False, "error": "filenames 必须是非空数组"}), 400
        if len(filenames) > 1000:
            return jsonify({"ok": False, "error": "单次最多 1000 个"}), 400

        errors = []
        added = []
        used_names = set()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for fname in filenames:
                if not isinstance(fname, str):
                    errors.append({"filename": str(fname), "error": "非字符串"})
                    continue
                try:
                    content, real_fname = db.read_codex_credential(fname)
                    try:
                        local = _json.loads(content)
                    except Exception:
                        local = {}
                    email = str(local.get("email") or "").strip()
                    cpa_text, cpa_name, _meta = download_cpa_codex_auth_text(email=email, local_filename=real_fname)
                    arcname = cpa_name
                    if arcname in used_names:
                        stem, dot, ext = arcname.rpartition(".")
                        arcname = f"{stem or arcname}-{len(used_names)+1}{dot}{ext}" if dot else f"{arcname}-{len(used_names)+1}"
                    used_names.add(arcname)
                    zf.writestr(arcname, cpa_text)
                    added.append({"local_filename": real_fname, "cpa_filename": cpa_name})
                    db.mark_codex_exported(real_fname)
                except Exception as exc:
                    errors.append({"filename": fname, "error": f"{type(exc).__name__}: {exc}"})
            manifest = {
                "exported_at": _dt.now().isoformat(timespec="seconds"),
                "source": "cpa",
                "count": len(added),
                "files": added,
                "errors": errors,
            }
            zf.writestr("manifest.json", _json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

        if not added:
            return jsonify({"ok": False, "error": "没有成功从 CPA 下载任何凭证", "errors": errors}), 502
        now = _dt.now()
        dl_name = f"codex-cpa-bulk-{now.strftime('%Y%m%d-%H%M%S')}.zip"
        buf.seek(0)
        return Response(
            buf.getvalue(),
            mimetype="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{dl_name}"'},
        )

    @app.post("/api/codex/download-bulk")
    def api_codex_download_bulk():
        """
        批量下载选中的 codex 凭证，打包到一个 JSON 文件里。

        Body: {"filenames": ["codex-xxx.json", ...]}
        响应：聚合 JSON（attachment 触发浏览器下载），结构：
            {
              "exported_at": "...",
              "count": N,
              "credentials": [{"filename": "...", "data": {...原始凭证内容...}}, ...],
              "errors": [...]   // 仅当部分失败时出现
            }
        注意：聚合格式**不能直接被 CPA 读**，CPA 是按单文件加载 auths/ 目录的。
              本接口主要用途是备份 / 跨机迁移 / 二次处理。
        每个成功的凭证会自动标记 mark_exported（计数+1）。
        """
        import json as _json
        from datetime import datetime as _dt

        data = request.get_json(silent=True) or {}
        filenames = data.get("filenames") or []
        if not isinstance(filenames, list) or not filenames:
            return jsonify({"ok": False, "error": "filenames 必须是非空数组"}), 400
        if len(filenames) > 1000:
            return jsonify({"ok": False, "error": "单次最多 1000 个"}), 400

        bundle = []
        errors = []
        for fname in filenames:
            if not isinstance(fname, str):
                errors.append({"filename": str(fname), "error": "非字符串"})
                continue
            try:
                content, real_fname = db.read_codex_credential(fname)
                parsed = _json.loads(content)
                bundle.append({"filename": real_fname, "data": parsed})
                db.mark_codex_exported(real_fname)
            except Exception as exc:
                errors.append({"filename": fname, "error": f"{type(exc).__name__}: {exc}"})

        now = _dt.now()
        result = {
            "exported_at": now.isoformat(timespec="seconds"),
            "count": len(bundle),
            "credentials": bundle,
        }
        if errors:
            result["errors"] = errors

        dl_name = f"codex-bulk-{now.strftime('%Y%m%d-%H%M%S')}.json"
        return Response(
            _json.dumps(result, ensure_ascii=False, indent=2),
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{dl_name}"'},
        )

    @app.post("/api/codex/reset-export")
    def api_codex_reset_export():
        """清掉某个 codex 凭证的导出状态（重新标为未导出）。body {filename}。"""
        data = request.get_json(silent=True) or {}
        fname = (data.get("filename") or "").strip()
        if not fname:
            return jsonify({"ok": False, "error": "filename 为空"}), 400
        try:
            db.reset_codex_exported(fname)
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True})

    @app.post("/api/codex/delete")
    def api_codex_delete():
        """删除一个 codex 凭证文件。body {filename}。"""
        data = request.get_json(silent=True) or {}
        fname = (data.get("filename") or "").strip()
        if not fname:
            return jsonify({"ok": False, "error": "filename 为空"}), 400
        try:
            deleted = db.delete_codex_credential(fname)
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        if not deleted:
            return jsonify({"ok": False, "error": "凭证文件不存在"}), 404
        return jsonify({"ok": True, "deleted": fname})

    @app.post("/api/codex/delete-bulk")
    def api_codex_delete_bulk():
        """批量删除 codex 凭证文件。body {filenames:[...]}。"""
        data = request.get_json(silent=True) or {}
        filenames = data.get("filenames") or []
        if not isinstance(filenames, list) or not filenames:
            return jsonify({"ok": False, "error": "filenames 必须是非空数组"}), 400
        if len(filenames) > 1000:
            return jsonify({"ok": False, "error": "单次最多删除 1000 个"}), 400
        deleted = []
        skipped = []
        seen = set()
        for fname in filenames:
            fname = str(fname or "").strip()
            if not fname or fname in seen:
                continue
            seen.add(fname)
            try:
                ok = db.delete_codex_credential(fname)
                if ok:
                    deleted.append(fname)
                else:
                    skipped.append({"filename": fname, "reason": "文件不存在"})
            except Exception as exc:
                skipped.append({"filename": fname, "reason": f"{type(exc).__name__}: {exc}"})
        return jsonify({"ok": True, "deleted": deleted, "deleted_count": len(deleted), "skipped": skipped})

    @app.post("/api/codex/stop")
    def api_codex_stop():
        """停止单个 Codex 补跑。Body {email}。"""
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        acc = db.get_account_by_email(email)
        if acc is None:
            return jsonify({"ok": False, "error": f"账号不存在: {email}"}), 404
        result = codex_retry_service.request_stop(email)
        status = int(result.pop("status", 200) or 200)
        return jsonify(result), status

    @app.post("/api/codex/stop-bulk")
    def api_codex_stop_bulk():
        """批量停止 Codex 补跑。Body {emails:[...]} 或 {account_ids:[...]}。"""
        data = request.get_json(silent=True) or {}
        emails = data.get("emails") or []
        ids = data.get("account_ids") or data.get("ids") or []
        targets = []
        if isinstance(emails, list) and emails:
            targets = [str(x or "").strip() for x in emails]
        elif isinstance(ids, list) and ids:
            for raw in ids:
                try:
                    acc = db.get_account(int(raw))
                except Exception:
                    acc = None
                if acc and acc.get("email"):
                    targets.append(str(acc.get("email") or "").strip())
        else:
            return jsonify({"ok": False, "error": "emails 或 account_ids 必须是非空数组"}), 400
        if len(targets) > 500:
            return jsonify({"ok": False, "error": "单次最多停止 500 个"}), 400
        stopped = []
        skipped = []
        seen = set()
        for email in targets:
            key = email.lower()
            if not email or key in seen:
                continue
            seen.add(key)
            acc = db.get_account_by_email(email)
            if acc is None:
                skipped.append({"email": email, "reason": "账号不存在"})
                continue
            if (acc.get("codex_status") or "") != "retrying" and not codex_retry_service.is_retrying(email):
                skipped.append({"email": email, "reason": "未处于补跑中"})
                continue
            r = codex_retry_service.request_stop(email)
            if r.get("ok"):
                stopped.append({"email": email, "injected": r.get("injected"), "running": r.get("running")})
            else:
                skipped.append({"email": email, "reason": r.get("error") or "停止失败"})
        return jsonify({"ok": True, "stopped": stopped, "stopped_count": len(stopped), "skipped": skipped})

    @app.post("/api/codex/reset-retrying")
    def api_codex_reset_retrying():
        """手动重置某账号的 Codex 补跑中状态。Body {email, status?}。"""
        from datetime import datetime as _dt

        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        raw_status = (data.get("status") or "failed").strip().lower()
        if raw_status in ("", "none", "null", "clear"):
            raw_status = "empty"
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        if raw_status not in ("failed", "skipped", "empty"):
            return jsonify({"ok": False, "error": "status 仅支持 failed/skipped/empty"}), 400

        acc = db.get_account_by_email(email)
        if acc is None:
            return jsonify({"ok": False, "error": f"账号不存在: {email}"}), 404

        new_status = "" if raw_status == "empty" else raw_status
        err = None if raw_status == "empty" else "用户手动重置补跑中状态"
        ok = db.update_account_codex_status(email, new_status, err)
        if not ok:
            return jsonify({"ok": False, "error": f"账号不存在: {email}"}), 404

        codex_retry_service.release(email)

        try:
            log_path = codex_retry_service.log_path(email)
            ts = _dt.now().strftime("%H:%M:%S")
            shown = new_status or "空"
            sqlite_store.append_file(
                log_path,
                f"{ts} [WARNING] [Codex 补跑] 用户手动重置补跑中状态，当前状态={shown}\n",
                category="codex_retry_logs",
                mirror=False,
            )
        except Exception:
            logger.exception("写入 Codex 补跑重置日志失败")

        return jsonify({"ok": True, "message": "已重置补跑中状态", "status": new_status})

    @app.post("/api/codex/retry")
    def api_codex_retry():
        """Reject the retired endpoint that bypassed the phone pool."""
        return jsonify({
            "ok": False,
            "error": "旧 Codex 补跑入口已停用；请在 GPT账号 页面发起授权，授权将使用手机号池",
        }), 410

    @app.post("/api/codex/retry-bulk")
    def api_codex_retry_bulk():
        """Reject the retired batch endpoint that bypassed the phone pool."""
        return jsonify({
            "ok": False,
            "error": "旧 Codex 批量补跑入口已停用；请在 GPT账号 页面选择账号后授权",
        }), 410

    @app.get("/api/codex/retry-log")
    def api_codex_retry_log():
        """读取某邮箱最近一次补跑的日志。?email=xxx"""
        email = (request.args.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        p = codex_retry_service.log_path(email)
        if not sqlite_store.file_exists(p, category="codex_retry_logs"):
            return jsonify({"ok": True, "log": "", "running": False})
        max_bytes = 50_000
        content = sqlite_store.read_file(p, category="codex_retry_logs")[-max_bytes:].decode("utf-8", errors="replace")
        return jsonify({
            "ok": True,
            "log": content,
            "running": codex_retry_service.is_retrying(email),
        })

    @app.get("/api/accounts/live-check-log")
    def api_account_live_check_log():
        """读取某邮箱最近一次查活日志。?email=xxx"""
        from core import account_liveness
        email = (request.args.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        p = account_liveness.log_path(email)
        if not sqlite_store.file_exists(p, category="liveness_logs"):
            return jsonify({"ok": True, "log": "", "running": live_check_service.is_checking(email)})
        max_bytes = 80_000
        content = sqlite_store.read_file(p, category="liveness_logs")[-max_bytes:].decode("utf-8", errors="replace")
        return jsonify({
            "ok": True,
            "log": content,
            "running": live_check_service.is_checking(email),
        })

    # ----------------------------------------------------------
    # 注册任务
    # ----------------------------------------------------------
    @app.get("/api/jobs")
    def api_jobs():
        limit = request.args.get("limit", default=100, type=int)
        paged = str(request.args.get("paged", default="") or "").lower() in {"1", "true", "yes"}
        page_arg = request.args.get("page", default=None, type=int)
        page_size_arg = request.args.get("page_size", default=None, type=int)
        fetch_limit = 1_000_000 if (paged or page_arg is not None or page_size_arg is not None) else limit
        rows = db.list_jobs(limit=fetch_limit)
        for row in rows:
            row["manual_otp_required"] = not bool(str(row.get("email_source") or "").strip())
            row.update(svc.get_retry_info(row))
        if paged or page_arg is not None or page_size_arg is not None:
            page = max(1, int(page_arg or 1))
            page_size = max(1, min(500, int(page_size_arg or limit or 50)))
            result = _paginate_items(rows, page=page, page_size=page_size)
            result["items"] = [_compact_job_for_list(r) for r in (result.get("items") or [])]
            result["status_counts"] = _job_status_counts(rows)
            result["compact"] = True
            return jsonify(result)
        return jsonify(rows)

    @app.post("/api/jobs")
    def api_jobs_create():
        """启动批量注册：body {count, workers, email_sources?}。"""
        data = request.get_json(silent=True) or {}
        try:
            count = int(data.get("count", 1))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "count 非法"}), 400
        if count < 1 or count > 200:
            return jsonify({"ok": False, "error": "count 需在 1~200 之间"}), 400

        # workers 控制本次新提交任务使用的线程池；若和上次不同，服务层会为新任务切换到新池。
        try:
            workers = max(1, min(16, int(data.get("workers", 3))))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "workers 非法"}), 400

        # 提交前先确认池里有足够可用邮箱，给前端一个温和提示（不阻断）
        from config import email as _email_cfg
        from config import register as _register_cfg
        from core.email_provider import (
            email_source_statuses,
            registration_email_status,
            validate_email_sources,
        )
        explicit_sources = data.get("email_sources")
        requested_sources = None
        if explicit_sources is not None:
            try:
                requested_sources = validate_email_sources(explicit_sources)
            except ValueError as exc:
                return jsonify({"ok": False, "error": str(exc)}), 400

        if not bool(getattr(_email_cfg, "USE_EMAIL_SERVICE", True)) and requested_sources is None:
            reg_email = str(getattr(_register_cfg, "REGISTER_EMAIL", "") or "").strip()
            if not reg_email:
                return jsonify({
                    "ok": False,
                    "error": "手动模式未配置 REGISTER_EMAIL。请到配置页填写「手动注册邮箱」，或开启自动取邮箱+收码。",
                }), 400
            if count > 1:
                return jsonify({
                    "ok": False,
                    "error": "手动模式建议每次只跑 1 个任务（同一 REGISTER_EMAIL）。请把数量设为 1。",
                }), 400
            jobs = svc.submit_registration(count=count, workers=workers)
            return jsonify({
                "ok": True,
                "submitted": len(jobs),
                "jobs": jobs,
                "warning": f"手动 OTP 模式：将使用 {reg_email}；验证码请在任务页提交",
                "workers": workers,
            })
        if requested_sources:
            channels = email_source_statuses(requested_sources)
            runtime = {
                "automatic": True,
                "sources": requested_sources,
                "channels": channels,
                "all_channels": channels,
                "usable_sources": [item["id"] for item in channels if item["ready"]],
                "ready_sources": [item["id"] for item in channels if item["ready"]],
                "ready": any(item["ready"] for item in channels),
            }
        else:
            runtime = registration_email_status(include_all=False)
            channels = runtime["channels"]
        if requested_sources:
            unavailable_channels = [item for item in channels if not item["ready"]]
            if unavailable_channels:
                details = "；".join(f"{item['label']}：{item['message']}" for item in unavailable_channels)
                return jsonify({
                    "ok": False,
                    "error": f"本次选择中有不可用的邮箱渠道。{details}",
                    "registration_email": runtime,
                }), 400
        ready_channels = [item for item in channels if item["ready"]]
        if not ready_channels:
            details = "；".join(f"{item['label']}：{item['message']}" for item in channels)
            return jsonify({
                "ok": False,
                "error": f"当前没有可用的邮箱渠道。{details}。请到「设置 → 邮箱与 OTP」完成配置。",
                "registration_email": runtime,
            }), 400

        warning_parts = []
        skipped_channels = [] if requested_sources else [item for item in channels if not item["ready"]]
        if skipped_channels:
            skipped = "；".join(f"{item['label']}（{item['message']}）" for item in skipped_channels)
            warning_parts.append(f"已跳过未就绪渠道：{skipped}")

        if not any(item["kind"] == "generated" for item in ready_channels):
            available = sum(int(item["available"] or 0) for item in ready_channels)
            if available < count:
                warning_parts.append(f"可用邮箱合计 {available} 个，少于任务数 {count}，超出部分可能失败")

        warning = "；".join(warning_parts)
        usable_sources = [item["id"] for item in ready_channels]
        if requested_sources:
            jobs = svc.submit_registration(
                count=count,
                workers=workers,
                email_source=",".join(usable_sources),
            )
        elif usable_sources == runtime["sources"]:
            jobs = svc.submit_registration(count=count, workers=workers)
        else:
            jobs = svc.submit_registration(
                count=count,
                workers=workers,
                email_source=",".join(usable_sources),
            )
        return jsonify({
            "ok": True,
            "submitted": len(jobs),
            "jobs": jobs,
            "warning": warning,
            "workers": workers,
            "usable_sources": usable_sources,
        })

    # ----------------------------------------------------------
    # 已有 GPT 账号 -> Codex OAuth 接码工作台
    # ----------------------------------------------------------
    @app.get("/api/gpt-accounts")
    def api_gpt_accounts():
        """统一 GPT 账号读模型（注册、Codex 与手机状态在服务端聚合）。"""
        rows = gpt_account_service.list_accounts(
            q=request.args.get("q") or "",
            registration_status=request.args.get("registration_status") or "",
            codex_status=request.args.get("codex_status") or "",
            phone_status=request.args.get("phone_status") or "",
            gpt_status=request.args.get("gpt_status") or "",
            provider=request.args.get("provider") or "",
        )
        paged = str(request.args.get("paged", default="") or "").lower() in {"1", "true", "yes"}
        page_arg = request.args.get("page", default=None, type=int)
        page_size_arg = request.args.get("page_size", default=None, type=int)
        if paged or page_arg is not None or page_size_arg is not None:
            result = _paginate_items(rows, page=max(1, int(page_arg or 1)), page_size=page_size_arg or 20)
        else:
            result = {"ok": True, "items": rows, "total": len(rows)}
        result["status_counts"] = {
            "registration": {key: sum(1 for row in rows if row.get("registration_status") == key) for key in ("registered", "unregistered", "registering", "failed")},
            "codex": {key: sum(1 for row in rows if row.get("codex_status") == key) for key in ("authorized", "unauthorized", "authorizing", "failed")},
            "phone": {key: sum(1 for row in rows if row.get("phone_status") == key) for key in ("verified", "unverified", "verifying", "failed")},
        }
        return jsonify(result)

    @app.get("/api/gpt-accounts/<account_id>/log")
    def api_gpt_account_log(account_id: str):
        """Read the newest registration/authorization log attached to an account row."""
        row = gpt_account_service.get_account(account_id)
        if not row:
            return jsonify({"ok": False, "error": "GPT账号不存在"}), 404
        ref = row.get("latest_log") or {}
        if not ref:
            return jsonify({"ok": True, "account_id": account_id, "kind": "", "job_id": None, "log": ""})
        try:
            if ref.get("kind") in {"registration", "codex_retry"}:
                log = svc.read_job_log(int(ref.get("id")))
            else:
                log = codex_relay_service.read_log(str(ref.get("id") or ""))
        except (TypeError, ValueError):
            log = ""
        return jsonify({"ok": True, "account_id": account_id, "kind": ref.get("kind"), "job_id": ref.get("id"), "log": log, "job": ref})

    @app.delete("/api/gpt-accounts")
    def api_gpt_accounts_soft_delete():
        """Soft-delete unified rows without removing source accounts or logs."""
        data = request.get_json(silent=True) or {}
        account_ids = data.get("account_ids") or []
        if not isinstance(account_ids, list) or not account_ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(account_ids) > 5000:
            return jsonify({"ok": False, "error": "单次最多删除 5000 个 GPT 账号"}), 400
        try:
            return jsonify(gpt_account_service.soft_delete_accounts(account_ids))
        except gpt_account_service.AccountBusyError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 409
        except LookupError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/gpt-accounts/register")
    def api_gpt_accounts_register():
        """Queue registration work from the unified workspace.

        Without account_ids this is the normal configured email-pool batch
        entry.  With account_ids, the selected rows' email API/Outlook
        material is prepared and the worker receives those exact addresses;
        retry/stop semantics remain owned by the existing registration service.
        """
        data = request.get_json(silent=True) or {}
        requested_ids = [str(value) for value in (data.get("account_ids") or []) if value]
        if requested_ids:
            rows = gpt_account_service.list_accounts()
            selected = [row for row in rows if str(row.get("id") or "") in requested_ids and row.get("registration_status") not in {"registered", "registering"}]
            if any(str(row.get("id") or "") in requested_ids and row.get("registration_status") == "registering" for row in rows):
                return jsonify({"ok": False, "error": "所选账号已有注册任务运行中，请等待当前任务结束"}), 409
            count = len(selected)
            if not count:
                return jsonify({"ok": False, "error": "所选账号均已注册或没有可注册账号"}), 400
            target_emails = []
            generic_material = []
            outlook_material = []
            for row in selected:
                email = str(row.get("email") or "").strip().lower()
                if not email:
                    continue
                target_emails.append(email)
                if row.get("email_code_url"):
                    generic_material.append({"email": email, "code_url": row.get("email_code_url")})
                elif row.get("mailbox_password") and row.get("outlook_client_id") and row.get("outlook_refresh_token"):
                    outlook_material.append({
                        "email": email,
                        "password": row.get("mailbox_password"),
                        "client_id": row.get("outlook_client_id"),
                        "refresh_token": row.get("outlook_refresh_token"),
                    })
                else:
                    return jsonify({"ok": False, "error": f"账号 {email} 缺少邮箱接码 API 或 Outlook 邮箱凭证，无法定向注册"}), 400
            if generic_material:
                db.import_generic_api_emails(generic_material)
            if outlook_material:
                db.import_outlook_accounts(outlook_material)
        else:
            target_emails = None
            try:
                count = max(1, min(200, int(data.get("count", 1))))
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "count 非法"}), 400
        try:
            workers = max(1, min(16, int(data.get("workers", 1))))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "workers 非法"}), 400
        jobs = svc.submit_registration(count=count, workers=workers, emails=target_emails)
        return jsonify({"ok": True, "submitted": len(jobs), "jobs": jobs, "workers": workers})

    @app.post("/api/gpt-accounts/authorize")
    def api_gpt_accounts_authorize():
        """Ensure selected unified rows have Relay records, then start OAuth.

        A registered account can predate the Codex Relay collection.  The
        bridge is intentionally lazy: it copies only the login/mailbox
        material needed by the Relay worker and is idempotent by normalized
        email.  This keeps the two SQLite collections compatible while making
        the GPT账号 page the single user-facing workflow.
        """
        data = request.get_json(silent=True) or {}
        requested_ids = [str(value).strip() for value in (data.get("account_ids") or []) if str(value).strip()]
        if not requested_ids:
            return jsonify({"ok": False, "error": "请先选择 GPT 账号"}), 400
        try:
            workers = max(1, min(8, int(data.get("workers", 1))))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "workers 非法"}), 400
        rows = gpt_account_service.list_accounts()
        selected = []
        missing = []
        seen_emails = set()
        for requested in requested_ids:
            row = next(
                (
                    candidate for candidate in rows
                    if str(candidate.get("id") or "") == requested
                    or str(candidate.get("relay_account_id") or "") == requested
                    or str(candidate.get("email") or "").strip().lower() == requested.lower()
                ),
                None,
            )
            if row is None:
                missing.append(requested)
                continue
            email = str(row.get("email") or "").strip().lower()
            if email and email not in seen_emails:
                selected.append(row)
                seen_emails.add(email)
        if missing:
            return jsonify({"ok": False, "error": f"找不到账号：{', '.join(missing[:3])}"}), 404
        not_registered = [row.get("email") or "" for row in selected if row.get("registration_status") != "registered"]
        if not_registered:
            return jsonify({"ok": False, "error": f"以下账号尚未完成 GPT 注册：{', '.join(not_registered[:3])}"}), 409

        materials = []
        try:
            for row in selected:
                material = gpt_account_service.authorization_material(row)
                # Validate the complete batch before creating any Relay rows;
                # one malformed account must not leave earlier rows half-bridged.
                materials.append(codex_relay_service.validate_account_material(material))
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        relay_ids = []
        created = []
        try:
            for row, material in zip(selected, materials):
                ensured = codex_relay_service.ensure_account_material(material)
                relay_id = str(ensured.get("id") or "")
                if not relay_id:
                    raise ValueError(f"账号 {row.get('email') or ''} 未生成 Relay ID")
                relay_ids.append(relay_id)
                if ensured.get("created"):
                    created.append(relay_id)
            result = codex_relay_service.start_jobs(
                relay_ids,
                workers=workers,
                phone_ids=data.get("phone_ids") or [],
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        result.update({
            "created_relay_account_ids": created,
            "relay_account_ids": relay_ids,
            "emails": [row.get("email") for row in selected],
        })
        return jsonify(result)

    @app.post("/api/codex-relay/import")
    def api_codex_relay_import():
        data = request.get_json(silent=True) or {}
        try:
            result = codex_relay_service.import_accounts(
                data.get("text") or "",
                format_name=data.get("format") or "auto",
                include_emails=True,
            )
            restored = gpt_account_service.restore_deleted_accounts(result.pop("emails", []))
            if restored["restored"]:
                result["restored"] = restored["restored"]
                result["message"] = f"导入完成，已恢复 {restored['restored']} 个 GPT 账号"
            else:
                result["message"] = "导入完成"
            return jsonify(result)
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/codex-relay/phones/import")
    def api_codex_relay_phone_import():
        data = request.get_json(silent=True) or {}
        try:
            return jsonify(codex_relay_service.import_phones(data.get("text") or ""))
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/codex-relay/accounts")
    def api_codex_relay_accounts():
        rows = codex_relay_service.list_accounts(
            q=request.args.get("q") or "",
            status=request.args.get("status") or "",
            provider=request.args.get("provider") or "",
            codex_status=request.args.get("codex_status") or "",
            liveness=request.args.get("liveness") or "",
            email_liveness=request.args.get("email_liveness") or "",
            quota_status=request.args.get("quota_status") or "",
            phone_status=request.args.get("phone_status") or "",
            twofa=request.args.get("twofa") or "",
        )
        paged = str(request.args.get("paged", default="") or "").lower() in {"1", "true", "yes"}
        page_arg = request.args.get("page", default=None, type=int)
        page_size_arg = request.args.get("page_size", default=None, type=int)
        if paged or page_arg is not None or page_size_arg is not None:
            return jsonify(_paginate_items(rows, page=max(1, int(page_arg or 1)), page_size=page_size_arg or 20))
        return jsonify({"ok": True, "items": rows, "total": len(rows)})

    @app.put("/api/codex-relay/accounts/<account_id>")
    def api_codex_relay_update_account(account_id: str):
        data = request.get_json(silent=True) or {}
        try:
            return jsonify({"ok": True, "account": codex_relay_service.update_account(account_id, data)})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/codex-relay/accounts/actions")
    def api_codex_relay_account_actions():
        data = request.get_json(silent=True) or {}
        try:
            return jsonify(codex_relay_service.start_account_actions(
                data.get("account_ids") or [],
                data.get("action") or "",
                workers=data.get("workers") or 1,
            ))
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/codex-relay/sub2-services")
    def api_codex_relay_sub2_services():
        rows = codex_relay_service.list_sub2_services()
        return jsonify({"ok": True, "items": rows, "total": len(rows)})

    @app.post("/api/codex-relay/sub2-services")
    def api_codex_relay_save_sub2_service():
        try:
            service = codex_relay_service.save_sub2_service(request.get_json(silent=True) or {})
            return jsonify({"ok": True, "service": service})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.delete("/api/codex-relay/sub2-services/<service_id>")
    def api_codex_relay_delete_sub2_service(service_id: str):
        try:
            deleted = codex_relay_service.delete_sub2_service(service_id)
            return jsonify({"ok": True, "deleted": deleted})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/codex-relay/accounts/import-sub2")
    def api_codex_relay_import_sub2():
        data = request.get_json(silent=True) or {}
        try:
            return jsonify(codex_relay_service.import_accounts_to_sub2(
                data.get("account_ids") or [],
                data.get("service_id") or "",
                delete_terminal=data.get("delete_terminal") is True,
            ))
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/codex-relay/accounts/sync-from-sub2")
    def api_codex_relay_sync_from_sub2():
        data = request.get_json(silent=True) or {}
        try:
            return jsonify(codex_relay_service.sync_accounts_from_sub2(
                data.get("service_id") or "",
            ))
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    def _relay_export_content(account_ids, format_name: str):
        import json as _json
        from datetime import datetime, timezone

        result = codex_relay_service.export_credentials(account_ids, format_name)
        items = result.get("items") or []
        if not items:
            raise ValueError("选中账号没有可导出的 Codex 凭证")
        if format_name == "rt":
            content = "\n".join(f"{item.get('email') or ''}----{item['refresh_token']}" for item in items) + "\n"
            return content, "text/plain; charset=utf-8", "txt", result
        if format_name == "sub2":
            from config import codex as codex_cfg

            # Sub2API's generic file importer expects its backup schema.  The
            # ``contents/update_existing`` shape belongs exclusively to the
            # /admin/accounts/import/codex-session HTTP endpoint and is not a
            # portable export file.
            accounts = []
            for item in items:
                source = dict(item["data"])
                credentials = dict(source)
                email = str(item.get("email") or source.get("email") or "").strip()
                if email and not credentials.get("email"):
                    credentials["email"] = email
                if source.get("account_id") and not credentials.get("chatgpt_account_id"):
                    credentials["chatgpt_account_id"] = source["account_id"]
                if source.get("expired") and not credentials.get("expires_at"):
                    credentials["expires_at"] = source["expired"]
                # Sub2API defaults this value for OAuth refreshes, but keeping
                # it in the export makes the file portable across versions.
                credentials.setdefault("client_id", codex_cfg.CODEX_CLIENT_ID)
                account_name = email or item.get("filename") or "codex-account"
                notes = str(item.get("sub2_notes") or "").replace("\r\n", "\n").replace("\r", "\n")
                if not notes.strip():
                    raise ValueError(f"账号 {account_name} 缺少严格三行 sub2api 备注")
                accounts.append({
                    "name": account_name,
                    "notes": notes,
                    "platform": "openai",
                    "type": "oauth",
                    "credentials": credentials,
                    "extra": {
                        "import_source": "codex_relay",
                        "source_filename": item.get("filename") or "",
                    },
                    "concurrency": 10,
                    "priority": 1,
                    "rate_multiplier": 1,
                    "auto_pause_on_expired": True,
                })
            payload = {
                "type": "sub2api-data",
                "version": 1,
                "exported_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "proxies": [],
                "accounts": accounts,
            }
            return _json.dumps(payload, ensure_ascii=False, indent=2) + "\n", "application/json", "json", result
        payload = {
            "format": "cpa",
            "count": len(items),
            "credentials": [{"filename": item["filename"], "data": item["data"]} for item in items],
            "errors": result.get("errors") or [],
        }
        return _json.dumps(payload, ensure_ascii=False, indent=2) + "\n", "application/json", "json", result

    @app.post("/api/codex-relay/accounts/export/copy")
    def api_codex_relay_export_copy():
        data = request.get_json(silent=True) or {}
        format_name = str(data.get("format") or "rt").strip().lower()
        try:
            content, _mimetype, _extension, result = _relay_export_content(data.get("account_ids") or [], format_name)
            return jsonify({
                "ok": True,
                "format": format_name,
                "content": content,
                "count": result.get("count") or 0,
                "errors": result.get("errors") or [],
            })
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/codex-relay/accounts/export/download")
    def api_codex_relay_export_download():
        data = request.get_json(silent=True) or {}
        account_ids = data.get("account_ids") or []
        format_name = str(data.get("format") or "rt").strip().lower()
        try:
            if format_name == "cpa":
                content, filename, _result = codex_relay_service.credential_download_zip(account_ids)
                return Response(content, mimetype="application/zip", headers={"Content-Disposition": f'attachment; filename="{filename}"'})
            content, mimetype, extension, _result = _relay_export_content(account_ids, format_name)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            export_label = "sub2api" if format_name == "sub2" else format_name
            filename = f"codex-{export_label}-relay-{stamp}.{extension}"
            return Response(content, mimetype=mimetype, headers={"Content-Disposition": f'attachment; filename="{filename}"'})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/codex-relay/phones")
    def api_codex_relay_phones():
        rows = codex_relay_service.list_phones(
            q=request.args.get("q") or "",
            status=request.args.get("status") or "",
        )
        paged = str(request.args.get("paged", default="") or "").lower() in {"1", "true", "yes"}
        page_arg = request.args.get("page", default=None, type=int)
        page_size_arg = request.args.get("page_size", default=None, type=int)
        if paged or page_arg is not None or page_size_arg is not None:
            return jsonify(_paginate_items(rows, page=max(1, int(page_arg or 1)), page_size=page_size_arg or 20))
        return jsonify({"ok": True, "items": rows, "total": len(rows)})

    @app.delete("/api/codex-relay/phones")
    def api_codex_relay_delete_phones():
        data = request.get_json(silent=True) or {}
        try:
            deleted = codex_relay_service.delete_phones(data.get("phone_ids") or [])
            return jsonify({"ok": True, "deleted": deleted})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.patch("/api/codex-relay/phones/available-uses")
    def api_codex_relay_adjust_phone_available_uses():
        data = request.get_json(silent=True) or {}
        try:
            return jsonify(codex_relay_service.adjust_phone_available_uses(
                data.get("phone_ids") or [],
                data.get("delta"),
            ))
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.delete("/api/codex-relay/accounts")
    def api_codex_relay_delete_accounts():
        data = request.get_json(silent=True) or {}
        try:
            deleted = codex_relay_service.delete_accounts(data.get("account_ids") or [])
            return jsonify({"ok": True, "deleted": deleted})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/codex-relay/jobs")
    def api_codex_relay_start_jobs():
        data = request.get_json(silent=True) or {}
        try:
            result = codex_relay_service.start_jobs(
                data.get("account_ids") or [],
                workers=data.get("workers") or 1,
                phone_ids=data.get("phone_ids") or [],
            )
            return jsonify(result)
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/codex-relay/jobs")
    def api_codex_relay_jobs():
        rows = codex_relay_service.list_jobs(
            q=request.args.get("q") or "",
            status=request.args.get("status") or "",
            action=request.args.get("action") or "",
        )
        paged = str(request.args.get("paged", default="") or "").lower() in {"1", "true", "yes"}
        page_arg = request.args.get("page", default=None, type=int)
        page_size_arg = request.args.get("page_size", default=None, type=int)
        if paged or page_arg is not None or page_size_arg is not None:
            return jsonify(_paginate_items(rows, page=max(1, int(page_arg or 1)), page_size=page_size_arg or 20))
        return jsonify({"ok": True, "items": rows, "total": len(rows)})

    @app.post("/api/codex-relay/jobs/<job_id>/verification")
    def api_codex_relay_verification(job_id: str):
        data = request.get_json(silent=True) or {}
        try:
            result = codex_relay_service.submit_verification(
                job_id,
                data.get("stage") or "",
                data.get("code") or data.get("otp") or "",
            )
            return jsonify(result)
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/codex-relay/jobs/<job_id>/browser-assist")
    def api_codex_relay_browser_assist(job_id: str):
        try:
            return jsonify(codex_relay_service.submit_browser_assist(job_id))
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/codex-relay/jobs/<job_id>/browser-focus")
    def api_codex_relay_browser_focus(job_id: str):
        try:
            return jsonify(codex_relay_service.focus_browser_assist(job_id))
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/codex-relay/jobs/<job_id>/stop")
    def api_codex_relay_stop(job_id: str):
        try:
            return jsonify(codex_relay_service.stop_job(job_id))
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.delete("/api/codex-relay/jobs")
    def api_codex_relay_delete_jobs():
        data = request.get_json(silent=True) or {}
        try:
            return jsonify(codex_relay_service.delete_jobs(data.get("job_ids") or []))
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.delete("/api/codex-relay/jobs/<job_id>")
    def api_codex_relay_delete_job(job_id: str):
        try:
            deleted = codex_relay_service.delete_job(job_id)
            return jsonify({"ok": True, "deleted": deleted})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/codex-relay/jobs/<job_id>/log")
    def api_codex_relay_log(job_id: str):
        return jsonify({"ok": True, "job_id": job_id, "log": codex_relay_service.read_log(job_id)})

    @app.get("/api/manual-otp/waiting")
    def api_manual_otp_waiting():
        """列出当前正在等待手动验证码的邮箱。"""
        from core.manual_otp import list_waiting
        return jsonify({"ok": True, "waiting": list_waiting()})

    @app.post("/api/manual-otp")
    def api_manual_otp_submit():
        """提交手动邮箱验证码。Body: {email, code} 或 {job_id, code}。"""
        from core.manual_otp import submit_manual_otp
        data = request.get_json(silent=True) or {}
        code = (data.get("code") or data.get("otp") or "").strip()
        email = (data.get("email") or "").strip()
        job_id = data.get("job_id")
        if not email and job_id is not None:
            job = db.get_job(int(job_id))
            email = (job or {}).get("email") or ""
        if not email:
            return jsonify({"ok": False, "error": "email/job_id 缺失"}), 400
        try:
            result = submit_manual_otp(email, code)
            return jsonify(result)
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400

    @app.post("/api/jobs/cancel-pending")
    def api_jobs_cancel_pending():
        """取消所有还在排队（status=pending）的任务。已在 running 的不动。"""
        cancelled = svc.cancel_pending_jobs()
        return jsonify({"ok": True, "cancelled": cancelled})

    @app.post("/api/jobs/<int:job_id>/stop")
    def api_job_stop(job_id: int):
        """手动停止单个注册任务。pending 取消；running 发送停止信号。"""
        result = svc.request_stop_job(job_id)
        if not result.get("ok"):
            return jsonify({"ok": False, "error": result.get("error") or "停止失败"}), int(result.get("status") or 400)
        return jsonify(result)

    @app.post("/api/jobs/<int:job_id>/retry")
    def api_job_retry(job_id: int):
        """重试失败/停止/取消的注册任务；不会隐式发起 Codex 授权。"""
        data = request.get_json(silent=True) or {}
        try:
            workers = max(1, min(16, int(data.get("workers", svc.get_executor_workers()))))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "workers 非法"}), 400
        result = svc.retry_job(job_id, workers=workers)
        if not result.get("ok"):
            return jsonify(result), int(result.get("status") or 400)
        return jsonify(result)

    @app.post("/api/jobs/retry-bulk")
    def api_jobs_retry_bulk():
        """批量重试任务；不支持项逐条跳过并返回原因。"""
        data = request.get_json(silent=True) or {}
        job_ids = data.get("job_ids") or data.get("ids") or []
        if not isinstance(job_ids, list) or not job_ids:
            return jsonify({"ok": False, "error": "job_ids 必须是非空数组"}), 400
        if len(job_ids) > 500:
            return jsonify({"ok": False, "error": "单次最多重试 500 个任务"}), 400
        try:
            workers = max(1, min(16, int(data.get("workers", svc.get_executor_workers()))))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "workers 非法"}), 400

        started: list[dict] = []
        reused: list[dict] = []
        skipped: list[dict] = []
        seen: set[int] = set()
        for raw_id in job_ids:
            try:
                one_id = int(raw_id)
            except (TypeError, ValueError):
                skipped.append({"id": raw_id, "reason": "ID 非法"})
                continue
            if one_id in seen:
                continue
            seen.add(one_id)
            result = svc.retry_job(one_id, workers=workers)
            if not result.get("ok"):
                skipped.append({"id": one_id, "reason": result.get("error") or "不能重试"})
            elif result.get("reused"):
                reused.append(result)
            else:
                started.append(result)
        return jsonify({
            "ok": True,
            "started": started,
            "started_count": len(started),
            "reused": reused,
            "reused_count": len(reused),
            "skipped": skipped,
            "skipped_count": len(skipped),
            "workers": workers,
        })

    @app.post("/api/jobs/<int:job_id>/delete")
    def api_job_delete(job_id: int):
        """删除一个任务记录。运行中的任务不允许删除；排队任务删除后执行前会自动跳过。"""
        job = db.get_job(job_id)
        if not job:
            return jsonify({"ok": False, "error": "任务不存在"}), 404
        if job.get("status") in ("running", "stopping"):
            return jsonify({"ok": False, "error": "运行中的任务不能删除，请等待完成后再删"}), 409
        deleted = db.delete_job(job_id, delete_log=True, allow_running=False)
        if not deleted:
            return jsonify({"ok": False, "error": "任务不存在或已开始运行"}), 409
        return jsonify({"ok": True, "deleted": deleted})

    @app.post("/api/jobs/delete-bulk")
    def api_jobs_delete_bulk():
        """批量删除任务记录。running 任务跳过，其它任务删除记录和日志。"""
        data = request.get_json(silent=True) or {}
        job_ids = data.get("job_ids") or data.get("ids") or []
        if not isinstance(job_ids, list) or not job_ids:
            return jsonify({"ok": False, "error": "job_ids 必须是非空数组"}), 400
        if len(job_ids) > 1000:
            return jsonify({"ok": False, "error": "单次最多删除 1000 个任务"}), 400

        deleted: list[int] = []
        skipped: list[dict] = []
        seen: set[int] = set()
        for raw_id in job_ids:
            try:
                job_id = int(raw_id)
            except (TypeError, ValueError):
                skipped.append({"id": raw_id, "reason": "ID 非法"})
                continue
            if job_id in seen:
                continue
            seen.add(job_id)

            job = db.get_job(job_id)
            if not job:
                skipped.append({"id": job_id, "reason": "任务不存在"})
                continue
            if job.get("status") in ("running", "stopping"):
                skipped.append({"id": job_id, "reason": "运行中，不能删除"})
                continue
            if db.delete_job(job_id, delete_log=True, allow_running=False):
                deleted.append(job_id)
            else:
                skipped.append({"id": job_id, "reason": "任务不存在或已开始运行"})

        return jsonify({"ok": True, "deleted": deleted, "deleted_count": len(deleted), "skipped": skipped})

    @app.get("/api/jobs/<int:job_id>/log")
    def api_job_log(job_id: int):
        job = db.get_job(job_id)
        if not job:
            return jsonify({"ok": False, "error": "任务不存在"}), 404
        return jsonify({
            "ok": True,
            "job": job,
            "log": svc.read_job_log(job_id),
        })

    # ----------------------------------------------------------
    # RoxyBrowser 辅助接口
    # ----------------------------------------------------------
    @app.get("/api/roxy/workspaces")
    def api_roxy_workspaces():
        try:
            from core.roxybrowser_client import RoxyBrowserClient
            result = RoxyBrowserClient().list_workspaces()
            return jsonify(result)
        except Exception as exc:
            logger.exception("获取 Roxy 团队/工作区失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500

    # ----------------------------------------------------------
    # 配置读写
    # ----------------------------------------------------------
    @app.get("/api/config")
    def api_config_get():
        return jsonify(config_editor.get_config())

    @app.post("/api/cloudmail/gen-token")
    def api_cloudmail_gen_token():
        """手动生成 CloudMail Authorization Token，并把本次填写的 CloudMail 配置一并写入 .env。"""
        data = request.get_json(silent=True) or {}
        try:
            from core.cloudmail_client import gen_token
            from config.env_loader import write_env_values

            api_base = (data.get("api_base") or "").strip()
            admin_email = (data.get("email") or data.get("admin_email") or "").strip()
            password = (data.get("password") or "").strip()
            path = (data.get("path") or "/api/public/genToken").strip() or "/api/public/genToken"
            token = gen_token(
                email=admin_email,
                password=password,
                path=path,
                base_url=api_base,
            )
            updates = {"CLOUDMAIL_AUTH_TOKEN": token}
            # 生成 Token 时用户通常尚未点“保存配置”；这里同步保存本次填写的字段，
            # 避免 loadConfig() 后 API 地址/账号/密码被旧 .env 值覆盖。
            if api_base:
                updates["CLOUDMAIL_API_BASE"] = api_base
            if admin_email:
                updates["CLOUDMAIL_ADMIN_EMAIL"] = admin_email
            if password:
                updates["CLOUDMAIL_PASSWORD"] = password
            if path:
                updates["CLOUDMAIL_TOKEN_PATH"] = path
            written = write_env_values(updates)
            try:
                import config as _config_pkg
                _config_pkg.reload_all()
            except Exception:
                logger.exception("CloudMail Token 写入后热加载失败")
            return jsonify({
                "ok": True,
                "token": token,
                "written": written,
                "message": "CloudMail Token 已生成，且当前 CloudMail 配置已保存",
            })
        except Exception as exc:
            logger.exception("生成 CloudMail Token 失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400

    @app.post("/api/cloudmail/domains")
    def api_cloudmail_domains():
        """从 CloudMail 平台获取域名列表，并可写入 .env 作为本地缓存。"""
        data = request.get_json(silent=True) or {}
        try:
            from core.cloudmail_client import fetch_domains
            from config.env_loader import write_env_values

            updates = {}
            api_base = (data.get("api_base") or "").strip()
            admin_email = (data.get("email") or data.get("admin_email") or "").strip()
            password = (data.get("password") or "").strip()
            token = (data.get("token") or "").strip()
            if api_base:
                updates["CLOUDMAIL_API_BASE"] = api_base
            if admin_email:
                updates["CLOUDMAIL_ADMIN_EMAIL"] = admin_email
            if password:
                updates["CLOUDMAIL_PASSWORD"] = password
            if token:
                updates["CLOUDMAIL_AUTH_TOKEN"] = token
            if updates:
                write_env_values(updates)
                import config as _config_pkg
                _config_pkg.reload_all()

            domains = fetch_domains(force=True)
            written = write_env_values({"CLOUDMAIL_DOMAINS": "\n".join(domains)})
            try:
                import config as _config_pkg
                _config_pkg.reload_all()
            except Exception:
                logger.exception("CloudMail 域名写入后热加载失败")
            return jsonify({
                "ok": True,
                "domains": domains,
                "count": len(domains),
                "written": written,
                "message": f"已获取 {len(domains)} 个 CloudMail 可用域名并保存",
            })
        except Exception as exc:
            logger.exception("获取 CloudMail 域名失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400

    @app.post("/api/config")
    def api_config_set():
        data = request.get_json(silent=True) or {}
        updates = data.get("updates") if isinstance(data.get("updates"), dict) else data
        if not isinstance(updates, dict) or not updates:
            return jsonify({"ok": False, "error": "无更新内容"}), 400
        try:
            result = config_editor.update_config(updates)
        except Exception as exc:
            logger.exception("配置写入失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500

        # 写盘成功后立即热加载所有 config 子模块，让运行时代码看到新值。
        reload_ok = True
        reload_err = ""
        try:
            import config as _config_pkg
            _config_pkg.reload_all()
        except Exception as exc:
            reload_ok = False
            reload_err = f"{type(exc).__name__}: {exc}"
            logger.exception("配置热加载失败")

        return jsonify({
            "ok": True,
            "updated": result["updated"],
            "ignored": result["ignored"],
            "reloaded": reload_ok,
            "note": (
                "✅ 已保存并热加载，新值立即生效"
                if reload_ok
                else f"⚠️ 已写入文件但热加载失败（{reload_err}），需重启 Web 服务才能生效"
            ),
        })

    return app
