"""Bounded, credential-free network diagnostics; never opens a browser or logs in."""
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
_TARGET_URL = "https://auth.openai.com/log-in"
# Bound concurrent requests across all HTTP callers, not just one batch.
_SLOTS = threading.BoundedSemaphore(4)
_MAX_PROXIES = 20


def _failure(exc: Exception) -> tuple[str, str]:
    code = getattr(exc, "code", None)
    if code == CurlECode.OPERATION_TIMEDOUT:
        return "timeout", "连接超时，请检查代理服务和网络线路"
    if code in {CurlECode.SSL_CONNECT_ERROR, CurlECode.PEER_FAILED_VERIFICATION,
                CurlECode.SSL_CERTPROBLEM, CurlECode.SSL_CACERT_BADFILE}:
        return "tls_error", "TLS 握手或证书校验失败，请检查代理协议与证书"
    if code in {CurlECode.COULDNT_RESOLVE_PROXY, CurlECode.COULDNT_RESOLVE_HOST}:
        return "dns_error", "域名解析失败，请检查代理地址和 DNS"
    if code == CurlECode.COULDNT_CONNECT:
        return "connection_error", "无法连接，请检查代理地址、端口及服务是否启动"
    if code == CurlECode.PROXY:
        return "proxy_error", "代理隧道或认证失败，请检查协议、账号和密码"
    # Never return str(exc): libcurl diagnostics can contain proxy credentials.
    return "network_error", "网络请求失败，请检查代理协议、认证信息及线路"


def _probe(session, url: str, *, read_ip: bool = False) -> dict:
    started = time.monotonic()
    body = bytearray()

    def collect(chunk):
        if len(body) < 1024:
            body.extend(chunk[:1024 - len(body)])
        return len(chunk)

    try:
        response = session.get(url, timeout=6, allow_redirects=False,
                               content_callback=collect, discard_cookies=True)
        status = response.status_code
        result = {"reachable": True, "http_status": status,
                  "ok": 200 <= status < 400, "status": "reachable"}
        if status == 407:
            result.update(ok=False, status="proxy_auth_error", message="代理认证失败（HTTP 407）")
        elif status >= 400:
            result.update(status="http_rejected", message=f"已收到 HTTP {status}，但目标拒绝访问；不能据此判断登录可用")
        else:
            result["message"] = f"目标已响应（HTTP {status}）；仅代表网络连通，不代表登录或账号可用"
        if read_ip and result["ok"] and status != 200:
            result.update(ok=False, status="invalid_response", message="出口检测未返回 IP；不跟随未知重定向")
        if read_ip and status == 200:
            import json
            try:
                result["exit_ip"] = str(ipaddress.ip_address(json.loads(body)["ip"]))
            except (ValueError, KeyError, TypeError):
                result.update(ok=False, status="invalid_response", message="出口检测服务没有返回有效 IP")
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
            network = _probe(session, _IP_URL, read_ip=True)
            target = _probe(session, _TARGET_URL)
        result.update(network=network, target=target, exit_ip=network.get("exit_ip", ""),
                      ok=bool(network["ok"] and target["ok"]),
                      status="reachable" if network["ok"] and target["ok"] else "failed",
                      message="测试完成；仅检测当前线路，不会登录账号或更改代理")
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
        except ValueError:
            return {"ok": False, "status": "invalid_proxy", "proxy_display": "代理配置无效",
                    "message": "请输入带协议和端口的代理地址，不支持订阅链接"}
        except Exception:
            return {"ok": False, "status": "network_error", "proxy_display": proxy_display(value),
                    "message": "无法完成代理测试，请检查网络与运行环境"}

    with ThreadPoolExecutor(max_workers=4) as executor:
        return list(executor.map(run, values))
