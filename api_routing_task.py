"""Speech pipeline: Whisper (speech to text) -> reply (llm_client: laptop LLM, cloud fallback)
-> ElevenLabs (text to speech) -> aplay.

Typed questions from the console (ASK_JOB) go to Gemini instead and the answer is shown as plain text,
with no speech either way. Without the Whisper/ElevenLabs keys, spoken questions (LISTEN_JOB) go to Gemini
too: it transcribes the recording and answers in one call, and both are shown as text.
The preset EXAMPLES have canned answers used when Gemini can't be reached.

Also speaks short cues from the behavior tree ("I fell over", posture reminder), caching them.
On any failure it plays assets/network_error.wav. Test keys and speaker with
`python api_routing_task.py "Hello"`.
"""
import asyncio
import base64
import contextlib
import io
import json
import logging
import re
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
ASK_JOB = "ask"  # (ASK_JOB, text): typed question -> Gemini -> text reply on the console, no audio
CONVERSATION_JOBS = (LISTEN_JOB, ASK_JOB)  # hold the conversation flag and busy_event until answered

# ElevenLabs returns MP3 unless output_format is set; the Accept header is ignored.
# Raw 16 kHz PCM is available on every plan and is wrapped into a WAV for aplay.
TTS_OUTPUT_FORMAT = "pcm_16000"
TTS_SAMPLE_RATE = 16_000

# What Whisper tends to invent from near-silent audio (YouTube-subtitle phrases); treated as "nothing heard"
# ("thank you" and "bye" are real things to say to a robot, so they are kept)
WHISPER_PHANTOMS = {
    "", "you", "you.", "thanks for watching", "thanks for watching!", "thank you for watching",
    "thank you for watching.", "thank you for watching!", "thank you so much for watching!",
    "please subscribe", "please subscribe.", ".", "...",
}

GEMINI_SYSTEM_PROMPT = (
    "You are EMO, a small, friendly desk robot with two legs and a face on a laptop screen. You can stand, walk, "
    "turn, do a knee bob and remind people to sit up straight. Answer in plain text only: no markdown, "
    "no asterisks, no bullet points, no emoji. Keep it to one or two short sentences."
)
MAX_QUESTION_CHARS = 300

# Preset questions for demos: the console shows them as buttons. The canned answer is shown if Gemini fails.
EXAMPLES: dict[str, str] = {
    "Hi EMO, who are you?": "Hi! I'm EMO, a little desk robot who keeps you company and reminds you to sit up.",
    "What can you do?": "I can stand, walk, turn, do a knee bob, and nudge you when you slouch.",
    "Tell me a joke.": "Why did the robot go on holiday? It needed to recharge its batteries.",
    "Give me a posture tip.": "Keep your screen at eye level and your feet flat on the floor.",
    "What is 12 times 8?": "12 times 8 is 96.",
    "Say something nice.": "You're doing great today, and I'm happy to be on your desk.",
}

_phrase_cache: dict[str, bytes] = {}


async def _transcribe(client: httpx.AsyncClient, wav_bytes: bytes) -> str:
    require_https(config.OPENAI_BASE_URL)
    data = {"model": config.WHISPER_MODEL, "temperature": "0"}
    if config.WHISPER_LANGUAGE:
        data["language"] = config.WHISPER_LANGUAGE  # no guessing the language from a few words
    if config.WHISPER_PROMPT:
        data["prompt"] = config.WHISPER_PROMPT  # spelling/vocabulary hint, e.g. the robot's name
    response = await client.post(
        f"{config.OPENAI_BASE_URL}/audio/transcriptions",
        headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
        files={"file": ("speech.wav", wav_bytes, "audio/wav")},
        data=data,
    )
    response.raise_for_status()
    text = str(response.json().get("text", "")).strip()
    if text.lower() in WHISPER_PHANTOMS:
        raise RuntimeError(f"Whisper heard no speech ({text!r})")
    return text


def plain_text(text: str) -> str:
    """Strip the markdown Gemini sometimes adds anyway, so the console shows clean text."""
    text = re.sub(r"[*_`#]+", "", text)
    text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.MULTILINE)
    return " ".join(text.split())


async def _gemini(client: httpx.AsyncClient, parts: list[dict], generation: Optional[dict] = None) -> str:
    """One generateContent call as EMO; returns the answer's text (thinking parts dropped)."""
    return await llm_client.gemini_generate(client, {
        "systemInstruction": {"parts": [{"text": GEMINI_SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": 0.6, "maxOutputTokens": 300, **(generation or {})},
    })


async def _ask_gemini(client: httpx.AsyncClient, question: str) -> str:
    answer = plain_text(await _gemini(client, [{"text": question}]))
    if not answer:
        raise RuntimeError(f"{config.GEMINI_MODEL} returned empty text")
    return answer


HEAR_INSTRUCTION = (
    "The audio is a person talking to you. Return JSON: \"heard\" is exactly what they said, word for word "
    "(empty if there is no speech, only noise or silence). Do not answer it."
)
HEAR_SCHEMA = {"type": "OBJECT", "properties": {"heard": {"type": "STRING"}}, "required": ["heard"]}


async def _hear_gemini(client: httpx.AsyncClient, wav_bytes: bytes) -> str:
    """Speech to text with Gemini (no OpenAI key for Whisper). Empty for silence."""
    raw = await _gemini(
        client,
        [{"inlineData": {"mimeType": "audio/wav", "data": base64.b64encode(wav_bytes).decode()}},
         {"text": HEAR_INSTRUCTION}],
        {"temperature": 0, "responseMimeType": "application/json", "responseSchema": HEAR_SCHEMA},
    )
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError(f"{config.GEMINI_MODEL} returned malformed JSON") from exc
    return " ".join(str(data.get("heard", "")).split())


def listen_with_gemini() -> bool:
    """Spoken questions are transcribed by Gemini and answered as text (no voice) unless the Whisper and
    ElevenLabs keys are both set."""
    return bool(config.GEMINI_API_KEY) and not (config.OPENAI_API_KEY and config.ELEVENLABS_API_KEY)


async def _reply(client: httpx.AsyncClient, transcript: str) -> str:
    """Laptop LLM first, cloud model as the fallback (llm_client)."""
    response_text, model = await llm_client.get_reply(client, transcript)
    logger.info("Reply from %s", model)
    # Conversation text stays out of the system journal unless LOG_CONVERSATIONS=1
    log_level = logging.INFO if config.LOG_CONVERSATIONS else logging.DEBUG
    logger.log(log_level, "Heard %r, replying %r", transcript, response_text)
    return response_text


async def _hear_and_reply(client: httpx.AsyncClient, wav_bytes: bytes, sink: "LocalSpeaker") -> bool:
    heard = await _hear_gemini(client, wav_bytes)
    if not heard:
        return False
    sink.event("heard", heard)
    sink.event("reply", await _reply(client, heard))
    return True


async def handle_heard(client: httpx.AsyncClient, wav_bytes: bytes, sink: Optional["LocalSpeaker"] = None) -> bool:
    """Answer a spoken question as text on ``sink``'s display (no audio played). False if only an error was shown."""
    sink = sink or LOCAL_SPEAKER
    sink.event("thinking")
    try:
        answered = await asyncio.wait_for(_hear_and_reply(client, wav_bytes, sink),
                                          timeout=config.API_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 - shown on the console instead
        logger.warning("Spoken question failed: %r", exc)
        sink.event("error", _short_reason(exc))
        sink.event("idle")
        return False
    if not answered:
        sink.event("error", "I didn't catch that: hold the button while you speak")
    sink.event("idle")
    return answered


async def handle_ask(client: httpx.AsyncClient, question: str, sink: Optional["LocalSpeaker"] = None) -> bool:
    """Answer a typed question as text on ``sink``'s display. Returns False if only an error was shown."""
    sink = sink or LOCAL_SPEAKER
    question = " ".join(str(question).split())[:MAX_QUESTION_CHARS]
    sink.event("thinking")
    sink.event("heard", question)
    try:
        if not config.GEMINI_API_KEY:
            raise RuntimeError("missing GEMINI_API_KEY in .env")
        answer = await asyncio.wait_for(_ask_gemini(client, question), timeout=config.API_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 - a preset question still gets its canned answer
        logger.warning("Gemini question failed: %r", exc)
        answer = EXAMPLES.get(question)
        if answer is None:
            sink.event("error", _short_reason(exc))
            sink.event("idle")
            return False
    logger.debug("Answer: %r", answer)
    sink.event("reply", answer)
    sink.event("idle")
    return True


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
    sink.event("heard", transcript)
    response_text = await _reply(client, transcript)
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
    """Consume (LISTEN_JOB, wav_bytes), (ASK_JOB, text) and (SAY_JOB, text) jobs.

    After each LISTEN_JOB or ASK_JOB the conversation flag is cleared on MQTT and ``busy_event`` is released
    so the wake-word listener re-arms. ``sink`` plays the result (default: the Pi's speaker).
    """
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(config.API_TIMEOUT_SECONDS))
    warm_up = asyncio.create_task(llm_client.warm_up(client), name="llm_warm_up")
    try:
        while True:
            kind, value = await job_queue.get()
            try:
                if kind == ASK_JOB:
                    await handle_ask(client, value, sink)
                elif kind == LISTEN_JOB and listen_with_gemini():
                    await handle_heard(client, value, sink)
                else:
                    await handle_job(client, kind, value, sink)
            finally:
                if kind in CONVERSATION_JOBS:
                    if mqtt_client is not None:
                        mqtt_client.publish(config.TOPIC_WAKE_FLAG, "0", qos=0, retain=False)
                    if busy_event is not None:
                        busy_event.clear()
                job_queue.task_done()
            if kind == LISTEN_JOB:
                await llm_client.rewarm_after_escalation(client)  # robot is idle now; next question stays fast
    finally:
        warm_up.cancel()
        await asyncio.gather(warm_up, return_exceptions=True)
        if owns_client:
            await client.aclose()


async def _say_once(text: str) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    async with httpx.AsyncClient(timeout=httpx.Timeout(config.API_TIMEOUT_SECONDS)) as client:
        ok = await handle_job(client, SAY_JOB, text)
    print("Spoke via ElevenLabs" if ok else "Failed (reason logged above); fallback sound attempted")


class _PrintSink(LocalSpeaker):
    def event(self, kind: str, text: str = "") -> None:
        if kind in ("reply", "error"):
            print(f"EMO: {text}" if kind == "reply" else f"Error: {text}")


async def _ask_once(question: str) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    async with httpx.AsyncClient(timeout=httpx.Timeout(config.API_TIMEOUT_SECONDS)) as client:
        await handle_ask(client, question, _PrintSink())


if __name__ == "__main__":
    # Quick check of keys + speaker: python api_routing_task.py "Hello, I am EMO"
    # Gemini text answer, no audio:   python api_routing_task.py --ask "Tell me a joke."
    if sys.argv[1:2] == ["--ask"]:
        asyncio.run(_ask_once(" ".join(sys.argv[2:]) or "Hi EMO, who are you?"))
    else:
        asyncio.run(_say_once(" ".join(sys.argv[1:]) or "Hello, I am EMO."))
