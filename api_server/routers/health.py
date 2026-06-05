from fastapi import APIRouter, Request

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(request: Request) -> dict[str, object]:
    settings = request.app.state.settings
    return {
        "status": "ok",
        "openai_configured": bool(settings.openai_api_key),
        "elevenlabs_configured": bool(settings.elevenlabs_api_key),
        "porcupine_configured": bool(settings.porcupine_access_key),
    }
