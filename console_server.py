"""Laptop console: the robot's face and status display, its microphone and its speaker, in a browser.

The robot has no screen, mic or speaker of its own; a laptop provides them. main.py runs this server;
open http://localhost:8080 on the laptop (through an SSH tunnel, see RUNNING.md section 9b) and:

- the page shows EMO's animated eyes, which follow the robot's state (idle, listening, thinking, speaking,
  resting, fallen, E-stop, posture reminder), plus link/IMU/pitch/distance status and recent events;
- hold the talk button (or the space bar) to speak: with MIC_SOURCE=robot the Pi records its own mic (the
  USB webcam's, robot_mic.py); otherwise the browser records the laptop mic and uploads a WAV (POST
  /api/listen). Either way the recording goes through the normal LISTEN_JOB pipeline;
- type a question, or click one of the preset examples: Gemini answers in plain text under the face
  (no speech either way);
- the robot's voice plays in the browser (ConsoleSink); cues it can't voice without an ElevenLabs key
  are shown as text instead;
- buttons send the same commands as robot/locomotion/cmd and robot/error (stand, rest, walk, E-stop...).

Security: by default it listens on 127.0.0.1 only. Binding it to the network requires CONSOLE_TOKEN, and
every WebSocket/upload must come from the page itself (Origin check), so another website open in the same
browser can't drive the robot.
"""
import asyncio
import contextlib
import hmac
import io
import json
import logging
import ssl
import threading
import time
import wave
from typing import Callable, Optional
from urllib.parse import urlsplit

from aiohttp import WSMsgType, web

import config
from api_routing_task import (
    ASK_JOB,
    EXAMPLES,
    LISTEN_JOB,
    LOCAL_SPEAKER,
    MAX_QUESTION_CHARS,
    LocalSpeaker,
    can_listen,
)
from frame_mailbox import CONSOLE_FRAMES, MAX_FRAME_BYTES
from netutil import is_local_host

logger = logging.getLogger(__name__)

WEB_DIR = config.REPO_ROOT / "web"
SNAPSHOT_PERIOD_S = 0.1  # state pushed to every open page at 10 Hz
CLIENT_QUEUE_MAX = 32  # per page; a stalled page loses old state updates, never blocks the robot
MAX_UPLOAD_BYTES = 2_000_000  # a 30 s, 16 kHz, 16-bit mono WAV is ~960 kB
MIN_RECORDING_S = 0.3
MAX_RECORDING_S = 30.0
FRESH_TELEMETRY_S = 1.0
FRESH_DISTANCE_S = 3.0
SPEECH_CHARS_PER_S = 14.0  # rough speaking rate, to wait for browser speech synthesis to finish
PLAYBACK_MARGIN_S = 0.3

SIMPLE_COMMANDS = {"stand", "rest", "stop", "gesture", "calibrate", "distance"}
LEVEL_MAX_DEG = 15.0  # same guard as tools/calibrate_sensors.py imu-level
LEVEL_STILL_DPS = 2.0

Deliver = Callable[[str, str], None]


class ConsoleConfigError(RuntimeError):
    pass


def check_settings(host: str, token: str, cert: str, key: str) -> None:
    """Refuse an open, unauthenticated console: anyone on the network could drive the robot."""
    if not is_local_host(host) and not token:
        raise ConsoleConfigError(f"CONSOLE_HOST={host} is reachable from the network: set CONSOLE_TOKEN "
                                 "(e.g. `openssl rand -hex 16`), or keep 127.0.0.1 and use an SSH tunnel")
    if bool(cert) != bool(key):
        raise ConsoleConfigError("set both CONSOLE_CERT and CONSOLE_KEY for HTTPS, or neither")


def parse_command(text: str) -> Optional[str]:
    """Validate a console command; returns the robot/locomotion/cmd payload, or None if not allowed."""
    parts = [p.strip() for p in str(text).strip().lower().split(",")]
    if parts[0] in SIMPLE_COMMANDS and len(parts) == 1:
        return parts[0]
    if parts[0] == "walk" and 2 <= len(parts) <= 4:
        try:
            numbers = [float(p) for p in parts[1:]]
        except ValueError:
            return None
        speed = max(-100, min(100, round(numbers[0])))
        turn = max(-100, min(100, round(numbers[1]))) if len(numbers) > 1 else 0
        seconds = max(0.0, min(10.0, numbers[2])) if len(numbers) > 2 else 2.0
        return f"walk,{speed},{turn},{seconds:g}"
    return None


def wav_duration_s(data: bytes) -> float:
    """Length of a WAV in seconds. Raises ValueError for anything that isn't a PCM WAV."""
    try:
        with wave.open(io.BytesIO(data)) as w:
            if w.getnchannels() not in (1, 2) or w.getsampwidth() != 2 or not 8000 <= w.getframerate() <= 48000:
                raise ValueError("expected 16-bit PCM, mono or stereo, 8-48 kHz")
            return w.getnframes() / w.getframerate()
    except (wave.Error, EOFError) as exc:
        raise ValueError(f"not a WAV file: {exc}") from exc


def same_origin(request: web.Request) -> bool:
    """True if the request comes from our own page (or from a non-browser client that sends no Origin)."""
    origin = request.headers.get("Origin")
    if origin is None:
        return True
    return urlsplit(origin).netloc.lower() == request.host.lower()


class _Page:
    """One open console page: an outgoing queue drained by its own writer, so a slow page can't stall others."""

    def __init__(self, ws: web.WebSocketResponse):
        self.ws = ws
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=CLIENT_QUEUE_MAX)

    def send(self, item: tuple[str, object], droppable: bool = True) -> None:
        while True:
            try:
                self.queue.put_nowait(item)
                return
            except asyncio.QueueFull:
                if droppable:
                    return  # the next snapshot replaces it anyway
                with contextlib.suppress(asyncio.QueueEmpty):
                    self.queue.get_nowait()  # audio and captions must arrive: drop the oldest instead

    async def writer(self) -> None:
        while True:
            kind, payload = await self.queue.get()
            if kind == "json":
                await self.ws.send_str(json.dumps(payload))
            else:
                await self.ws.send_bytes(payload)


class Console:
    def __init__(
        self,
        state,
        status,
        deliver: Deliver,
        speech_queue: asyncio.Queue,
        busy_event: threading.Event,
        publisher=None,
        token: str = "",
        mic=None,
    ):
        self.state = state  # behavior_tree_module.SharedState (read-only here; changes go through deliver)
        self.status = status  # robot_status.RobotStatus
        self.deliver = deliver  # (topic, payload) -> applied to SharedState on the loop, like an MQTT message
        self.speech_queue = speech_queue
        self.busy = busy_event
        self.publisher = publisher
        self.token = token
        self.pages: set[_Page] = set()
        self.voice = {"status": "idle", "heard": "", "reply": "", "error": ""}
        self.recording = False  # a page (or the robot's mic, for a page) is recording right now
        self.mic = mic  # robot_mic.RobotMic when MIC_SOURCE=robot: the Pi records, not the browser
        self.camera_wanted = config.ENABLE_VISION and config.CAMERA_SOURCE == "console"

    # ------------------------------------------------------------ to the pages
    def has_pages(self) -> bool:
        return bool(self.pages)

    def broadcast(self, message: dict, droppable: bool = False) -> None:
        for page in list(self.pages):
            page.send(("json", message), droppable)

    def broadcast_audio(self, wav_bytes: bytes) -> None:
        for page in list(self.pages):
            page.send(("bytes", wav_bytes), droppable=False)

    def voice_event(self, kind: str, text: str = "") -> None:
        if kind in ("heard", "reply", "error"):
            self.voice[kind] = text
        else:
            self.voice["status"] = kind
            if kind in ("listening", "thinking"):  # a new question or cue: the last exchange's captions go
                self.voice.update(heard="", reply="", error="")
        self.broadcast({"type": "voice", **self.voice})

    def snapshot(self, now: Optional[float] = None) -> dict:
        now = time.monotonic() if now is None else now
        s, st = self.state, self.status
        fresh = st.telemetry_fresh(now, FRESH_TELEMETRY_S)
        cal = st.calibration
        return {
            "type": "state",
            "link": st.link_up(now),
            "banner": st.banner,
            "imu_fault": s.imu_fault,
            "pitch": st.pitch_deg if fresh else None,
            "rate": st.rate_dps if fresh else None,
            "correction": st.correction_deg if fresh else None,
            "mode": st.mode if fresh else "",
            "distance": st.distance_mm if now - st.distance_at < FRESH_DISTANCE_S else None,
            "tof_missing": st.tof_missing,
            "estop": s.estop_active,
            "fallen": s.fallen,
            "resting": s.resting,
            "standing": s.legs_standing,
            "walking": s.walking,
            "posture_poor": s.posture_poor,
            "conversation": s.conversation_active,
            "busy": self.busy.is_set(),
            "camera": self.camera_wanted,
            "voice": self.voice,
            "events": [f"{t} {line}" for t, line in list(st.events)[-8:]],
            "keys": {"openai": bool(config.OPENAI_API_KEY), "elevenlabs": bool(config.ELEVENLABS_API_KEY),
                     "gemini": bool(config.GEMINI_API_KEY), "stt": bool(config.STT_URL)},
            "can_listen": can_listen(),
            "robot_mic": self.mic is not None,
            "mic_level": round(self.mic.level, 2) if self.mic is not None and self.mic.recording else 0,
            "calibration": {
                "tof": bool(cal.tof_points),
                "tof_scale": cal.tof_scale,
                "tof_offset_mm": cal.tof_offset_mm,
                "imu_level": cal.imu_level_offset_deg is not None,
                "imu_sign_ok": cal.imu_pitch_sign_ok,
            },
        }

    # ------------------------------------------------------------ from the pages
    def _authorised(self, request: web.Request) -> bool:
        if not same_origin(request):
            return False
        if not self.token:
            return True
        supplied = request.query.get("token") or request.headers.get("X-Console-Token", "")
        return hmac.compare_digest(supplied.encode(), self.token.encode())

    def _set_conversation(self, active: bool) -> None:
        payload = "1" if active else "0"
        self.deliver(config.TOPIC_WAKE_FLAG, payload)
        if self.publisher is not None:
            with contextlib.suppress(Exception):
                self.publisher.publish(config.TOPIC_WAKE_FLAG, payload, qos=0, retain=False)

    def handle_message(self, message: dict) -> Optional[str]:
        """Apply one message from a page. Returns an error text for the page, or None."""
        kind = message.get("type")
        if kind == "estop":
            self.deliver(config.TOPIC_ERROR, "error")
            logger.warning("E-stop pressed on the console")
            return None
        if kind == "clear":
            self.deliver(config.TOPIC_ERROR, "clear")
            logger.info("E-stop cleared on the console")
            return None
        if kind == "cmd":
            payload = parse_command(message.get("cmd", ""))
            if payload is None:
                return f"command not allowed: {message.get('cmd')!r}"
            if payload == "calibrate":
                refusal = self._level_refusal()
                if refusal:
                    return refusal
            self.deliver(config.TOPIC_LOCOMOTION_CMD, payload)
            return None
        if kind == "listening":  # a page started recording: stand still and show the listening face
            if self.busy.is_set():
                return "busy: still answering the last question"
            if self.mic is not None:
                try:
                    self.mic.start()
                except Exception as exc:  # noqa: BLE001 - no mic, device busy...: tell the page
                    logger.warning("Robot microphone failed: %s", exc)
                    return f"robot mic: {exc}"
            self.recording = True
            self._set_conversation(True)
            self.voice_event("listening")
            return None
        if kind == "stop":  # talk button released: the robot's recording goes to the pipeline
            if self.mic is None or not self.mic.recording:
                return None
            return self.queue_recording(self.mic.stop())
        if kind == "ask":  # a typed question (or a preset example): answered as text by api_routing_task
            return self._ask(message.get("text", ""))
        if kind == "cancel":  # recording aborted (too short, mic error)
            if self.mic is not None:
                self.mic.cancel()
            self.recording = False
            if not self.busy.is_set():
                self._set_conversation(False)
                self.voice_event("idle")
            return None
        return f"unknown message type {kind!r}"

    def _ask(self, text) -> Optional[str]:
        question = " ".join(str(text).split())
        if not question:
            return "type a question first"
        if len(question) > MAX_QUESTION_CHARS:
            return f"question too long (max {MAX_QUESTION_CHARS} characters)"
        if self.busy.is_set() or self.recording:
            return "busy: still answering the last question"
        self.busy.set()  # released by api_routing_task once the answer is shown
        self._set_conversation(True)
        try:
            self.speech_queue.put_nowait((ASK_JOB, question))
        except asyncio.QueueFull:
            self.busy.clear()
            self._set_conversation(False)
            return "speech queue full, try again"
        return None

    def _level_refusal(self, now: Optional[float] = None) -> Optional[str]:
        """Why a level calibration would store a bad offset right now, or None if it looks sane."""
        st = self.status
        if not st.telemetry_fresh(now, FRESH_TELEMETRY_S) or st.pitch_deg is None:
            return "can't calibrate: no IMU telemetry from the controller"
        raw = st.pitch_deg + (st.calibration.imu_level_offset_deg or 0.0)
        if abs(raw) > LEVEL_MAX_DEG:
            return (f"can't calibrate: the IMU reads {raw:+.0f}° from level. Is the robot upright and the MPU6050 "
                    "fixed flat to the pelvis? (tools/calibrate_sensors.py imu-level --force overrides)")
        if st.rate_dps is not None and abs(st.rate_dps) > LEVEL_STILL_DPS:
            return "can't calibrate: the robot is moving, hold it still"
        return None

    # ------------------------------------------------------------ HTTP
    async def index(self, request: web.Request) -> web.StreamResponse:
        return web.FileResponse(WEB_DIR / "console.html", headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline' blob:; "
                                       "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
                                       "media-src 'self' blob:; img-src 'self' data:",
            "X-Frame-Options": "DENY",
            "Permissions-Policy": "microphone=(self)",
        })

    async def ws_handler(self, request: web.Request) -> web.StreamResponse:
        if not self._authorised(request):
            raise web.HTTPForbidden(text="bad token or origin")
        ws = web.WebSocketResponse(heartbeat=10, max_msg_size=MAX_FRAME_BYTES + 1024)
        await ws.prepare(request)
        page = _Page(ws)
        self.pages.add(page)
        writer = asyncio.create_task(page.writer())
        logger.info("Console opened from %s (%d open)", request.remote, len(self.pages))
        page.send(("json", self.snapshot()))
        page.send(("json", {"type": "voice", **self.voice}))
        page.send(("json", {"type": "examples", "items": list(EXAMPLES)}))
        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    if self.camera_wanted:  # a webcam frame for the vision task (latest one only)
                        CONSOLE_FRAMES.put(msg.data)
                    continue
                if msg.type != WSMsgType.TEXT or len(msg.data) > 4096:
                    continue
                try:
                    message = json.loads(msg.data)
                    error = self.handle_message(message) if isinstance(message, dict) else "expected an object"
                except ValueError:
                    error = "invalid JSON"
                if error:
                    page.send(("json", {"type": "error", "text": error}), droppable=False)
        finally:
            self.pages.discard(page)
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)
            if self.recording and not self.pages:
                self.handle_message({"type": "cancel"})
            logger.info("Console closed (%d open)", len(self.pages))
        return ws

    async def listen_handler(self, request: web.Request) -> web.StreamResponse:
        """POST a WAV recorded on the laptop: it goes through the same pipeline as the wake-word recording."""
        if not self._authorised(request):
            raise web.HTTPForbidden(text="bad token or origin")
        if request.content_length is not None and request.content_length > MAX_UPLOAD_BYTES:
            raise web.HTTPRequestEntityTooLarge(max_size=MAX_UPLOAD_BYTES, actual_size=request.content_length)
        data = await request.read()
        self.recording = False
        try:
            seconds = wav_duration_s(data)
        except ValueError as exc:
            self.handle_message({"type": "cancel"})
            raise web.HTTPBadRequest(text=str(exc)) from exc
        if not MIN_RECORDING_S <= seconds <= MAX_RECORDING_S:
            self.handle_message({"type": "cancel"})
            raise web.HTTPBadRequest(text=f"recording must be {MIN_RECORDING_S}-{MAX_RECORDING_S:.0f} s long")
        if self.busy.is_set():
            raise web.HTTPConflict(text="busy: still answering the last question")
        if not self._queue_listen(data):
            raise web.HTTPServiceUnavailable(text="speech queue full, try again")
        logger.info("Console recording queued (%.1f s)", seconds)
        return web.json_response({"ok": True, "seconds": round(seconds, 2)})

    def _queue_listen(self, data: bytes) -> bool:
        self.busy.set()  # released by api_routing_task once the answer has played
        self._set_conversation(True)
        try:
            self.speech_queue.put_nowait((LISTEN_JOB, data))
        except asyncio.QueueFull:
            self.busy.clear()
            self._set_conversation(False)
            return False
        self.voice_event("thinking")
        return True

    def queue_recording(self, data: bytes) -> Optional[str]:
        """Hand the robot mic's recording (WAV) to api_routing_task. Returns an error text, or None if queued."""
        self.recording = False
        seconds = wav_duration_s(data)
        if seconds < MIN_RECORDING_S:
            self.handle_message({"type": "cancel"})
            return "hold the button while you speak"
        if not self._queue_listen(data):
            self.handle_message({"type": "cancel"})
            return "speech queue full, try again"
        logger.info("Robot mic recording queued (%.1f s)", seconds)
        return None

    def build_app(self) -> web.Application:
        app = web.Application(client_max_size=MAX_UPLOAD_BYTES)
        app.router.add_get("/", self.index)
        app.router.add_get("/ws", self.ws_handler)
        app.router.add_post("/api/listen", self.listen_handler)
        return app


class ConsoleSink(LocalSpeaker):
    """Audio sink for api_routing_task: the laptop while a console page is open, else the Pi's speaker.

    mode (AUDIO_OUTPUT): "auto" (default), "console" (only the laptop: silent without a page) or "local".
    Plays are awaited for the length of the audio so cues and answers never talk over each other.
    """

    def __init__(self, console: Console, mode: str = "auto", local: LocalSpeaker = LOCAL_SPEAKER):
        self.console = console
        self.mode = mode if mode in ("auto", "console", "local") else "auto"
        self.local = local

    def _to_console(self) -> bool:
        return self.mode == "console" or (self.mode == "auto" and self.console.has_pages())

    async def play_wav(self, wav_bytes: bytes) -> None:
        if not self._to_console():
            if self.mode != "console":
                await self.local.play_wav(wav_bytes)
            return
        self.console.broadcast_audio(wav_bytes)
        with contextlib.suppress(ValueError):
            await asyncio.sleep(wav_duration_s(wav_bytes) + PLAYBACK_MARGIN_S)

    async def play_fallback(self) -> None:
        if not self._to_console():
            if self.mode != "console":
                await self.local.play_fallback()
            return
        try:
            data = config.NETWORK_ERROR_FILE.read_bytes()
        except OSError as exc:
            logger.warning("Fallback sound %s unreadable: %s", config.NETWORK_ERROR_FILE, exc)
            return
        await self.play_wav(data)

    async def speak_text(self, text: str) -> bool:
        """No cloud voice: show the cue as text under the face for as long as it would take to say it."""
        if not self._to_console() or not self.console.has_pages():
            return False
        self.console.voice_event("heard", "")
        self.console.voice_event("reply", text)
        await asyncio.sleep(len(text) / SPEECH_CHARS_PER_S + PLAYBACK_MARGIN_S)
        return True

    def event(self, kind: str, text: str = "") -> None:
        self.console.voice_event(kind, text)


def _ssl_context(cert: str, key: str) -> Optional[ssl.SSLContext]:
    if not cert:
        return None
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(cert, key)
    return context


async def console_task(
    console: Console,
    host: Optional[str] = None,
    port: Optional[int] = None,
    cert: Optional[str] = None,
    key: Optional[str] = None,
) -> None:
    host = host or config.CONSOLE_HOST
    port = port if port is not None else config.CONSOLE_PORT
    cert = config.CONSOLE_CERT if cert is None else cert
    key = config.CONSOLE_KEY if key is None else key
    check_settings(host, console.token, cert, key)

    runner = web.AppRunner(console.build_app(), access_log=None)
    await runner.setup()
    try:
        site = web.TCPSite(runner, host, port, ssl_context=_ssl_context(cert, key))
        await site.start()
        scheme = "https" if cert else "http"
        shown = "localhost" if is_local_host(host) else host
        logger.info("Console on %s://%s:%d/%s", scheme, shown, port, "?token=..." if console.token else "")
        if not cert and not is_local_host(host):
            logger.warning("Console is plain HTTP on the network: browsers only allow the mic on https:// or "
                           "localhost (use an SSH tunnel, or set CONSOLE_CERT/CONSOLE_KEY)")
        while True:
            if console.mic is not None and console.mic.too_long():  # talk button held too long: answer now
                error = console.handle_message({"type": "stop"})
                if error:
                    console.broadcast({"type": "error", "text": error})
            if console.pages:
                console.broadcast(console.snapshot(), droppable=True)
            await asyncio.sleep(SNAPSHOT_PERIOD_S)
    finally:
        for page in list(console.pages):
            with contextlib.suppress(Exception):
                await page.ws.close()
        await runner.cleanup()
