"""Tests for api_routing_task.py: the Whisper/GPT/ElevenLabs pipeline against a mocked HTTP server."""
import asyncio
import io
import threading
import wave
from pathlib import Path

import httpx
import pytest

import api_routing_task as api
import audio_trigger_task
import config


def wav_file(tmp_path, name="in.wav"):
    path = tmp_path / name
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 1600)
    return path


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(config, "ELEVENLABS_API_KEY", "el-test")
    api._phrase_cache.clear()
    played = []

    async def fake_play(path):
        played.append(Path(path).read_bytes())  # noqa: ASYNC240 - test double

    monkeypatch.setattr(api, "_play_audio", fake_play)
    return played


def mock_client(requests):
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/audio/transcriptions"):
            return httpx.Response(200, json={"text": "hi robot"})
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={"choices": [{"message": {"content": "Hello!"}}]})
        return httpx.Response(200, content=b"\x01\x00" * 800)  # raw PCM
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_job_names_match():
    assert audio_trigger_task.LISTEN_JOB == api.LISTEN_JOB


def test_full_cascade_plays_valid_wav(env, tmp_path):
    requests = []
    src = wav_file(tmp_path)

    async def run():
        async with mock_client(requests) as client:
            return await api.handle_job(client, api.LISTEN_JOB, str(src))

    assert asyncio.run(run()) is True
    tts = requests[-1]
    assert tts.url.params["output_format"] == "pcm_16000"
    with wave.open(io.BytesIO(env[0])) as w:  # what aplay receives is a real WAV
        assert (w.getframerate(), w.getnchannels(), w.getnframes()) == (16000, 1, 800)
    assert not src.exists()  # recording cleaned up


def test_missing_keys_fall_back(env, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OPENAI_API_KEY", "")
    requests = []

    async def run():
        async with mock_client(requests) as client:
            return await api.handle_job(client, api.LISTEN_JOB, str(wav_file(tmp_path)))

    assert asyncio.run(run()) is False
    assert requests == []
    assert env and env[0][:4] == b"RIFF"  # the bundled network_error.wav was played


def test_cascade_timeout_falls_back(env, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "API_TIMEOUT_SECONDS", 0.2)

    async def slow(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json={"text": "x"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as client:
            return await api.handle_job(client, api.LISTEN_JOB, str(wav_file(tmp_path)))

    assert asyncio.run(run()) is False


def test_say_jobs_are_cached(env):
    requests = []

    async def run():
        async with mock_client(requests) as client:
            await api.handle_job(client, api.SAY_JOB, "Sit up straight")
            await api.handle_job(client, api.SAY_JOB, "Sit up straight")

    asyncio.run(run())
    assert len(requests) == 1 and len(env) == 2


def test_listen_job_clears_conversation_flag(env, tmp_path):
    published = []
    busy = threading.Event()
    busy.set()

    class Pub:
        def publish(self, topic, payload, **_):
            published.append((topic, payload))

    async def run():
        q = asyncio.Queue()
        async with mock_client([]) as client:
            task = asyncio.create_task(api.api_routing_task(q, Pub(), busy, client))
            await q.put((api.LISTEN_JOB, str(wav_file(tmp_path))))
            await asyncio.wait_for(q.join(), 2)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    assert (config.TOPIC_WAKE_FLAG, "0") in published
    assert not busy.is_set()
