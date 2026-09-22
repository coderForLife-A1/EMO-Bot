"""Shared MQTT client factory, so every module connects with the same login and TLS settings.

Settings come from config (.env): MQTT_HOST, MQTT_PORT, MQTT_USERNAME, MQTT_PASSWORD, MQTT_TLS,
MQTT_CA_CERTS. Callbacks are attached before connecting, so no on_connect is ever missed, and every
client logs a refused or failed connection (once per failure streak) instead of retrying silently.
"""
import logging
import ssl
from typing import Callable, Optional

import paho.mqtt.client as mqtt

import config
from netutil import is_local_host

logger = logging.getLogger(__name__)

# Commands and events on this robot are a few dozen bytes; anything bigger is dropped unread.
MAX_PAYLOAD_BYTES = 256

_warned_plaintext = False


def _is_failure(reason_code) -> bool:
    return bool(getattr(reason_code, "is_failure", reason_code != 0))


def make_client(
    client_id: str,
    on_connect: Optional[Callable] = None,
    on_message: Optional[Callable] = None,
    connect: bool = True,
) -> mqtt.Client:
    """Create a client with credentials/TLS applied and (by default) start connecting in the background.

    ``on_connect`` (paho callback API v2 signature) is called after the logging wrapper.
    """
    global _warned_plaintext
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    if config.MQTT_USERNAME:
        client.username_pw_set(config.MQTT_USERNAME, config.MQTT_PASSWORD or None)
    if config.MQTT_TLS:
        context = ssl.create_default_context(cafile=config.MQTT_CA_CERTS or None)
        client.tls_set_context(context)
    elif not is_local_host(config.MQTT_HOST) and not _warned_plaintext:
        _warned_plaintext = True
        logger.warning("MQTT to %s is unencrypted; set MQTT_TLS=1 (and a login) for a broker on the network",
                       config.MQTT_HOST)

    failing = [False]  # log once per failure streak, and once more when it recovers

    def logged_on_connect(c, userdata, flags, reason_code, properties) -> None:
        if _is_failure(reason_code):
            if not failing[0]:
                failing[0] = True
                logger.error("MQTT %s: broker %s:%s refused the connection: %s (check MQTT_USERNAME/"
                             "MQTT_PASSWORD and the broker ACLs)", client_id, config.MQTT_HOST, config.MQTT_PORT,
                             reason_code)
        elif failing[0]:
            failing[0] = False
            logger.info("MQTT %s: connected to %s:%s", client_id, config.MQTT_HOST, config.MQTT_PORT)
        if on_connect is not None:
            on_connect(c, userdata, flags, reason_code, properties)

    def logged_on_connect_fail(c, userdata) -> None:
        if not failing[0]:
            failing[0] = True
            logger.error("MQTT %s: can't reach broker %s:%s (%s); retrying in the background", client_id,
                         config.MQTT_HOST, config.MQTT_PORT, "TLS" if config.MQTT_TLS else "plain TCP")

    client.on_connect = logged_on_connect
    client.on_connect_fail = logged_on_connect_fail
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
