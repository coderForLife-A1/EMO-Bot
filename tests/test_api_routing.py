"""Tests for api_routing_task.py: the Whisper/GPT/ElevenLabs pipeline against a mocked HTTP server,
in-memory audio (#25), HTTPS-only API keys (#21), no transcripts at INFO (#22)."""
import asyncio
import io
import logging
import threading
import wave

import httpx
import pytest

import api_routing_task as api
import audio_trigger_task
import config


def wav_bytes(frames=1600):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * frames)
    return buf.getvalue()


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(config, "ELEVENLABS_API_KEY", "el-test")
    monkeypatch.setattr(config, "OPENAI_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setattr(config, "ELEVENLABS_TTS_URL", "https://api.elevenlabs.io/v1/text-to-speech")
    monkeypatch.setattr(config, "LOCAL_LLM_URL", "")  # cloud reply path; the laptop LLM is in test_llm_client.py
    api._phrase_cache.clear()
    played = []  # (source, data): source "-" means WAV bytes piped to aplay's stdin

    async def fake_aplay(source, data=None):
        played.append((source, data))

    monkeypatch.setattr(api, "_aplay", fake_aplay)
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


def test_full_cascade_plays_valid_wav_from_memory(env):
    requests = []

    async def run():
        async with mock_client(requests) as client:
            return await api.handle_job(client, api.LISTEN_JOB, wav_bytes())

    assert asyncio.run(run()) is True
    assert b"RIFF" in requests[0].content  # the recording was uploaded straight from memory
    assert requests[-1].url.params["output_format"] == "pcm_16000"
    source, data = env[0]
    assert source == "-"  # piped into aplay, no temp file
    with wave.open(io.BytesIO(data)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getnframes()) == (16000, 1, 800)


def test_missing_keys_fall_back(env, monkeypatch):
    monkeypatch.setattr(config, "OPENAI_API_KEY", "")
    requests = []

    async def run():
        async with mock_client(requests) as client:
            return await api.handle_job(client, api.LISTEN_JOB, wav_bytes())

    assert asyncio.run(run()) is False
    assert requests == []
    assert env == [(str(config.NETWORK_ERROR_FILE), None)]  # the bundled fallback sound


def test_cascade_timeout_falls_back(env, monkeypatch):
    monkeypatch.setattr(config, "API_TIMEOUT_SECONDS", 0.2)

    async def slow(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json={"text": "x"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as client:
            return await api.handle_job(client, api.LISTEN_JOB, wav_bytes())

    assert asyncio.run(run()) is False


def test_say_jobs_are_cached(env):
    requests = []

    async def run():
        async with mock_client(requests) as client:
            await api.handle_job(client, api.SAY_JOB, "Sit up straight")
            await api.handle_job(client, api.SAY_JOB, "Sit up straight")

    asyncio.run(run())
    assert len(requests) == 1 and len(env) == 2


def test_listen_job_clears_conversation_flag(env):
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
            await q.put((api.LISTEN_JOB, wav_bytes()))
            await asyncio.wait_for(q.join(), 2)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    assert (config.TOPIC_WAKE_FLAG, "0") in published
    assert not busy.is_set()


@pytest.mark.parametrize("url, allowed", [
    ("https://api.openai.com/v1", True),
    ("http://localhost:8080/v1", True),
    ("http://127.0.0.1/v1", True),
    ("http://[::1]:9000", True),
    ("http://api.openai.com/v1", False),
    ("http://192.168.1.20:8080", False),
    ("ftp://example.com", False),
])
def test_require_https(url, allowed):
    """#21: API keys travel in headers, so plain HTTP is only allowed to this machine."""
    if allowed:
        api.require_https(url)
    else:
        with pytest.raises(RuntimeError, match="refusing"):
            api.require_https(url)


def test_plain_http_api_url_never_receives_keys(env, monkeypatch):
    monkeypatch.setattr(config, "OPENAI_BASE_URL", "http://api.openai.com/v1")
    requests = []

    async def run():
        async with mock_client(requests) as client:
            return await api.handle_job(client, api.LISTEN_JOB, wav_bytes())

    assert asyncio.run(run()) is False
    assert requests == []  # nothing was sent


def test_conversation_text_is_not_logged_at_info(env, caplog):
    """#22: what the user said must not end up in journald at the default log level."""
    async def run():
        async with mock_client([]) as client:
            await api.handle_job(client, api.LISTEN_JOB, wav_bytes())

    with caplog.at_level(logging.INFO, logger="api_routing_task"):
        asyncio.run(run())
    assert "hi robot" not in caplog.text and "Hello!" not in caplog.text


def test_recording_stays_in_memory():
    """#25: the recording is returned as WAV bytes, not written to /dev/shm."""
    class Stream:
        def read(self, n):
            return b"\x01\x00" * n, False

    data = audio_trigger_task._record_after_wake(Stream(), 512)
    with wave.open(io.BytesIO(data)) as w:
        assert w.getnframes() == audio_trigger_task.SAMPLE_RATE * audio_trigger_task.RECORD_SECONDS


def test_cached_phrase_plays_even_with_an_http_url(env, monkeypatch):
    """#33: the HTTPS check ran for every job, so cached phrases (no request at all) played the error sound."""
    requests = []

    async def run():
        async with mock_client(requests) as client:
            await api.handle_job(client, api.SAY_JOB, "Emergency stop.")  # cached over HTTPS
            monkeypatch.setattr(config, "ELEVENLABS_TTS_URL", "http://tts.example.com/v1/text-to-speech")
            again = await api.handle_job(client, api.SAY_JOB, "Emergency stop.")  # cached: no request
            fresh = await api.handle_job(client, api.SAY_JOB, "Something new")  # needs a request: refused
            return again, fresh

    again, fresh = asyncio.run(run())
    assert again is True and fresh is False
    assert len(requests) == 1


@pytest.mark.parametrize("host, local", [
    ("localhost", True), ("127.0.0.1", True), ("127.0.0.2", True), ("::1", True), ("[::1]", True),
    ("192.168.1.5", False), ("api.openai.com", False), (None, False),
])
def test_one_shared_local_check(host, local):
    """#33: API and MQTT used different ideas of "local"; both now use netutil.is_local_host."""
    import mqtt_client
    import netutil

    assert netutil.is_local_host(host) is local
    assert mqtt_client.is_local_host is netutil.is_local_host is api.is_local_host
