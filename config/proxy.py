# -*- coding: utf-8 -*-
"""
代理池配置

每次注册随机抽取一个代理，保证不同 sid 之间彼此独立，避免风控关联。

协议说明：
    - http:// / https://   HTTP(S) 代理
    - socks5://            SOCKS5（DNS 本地解析，可能泄漏）
    - socks5h://           SOCKS5（DNS 在代理端解析，推荐，避免 DNS-IP 错配）
"""
from config.env_loader import apply_env_overrides
import random
from urllib.parse import quote, unquote, urlparse


def normalize_proxy_url(value: str) -> str:
    """Normalize a proxy *endpoint*, rejecting subscription/download links.

    PROXY_POOL entries must have a host and port and cannot have a path or
    query. Providers often issue HTTPS subscription URLs with a token query;
    accepting one as an HTTPS proxy makes CONNECT fail with a misleading 400.
    Error messages include the original value for local diagnosis.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" in text:
        scheme, payload = text.split("://", 1)
        # Common four-field provider export with a prepended scheme.
        fields = payload.split(":")
        if len(fields) == 4 and fields[1].isdigit() and fields[2] and fields[3] and not any(
            marker in payload for marker in ("/", "?", "#", "@")
        ):
            host, port, username, password = fields
            text = f"{scheme.lower()}://{quote(username, safe='')}:{quote(password, safe='')}@{host}:{port}"
        else:
            text = f"{scheme.lower()}://{payload}"
    else:
        parts = text.split(":")
        if len(parts) == 4 and parts[1].isdigit() and parts[0] and parts[2] and parts[3]:
            host, port, username, password = parts
            text = f"http://{quote(username, safe='')}:{quote(password, safe='')}@{host}:{port}"
        elif len(parts) == 2 and parts[0] and parts[1].isdigit():
            text = f"http://{text}"
        else:
            raise ValueError(f"代理地址无效：请输入带端口的代理服务器地址；原值={text}")

    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https", "socks4", "socks4a", "socks5", "socks5h"}:
        raise ValueError(f"代理协议无效：仅支持 HTTP(S) 或 SOCKS 代理；原值={text}")
    if parsed.path not in ("", "/") or parsed.params or parsed.query or parsed.fragment:
        raise ValueError(f"代理池需要代理服务器 host:port，不能填写订阅下载链接；原值={text}")
    if not parsed.hostname:
        raise ValueError(f"代理地址无效：缺少服务器主机名；原值={text}")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"代理端口无效；原值={text}") from exc
    if port is None:
        raise ValueError(f"代理地址无效：缺少代理端口；订阅链接不能作为代理服务器；原值={text}")
    auth = ""
    if parsed.username is not None:
        auth = quote(unquote(parsed.username), safe="")
        if parsed.password is not None:
            auth += ":" + quote(unquote(parsed.password), safe="")
        auth += "@"
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return f"{parsed.scheme}://{auth}{host}:{port}"


# 有配置优先走代理；没有配置时使用本机网络，不假定本机存在代理服务。
# 推荐使用 socks5h://（DNS 在代理端解析），避免本地 DNS 与出口 IP 地区错配。
PROXY_POOL = []
# WebUI 保留的禁用代理记录；运行时不会从这里抽取代理。
PROXY_POOL_DISABLED = []

# 套餐/Plus 试用资格查询与 Token 生成共用这组独立网络策略，
# 避免批量请求被注册代理池中的临时本地代理拖垮，也避免无条件直连造成出口策略失控。
#   auto   = 优先使用 PLAN_CHECK_PROXY 或代理池；本地代理端口未监听时回退直连
#   proxy  = 强制使用 PLAN_CHECK_PROXY 或代理池，失败直接报错
#   direct = 始终直连
PLAN_CHECK_PROXY_MODE = "auto"

# 套餐查询 / Token 生成专用代理。留空时 auto/proxy 模式从 PROXY_POOL 选择。
# 代理可能包含账号密码，因此 WebUI 会把它保存到 .env。
PLAN_CHECK_PROXY = ""

# 查套餐 / 生成 Token 使用独立的短超时和有限重试，避免后台任务长时间卡住。
PLAN_CHECK_TIMEOUT = 15.0
PLAN_CHECK_MAX_ATTEMPTS = 2
PLAN_CHECK_RETRY_DELAY = 1.5

# 新注册账号的权益可能存在短暂同步延迟。首次查询失败，或返回 free 且暂未发现
# Plus 试用资格时，等待该秒数后再复查一次；设为 0 可关闭复查。
PLAN_CHECK_REGISTRATION_RECHECK_DELAY = 2.0

# 自动、手动和批量套餐查询共用同一个后台队列；Token 生成使用独立队列，
# 但复用这里的网络模式、请求启动间隔与随机抖动，避免批量后台请求过于集中。
PLAN_CHECK_WORKERS = 3
PLAN_CHECK_QUEUE_LIMIT = 500
PLAN_CHECK_MIN_INTERVAL = 0.4
PLAN_CHECK_JITTER = 0.3


def pick_proxy() -> str:
    """从代理池中随机抽取一个代理 URL；池为空时返回空串（即不使用代理）。"""
    candidates = [str(value).strip() for value in PROXY_POOL if str(value or "").strip()]
    return normalize_proxy_url(random.choice(candidates)) if candidates else ""


def resolve_proxy(proxy: str | None = None) -> str:
    """Explicit route > configured pool > local network; empty string pins direct.

    Invalid/unreachable configured proxies are not equivalent to no proxy. Do
    not silently switch an account's network exit after a proxy error.
    """
    return pick_proxy() if proxy is None else normalize_proxy_url(proxy)


# 兼容入口：默认每次进程启动随机选一个，作为本次注册全程的固定代理
# The legacy constant is imported during application startup. Defer invalid
# user configuration errors to the route selection so WebUI can show them.
def _legacy_proxy() -> str:
    try:
        return pick_proxy()
    except ValueError:
        return ""


PROXY = _legacy_proxy()

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    'PROXY_POOL': 'list_str_multiline',
    'PROXY_POOL_DISABLED': 'list_str_multiline',
    'PLAN_CHECK_PROXY_MODE': 'str',
    'PLAN_CHECK_PROXY': 'str',
    'PLAN_CHECK_TIMEOUT': 'float',
    'PLAN_CHECK_MAX_ATTEMPTS': 'int',
    'PLAN_CHECK_RETRY_DELAY': 'float',
    'PLAN_CHECK_REGISTRATION_RECHECK_DELAY': 'float',
    'PLAN_CHECK_WORKERS': 'int',
    'PLAN_CHECK_QUEUE_LIMIT': 'int',
    'PLAN_CHECK_MIN_INTERVAL': 'float',
    'PLAN_CHECK_JITTER': 'float',
})
PROXY = _legacy_proxy()
