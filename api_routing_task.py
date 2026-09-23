"""Cloud speech pipeline: Whisper (speech to text) -> GPT (reply) -> ElevenLabs (text to speech) -> aplay.

Also speaks short cues from the behavior tree ("I fell over", posture reminder), caching them.
On any failure it plays assets/network_error.wav. Test keys and speaker with
`python api_routing_task.py "Hello"`.
"""
import asyncio
import contextlib
import io
import logging
import sys
import threading
import wave
from typing import Optional
from urllib.parse import urlsplit

import httpx

import config
from netutil import is_local_host

logger = logging.getLogger(__name__)

LISTEN_JOB = "listen"  # (LISTEN_JOB, wav_bytes): full Whisper -> LLM -> TTS cascade
SAY_JOB = "say"  # (SAY_JOB, text): speak a fixed phrase (behavior tree cues)

# ElevenLabs returns MP3 unless output_format is set; the Accept header is ignored.
# Raw 16 kHz PCM is available on every plan and is wrapped into a WAV for aplay.
TTS_OUTPUT_FORMAT = "pcm_16000"
TTS_SAMPLE_RATE = 16_000

SYSTEM_PROMPT = (
    "You are a concise, helpful assistant inside a desktop companion robot. "
    "Respond in one or two short sentences."
)

_phrase_cache: dict[str, bytes] = {}


def require_https(url: str) -> None:
    """API keys go in request headers: refuse plain HTTP unless the server is on this machine.

    Called right before each request, so cached phrases (which send nothing) are never blocked.
    """
    parts = urlsplit(url)
    if parts.scheme == "https":
        return
    if parts.scheme == "http" and is_local_host(parts.hostname):
        return
    raise RuntimeError(f"refusing to send API keys to {parts.scheme}://{parts.hostname}: use https")


async def _transcribe(client: httpx.AsyncClient, wav_bytes: bytes) -> str:
    require_https(config.OPENAI_BASE_URL)
    response = await client.post(
        f"{config.OPENAI_BASE_URL}/audio/transcriptions",
        headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
        files={"file": ("speech.wav", wav_bytes, "audio/wav")},
        data={"model": config.WHISPER_MODEL},
    )
    response.raise_for_status()
    text = str(response.json().get("text", "")).strip()
    if not text:
        raise RuntimeError("Whisper returned empty text")
    return text


async def _request_response(client: httpx.AsyncClient, transcript: str) -> str:
    require_https(config.OPENAI_BASE_URL)
    response = await client.post(
        f"{config.OPENAI_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
        json={
            "model": config.CHAT_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": transcript},
            ],
            "temperature": 0.6,
            "max_tokens": 100,
        },
    )
    response.raise_for_status()
    response_text = str(response.json()["choices"][0]["message"]["content"]).strip()
    if not response_text:
        raise RuntimeError(f"{config.CHAT_MODEL} returned empty text")
    return response_text


def pcm_to_wav(pcm: bytes, sample_rate: int = TTS_SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm)
    return buf.getvalue()


async def _synthesize(client: httpx.AsyncClient, response_text: str) -> bytes:
    require_https(config.ELEVENLABS_TTS_URL)
    response = await client.post(
        f"{config.ELEVENLABS_TTS_URL}/{config.ELEVENLABS_VOICE_ID}",
        params={"output_format": TTS_OUTPUT_FORMAT},
        headers={"xi-api-key": config.ELEVENLABS_API_KEY},
        json={
            "text": response_text,
            "model_id": "eleven_turbo_v2_5",
            "voice_settings": {"stability": 0.45, "similarity_boost": 0.75},
        },
    )
    response.raise_for_status()
    if not response.content:
        raise RuntimeError("ElevenLabs returned no audio")
    return pcm_to_wav(response.content)


async def _aplay(source: str, data: Optional[bytes] = None) -> None:
    """Play a WAV file, or WAV bytes piped into aplay's stdin when ``source`` is "-"."""
    args = ["aplay", "-q"]
    if config.AUDIO_OUTPUT_DEVICE:
        args += ["-D", config.AUDIO_OUTPUT_DEVICE]
    process = await asyncio.create_subprocess_exec(
        *args, source, stdin=asyncio.subprocess.PIPE if data is not None else None)
    try:
        await process.communicate(data)
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        raise
    if process.returncode != 0:
        raise RuntimeError(f"aplay exited with status {process.returncode}")


async def _play_wav_bytes(wav_bytes: bytes) -> None:
    await _aplay("-", wav_bytes)  # straight from memory, no temp file


async def _play_fallback() -> None:
    if not config.NETWORK_ERROR_FILE.exists():
        logger.warning("Fallback sound %s is missing", config.NETWORK_ERROR_FILE)
        return
    try:
        await _aplay(str(config.NETWORK_ERROR_FILE))
    except Exception as exc:  # noqa: BLE001 - fallback must never raise
        logger.warning("Fallback audio failed: %s", exc)


class LocalSpeaker:
    """Audio sink for the Pi's own speaker (aplay on AUDIO_OUTPUT_DEVICE). Other sinks (the laptop console,
    console_server.ConsoleSink) implement the same four methods."""

    async def play_wav(self, wav_bytes: bytes) -> None:
        await _play_wav_bytes(wav_bytes)

    async def play_fallback(self) -> None:
        await _play_fallback()

    async def speak_text(self, text: str) -> bool:
        """Speak ``text`` without the cloud (e.g. the browser's own voice). False: this sink can't."""
        return False

    def event(self, kind: str, text: str = "") -> None:
        """Progress for a display: thinking / heard / reply / speaking / idle / error."""


LOCAL_SPEAKER = LocalSpeaker()


def _missing_keys(kind: str = LISTEN_JOB) -> list[str]:
    needed = ("OPENAI_API_KEY", "ELEVENLABS_API_KEY") if kind == LISTEN_JOB else ("ELEVENLABS_API_KEY",)
    return [name for name in needed if not getattr(config, name)]


async def _cascade(client: httpx.AsyncClient, wav_bytes: bytes, sink: LocalSpeaker) -> bytes:
    transcript = await _transcribe(client, wav_bytes)
    logger.debug("Heard: %r", transcript)  # DEBUG: keep conversations out of the system journal
    sink.event("heard", transcript)
    response_text = await _request_response(client, transcript)
    logger.debug("Replying: %r", response_text)
    sink.event("reply", response_text)
    return await _synthesize(client, response_text)


async def _speech_for(client: httpx.AsyncClient, kind: str, value, sink: LocalSpeaker) -> bytes:
    if kind == SAY_JOB:
        if value not in _phrase_cache:
            missing = _missing_keys(SAY_JOB)
            if missing:
                raise RuntimeError(f"missing {', '.join(missing)} in .env")
            _phrase_cache[value] = await _synthesize(client, value)
        return _phrase_cache[value]
    missing = _missing_keys(LISTEN_JOB)
    if missing:
        raise RuntimeError(f"missing {', '.join(missing)} in .env")
    return await _cascade(client, value, sink)


def _short_reason(exc: BaseException) -> str:
    if isinstance(exc, asyncio.TimeoutError):
        return f"no answer within {config.API_TIMEOUT_SECONDS:.0f} s"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"{exc.request.url.host} answered HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.TransportError):
        return f"network error ({type(exc).__name__})"
    return str(exc) or type(exc).__name__


async def handle_job(client: httpx.AsyncClient, kind: str, value, sink: Optional[LocalSpeaker] = None) -> bool:
    """Run one job end to end (``value``: WAV bytes for LISTEN_JOB, text for SAY_JOB).

    Returns False if the fallback sound was played instead. A SAY_JOB the cloud can't voice is spoken
    by the sink itself when it can (the console uses the browser's voice), which counts as success.
    """
    sink = sink or LOCAL_SPEAKER
    sink.event("thinking")
    try:
        wav_bytes = await asyncio.wait_for(_speech_for(client, kind, value, sink), timeout=config.API_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 - any failure falls back to the local sound
        logger.warning("API %s job failed: %r", kind, exc)
        try:
            if kind == SAY_JOB and await sink.speak_text(value):
                return True
            sink.event("error", _short_reason(exc))
            await sink.play_fallback()
        finally:
            sink.event("idle")
        return False

    try:
        sink.event("speaking")
        await sink.play_wav(wav_bytes)
    except (OSError, RuntimeError) as exc:
        logger.warning("Playback failed: %s", exc)
    finally:
        sink.event("idle")
    return True


async def api_routing_task(
    job_queue: asyncio.Queue,
    mqtt_client=None,
    busy_event: Optional[threading.Event] = None,
    client: Optional[httpx.AsyncClient] = None,
    sink: Optional[LocalSpeaker] = None,
) -> None:
    """Consume (LISTEN_JOB, wav_bytes) and (SAY_JOB, text) jobs.

    After each LISTEN_JOB the conversation flag is cleared on MQTT and ``busy_event`` is released
    so the wake-word listener re-arms. ``sink`` plays the result (default: the Pi's speaker).
    """
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(config.API_TIMEOUT_SECONDS))
    try:
        while True:
            kind, value = await job_queue.get()
            try:
                await handle_job(client, kind, value, sink)
            finally:
                if kind == LISTEN_JOB:
                    if mqtt_client is not None:
                        mqtt_client.publish(config.TOPIC_WAKE_FLAG, "0", qos=0, retain=False)
                    if busy_event is not None:
                        busy_event.clear()
                job_queue.task_done()
    finally:
        if owns_client:
            await client.aclose()


async def _say_once(text: str) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    async with httpx.AsyncClient(timeout=httpx.Timeout(config.API_TIMEOUT_SECONDS)) as client:
        ok = await handle_job(client, SAY_JOB, text)
    print("Spoke via ElevenLabs" if ok else "Failed (reason logged above); fallback sound attempted")


if __name__ == "__main__":
    # Quick check of keys + speaker: python api_routing_task.py "Hello, I am EMO"
    asyncio.run(_say_once(" ".join(sys.argv[1:]) or "Hello, I am EMO."))
