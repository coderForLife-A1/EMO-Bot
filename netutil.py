"""Small network helpers shared by the MQTT client and the API pipeline, so both agree on what "local" means."""
import ipaddress
from typing import Optional
from urllib.parse import urlsplit


def is_local_host(host: Optional[str]) -> bool:
    """True for this machine: "localhost" or any loopback address (127.0.0.0/8, ::1)."""
    if host is None:
        return False
    host = host.strip("[]").lower()
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_lan_host(host: Optional[str]) -> bool:
    """True for this machine or a private-network address (10/8, 172.16/12, 192.168/16, link-local, fd00::/8)
    or an mDNS name such as "laptop.local". Other hostnames could resolve anywhere, so they are not LAN."""
    if is_local_host(host):
        return True
    host = host.strip("[]").lower()
    if host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private and not ip.is_unspecified


def require_https(url: str, allow_lan: bool = False) -> None:
    """Refuse plain HTTP unless the server is on this machine (or, with ``allow_lan``, on the local network).

    API keys go in request headers, so key-bearing URLs keep the default. ``allow_lan`` is for the laptop LLM,
    which gets no key. Called right before each request, so cached phrases (which send nothing) are never blocked.
    """
    parts = urlsplit(url)
    if parts.scheme == "https":
        return
    if parts.scheme == "http" and (is_lan_host(parts.hostname) if allow_lan else is_local_host(parts.hostname)):
        return
    raise RuntimeError(f"refusing to send data to {parts.scheme}://{parts.hostname}: use https")
