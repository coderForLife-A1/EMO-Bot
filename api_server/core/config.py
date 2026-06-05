from dataclasses import dataclass
import os

from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class Settings:
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    elevenlabs_api_key: str = os.getenv("ELEVENLABS_API_KEY", "")
    porcupine_access_key: str = os.getenv("PORCUPINE_ACCESS_KEY", "")
    porcupine_keyword_path: str = os.getenv("PORCUPINE_KEYWORD_PATH", "")
    elevenlabs_voice_id: str = os.getenv("ELEVENLABS_VOICE_ID", "EXAVITQu4vr4xnSDxMaL")
    openai_base_url: str = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    elevenlabs_tts_url: str = os.getenv("ELEVENLABS_TTS_URL", "https://api.elevenlabs.io/v1/text-to-speech")
    whisper_model: str = os.getenv("WHISPER_MODEL", "whisper-1")
    chat_model: str = os.getenv("CHAT_MODEL", "gpt-4o")
    api_timeout_seconds: float = float(os.getenv("API_TIMEOUT_SECONDS", "15"))


def get_settings() -> Settings:
    return Settings()
