"""Bounded network diagnostics; never opens a browser or logs in."""
from __future__ import annotations

import ipaddress
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from curl_cffi import CurlECode, CurlOpt
from curl_cffi import requests

from config.proxy import normalize_proxy_url
from core.account_proxy import proxy_display

_IP_URL = "https://api.ipify.org?format=json"
# Bound concurrent requests across all HTTP callers, not just one batch.
_SLOTS = threading.BoundedSemaphore(4)
_MAX_PROXIES = 20


def _failure(exc: Exception) -> tuple[str, str]:
    code = getattr(exc, "code", None)
    if code == CurlECode.OPERATION_TIMEDOUT:
        status = "timeout"
    elif code in {CurlECode.SSL_CONNECT_ERROR, CurlECode.PEER_FAILED_VERIFICATION,
                  CurlECode.SSL_CERTPROBLEM, CurlECode.SSL_CACERT_BADFILE}:
        status = "tls_error"
    elif code in {CurlECode.COULDNT_RESOLVE_PROXY, CurlECode.COULDNT_RESOLVE_HOST}:
        status = "dns_error"
    elif code == CurlECode.COULDNT_CONNECT:
        status = "connection_error"
    elif code == CurlECode.PROXY:
        status = "proxy_error"
    else:
        status = "network_error"
    return status, str(exc)


def _probe(session) -> dict:
    started = time.monotonic()
    body = bytearray()

    def collect(chunk):
        if len(body) < 1024:
            body.extend(chunk[:1024 - len(body)])
        return len(chunk)

    try:
        response = session.get(_IP_URL, timeout=6, allow_redirects=False,
                               content_callback=collect, discard_cookies=True)
        status = response.status_code
        result = {"reachable": True, "http_status": status,
                  "ok": False, "status": "http_rejected"}
        result["response_body"] = bytes(body).decode("utf-8", errors="replace")
        if status == 407:
            result.update(status="proxy_auth_error", message="代理认证失败（HTTP 407）")
        elif status >= 400:
            result["message"] = f"出口检测服务返回 HTTP {status}，未确认代理连通，请稍后重试"
        elif status != 200:
            result.update(status="invalid_response", message="出口检测未返回 IP；不跟随未知重定向")
        else:
            import json
            try:
                result["exit_ip"] = str(ipaddress.ip_address(json.loads(body)["ip"]))
                result.update(ok=True, status="reachable", message="出口检测成功，当前线路连通")
            except (ValueError, KeyError, TypeError):
                result.update(status="invalid_response", message="出口检测服务没有返回有效 IP")
    except Exception as exc:
        code, message = _failure(exc)
        result = {"ok": False, "reachable": False, "status": code, "message": message}
    result["latency_ms"] = round((time.monotonic() - started) * 1000)
    return result


def test_proxy(proxy: str = "") -> dict:
    """Use exactly this route, including an explicit empty/direct route."""
    route = normalize_proxy_url(proxy)
    result = {"route": "proxy" if route else "direct", "proxy_display": proxy_display(route)}
    if not _SLOTS.acquire(blocking=False):
        return {**result, "ok": False, "status": "busy", "message": "代理测试繁忙，请稍后重试"}
    try:
        # Explicit libcurl options also defeat ALL_PROXY and NO_PROXY inherited
        # from the backend process. trust_env alone is insufficient in some versions.
        with requests.Session(trust_env=False, verify=True, curl_options={
            CurlOpt.PROXY: route, CurlOpt.NOPROXY: "",
            CurlOpt.PROXY_SSL_VERIFYPEER: 1, CurlOpt.PROXY_SSL_VERIFYHOST: 2,
        }) as session:
            network = _probe(session)
        result.update(network=network, exit_ip=network.get("exit_ip", ""),
                      ok=network["ok"], status=network["status"],
                      message="仅检测出口 IP 和网络连通性，不代表 ChatGPT 登录或注册可用")
        return result
    finally:
        _SLOTS.release()


def test_proxies(values: list[str]) -> list[dict]:
    if not isinstance(values, list) or not 1 <= len(values) <= _MAX_PROXIES:
        raise ValueError(f"每次测试 1–{_MAX_PROXIES} 条代理；空字符串表示本机网络")
    if any(not isinstance(value, str) or len(value) > 4096 for value in values):
        raise ValueError("代理必须是长度不超过 4096 的字符串")

    def run(value):
        try:
            return test_proxy(value)
        except ValueError as exc:
            return {"ok": False, "status": "invalid_proxy", "proxy_display": proxy_display(value),
                    "message": str(exc)}
        except Exception:
            return {"ok": False, "status": "network_error", "proxy_display": proxy_display(value),
                    "message": "无法完成代理测试，请检查网络与运行环境"}

    with ThreadPoolExecutor(max_workers=4) as executor:
        return list(executor.map(run, values))
