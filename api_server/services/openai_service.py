from __future__ import annotations

import httpx
from fastapi import UploadFile

from api_server.core.config import Settings


class OpenAIService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._settings.openai_api_key}"}

    async def transcribe(self, audio_file: UploadFile) -> str:
        content = await audio_file.read()
        files = {
            "file": (
                audio_file.filename or "audio.wav",
                content,
                audio_file.content_type or "application/octet-stream",
            )
        }
        data = {"model": self._settings.whisper_model}

        async with httpx.AsyncClient(timeout=self._settings.api_timeout_seconds) as client:
            response = await client.post(
                f"{self._settings.openai_base_url}/audio/transcriptions",
                headers=self._headers(),
                files=files,
                data=data,
            )

        response.raise_for_status()
        payload = response.json()
        return str(payload.get("text", "")).strip()

    async def chat(self, text: str) -> str:
        payload = {
            "model": self._settings.chat_model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a desktop companion robot. Respond in 1-2 short sentences, "
                        "be helpful, calm, and practical. Avoid verbosity."
                    ),
                },
                {"role": "user", "content": text},
            ],
            "temperature": 0.6,
            "max_tokens": 100,
        }

        async with httpx.AsyncClient(timeout=self._settings.api_timeout_seconds) as client:
            response = await client.post(
                f"{self._settings.openai_base_url}/chat/completions",
                headers={**self._headers(), "Content-Type": "application/json"},
                json=payload,
            )

        response.raise_for_status()
        payload = response.json()
        return str(payload["choices"][0]["message"]["content"]).strip()
