"""Shared MQTT client factory, so every module connects with the same login and TLS settings.

Settings come from config (.env): MQTT_HOST, MQTT_PORT, MQTT_USERNAME, MQTT_PASSWORD, MQTT_TLS,
MQTT_CA_CERTS. Callbacks are attached before connecting, so no on_connect is ever missed.
"""
import ipaddress
import logging
import ssl
from typing import Callable, Optional

import paho.mqtt.client as mqtt

import config

logger = logging.getLogger(__name__)

# Commands and events on this robot are a few dozen bytes; anything bigger is dropped unread.
MAX_PAYLOAD_BYTES = 256

_warned_plaintext = False


def _is_local(host: str) -> bool:
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def make_client(
    client_id: str,
    on_connect: Optional[Callable] = None,
    on_message: Optional[Callable] = None,
    connect: bool = True,
) -> mqtt.Client:
    """Create a client with credentials/TLS applied and (by default) start connecting in the background."""
    global _warned_plaintext
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    if config.MQTT_USERNAME:
        client.username_pw_set(config.MQTT_USERNAME, config.MQTT_PASSWORD or None)
    if config.MQTT_TLS:
        context = ssl.create_default_context(cafile=config.MQTT_CA_CERTS or None)
        client.tls_set_context(context)
    elif not _is_local(config.MQTT_HOST) and not _warned_plaintext:
        _warned_plaintext = True
        logger.warning("MQTT to %s is unencrypted; set MQTT_TLS=1 (and a login) for a broker on the network",
                       config.MQTT_HOST)
    if on_connect is not None:
        client.on_connect = on_connect
    if on_message is not None:
        client.on_message = on_message
    if connect:
        client.connect_async(config.MQTT_HOST, config.MQTT_PORT, keepalive=30)
        client.loop_start()
    return client


def stop_client(client: mqtt.Client) -> None:
    client.loop_stop()
    try:
        client.disconnect()
    except Exception:  # noqa: BLE001 - already disconnected
        pass
