"""Round-2 regressions that don't belong to one module: MQTT login/TLS (#16), speech cues never evict a
recording (#17), private simulator build directory (#23), one shared MQTT/camera code path (#26)."""
import asyncio
import importlib
import sys
from pathlib import Path

import config
import main
import mqtt_client

ROOT = Path(__file__).resolve().parents[1]


def test_mqtt_login_and_tls_are_applied(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MQTT_USERNAME", "robot")
    monkeypatch.setattr(config, "MQTT_PASSWORD", "s3cret")
    monkeypatch.setattr(config, "MQTT_TLS", True)
    monkeypatch.setattr(config, "MQTT_CA_CERTS", "")
    client = mqtt_client.make_client("test", connect=False)
    assert client._username == b"robot" and client._password == b"s3cret"
    assert client._ssl_context is not None


def test_mqtt_defaults_are_anonymous_plaintext_on_localhost(monkeypatch):
    monkeypatch.setattr(config, "MQTT_USERNAME", "")
    monkeypatch.setattr(config, "MQTT_TLS", False)
    client = mqtt_client.make_client("test", connect=False)
    assert client._username is None and client._ssl_context is None


def test_tls_port_default(monkeypatch):
    monkeypatch.setenv("MQTT_TLS", "1")
    monkeypatch.setenv("MQTT_PORT", "")  # as in .env.example: empty = the default for the protocol
    try:
        assert importlib.reload(config).MQTT_PORT == 8883
    finally:
        monkeypatch.delenv("MQTT_TLS")
        importlib.reload(config)


def test_callbacks_are_attached_before_connecting():
    calls = []
    client = mqtt_client.make_client("test", on_connect=lambda *a: calls.append("c"),
                                     on_message=lambda *a: calls.append("m"), connect=False)
    assert client.on_connect is not None and client.on_message is not None


def test_every_module_uses_the_shared_mqtt_factory():
    """#16/#26: no module builds its own paho client any more (they'd skip the login/TLS settings)."""
    offenders = [p.name for p in ROOT.glob("*.py")
                 if p.name != "mqtt_client.py" and "mqtt.Client(" in p.read_text(encoding="utf-8")]
    assert offenders == []


def test_speech_cue_never_evicts_a_recording():
    """#17: a full speech queue used to drop its oldest job, which could be a recording."""
    async def run():
        q = asyncio.Queue(maxsize=3)
        q.put_nowait(("listen", b"RIFF..."))
        for _ in range(10):  # an E-stop storm queues many cues
            main.queue_cue(q, "Emergency stop.")
        return [q.get_nowait()[0] for _ in range(q.qsize())]

    assert asyncio.run(run()) == ["listen", "say", "say"]


def test_simulator_builds_into_a_private_directory(tmp_path, monkeypatch):
    """#23: the executable used to live at a predictable path in the shared temp dir."""
    sys.path.insert(0, str(ROOT / "tools"))
    try:
        sim_nano = importlib.import_module("sim_nano")
    finally:
        sys.path.remove(str(ROOT / "tools"))
    commands = []
    monkeypatch.setattr(sim_nano.subprocess, "run", lambda cmd, check: commands.append(cmd))
    monkeypatch.setattr(sim_nano.shutil, "which", lambda _: "g++")
    exe = sim_nano.build("esp32", tmp_path)
    assert exe.parent == tmp_path
    assert "-o" in commands[0] and commands[0][commands[0].index("-o") + 1] == str(exe)
    source = (ROOT / "tools" / "sim_nano.py").read_text(encoding="utf-8")
    assert "mkdtemp" in source and "gettempdir" not in source
