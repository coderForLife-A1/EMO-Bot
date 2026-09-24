"""Tests for tools/laptop_stt.py: the laptop's speech-to-text server, with a fake transcriber (no GPU or
faster-whisper needed). Requests are built by httpx exactly as the Pi's api_routing_task sends them."""
import importlib
import sys
import threading
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
try:
    laptop_stt = importlib.import_module("laptop_stt")
finally:
    sys.path.remove(str(ROOT / "tools"))


@pytest.fixture
def server():
    calls = []

    def transcribe(audio, language, prompt, temperature):
        calls.append((audio, language, prompt, temperature))
        if audio == b"boom":
            raise RuntimeError("CUDA out of memory")
        return "what time is it"

    srv = laptop_stt.SttServer(("127.0.0.1", 0), transcribe, "fake-model")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", calls
    srv.shutdown()
    srv.server_close()


def post(url, audio, **data):
    return httpx.post(f"{url}/v1/audio/transcriptions", files={"file": ("speech.wav", audio, "audio/wav")},
                      data={"model": "whisper-1", **data})


def test_transcribes_like_openai(server):
    url, calls = server
    wav = laptop_stt.silent_wav(0.1) + bytes(range(256))  # binary content must arrive unchanged
    response = post(url, wav, language="en", prompt="EMO is a robot.", temperature="0")
    assert response.status_code == 200
    assert response.json() == {"text": "what time is it"}
    assert calls == [(wav, "en", "EMO is a robot.", 0.0)]


def test_language_and_prompt_are_optional(server):
    url, calls = server
    assert post(url, b"RIFF....").status_code == 200
    assert calls[0][1:] == ("", "", 0.0)


def test_missing_file_is_rejected(server):
    url, calls = server
    response = httpx.post(f"{url}/v1/audio/transcriptions", data={"model": "whisper-1"},
                          files={"other": ("x.txt", b"x")})
    assert response.status_code == 400
    assert calls == []


def test_not_multipart_is_rejected(server):
    url, _ = server
    response = httpx.post(f"{url}/v1/audio/transcriptions", content=b"RIFF", headers={"Content-Type": "audio/wav"})
    assert response.status_code == 400


def test_oversized_upload_is_rejected(server, monkeypatch):
    url, calls = server
    monkeypatch.setattr(laptop_stt, "MAX_UPLOAD_BYTES", 100)
    assert post(url, b"x" * 200).status_code == 413
    assert calls == []


def test_model_error_is_a_500(server):
    url, _ = server
    response = post(url, b"boom")
    assert response.status_code == 500
    assert "CUDA out of memory" in response.json()["error"]["message"]


def test_unknown_path_and_health(server):
    url, _ = server
    assert httpx.post(f"{url}/v1/chat/completions", content=b"{}").status_code == 404
    assert httpx.get(f"{url}/health").json() == {"model": "fake-model"}
