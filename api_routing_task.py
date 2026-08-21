import asyncio
import os
import tempfile
from pathlib import Path

import httpx


OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
ELEVENLABS_TTS_URL = os.getenv(
    "ELEVENLABS_TTS_URL",
    "https://api.elevenlabs.io/v1/text-to-speech",
)
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "EXAVITQu4vr4xnSDxMaL")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "whisper-1")
CHAT_MODEL = "gpt-4o"
NETWORK_ERROR_FILE = Path("network_error.wav")
API_CASCADE_TIMEOUT_SECONDS = 3.0

SYSTEM_PROMPT = (
    "You are a concise, helpful assistant inside a desktop companion robot. "
    "Respond in one or two short sentences."
)


async def _transcribe(client: httpx.AsyncClient, wav_path: str) -> str:
    with open(wav_path, "rb") as audio_file:
        response = await client.post(
            f"{OPENAI_BASE_URL}/audio/transcriptions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            files={
                "file": (
                    Path(wav_path).name,
                    audio_file,
                    "audio/wav",
                )
            },
            data={"model": WHISPER_MODEL},
        )
    response.raise_for_status()
    text = str(response.json().get("text", "")).strip()
    if not text:
        raise RuntimeError("Whisper returned empty text")
    return text


async def _request_response(client: httpx.AsyncClient, transcript: str) -> str:
    response = await client.post(
        f"{OPENAI_BASE_URL}/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": CHAT_MODEL,
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
        raise RuntimeError("GPT-4o returned empty text")
    return response_text


async def _synthesize(
    client: httpx.AsyncClient,
    response_text: str,
) -> str:
    response = await client.post(
        f"{ELEVENLABS_TTS_URL}/{ELEVENLABS_VOICE_ID}",
        headers={
            "xi-api-key": ELEVENLABS_API_KEY,
            "Content-Type": "application/json",
            "Accept": "audio/wav",
        },
        json={
            "text": response_text,
            "model_id": "eleven_turbo_v2_5",
            "voice_settings": {"stability": 0.45, "similarity_boost": 0.75},
        },
    )
    response.raise_for_status()

    with tempfile.NamedTemporaryFile(
        mode="wb",
        suffix=".wav",
        prefix="robot_tts_",
        dir="/dev/shm" if Path("/dev/shm").is_dir() else None,
        delete=False,
    ) as audio_file:
        audio_file.write(response.content)
        return audio_file.name


async def _play_audio(audio_path: str) -> None:
    process = await asyncio.create_subprocess_exec("aplay", audio_path)
    await process.wait()
    if process.returncode != 0:
        raise RuntimeError(f"aplay exited with status {process.returncode}")


async def _play_fallback() -> None:
    if NETWORK_ERROR_FILE.exists():
        try:
            await _play_audio(str(NETWORK_ERROR_FILE))
        except Exception as exc:
            print(f"Fallback audio failed: {exc}")


async def _process_audio_file(wav_path: str) -> None:
    tts_path: str | None = None
    try:
        timeout = httpx.Timeout(API_CASCADE_TIMEOUT_SECONDS)
        async with httpx.AsyncClient(timeout=timeout) as client:
            transcript = await _transcribe(client, wav_path)
            response_text = await _request_response(client, transcript)
            tts_path = await _synthesize(client, response_text)

        await _play_audio(tts_path)
    finally:
        try:
            os.remove(wav_path)
        except OSError:
            pass
        if tts_path is not None:
            try:
                os.remove(tts_path)
            except OSError:
                pass


async def api_routing_task(llm_processing_queue: asyncio.Queue[str]) -> None:
    while True:
        wav_path = await llm_processing_queue.get()
        try:
            await asyncio.wait_for(
                _process_audio_file(wav_path),
                timeout=API_CASCADE_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            print(f"API cascade failed: {exc}")
            await _play_fallback()
        finally:
            llm_processing_queue.task_done()
