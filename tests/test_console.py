"""Laptop console (console_server.py): auth, commands, push-to-talk uploads and the audio sink."""
import asyncio
import io
import json
import threading
import wave

import pytest
from aiohttp.test_utils import TestClient, TestServer

import config
import console_server as cs
from api_routing_task import LISTEN_JOB
from behavior_tree_module import SharedState, safe_apply
from robot_status import RobotStatus


def wav(seconds=1.0, rate=16000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


class Rig:
    def __init__(self, token=""):
        self.state = SharedState()
        self.status = RobotStatus()
        self.delivered = []
        self.queue = asyncio.Queue(maxsize=2)
        self.busy = threading.Event()

        def deliver(topic, payload):
            self.delivered.append((topic, payload))
            safe_apply(self.state, topic, payload)

        self.console = cs.Console(self.state, self.status, deliver, self.queue, self.busy, token=token)


def run(rig, body):
    async def main():
        async with TestClient(TestServer(rig.console.build_app())) as client:
            return await body(client)
    return asyncio.run(main())


@pytest.mark.parametrize("text, expected", [
    ("stand", "stand"), (" REST ", "rest"), ("gesture", "gesture"), ("calibrate", "calibrate"),
    ("walk,60,0,2", "walk,60,0,2"), ("walk,500,-500,99", "walk,100,-100,10"), ("walk,30", "walk,30,0,2"),
    ("gains,1,2,3", None), ("telemetry,0", None), ("walk,x,0", None), ("stand,now", None), ("", None),
])
def test_only_known_commands_pass(text, expected):
    assert cs.parse_command(text) == expected


def test_network_console_needs_a_token():
    cs.check_settings("127.0.0.1", "", "", "")
    cs.check_settings("0.0.0.0", "secret", "", "")
    with pytest.raises(cs.ConsoleConfigError, match="CONSOLE_TOKEN"):
        cs.check_settings("0.0.0.0", "", "", "")
    with pytest.raises(cs.ConsoleConfigError, match="both"):
        cs.check_settings("127.0.0.1", "", "cert.pem", "")


def test_page_is_served_with_a_strict_policy():
    rig = Rig()

    async def body(client):
        res = await client.get("/")
        return res.status, res.headers, await res.text()

    status, headers, text = run(rig, body)
    assert status == 200 and "EMO console" in text
    assert "frame-ancestors" in headers["Content-Security-Policy"] or headers["X-Frame-Options"] == "DENY"


def test_websocket_commands_reach_the_behavior_tree():
    rig = Rig()

    async def body(client):
        async with client.ws_connect("/ws") as ws:
            first = json.loads((await ws.receive()).data)
            await ws.send_str(json.dumps({"type": "cmd", "cmd": "rest"}))
            await ws.send_str(json.dumps({"type": "estop"}))
            await ws.send_str(json.dumps({"type": "cmd", "cmd": "gains,9,9,9"}))
            while True:
                msg = json.loads((await ws.receive()).data)
                if msg["type"] == "error":
                    return first, msg

    first, error = run(rig, body)
    assert first["type"] == "state" and first["link"] is False
    assert "not allowed" in error["text"]
    assert rig.state.resting and rig.state.estop_active
    assert (config.TOPIC_LOCOMOTION_CMD, "rest") in rig.delivered


def test_token_and_origin_are_checked():
    rig = Rig(token="s3cret")

    async def body(client):
        results = []
        for url, headers in [("/ws", {}), ("/ws?token=wrong", {}), ("/ws?token=s3cret", {"Origin": "https://evil.example"})]:
            res = await client.get(url, headers=headers)
            results.append(res.status)
        async with client.ws_connect("/ws?token=s3cret") as ws:
            results.append(json.loads((await ws.receive()).data)["type"])
        return results

    assert run(rig, body) == [403, 403, 403, "state"]


def test_cross_site_upload_is_refused_even_without_a_token():
    rig = Rig()

    async def body(client):
        res = await client.post("/api/listen", data=wav(), headers={"Origin": "http://attacker.example"})
        return res.status

    assert run(rig, body) == 403
    assert rig.queue.empty()


def test_recording_is_queued_for_the_speech_pipeline():
    rig = Rig()

    async def body(client):
        res = await client.post("/api/listen", data=wav(1.5), headers={"Content-Type": "audio/wav"})
        again = await client.post("/api/listen", data=wav(1.0))  # still answering the first one
        return res.status, await res.json(), again.status

    status, reply, again = run(rig, body)
    assert status == 200 and reply["seconds"] == 1.5
    assert again == 409
    kind, data = rig.queue.get_nowait()
    assert kind == LISTEN_JOB and data[:4] == b"RIFF"
    assert rig.busy.is_set()  # released by api_routing_task after the reply
    assert rig.state.conversation_active  # the robot stands still while it answers


@pytest.mark.parametrize("data", [b"not a wav", wav(0.1), wav(40)])
def test_bad_recordings_are_rejected(data):
    rig = Rig()

    async def body(client):
        return (await client.post("/api/listen", data=data)).status

    assert run(rig, body) in (400, 413)
    assert rig.queue.empty() and not rig.busy.is_set()


def test_cancelled_recording_lowers_the_conversation_flag():
    rig = Rig()
    assert rig.console.handle_message({"type": "listening"}) is None
    assert rig.state.conversation_active
    rig.console.handle_message({"type": "cancel"})
    assert not rig.state.conversation_active


class FakeLocal:
    def __init__(self):
        self.played = []

    async def play_wav(self, data):
        self.played.append(data)

    async def play_fallback(self):
        self.played.append("fallback")


def test_sink_uses_the_pi_speaker_until_a_page_is_open(monkeypatch):
    monkeypatch.setattr(cs, "PLAYBACK_MARGIN_S", 0)
    rig = Rig()
    local = FakeLocal()
    sink = cs.ConsoleSink(rig.console, "auto", local)
    page = cs._Page(ws=None)

    async def main():
        await sink.play_wav(wav(0.01))
        assert await sink.speak_text("hi") is False  # no page: can't use the browser's voice
        rig.console.pages.add(page)
        await sink.play_wav(wav(0.01))
        assert await sink.speak_text("hi") is True

    asyncio.run(main())
    assert len(local.played) == 1
    sent = []
    while not page.queue.empty():
        sent.append(page.queue.get_nowait())
    kinds = [k for k, _ in sent]
    assert kinds.count("bytes") == 1
    assert {"type": "say", "text": "hi"} in [p for k, p in sent if k == "json"]


def test_console_only_mode_never_uses_the_pi_speaker(monkeypatch):
    monkeypatch.setattr(cs, "PLAYBACK_MARGIN_S", 0)
    rig = Rig()
    local = FakeLocal()
    sink = cs.ConsoleSink(rig.console, "console", local)
    asyncio.run(sink.play_wav(wav(0.01)))
    asyncio.run(sink.play_fallback())
    assert local.played == []


def test_snapshot_reports_sensors():
    rig = Rig()
    rig.status.on_line("T,52,0,0,B", now=100.0)
    rig.status.on_line("ACK,D,321", now=100.0)
    snap = rig.console.snapshot(now=100.5)
    assert snap["pitch"] == 5.2 and snap["mode"] == "B" and snap["distance"] == 321 and snap["link"]
    later = rig.console.snapshot(now=110.0)
    assert later["pitch"] is None and later["distance"] is None and not later["link"]
    json.dumps(later)  # everything in it is serialisable


def test_console_refuses_a_level_calibration_that_would_store_a_bad_offset():
    rig = Rig()
    assert "no IMU telemetry" in rig.console.handle_message({"type": "cmd", "cmd": "calibrate"})
    rig.status.on_line("T,741,0,0,O")  # IMU lying loose at 74 deg
    assert "from level" in rig.console.handle_message({"type": "cmd", "cmd": "calibrate"})
    rig.status.on_line("T,20,55,0,O")  # upright but swinging
    assert "moving" in rig.console.handle_message({"type": "cmd", "cmd": "calibrate"})
    rig.status.on_line("T,20,3,0,O")
    assert rig.console.handle_message({"type": "cmd", "cmd": "calibrate"}) is None
    assert (config.TOPIC_LOCOMOTION_CMD, "calibrate") in rig.delivered
