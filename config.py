"""Central runtime configuration.

Importing this module loads ``.env`` from the repository root, so it must be imported before
anything reads the environment. Other modules read values as ``config.NAME`` at call time
(not ``from config import NAME``) so tests and callers can override them.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent
load_dotenv(REPO_ROOT / ".env")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    return float(raw) if raw else default


# MQTT broker (Mosquitto on the Pi)
MQTT_HOST = os.getenv("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))

TOPIC_STATE = "robot/state"  # POSTURE_POOR / POSTURE_OK
TOPIC_FACE_ERROR = "robot/vision/face_error"  # "<x_err>,<y_err>" in pixels
TOPIC_ERROR = "robot/error"  # "error" engages E-stop, "clear" releases it
TOPIC_WAKE_FLAG = "robot/audio/wake_flag"  # "1" while a conversation is in progress
TOPIC_AUDIO_STATE = "robot/audio/state"  # JSON status from the audio pipeline
TOPIC_AUDIO_INTENT = "robot/audio/intent"  # audio cues requested by the behavior tree
TOPIC_LOCOMOTION_CMD = "robot/locomotion/cmd"  # stand | rest | stop | walk,<speed>,<turn>[,<s>] | ...
TOPIC_LOCOMOTION_EVENT = "robot/locomotion/event"  # EVT,FALLEN / EVT,WATCHDOG / NACK,... from the Nano
TOPIC_LOCOMOTION_TELEMETRY = "robot/locomotion/telemetry"  # T,<pitch x10>,<rate x10>,<corr x10>,<mode>

# Pi <-> Nano serial link. "sim" logs commands instead of opening a port.
SERIAL_PORT = os.getenv("SERIAL_PORT", "/dev/ttyUSB0")
SERIAL_BAUD = int(os.getenv("SERIAL_BAUD", "115200"))

# Subsystem switches (the robot keeps running in degraded mode if an optional one fails)
ENABLE_VISION = _env_bool("ENABLE_VISION", True)
ENABLE_AUDIO = _env_bool("ENABLE_AUDIO", True)

# "picamera2" (Pi CSI camera), a V4L2 path such as "/dev/video0", or a webcam index such as "0"
CAMERA_SOURCE = os.getenv("CAMERA_SOURCE", "/dev/video0")

# Microphone for sounddevice (name substring or index); empty = system default.
# List devices with: python -m sounddevice
AUDIO_INPUT_DEVICE = os.getenv("AUDIO_INPUT_DEVICE", "").strip() or None
# ALSA playback device for aplay, e.g. "plughw:0"; empty = default
AUDIO_OUTPUT_DEVICE = os.getenv("AUDIO_OUTPUT_DEVICE", "").strip() or None

PORCUPINE_ACCESS_KEY = os.getenv("PORCUPINE_ACCESS_KEY", "")
PORCUPINE_KEYWORD_PATH = os.getenv("PORCUPINE_KEYWORD_PATH", "")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "whisper-1")
CHAT_MODEL = os.getenv("CHAT_MODEL", "gpt-4o")

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_TTS_URL = os.getenv("ELEVENLABS_TTS_URL", "https://api.elevenlabs.io/v1/text-to-speech")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "EXAVITQu4vr4xnSDxMaL")

# Budget for the whole Whisper -> LLM -> TTS cascade (playback is not included)
API_TIMEOUT_SECONDS = _env_float("API_TIMEOUT_SECONDS", 15.0)

NETWORK_ERROR_FILE = REPO_ROOT / "assets" / "network_error.wav"
