"""Reply to what the user said: laptop LLM first, cloud model as the fallback (reply only, no robot control).

1. LOCAL_LLM_MODEL (gemma4:e4b) on the laptop's Ollama, streamed. It must be a non-thinking model:
   thinking-only builds (the current qwen3:4b tag) reason out loud and take ~10 s.
   It is told to answer ESCALATE when unsure.
2. ESCALATE, a refusal ("I can't...", "I don't know...") or an empty reply escalates: to
   LOCAL_LLM_ESCALATE_MODEL if one is set (off by default), else straight to the cloud. The stream is cut as soon
   as the first sentence shows this. A second local model only helps if both fit in GPU memory at once:
   otherwise every escalation reloads a model (4-7 s on an 8 GB laptop GPU).
3. If the laptop can't be reached, errors, or no local model answers, the cloud CHAT_MODEL replies.

Every prompt carries the current date and time, because a model has no clock, and the last few exchanges
(CONVERSATION_TURNS, forgotten after CONVERSATION_MEMORY_SECONDS of silence) so follow-up questions work.
"""
import json
import logging
import re
import time
from datetime import datetime
from typing import Optional

import httpx

import config
from netutil import require_https

logger = logging.getLogger(__name__)

ESCALATE_TOKEN = "ESCALATE"

BASE_PROMPT = (
    "You are EMO, a small two-legged desktop companion robot. You hear the user through a microphone and your "
    "reply is spoken aloud, so answer in one or two short, plain sentences with no lists, markdown or emoji. "
    "You have no internet access and no live data except what is given here. Your knowledge comes from training "
    "and may be out of date: for recent events or anything that changes, say so instead of stating it as current. "
    "If you do not know something, say so briefly instead of guessing."
)
ESCALATE_RULE = f" If you are not confident you can answer correctly, reply with exactly {ESCALATE_TOKEN} and nothing else."

_ESCALATE_RE = re.compile(rf"\b{ESCALATE_TOKEN}\b")
_THINK_RE = re.compile(r"<think>.*?(?:</think>|$)", re.S)  # an unclosed block is still being generated
_THINK_END = "</think>"
_SENTENCE_END_RE = re.compile(r"[.!?](?:\s|$)")
_LAST_SENTENCE_END_RE = re.compile(r".*[.!?]", re.S)
_REFUSAL_RE = re.compile(
    r"\b(?:i can't|i cannot|i can not|i'm unable|i am unable|i'm not able|i am not able|i don't know|i do not know"
    r"|i don't have access|i do not have access|as an ai)\b")


def part_of_day(hour: int) -> str:
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 21:
        return "evening"
    return "night"


def system_prompt(escalate: bool = False, now: Optional[datetime] = None) -> str:
    now = now or datetime.now().astimezone()
    # 12-hour time as it is spoken, and the part of the day spelled out: small models get both wrong otherwise
    prompt = (f"{BASE_PROMPT} Current local date and time: {now:%A, %d %B %Y, %I:%M %p} ({now.tzname()}), "
              f"so it is {part_of_day(now.hour)}.")
    if config.ROBOT_LOCATION:
        prompt += f" You are in {config.ROBOT_LOCATION}."
    return prompt + (ESCALATE_RULE if escalate else "")


_history: list[tuple[float, str, str]] = []  # (monotonic time, what the user said, reply)
_escalated = False  # the escalation model ran, so the laptop may have unloaded the first one


def clear_history() -> None:
    _history.clear()


def _recent_history() -> list[dict]:
    if _history and time.monotonic() - _history[-1][0] > config.CONVERSATION_MEMORY_SECONDS:
        _history.clear()  # a new conversation: old context would only confuse the model
    messages = []
    for _, said, reply in _history:
        messages += [{"role": "user", "content": said}, {"role": "assistant", "content": reply}]
    return messages


def _remember(transcript: str, reply: str) -> None:
    _history.append((time.monotonic(), transcript, reply))
    del _history[:max(len(_history) - config.CONVERSATION_TURNS, 0)]  # keep the newest CONVERSATION_TURNS


def _messages(transcript: str, escalate: bool = False) -> list[dict]:
    return ([{"role": "system", "content": system_prompt(escalate)}] + _recent_history()
            + [{"role": "user", "content": transcript}])


def trim_to_sentence(text: str) -> str:
    """Cut a reply that hit the token limit back to its last full sentence, so it isn't spoken half-finished."""
    text = text.strip()
    if not text or text[-1] in ".!?\"')":
        return text
    match = _LAST_SENTENCE_END_RE.match(text)
    return match.group(0) if match else text


def strip_think(text: str) -> str:
    """Drop <think> blocks: thinking is switched off, this is only a safety net.

    Thinking-only models get the opening <think> from their template, so only the closing tag shows up.
    """
    if _THINK_END in text and "<think>" not in text.split(_THINK_END)[0]:
        text = text.rsplit(_THINK_END, 1)[1]
    return _THINK_RE.sub("", text)


def first_sentence(text: str) -> Optional[str]:
    """The first complete sentence, or None while it is still being streamed."""
    match = _SENTENCE_END_RE.search(text)
    return text[:match.end()].strip() if match else None


def needs_escalation(reply: str) -> bool:
    """True when the first model's reply should go to the bigger model: ESCALATE, empty, or a refusal."""
    reply = reply.strip()
    if not reply or _ESCALATE_RE.search(reply):
        return True
    opening = (first_sentence(reply) or reply).lower().replace("\u2019", "'")
    return bool(_REFUSAL_RE.search(opening))


async def _ollama_chat(client: httpx.AsyncClient, model: str, transcript: str, first_tier: bool) -> str:
    """Stream one reply from Ollama's /api/chat (NDJSON). No API key is sent: Ollama has no auth."""
    url = config.LOCAL_LLM_URL.rstrip("/")
    require_https(url, allow_lan=True)  # the transcript still shouldn't cross the internet in plain text
    payload = {
        "model": model,
        "messages": _messages(transcript, escalate=first_tier),
        "stream": True,
        "think": False,
        "keep_alive": config.LOCAL_LLM_KEEP_ALIVE,
        "options": {"temperature": config.LLM_TEMPERATURE, "num_predict": config.LLM_MAX_TOKENS,
                    "num_ctx": config.LOCAL_LLM_CONTEXT},
    }
    timeout = httpx.Timeout(config.LOCAL_LLM_TIMEOUT, connect=config.LOCAL_LLM_CONNECT_TIMEOUT)
    text = ""
    checked = not first_tier
    async with client.stream("POST", f"{url}/api/chat", json=payload, timeout=timeout) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if not line.strip():
                continue
            chunk = json.loads(line)
            if chunk.get("error"):
                raise RuntimeError(f"{model}: {chunk['error']}")
            text += chunk.get("message", {}).get("content", "")
            if not checked:
                visible = strip_think(text).lstrip()
                if visible.startswith(ESCALATE_TOKEN):
                    break  # closing the stream stops generation on the laptop
                opening = first_sentence(visible)
                if opening is not None:
                    checked = True
                    if needs_escalation(opening):
                        break
            if chunk.get("done"):
                break
    return strip_think(text).strip()


async def _cloud_chat(client: httpx.AsyncClient, transcript: str) -> str:
    require_https(config.OPENAI_BASE_URL)
    response = await client.post(
        f"{config.OPENAI_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
        json={
            "model": config.CHAT_MODEL,
            "messages": _messages(transcript),
            "temperature": config.LLM_TEMPERATURE,
            "max_tokens": config.LLM_MAX_TOKENS,
        },
    )
    response.raise_for_status()
    reply = str(response.json()["choices"][0]["message"]["content"]).strip()
    if not reply:
        raise RuntimeError(f"{config.CHAT_MODEL} returned empty text")
    return reply


async def _local_reply(client: httpx.AsyncClient, transcript: str) -> Optional[tuple[str, str]]:
    reply = await _ollama_chat(client, config.LOCAL_LLM_MODEL, transcript, first_tier=True)
    if not needs_escalation(reply):
        return reply, config.LOCAL_LLM_MODEL
    if not config.LOCAL_LLM_ESCALATE_MODEL:
        logger.info("%s unsure and no escalation model: asking %s", config.LOCAL_LLM_MODEL, config.CHAT_MODEL)
        return None
    global _escalated
    logger.info("%s unsure: escalating to %s", config.LOCAL_LLM_MODEL, config.LOCAL_LLM_ESCALATE_MODEL)
    _escalated = True
    reply = await _ollama_chat(client, config.LOCAL_LLM_ESCALATE_MODEL, transcript, first_tier=False)
    if reply and not _ESCALATE_RE.search(reply):
        return reply, config.LOCAL_LLM_ESCALATE_MODEL
    logger.info("%s gave no answer: asking %s", config.LOCAL_LLM_ESCALATE_MODEL, config.CHAT_MODEL)
    return None


async def warm_up(client: httpx.AsyncClient) -> None:
    """Load LOCAL_LLM_MODEL on the laptop now, so a question doesn't wait for it (or time out to the cloud).

    Called at start-up and after an escalation (the bigger model may have pushed it out of GPU memory).
    Never raises: an unreachable laptop only means the cloud answers until it is back.
    """
    global _escalated
    _escalated = False
    if not config.LOCAL_LLM_URL:
        return
    url = config.LOCAL_LLM_URL.rstrip("/")
    try:
        require_https(url, allow_lan=True)
        response = await client.post(
            f"{url}/api/chat",  # no messages = just load the model
            json={"model": config.LOCAL_LLM_MODEL, "messages": [], "keep_alive": config.LOCAL_LLM_KEEP_ALIVE,
                  "options": {"num_ctx": config.LOCAL_LLM_CONTEXT}},
            timeout=httpx.Timeout(60.0, connect=config.LOCAL_LLM_CONNECT_TIMEOUT),
        )
        response.raise_for_status()
        logger.info("Laptop LLM %s loaded", config.LOCAL_LLM_MODEL)
    except (httpx.HTTPError, RuntimeError) as exc:
        logger.info("Laptop LLM %s not loaded (%r); %s answers until it is", config.LOCAL_LLM_MODEL, exc,
                    config.CHAT_MODEL)


async def rewarm_after_escalation(client: httpx.AsyncClient) -> None:
    if _escalated:
        await warm_up(client)


async def get_reply(client: httpx.AsyncClient, transcript: str) -> tuple[str, str]:
    """Return (reply, model that answered). Raises only if the cloud fallback fails too."""
    result = None
    if config.LOCAL_LLM_URL:
        try:
            result = await _local_reply(client, transcript)
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:  # unreachable, timeout, bad JSON, model error
            logger.warning("Laptop LLM failed (%r): asking %s", exc, config.CHAT_MODEL)
    if result is None:
        result = await _cloud_chat(client, transcript), config.CHAT_MODEL
    reply, model = trim_to_sentence(result[0]), result[1]
    _remember(transcript, reply)
    return reply, model
