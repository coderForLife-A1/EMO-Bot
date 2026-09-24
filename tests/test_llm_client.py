"""Tests for llm_client.py: laptop LLM (qwen3:4b) -> escalation (gemma4:e4b) -> cloud fallback,
date/time in the prompt, LAN-only plain HTTP, all against a mocked HTTP server."""
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import api_routing_task as api
import config
import llm_client
import netutil

LAPTOP = "http://192.168.43.20:11434"
QWEN, GEMMA = "qwen3:4b", "gemma4:e4b"


@pytest.fixture
def env(monkeypatch):
    for name, value in {
        "OPENAI_API_KEY": "sk-test", "ELEVENLABS_API_KEY": "el-test",
        "OPENAI_BASE_URL": "https://api.openai.com/v1", "CHAT_MODEL": "gpt-4o",
        "LOCAL_LLM_URL": LAPTOP, "LOCAL_LLM_MODEL": QWEN, "LOCAL_LLM_ESCALATE_MODEL": GEMMA,
        "ROBOT_LOCATION": "", "LOG_CONVERSATIONS": False,
    }.items():
        monkeypatch.setattr(config, name, value)
    llm_client.clear_history()


def ndjson(*pieces):
    lines = [json.dumps({"message": {"role": "assistant", "content": p}, "done": False}) for p in pieces]
    return ("\n".join(lines + [json.dumps({"done": True})]) + "\n").encode()


def run_reply(replies, transcript="what day is it"):
    """replies: model name -> NDJSON bytes or an httpx exception; "gpt-4o" -> cloud reply text."""
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/api/chat"):
            reply = replies[json.loads(request.content)["model"]]
            if isinstance(reply, Exception):
                raise reply
            return httpx.Response(200, content=reply)
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={"choices": [{"message": {"content": replies["gpt-4o"]}}]})
        return httpx.Response(404)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await llm_client.get_reply(client, transcript)

    return asyncio.run(go()), requests


def models(requests):
    return [json.loads(r.content)["model"] for r in requests]


def test_prompt_has_date_time_and_location(monkeypatch):
    monkeypatch.setattr(config, "ROBOT_LOCATION", "Chennai, India")
    now = datetime(2026, 9, 24, 18, 5, tzinfo=timezone(timedelta(hours=5, minutes=30), "IST"))
    prompt = llm_client.system_prompt(now=now)
    assert "Thursday, 24 September 2026, 18:05 (IST)" in prompt
    assert "Chennai, India" in prompt
    assert "ESCALATE" not in prompt
    assert "reply with exactly ESCALATE" in llm_client.system_prompt(escalate=True, now=now)


def test_local_answer_is_used(env):
    (reply, model), requests = run_reply({QWEN: ndjson("It is ", "Thursday. ", "Anything else?")})
    assert (reply, model) == ("It is Thursday. Anything else?", QWEN)
    body = json.loads(requests[0].content)
    assert str(requests[0].url) == f"{LAPTOP}/api/chat"
    assert "authorization" not in requests[0].headers  # the OpenAI key never goes to the laptop
    assert body["stream"] is True and body["think"] is False
    assert "Current local date and time" in body["messages"][0]["content"]
    assert "ESCALATE" in body["messages"][0]["content"]
    assert body["messages"][1] == {"role": "user", "content": "what day is it"}


@pytest.mark.parametrize("first", [
    ndjson("ESCAL", "ATE"),
    ndjson("I don't know ", "that one. ", "Sorry."),
    ndjson("I can’t help with that."),
    ndjson(""),
    ndjson("<think>hmm</think>", "  "),
])
def test_unsure_first_model_escalates(env, first):
    (reply, model), requests = run_reply({QWEN: first, GEMMA: ndjson("It is Thursday.")})
    assert (reply, model) == ("It is Thursday.", GEMMA)
    assert models(requests) == [QWEN, GEMMA]
    assert "ESCALATE" not in json.loads(requests[1].content)["messages"][0]["content"]


def test_stream_is_cut_once_the_first_sentence_shows_a_refusal(env):
    """Everything after the refusal would be wasted time: stop reading (and generating) there."""
    pieces_read = []

    async def body():
        for piece in ["I cannot", " say.", " More", " text", " here."]:
            pieces_read.append(piece)
            yield (json.dumps({"message": {"content": piece}, "done": False}) + "\n").encode()

    def handler(request):
        if json.loads(request.content)["model"] == QWEN:
            return httpx.Response(200, content=body())
        return httpx.Response(200, content=ndjson("Fine."))

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await llm_client.get_reply(client, "x")

    assert asyncio.run(go()) == ("Fine.", GEMMA)
    assert len(pieces_read) < 5


def test_empty_second_model_goes_to_cloud(env):
    (reply, model), requests = run_reply({QWEN: ndjson("ESCALATE"), GEMMA: ndjson(""), "gpt-4o": "Thursday."})
    assert (reply, model) == ("Thursday.", "gpt-4o")
    assert requests[-1].headers["authorization"] == "Bearer sk-test"


def test_escalation_off_goes_to_cloud(env, monkeypatch):
    monkeypatch.setattr(config, "LOCAL_LLM_ESCALATE_MODEL", "")
    (_, model), requests = run_reply({QWEN: ndjson("ESCALATE"), "gpt-4o": "Thursday."})
    assert model == "gpt-4o" and len(requests) == 2


@pytest.mark.parametrize("error", [httpx.ConnectError("unreachable"), httpx.ReadTimeout("slow")])
def test_unreachable_or_slow_laptop_goes_to_cloud(env, error):
    (reply, model), _ = run_reply({QWEN: error, "gpt-4o": "Thursday."})
    assert (reply, model) == ("Thursday.", "gpt-4o")


def test_ollama_error_line_goes_to_cloud(env):
    bad = (json.dumps({"error": "model 'qwen3:4b' not found"}) + "\n").encode()
    (_, model), _ = run_reply({QWEN: bad, "gpt-4o": "Thursday."})
    assert model == "gpt-4o"


def test_no_laptop_url_uses_cloud_only(env, monkeypatch):
    monkeypatch.setattr(config, "LOCAL_LLM_URL", "")
    (_, model), requests = run_reply({"gpt-4o": "Thursday."})
    assert model == "gpt-4o" and len(requests) == 1


def test_public_http_laptop_url_is_refused(env, monkeypatch):
    monkeypatch.setattr(config, "LOCAL_LLM_URL", "http://8.8.8.8:11434")
    (_, model), requests = run_reply({"gpt-4o": "Thursday."})
    assert model == "gpt-4o"
    assert all(r.url.host != "8.8.8.8" for r in requests)  # the transcript was never sent there


def test_think_blocks_are_stripped(env):
    (reply, _), _ = run_reply({QWEN: ndjson("<think>the user asks", "...</think>", "It is Thursday.")})
    assert reply == "It is Thursday."


@pytest.mark.parametrize("reply, escalate", [
    ("It is Thursday.", False),
    ("I'm sorry to hear that. Want a joke?", False),
    ("ESCALATE", True), ("ESCALATE.", True), ("", True), ("   ", True),
    ("I don't know the weather.", True), ("As an AI, I have no feelings.", True),
    ("Sure. I don't know why, though.", False),  # only the opening sentence counts
])
def test_needs_escalation(reply, escalate):
    assert llm_client.needs_escalation(reply) is escalate


@pytest.mark.parametrize("url, allowed", [
    ("http://192.168.43.20:11434", True),  # Android hotspot
    ("http://172.20.10.3:11434", True),  # iPhone hotspot
    ("http://10.9.233.36:11434", True),
    ("http://laptop.local:11434", True),
    ("http://localhost:11434", True),
    ("https://llm.example.com", True),
    ("http://8.8.8.8:11434", False),
    ("http://llm.example.com", False),
    ("http://0.0.0.0:11434", False),
])
def test_lan_http_check(url, allowed):
    if allowed:
        netutil.require_https(url, allow_lan=True)
    else:
        with pytest.raises(RuntimeError, match="refusing"):
            netutil.require_https(url, allow_lan=True)


def test_api_keys_still_need_https_on_the_lan():
    with pytest.raises(RuntimeError, match="refusing"):
        netutil.require_https("http://192.168.43.20:8080/v1")


def last_request_contents(requests):
    return [m["content"] for m in json.loads(requests[0].content)["messages"][1:]]


def test_follow_up_questions_see_the_last_exchanges(env, monkeypatch):
    monkeypatch.setattr(config, "CONVERSATION_TURNS", 2)
    for question in ["one", "two", "three"]:
        _, requests = run_reply({QWEN: ndjson(f"Answer {question}.")}, transcript=question)
    assert last_request_contents(requests) == ["one", "Answer one.", "two", "Answer two.", "three"]


def test_memory_keeps_only_the_newest_turns(env, monkeypatch):
    monkeypatch.setattr(config, "CONVERSATION_TURNS", 1)
    for question in ["one", "two", "three"]:
        _, requests = run_reply({QWEN: ndjson(f"Answer {question}.")}, transcript=question)
    assert last_request_contents(requests) == ["two", "Answer two.", "three"]


def test_memory_off(env, monkeypatch):
    monkeypatch.setattr(config, "CONVERSATION_TURNS", 0)
    run_reply({QWEN: ndjson("First.")}, transcript="one")
    _, requests = run_reply({QWEN: ndjson("Second.")}, transcript="two")
    assert last_request_contents(requests) == ["two"]


def test_memory_is_forgotten_after_a_pause(env, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(llm_client.time, "monotonic", lambda: clock[0])
    run_reply({QWEN: ndjson("First.")}, transcript="one")
    clock[0] += config.CONVERSATION_MEMORY_SECONDS + 1
    _, requests = run_reply({QWEN: ndjson("Second.")}, transcript="two")
    assert last_request_contents(requests) == ["two"]


def test_only_the_final_answer_is_remembered(env):
    run_reply({QWEN: ndjson("ESCALATE"), GEMMA: ndjson("Thursday.")}, transcript="day?")
    _, requests = run_reply({QWEN: ndjson("Sure.")}, transcript="thanks")
    assert last_request_contents(requests) == ["day?", "Thursday.", "thanks"]


@pytest.mark.parametrize("text, trimmed", [
    ("It is Thursday. The weather looks", "It is Thursday."),
    ("It is Thursday.", "It is Thursday."),
    ("Sure thing", "Sure thing"),  # nothing to trim back to
    ("Is it? Yes! And then", "Is it? Yes!"),
    ('He said "hi."', 'He said "hi."'),
])
def test_cut_off_reply_is_trimmed_to_a_full_sentence(text, trimmed):
    assert llm_client.trim_to_sentence(text) == trimmed


def test_tuning_values_are_sent(env, monkeypatch):
    monkeypatch.setattr(config, "LLM_TEMPERATURE", 0.3)
    monkeypatch.setattr(config, "LLM_MAX_TOKENS", 77)
    _, requests = run_reply({QWEN: ndjson("ESCALATE"), GEMMA: ndjson(""), "gpt-4o": "Hi."})
    assert json.loads(requests[0].content)["options"] == {"temperature": 0.3, "num_predict": 77}
    cloud = json.loads(requests[-1].content)
    assert (cloud["temperature"], cloud["max_tokens"]) == (0.3, 77)


def _full_job(monkeypatch, caplog):
    monkeypatch.setattr(config, "ELEVENLABS_TTS_URL", "https://api.elevenlabs.io/v1/text-to-speech")

    async def fake_aplay(source, data=None):
        pass

    monkeypatch.setattr(api, "_aplay", fake_aplay)

    def handler(request):
        if request.url.path.endswith("/audio/transcriptions"):
            return httpx.Response(200, json={"text": "hi robot"})
        if request.url.path.endswith("/api/chat"):
            return httpx.Response(200, content=ndjson("Hello there."))
        return httpx.Response(200, content=b"\x01\x00" * 800)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await api.handle_job(client, api.LISTEN_JOB, b"RIFF")

    with caplog.at_level(logging.INFO, logger="api_routing_task"):
        assert asyncio.run(go()) is True


def test_pipeline_logs_model_but_not_conversation_by_default(env, monkeypatch, caplog):
    _full_job(monkeypatch, caplog)
    assert f"Reply from {QWEN}" in caplog.text
    assert "hi robot" not in caplog.text and "Hello there." not in caplog.text


def test_log_conversations_switch(env, monkeypatch, caplog):
    monkeypatch.setattr(config, "LOG_CONVERSATIONS", True)
    _full_job(monkeypatch, caplog)
    assert "hi robot" in caplog.text and "Hello there." in caplog.text
