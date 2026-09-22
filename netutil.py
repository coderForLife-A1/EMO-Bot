"""Small network helpers shared by the MQTT client and the API pipeline, so both agree on what "local" means."""
import ipaddress
from typing import Optional


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
