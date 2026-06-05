# EMO-Bot

## FastAPI backend

This repo now includes a separate FastAPI service under `api_server/` so API routing stays isolated from the robot runtime scripts.

### Available routes

- `GET /health` - checks whether the required API keys are configured.
- `POST /api/v1/audio/transcribe` - uploads an audio file to OpenAI Whisper.
- `POST /api/v1/audio/chat` - sends text to the chat model.
- `POST /api/v1/audio/tts` - turns text into WAV audio using ElevenLabs.
- `POST /api/v1/audio/turn` - runs transcription, chat, and TTS together.

### Run

```bash
uvicorn api_server.main:app --reload
```

### Setup

```bash
Copy-Item .env.example .env
pip install -r requirements.txt
```
