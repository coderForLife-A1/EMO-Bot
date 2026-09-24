"""Reply to what the user said: laptop LLM first, cloud model as the fallback (reply only, no robot control).

1. LOCAL_LLM_MODEL (qwen3:4b) on the laptop's Ollama, streamed. It is told to answer ESCALATE when unsure.
2. ESCALATE, a refusal ("I can't...", "I don't know...") or an empty reply sends the question to
   LOCAL_LLM_ESCALATE_MODEL (gemma4:e4b). The stream is cut as soon as the first sentence shows this.
3. If the laptop can't be reached, errors, or neither local model answers, the cloud CHAT_MODEL replies.

Every prompt carries the current date and time, because a model has no clock.
"""
import json
import logging
import re
from datetime import datetime
from typing import Optional

import httpx

import config
from netutil import require_https

logger = logging.getLogger(__name__)

ESCALATE_TOKEN = "ESCALATE"
TEMPERATURE = 0.6
MAX_TOKENS = 100

BASE_PROMPT = (
    "You are EMO, a small two-legged desktop companion robot. You hear the user through a microphone and your "
    "reply is spoken aloud, so answer in one or two short, plain sentences with no lists, markdown or emoji. "
    "You have no internet access and no live data except what is given here. "
    "If you do not know something, say so briefly instead of guessing."
)
ESCALATE_RULE = f" If you are not confident you can answer correctly, reply with exactly {ESCALATE_TOKEN} and nothing else."

_ESCALATE_RE = re.compile(rf"\b{ESCALATE_TOKEN}\b")
_THINK_RE = re.compile(r"<think>.*?(?:</think>|$)", re.S)  # an unclosed block is still being generated
_SENTENCE_END_RE = re.compile(r"[.!?](?:\s|$)")
_REFUSAL_RE = re.compile(
    r"\b(?:i can't|i cannot|i can not|i'm unable|i am unable|i'm not able|i am not able|i don't know|i do not know"
    r"|i don't have access|i do not have access|as an ai)\b")


def system_prompt(escalate: bool = False, now: Optional[datetime] = None) -> str:
    now = now or datetime.now().astimezone()
    prompt = f"{BASE_PROMPT} Current local date and time: {now:%A, %d %B %Y, %H:%M} ({now.tzname()})."
    if config.ROBOT_LOCATION:
        prompt += f" You are in {config.ROBOT_LOCATION}."
    return prompt + (ESCALATE_RULE if escalate else "")


def _messages(transcript: str, escalate: bool = False) -> list[dict]:
    return [{"role": "system", "content": system_prompt(escalate)}, {"role": "user", "content": transcript}]


def strip_think(text: str) -> str:
    """Drop <think> blocks: thinking is switched off, this is only a safety net."""
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
        "options": {"temperature": TEMPERATURE, "num_predict": MAX_TOKENS},
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
            "temperature": TEMPERATURE,
            "max_tokens": MAX_TOKENS,
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
    logger.info("%s unsure: escalating to %s", config.LOCAL_LLM_MODEL, config.LOCAL_LLM_ESCALATE_MODEL)
    reply = await _ollama_chat(client, config.LOCAL_LLM_ESCALATE_MODEL, transcript, first_tier=False)
    if reply and not _ESCALATE_RE.search(reply):
        return reply, config.LOCAL_LLM_ESCALATE_MODEL
    logger.info("%s gave no answer: asking %s", config.LOCAL_LLM_ESCALATE_MODEL, config.CHAT_MODEL)
    return None


async def get_reply(client: httpx.AsyncClient, transcript: str) -> tuple[str, str]:
    """Return (reply, model that answered). Raises only if the cloud fallback fails too."""
    if config.LOCAL_LLM_URL:
        try:
            result = await _local_reply(client, transcript)
            if result is not None:
                return result
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:  # unreachable, timeout, bad JSON, model error
            logger.warning("Laptop LLM failed (%r): asking %s", exc, config.CHAT_MODEL)
    return await _cloud_chat(client, transcript), config.CHAT_MODEL
