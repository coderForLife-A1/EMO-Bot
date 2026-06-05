from __future__ import annotations

import httpx

from api_server.core.config import Settings


class ElevenLabsService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def synthesize(self, text: str) -> bytes:
        payload = {
            "text": text,
            "model_id": "eleven_turbo_v2_5",
            "voice_settings": {
                "stability": 0.45,
                "similarity_boost": 0.75,
            },
        }

        async with httpx.AsyncClient(timeout=self._settings.api_timeout_seconds) as client:
            response = await client.post(
                f"{self._settings.elevenlabs_tts_url}/{self._settings.elevenlabs_voice_id}",
                headers={
                    "xi-api-key": self._settings.elevenlabs_api_key,
                    "Content-Type": "application/json",
                    "Accept": "audio/wav",
                },
                json=payload,
            )

        response.raise_for_status()
        return response.content
