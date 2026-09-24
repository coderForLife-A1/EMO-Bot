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


# MQTT broker (Mosquitto on the Pi). Anyone who can reach the broker can drive the robot, so keep it on
# localhost, or use a login (password_file + ACLs in Mosquitto) and TLS for a broker on the network.
MQTT_HOST = os.getenv("MQTT_HOST", "127.0.0.1")
MQTT_TLS = _env_bool("MQTT_TLS", False)
MQTT_PORT = int(os.getenv("MQTT_PORT", "").strip() or ("8883" if MQTT_TLS else "1883"))
MQTT_USERNAME = os.getenv("MQTT_USERNAME", "")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")
MQTT_CA_CERTS = os.getenv("MQTT_CA_CERTS", "")  # CA file for a self-signed broker certificate; empty = system CAs

TOPIC_STATE = "robot/state"  # POSTURE_POOR / POSTURE_OK
TOPIC_FACE_ERROR = "robot/vision/face_error"  # "<x_err>,<y_err>" in pixels
TOPIC_ERROR = "robot/error"  # "error" engages E-stop, "clear" releases it
TOPIC_WAKE_FLAG = "robot/audio/wake_flag"  # "1" while a conversation is in progress
TOPIC_AUDIO_STATE = "robot/audio/state"  # JSON status from the audio pipeline
TOPIC_AUDIO_INTENT = "robot/audio/intent"  # audio cues requested by the behavior tree
TOPIC_LOCOMOTION_CMD = "robot/locomotion/cmd"  # stand | rest | stop | walk,<speed>,<turn>[,<s>] | ...
TOPIC_LOCOMOTION_EVENT = "robot/locomotion/event"  # EVT,FALLEN / EVT,WATCHDOG / NACK,... from the controller
TOPIC_LOCOMOTION_TELEMETRY = "robot/locomotion/telemetry"  # T,<pitch x10>,<rate x10>,<corr x10>,<mode>
TOPIC_DISTANCE = "robot/sensor/distance"  # <mm> from the ESP32's VL53L0X, -1 = nothing in range (after "distance")
TOPIC_VISION_STATE = "robot/vision/state"  # UP / DOWN when the camera starts or stops delivering frames

# Pi <-> controller (ESP32 or Nano) serial link. "sim" logs commands instead of opening a port.
SERIAL_PORT = os.getenv("SERIAL_PORT", "/dev/ttyUSB0")
SERIAL_BAUD = int(os.getenv("SERIAL_BAUD", "115200"))

# Subsystem switches (the robot keeps running in degraded mode if an optional one fails)
ENABLE_VISION = _env_bool("ENABLE_VISION", True)
ENABLE_AUDIO = _env_bool("ENABLE_AUDIO", True)
# Face detection costs CPU on the Pi and nothing uses its output yet (no head servos); off by default.
FACE_DETECTION = _env_bool("FACE_DETECTION", False)
# Let the robot stand/walk when the controller booted without an IMU (no balance, no fall detection).
# Only for bench tests: keep it off on a real robot.
ALLOW_NO_IMU = _env_bool("ALLOW_NO_IMU", False)

# "picamera2" (Pi CSI camera), a V4L2 path such as "/dev/video0", or a webcam index such as "0"
CAMERA_SOURCE = os.getenv("CAMERA_SOURCE", "/dev/video0")

# Microphone for sounddevice (name substring or index); empty = system default.
# List devices with: python -m sounddevice
AUDIO_INPUT_DEVICE = os.getenv("AUDIO_INPUT_DEVICE", "").strip() or None
# ALSA playback device for aplay, e.g. "plughw:0"; empty = default
AUDIO_OUTPUT_DEVICE = os.getenv("AUDIO_OUTPUT_DEVICE", "").strip() or None

# Microphone level (RMS of 16-bit samples) that counts as speech after the wake word. Too high = speech is
# missed; too low = room noise keeps the recording going. "No speech ... loudest RMS" in the log helps tune it.
SPEECH_RMS_THRESHOLD = _env_float("SPEECH_RMS_THRESHOLD", 500.0)

PORCUPINE_ACCESS_KEY = os.getenv("PORCUPINE_ACCESS_KEY", "")
PORCUPINE_KEYWORD_PATH = os.getenv("PORCUPINE_KEYWORD_PATH", "")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "whisper-1")
# Spoken language (ISO 639-1, e.g. "en", "hi", "ta"); "auto" = let Whisper guess, which is unreliable on short clips
_whisper_language = os.getenv("WHISPER_LANGUAGE", "").strip() or "en"
WHISPER_LANGUAGE = "" if _whisper_language.lower() == "auto" else _whisper_language
# Vocabulary hint for Whisper (names it should spell right); "off" = none
_whisper_prompt = os.getenv("WHISPER_PROMPT", "").strip() or "EMO is a small desktop robot. The user is talking to EMO."
WHISPER_PROMPT = "" if _whisper_prompt.lower() == "off" else _whisper_prompt
CHAT_MODEL = os.getenv("CHAT_MODEL", "gpt-4o")  # cloud reply model: the fallback when the laptop LLM is set

# Reply model on the laptop's Ollama, e.g. http://192.168.43.20:11434 (llm_client.py).
# Empty = skip it and always use CHAT_MODEL. Plain http is allowed only to a LAN address; Ollama gets no API key.
LOCAL_LLM_URL = os.getenv("LOCAL_LLM_URL", "").strip()
# Must not be a thinking model (they reason out loud and are slow). gemma4:e4b: 3.2 GB of GPU memory at a
# 4096 context, about 0.5-1.7 s per reply, and says when its knowledge may be out of date.
LOCAL_LLM_MODEL = os.getenv("LOCAL_LLM_MODEL", "").strip() or "gemma4:e4b"
# Second local model asked when LOCAL_LLM_MODEL is unsure; "off" (default) = the cloud CHAT_MODEL is asked instead.
# Only worth it if both models fit in GPU memory together, or each escalation reloads a model for seconds.
_escalate = os.getenv("LOCAL_LLM_ESCALATE_MODEL", "").strip() or "off"
LOCAL_LLM_ESCALATE_MODEL = "" if _escalate.lower() in {"off", "none", "0"} else _escalate
LOCAL_LLM_CONNECT_TIMEOUT = _env_float("LOCAL_LLM_CONNECT_TIMEOUT", 1.0)  # laptop unreachable -> cloud quickly
LOCAL_LLM_TIMEOUT = _env_float("LOCAL_LLM_TIMEOUT", 5.0)  # longest wait for the next piece of a reply
LOCAL_LLM_KEEP_ALIVE = os.getenv("LOCAL_LLM_KEEP_ALIVE", "").strip() or "30m"  # keep the model loaded on the laptop
# Context window in tokens. Ollama may otherwise pick the model's maximum (131k for gemma3:4b), which pushes part
# of the model out of an 8 GB GPU onto the CPU. A short spoken chat needs far less.
LOCAL_LLM_CONTEXT = int(os.getenv("LOCAL_LLM_CONTEXT", "").strip() or "4096")

# Reply tuning (laptop and cloud models). Low temperature = fewer made-up facts; a reply cut by the token
# limit is trimmed back to its last full sentence.
LLM_TEMPERATURE = _env_float("LLM_TEMPERATURE", 0.4)
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "").strip() or "150")
# Follow-up questions: remember this many exchanges, forgotten after this many seconds without talking
CONVERSATION_TURNS = int(os.getenv("CONVERSATION_TURNS", "").strip() or "3")
CONVERSATION_MEMORY_SECONDS = _env_float("CONVERSATION_MEMORY_SECONDS", 120.0)

# Told to the reply model with the date and time, e.g. "Chennai, India"; empty = not mentioned
ROBOT_LOCATION = os.getenv("ROBOT_LOCATION", "").strip()
# 1 = log what the user said and the reply at INFO (they land in the system journal); 0 = DEBUG only
LOG_CONVERSATIONS = _env_bool("LOG_CONVERSATIONS", False)

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_TTS_URL = os.getenv("ELEVENLABS_TTS_URL", "https://api.elevenlabs.io/v1/text-to-speech")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "EXAVITQu4vr4xnSDxMaL")

# Budget for the whole Whisper -> LLM (all tiers) -> TTS cascade (playback is not included)
API_TIMEOUT_SECONDS = _env_float("API_TIMEOUT_SECONDS", 15.0)

NETWORK_ERROR_FILE = REPO_ROOT / "assets" / "network_error.wav"
