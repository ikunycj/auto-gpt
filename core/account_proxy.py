"""Account routes never implicitly inherit the registration proxy pool."""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from config.proxy import normalize_proxy_url


def account_proxy(account: dict) -> str:
    """An explicitly saved route, or local network; ignore old Profile snapshots."""
    return normalize_proxy_url(account.get("proxy") or "")


def registration_proxy(proxy: str | None) -> str:
    """Auto-bind remote proxies, not loopback/local-machine proxy endpoints.

    Private LAN proxies on other machines are still remote and must be saved.
    Explicit imports/edits are allowed to bind a loopback proxy intentionally.
    """
    normalized = normalize_proxy_url(proxy or "")
    if not normalized:
        return ""
    host = (urlparse(normalized).hostname or "").lower().rstrip(".")
    local_name = socket.gethostname().lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost") or host in {local_name, local_name.split('.')[0]}:
        return ""
    try:
        address = ipaddress.ip_address(host)
        address = getattr(address, "ipv4_mapped", None) or address
        if address.is_loopback or address.is_unspecified:
            return ""
        # A local proxy may listen on this machine's LAN address, not loopback.
        # Resolve only our own hostname; never resolve a supplied remote host.
        try:
            local_addresses = socket.getaddrinfo(socket.gethostname(), None, type=socket.SOCK_STREAM)
            if any(address == ipaddress.ip_address(item[4][0].split("%", 1)[0]) for item in local_addresses):
                return ""
        except (OSError, ValueError):
            pass
    except ValueError:
        pass
    return normalized


def proxy_display(proxy: str | None) -> str:
    """Display the complete normalized proxy URL for local auditing."""
    if not proxy:
        return "本机网络"
    try:
        return normalize_proxy_url(proxy)
    except (ValueError, TypeError):
        return str(proxy)
