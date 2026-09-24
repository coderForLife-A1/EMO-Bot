"""Speech pipeline: Whisper (speech to text) -> reply (llm_client: laptop LLM, cloud fallback)
-> ElevenLabs (text to speech) -> aplay.

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

import httpx

import config
import llm_client
from netutil import is_local_host, require_https  # noqa: F401 - is_local_host re-exported for tests

logger = logging.getLogger(__name__)

LISTEN_JOB = "listen"  # (LISTEN_JOB, wav_bytes): full Whisper -> LLM -> TTS cascade
SAY_JOB = "say"  # (SAY_JOB, text): speak a fixed phrase (behavior tree cues)

# ElevenLabs returns MP3 unless output_format is set; the Accept header is ignored.
# Raw 16 kHz PCM is available on every plan and is wrapped into a WAV for aplay.
TTS_OUTPUT_FORMAT = "pcm_16000"
TTS_SAMPLE_RATE = 16_000

_phrase_cache: dict[str, bytes] = {}


def _missing_keys() -> list[str]:
    return [name for name in ("OPENAI_API_KEY", "ELEVENLABS_API_KEY") if not getattr(config, name)]


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


async def _cascade(client: httpx.AsyncClient, wav_bytes: bytes) -> bytes:
    transcript = await _transcribe(client, wav_bytes)
    response_text, model = await llm_client.get_reply(client, transcript)
    logger.info("Reply from %s", model)
    # Conversation text stays out of the system journal unless LOG_CONVERSATIONS=1
    log_level = logging.INFO if config.LOG_CONVERSATIONS else logging.DEBUG
    logger.log(log_level, "Heard %r, replying %r", transcript, response_text)
    return await _synthesize(client, response_text)


async def _speech_for(client: httpx.AsyncClient, kind: str, value) -> bytes:
    if kind == SAY_JOB:
        if value not in _phrase_cache:
            _phrase_cache[value] = await _synthesize(client, value)
        return _phrase_cache[value]
    return await _cascade(client, value)


async def handle_job(client: httpx.AsyncClient, kind: str, value) -> bool:
    """Run one job end to end (``value``: WAV bytes for LISTEN_JOB, text for SAY_JOB).

    Returns False if the fallback sound was played instead.
    """
    try:
        missing = _missing_keys()
        if missing:
            raise RuntimeError(f"missing {', '.join(missing)} in .env")
        wav_bytes = await asyncio.wait_for(_speech_for(client, kind, value), timeout=config.API_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 - any failure falls back to the local sound
        logger.warning("API %s job failed: %r", kind, exc)
        await _play_fallback()
        return False

    try:
        await _play_wav_bytes(wav_bytes)
    except (OSError, RuntimeError) as exc:
        logger.warning("Playback failed: %s", exc)
    return True


async def api_routing_task(
    job_queue: asyncio.Queue,
    mqtt_client=None,
    busy_event: Optional[threading.Event] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> None:
    """Consume (LISTEN_JOB, wav_bytes) and (SAY_JOB, text) jobs.

    After each LISTEN_JOB the conversation flag is cleared on MQTT and ``busy_event`` is released
    so the wake-word listener re-arms.
    """
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(config.API_TIMEOUT_SECONDS))
    try:
        while True:
            kind, value = await job_queue.get()
            try:
                await handle_job(client, kind, value)
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
