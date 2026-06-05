from contextlib import asynccontextmanager

from fastapi import FastAPI

from api_server.core.config import get_settings
from api_server.routers.audio import router as audio_router
from api_server.routers.health import router as health_router
from api_server.services.elevenlabs_service import ElevenLabsService
from api_server.services.openai_service import OpenAIService


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings
    app.state.openai_service = OpenAIService(settings)
    app.state.elevenlabs_service = ElevenLabsService(settings)
    yield


app = FastAPI(
    title="EMO-Bot API Server",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(health_router)
app.include_router(audio_router)
