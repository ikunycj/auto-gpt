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
import time
import uuid
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

from core import (
    codex_retry_service, codex_relay_service, gpt_account_service, db,
    plan_check_service, extract_link_service, codex_agent_service,
    live_check_service, sqlite_store,
)
from core.import_parser import (
    clean_import_value,
    is_email,
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


# The GPT account row is also the durable credential record. List/edit
# responses only expose fields used for the workspace display; a new stored
# field must not silently become part of the HTTP response.
_PUBLIC_GPT_ACCOUNT_FIELDS = frozenset({
    "id", "email", "registration_status", "registration_status_raw",
    "codex_status", "codex_status_raw", "phone_status", "phone", "phone_number",
    "sms_code_url", "sms_provider", "phone_verified_at", "password",
    "chatgpt_password", "totp_secret", "email_code_url", "login_method",
    "has_password", "has_totp", "has_email_code_url", "has_phone",
    "has_sms_code_url", "has_proxy", "proxy_source", "proxy_display", "email_provider",
    "email_provider_label", "gpt_status", "liveness_status", "quota_plan",
    "plan", "quota_status", "note", "created_at", "updated_at",
    "password_changed_at", "twofa_enabled_at", "codex_authorized_at",
    "registration_job_id", "codex_job_id", "codex_job_kind",
    "registration_job", "codex_job", "maintenance_job", "latest_log",
    "logs", "log_count", "active_operation", "active_operation_label",
    "sub2_service_id", "sub2_account_id", "sub2_status", "sub2_checked_at",
    "sub2_deleted_at", "sub2_liveness_status", "sub2_account_status",
    "sub2_schedulable", "sub2_quota_plan", "sub2_quota_allowed",
    "sub2_quota_limit_reached", "sub2_quota_primary_used_percent",
    "sub2_quota_primary_window_seconds", "sub2_quota_primary_reset_at",
    "sub2_quota_primary_reset_after_seconds", "sub2_five_hour_utilization",
    "sub2_five_hour_resets_at", "sub2_five_hour_remaining_seconds",
    "sub2_five_hour_stats", "sub2_seven_day_utilization",
    "sub2_seven_day_resets_at", "sub2_seven_day_remaining_seconds",
    "sub2_seven_day_stats", "sub2_credentials_update_status",
    "sub2_credentials_updated_at", "sub2_refresh_status", "sub2_refresh_at",
    "sub2_refresh_error", "sub2_status_error", "sub2_synced_at",
})


def _public_gpt_account(row: dict) -> dict:
    return {key: value for key, value in row.items() if key in _PUBLIC_GPT_ACCOUNT_FIELDS}


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


def _select_gpt_accounts(requested_ids: list) -> dict:
    """Resolve selected rows from the canonical GPT account collection."""
    requested = [str(value).strip() for value in (requested_ids or []) if str(value).strip()]
    if not requested:
        raise ValueError("请先选择 GPT 账号")

    rows = gpt_account_service.list_accounts()
    by_id = {str(row.get("id") or ""): row for row in rows}
    selected = []
    missing = []
    seen = set()
    for account_id in requested:
        row = by_id.get(account_id)
        if row is None:
            missing.append(account_id)
            continue
        row_id = str(row.get("id") or "")
        if row_id and row_id not in seen:
            selected.append(row)
            seen.add(row_id)
    if missing:
        raise LookupError(f"找不到 GPT 账号：{', '.join(missing[:3])}")

    not_registered = [str(row.get("email") or "") for row in selected if row.get("registration_status") != "registered"]
    if not_registered:
        raise RuntimeError(f"以下账号尚未完成 GPT 注册：{', '.join(not_registered[:3])}")
    return {
        "selected": selected,
        "account_ids": [str(row.get("id")) for row in selected],
    }


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
        for key, value in list(_prepared_downloads.items()):
            if now - float(value.get("created_at") or 0) > 600:
                _prepared_downloads.pop(key, None)
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
                "Cache-Control": "no-store",
                "Pragma": "no-cache",
                "X-Content-Type-Options": "nosniff",
                "X-Download-Options": "noopen",
            },
        )

    # Initialize current collections and recover interrupted in-flight work.
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
    recovered_codex_jobs = codex_relay_service.recover_interrupted_jobs()
    if recovered_codex_jobs:
        logger.warning("已停止 %s 个因 WebUI 重启中断的 GPT 账号 Codex 接码任务", recovered_codex_jobs)
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
        # The mailbox workspace contains only addresses without a GPT account.
        registered_emails = {
            str(row.get("email") or "").strip().lower()
            for row in gpt_account_service.list_accounts()
            if row.get("registration_status") == "registered"
        }
        rows = [r for r in rows if str(r.get("email") or "").strip().lower() not in registered_emails]
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
                material = parse_account_material_line(line)
                if not material or not material.get("email_code_url"):
                    continue
                if not is_email(material.get("email")):
                    continue
                records.append({
                    "email": material["email"],
                    "code_url": material["email_code_url"],
                    "chatgpt_password": material.get("chatgpt_password") or "",
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
                "error": f"当前没有可用的邮箱渠道。{details}。请到「设置 → 邮箱设置」完成配置。",
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
    # GPT 账号与 Codex OAuth 接码工作台
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
        rows = [_public_gpt_account(row) for row in rows]
        paged = str(request.args.get("paged", default="") or "").lower() in {"1", "true", "yes"}
        page_arg = request.args.get("page", default=None, type=int)
        page_size_arg = request.args.get("page_size", default=None, type=int)
        if paged or page_arg is not None or page_size_arg is not None:
            result = _paginate_items(rows, page=max(1, int(page_arg or 1)), page_size=page_size_arg or 20)
        else:
            result = {"ok": True, "items": rows, "total": len(rows)}
        result["status_counts"] = {
            "registration": {key: sum(1 for row in rows if row.get("registration_status") == key) for key in ("registered", "unregistered", "registering", "failed")},
            "codex": {key: sum(1 for row in rows if row.get("codex_status") == key) for key in ("authorized", "unauthorized", "authorizing", "failed", "deactivated")},
            "phone": {key: sum(1 for row in rows if row.get("phone_status") == key) for key in ("verified", "unverified", "verifying", "failed")},
        }
        return jsonify(result)

    @app.put("/api/gpt-accounts/<account_id>")
    def api_gpt_account_update(account_id: str):
        """Edit the selected row in the canonical GPT account collection."""
        data = request.get_json(silent=True) or {}
        try:
            account = gpt_account_service.update_account(account_id, data)
            return jsonify({"ok": True, "account": _public_gpt_account(account)})
        except gpt_account_service.AccountBusyError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 409
        except LookupError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/gpt-accounts/<account_id>/proxy/test")
    def api_gpt_account_proxy_test(account_id: str):
        from core.account_proxy import account_proxy
        from core.proxy_test_service import test_proxies
        # Read server-side; never send the stored authenticated URL to the UI.
        row = codex_relay_service._get_account(account_id)
        if row is None:
            return jsonify({"ok": False, "error": "GPT账号不存在或已删除"}), 404
        try:
            return jsonify({"ok": True, "items": test_proxies([account_proxy(row)])})
        except ValueError:
            return jsonify({"ok": False, "error": "账号代理配置无效，请编辑后重试"}), 400

    @app.post("/api/proxies/test")
    def api_proxy_test():
        from core.proxy_test_service import test_proxies
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "请提交代理列表"}), 400
        try:
            return jsonify({"ok": True, "items": test_proxies(data.get("proxies"))})
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

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
    def api_gpt_accounts_delete():
        """Permanently delete selected GPT account rows."""
        data = request.get_json(silent=True) or {}
        account_ids = data.get("account_ids") or []
        if not isinstance(account_ids, list) or not account_ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(account_ids) > 5000:
            return jsonify({"ok": False, "error": "单次最多删除 5000 个 GPT 账号"}), 400
        try:
            return jsonify(gpt_account_service.delete_accounts(account_ids))
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
        requested_emails = list(dict.fromkeys(
            str(value or "").strip().lower() for value in (data.get("emails") or []) if str(value or "").strip()
        ))
        if requested_emails:
            if len(requested_emails) > 200:
                return jsonify({"ok": False, "error": "单次最多注册 200 个邮箱"}), 400
            already_registered = {
                str(row.get("email") or "").strip().lower()
                for row in gpt_account_service.list_accounts()
                if row.get("registration_status") == "registered"
            }
            if any(email in already_registered for email in requested_emails):
                return jsonify({"ok": False, "error": "所选邮箱中存在已注册 GPT 账号，请刷新邮箱列表"}), 409
            pool = {
                str(row.get("email") or "").strip().lower(): row
                for rows in (db.list_outlook_pool(limit=1_000_000), db.list_generic_api_email_pool(limit=1_000_000))
                for row in rows
            }
            unavailable = [email for email in requested_emails if email not in pool or pool[email].get("status") != "available"]
            if unavailable:
                return jsonify({"ok": False, "error": f"邮箱不可用或不存在：{', '.join(unavailable[:3])}"}), 400
            target_emails = requested_emails
            count = len(target_emails)
        elif requested_ids:
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
        """Start Codex OAuth for selected canonical GPT accounts."""
        data = request.get_json(silent=True) or {}
        try:
            workers = max(1, min(8, int(data.get("workers", 1))))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "workers 非法"}), 400
        try:
            selected = _select_gpt_accounts(data.get("account_ids") or [])
            result = codex_relay_service.start_jobs(
                selected["account_ids"],
                workers=workers,
                phone_ids=data.get("phone_ids") or [],
            )
        except Exception as exc:
            status = 404 if isinstance(exc, LookupError) else 409 if isinstance(exc, RuntimeError) and "尚未完成" in str(exc) else 400
            return jsonify({"ok": False, "error": str(exc)}), status
        result["emails"] = [row.get("email") for row in selected["selected"]]
        return jsonify(result)

    @app.post("/api/gpt-accounts/actions")
    def api_gpt_accounts_actions():
        """Run maintenance actions for unified GPT account selections.

        The GPT account page can contain registered rows without a Relay row.
        Bridge those rows before dispatching the existing Relay maintenance
        worker, so actions such as enabling 2FA work for freshly registered
        accounts as well as imported Relay accounts.
        """
        data = request.get_json(silent=True) or {}
        try:
            workers = max(1, min(8, int(data.get("workers", 1))))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "workers 非法"}), 400
        try:
            selected = _select_gpt_accounts(data.get("account_ids") or [])
            result = codex_relay_service.start_account_actions(
                selected["account_ids"],
                data.get("action") or "",
                workers=workers,
            )
        except Exception as exc:
            status = 404 if isinstance(exc, LookupError) else 409 if isinstance(exc, RuntimeError) and "尚未完成" in str(exc) else 400
            return jsonify({"ok": False, "error": str(exc)}), status
        result["emails"] = [row.get("email") for row in selected["selected"]]
        return jsonify(result)

    @app.post("/api/gpt-accounts/import")
    def api_codex_relay_import():
        data = request.get_json(silent=True) or {}
        try:
            result = codex_relay_service.import_accounts(
                data.get("text") or "",
                format_name=data.get("format") or "auto",
                proxy=data.get("proxy"),
            )
            result["message"] = "导入完成"
            return jsonify(result)
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/phones/import")
    def api_codex_relay_phone_import():
        data = request.get_json(silent=True) or {}
        try:
            return jsonify(codex_relay_service.import_phones(data.get("text") or ""))
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/sub2-services")
    def api_codex_relay_sub2_services():
        rows = codex_relay_service.list_sub2_services()
        return jsonify({"ok": True, "items": rows, "total": len(rows)})

    @app.post("/api/sub2-services")
    def api_codex_relay_save_sub2_service():
        try:
            service = codex_relay_service.save_sub2_service(request.get_json(silent=True) or {})
            return jsonify({"ok": True, "service": service})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.delete("/api/sub2-services/<service_id>")
    def api_codex_relay_delete_sub2_service(service_id: str):
        try:
            deleted = codex_relay_service.delete_sub2_service(service_id)
            return jsonify({"ok": True, "deleted": deleted})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/gpt-accounts/sub2/import")
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

    @app.post("/api/gpt-accounts/sub2/sync")
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

    @app.post("/api/gpt-accounts/export/copy")
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

    @app.post("/api/gpt-accounts/export/download")
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

    @app.get("/api/phones")
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

    @app.delete("/api/phones")
    def api_codex_relay_delete_phones():
        data = request.get_json(silent=True) or {}
        try:
            deleted = codex_relay_service.delete_phones(data.get("phone_ids") or [])
            return jsonify({"ok": True, "deleted": deleted})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.patch("/api/phones/available-uses")
    def api_codex_relay_adjust_phone_available_uses():
        data = request.get_json(silent=True) or {}
        try:
            return jsonify(codex_relay_service.adjust_phone_available_uses(
                data.get("phone_ids") or [],
                data.get("delta"),
            ))
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.patch("/api/phones/settings")
    def api_codex_relay_update_phone_settings():
        data = request.get_json(silent=True)
        try:
            if not isinstance(data, dict):
                raise ValueError("请求必须是 JSON 对象")
            settings = {key: data[key] for key in ("available_uses", "expiration_days") if key in data}
            return jsonify(codex_relay_service.update_phone_settings(data.get("phone_ids"), **settings))
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/codex-jobs")
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

    @app.get("/api/codex-jobs")
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

    @app.post("/api/codex-jobs/<job_id>/verification")
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

    @app.post("/api/codex-jobs/<job_id>/browser-assist")
    def api_codex_relay_browser_assist(job_id: str):
        try:
            return jsonify(codex_relay_service.submit_browser_assist(job_id))
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/codex-jobs/<job_id>/browser-focus")
    def api_codex_relay_browser_focus(job_id: str):
        try:
            return jsonify(codex_relay_service.focus_browser_assist(job_id))
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/codex-jobs/<job_id>/stop")
    def api_codex_relay_stop(job_id: str):
        try:
            return jsonify(codex_relay_service.stop_job(job_id))
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.delete("/api/codex-jobs")
    def api_codex_relay_delete_jobs():
        data = request.get_json(silent=True) or {}
        try:
            return jsonify(codex_relay_service.delete_jobs(data.get("job_ids") or []))
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.delete("/api/codex-jobs/<job_id>")
    def api_codex_relay_delete_job(job_id: str):
        try:
            deleted = codex_relay_service.delete_job(job_id)
            return jsonify({"ok": True, "deleted": deleted})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/codex-jobs/<job_id>/log")
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
        """手动生成 CloudMail Authorization Token。

        设置页编辑草稿时传入 ``persist=false``；未传该参数的旧 API 调用仍
        保持原有的即时写入行为。
        """
        data = request.get_json(silent=True) or {}
        try:
            from core.cloudmail_client import gen_token

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
            persist = data.get("persist", True) is not False
            written = []
            if persist:
                from config.env_loader import write_env_values

                updates = {"CLOUDMAIL_AUTH_TOKEN": token}
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
                "message": "CloudMail Token 已生成并保存" if persist else "CloudMail Token 已生成，请点击顶部“保存”",
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
            persist = data.get("persist", True) is not False
            if persist and updates:
                from config.env_loader import write_env_values

                write_env_values(updates)
                import config as _config_pkg
                _config_pkg.reload_all()

            domains = fetch_domains(
                force=True,
                base_url=api_base or None,
                admin_email=admin_email or None,
                password=password or None,
                token=token or None,
            )
            written = []
            if persist:
                from config.env_loader import write_env_values

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
                "message": f"已获取 {len(domains)} 个 CloudMail 可用域名并保存" if persist else f"已获取 {len(domains)} 个 CloudMail 可用域名，请点击顶部“保存”",
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
