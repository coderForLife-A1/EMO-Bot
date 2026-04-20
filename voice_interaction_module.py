import json
import os
import queue
import struct
import subprocess
import tempfile
import threading
import time
import wave
from pathlib import Path
from typing import Optional

import pyaudio
import pvporcupine
import requests


OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
PORCUPINE_ACCESS_KEY = os.getenv("PORCUPINE_ACCESS_KEY", "")

WHISPER_MODEL = "whisper-1"
CHAT_MODEL = "gpt-4o"
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "EXAVITQu4vr4xnSDxMaL")

WAKEWORD_PATH = os.getenv("PORCUPINE_KEYWORD_PATH", "")
NETWORK_ERROR_FILE = "network_error.wav"

RECORD_DEVICE = "plughw:0"
RECORD_SECONDS = 5

OPENAI_BASE_URL = "https://api.openai.com/v1"
ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech"

API_TIMEOUT_SECONDS = 3

SYSTEM_PROMPT = (
    "You are a desktop companion robot. Respond in 1-2 short sentences, "
    "be helpful, calm, and practical. Avoid verbosity."
)


def play_audio_file(path: str) -> None:
    subprocess.run(["aplay", path], check=False)


def play_network_error() -> None:
    if Path(NETWORK_ERROR_FILE).exists():
        play_audio_file(NETWORK_ERROR_FILE)


def record_audio_5s() -> Optional[str]:
    fd, wav_path = tempfile.mkstemp(prefix="robot_input_", suffix=".wav")
    os.close(fd)

    result = subprocess.run(
        [
            "arecord",
            "-D",
            RECORD_DEVICE,
            "-f",
            "S16_LE",
            "-r",
            "16000",
            "-c",
            "1",
            "-d",
            str(RECORD_SECONDS),
            wav_path,
        ],
        check=False,
    )
    if result.returncode != 0:
        try:
            os.remove(wav_path)
        except OSError:
            pass
        return None
    return wav_path


def transcribe_with_whisper(audio_path: str) -> Optional[str]:
    try:
        with open(audio_path, "rb") as audio_file:
            response = requests.post(
                f"{OPENAI_BASE_URL}/audio/transcriptions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                files={"file": audio_file},
                data={"model": WHISPER_MODEL},
                timeout=API_TIMEOUT_SECONDS,
            )
        response.raise_for_status()
        payload = response.json()
        text = payload.get("text", "").strip()
        return text if text else None
    except Exception:
        return None


def chat_with_gpt(user_text: str) -> Optional[str]:
    try:
        response = requests.post(
            f"{OPENAI_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            data=json.dumps(
                {
                    "model": CHAT_MODEL,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_text},
                    ],
                    "temperature": 0.6,
                    "max_tokens": 100,
                }
            ),
            timeout=API_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        content = payload["choices"][0]["message"]["content"].strip()
        return content if content else None
    except Exception:
        return None


def synthesize_with_elevenlabs(text: str) -> Optional[str]:
    fd, out_path = tempfile.mkstemp(prefix="robot_tts_", suffix=".wav")
    os.close(fd)
    try:
        response = requests.post(
            f"{ELEVENLABS_TTS_URL}/{ELEVENLABS_VOICE_ID}",
            headers={
                "xi-api-key": ELEVENLABS_API_KEY,
                "Content-Type": "application/json",
                "Accept": "audio/wav",
            },
            data=json.dumps(
                {
                    "text": text,
                    "model_id": "eleven_turbo_v2_5",
                    "voice_settings": {"stability": 0.45, "similarity_boost": 0.75},
                }
            ),
            timeout=API_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        with open(out_path, "wb") as f:
            f.write(response.content)
        return out_path
    except Exception:
        try:
            os.remove(out_path)
        except OSError:
            pass
        return None


def wakeword_listener(stop_event: threading.Event, wake_queue: "queue.Queue[bool]") -> None:
    porcupine = None
    pa = None
    audio_stream = None
    try:
        if not PORCUPINE_ACCESS_KEY:
            raise RuntimeError("PORCUPINE_ACCESS_KEY is not set")

        if WAKEWORD_PATH:
            porcupine = pvporcupine.create(
                access_key=PORCUPINE_ACCESS_KEY,
                keyword_paths=[WAKEWORD_PATH],
            )
        else:
            porcupine = pvporcupine.create(
                access_key=PORCUPINE_ACCESS_KEY,
                keywords=["porcupine"],
            )

        pa = pyaudio.PyAudio()
        audio_stream = pa.open(
            rate=porcupine.sample_rate,
            channels=1,
            format=pyaudio.paInt16,
            input=True,
            frames_per_buffer=porcupine.frame_length,
        )

        while not stop_event.is_set():
            pcm = audio_stream.read(porcupine.frame_length, exception_on_overflow=False)
            pcm_unpacked = struct.unpack_from("h" * porcupine.frame_length, pcm)
            keyword_index = porcupine.process(pcm_unpacked)
            if keyword_index >= 0:
                try:
                    wake_queue.put_nowait(True)
                except queue.Full:
                    pass
                time.sleep(0.25)
    except Exception as exc:
        print(f"Wakeword listener error: {exc}")
    finally:
        if audio_stream is not None:
            audio_stream.close()
        if pa is not None:
            pa.terminate()
        if porcupine is not None:
            porcupine.delete()


def validate_wav(path: str) -> bool:
    try:
        with wave.open(path, "rb") as _wf:
            return True
    except Exception:
        return False


def handle_interaction_once() -> None:
    input_audio = record_audio_5s()
    if not input_audio:
        play_network_error()
        return

    try:
        transcript = transcribe_with_whisper(input_audio)
        if not transcript:
            play_network_error()
            return

        response_text = chat_with_gpt(transcript)
        if not response_text:
            play_network_error()
            return

        tts_wav = synthesize_with_elevenlabs(response_text)
        if not tts_wav or not validate_wav(tts_wav):
            play_network_error()
            return

        try:
            play_audio_file(tts_wav)
        finally:
            try:
                os.remove(tts_wav)
            except OSError:
                pass
    finally:
        try:
            os.remove(input_audio)
        except OSError:
            pass


def main() -> None:
    if not OPENAI_API_KEY or not ELEVENLABS_API_KEY:
        print("Missing OPENAI_API_KEY or ELEVENLABS_API_KEY")
        play_network_error()
        return

    wake_queue: "queue.Queue[bool]" = queue.Queue(maxsize=2)
    stop_event = threading.Event()

    listener = threading.Thread(
        target=wakeword_listener,
        args=(stop_event, wake_queue),
        name="wakeword-thread",
        daemon=True,
    )
    listener.start()

    print("Voice interaction module running. Waiting for wake word...")
    try:
        while True:
            try:
                _ = wake_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            handle_interaction_once()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        listener.join(timeout=1.0)


if __name__ == "__main__":
    main()