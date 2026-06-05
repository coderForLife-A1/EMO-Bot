from __future__ import annotations

import io

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from api_server.schemas import ChatRequest, ChatResponse, TTSRequest, TranscriptionResponse, TurnResponse
from api_server.services.elevenlabs_service import ElevenLabsService
from api_server.services.openai_service import OpenAIService


router = APIRouter(prefix="/api/v1/audio", tags=["audio"])


def _services(request: Request) -> tuple[OpenAIService, ElevenLabsService]:
    return request.app.state.openai_service, request.app.state.elevenlabs_service


@router.post("/transcribe", response_model=TranscriptionResponse)
async def transcribe(request: Request, file: UploadFile = File(...)) -> TranscriptionResponse:
    openai_service, _ = _services(request)
    try:
        text = await openai_service.transcribe(file)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Transcription failed: {exc}") from exc

    if not text:
        raise HTTPException(status_code=502, detail="Transcription returned empty text")

    return TranscriptionResponse(text=text)


@router.post("/chat", response_model=ChatResponse)
async def chat(request: Request, payload: ChatRequest) -> ChatResponse:
    openai_service, _ = _services(request)
    try:
        response_text = await openai_service.chat(payload.text)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Chat failed: {exc}") from exc

    if not response_text:
        raise HTTPException(status_code=502, detail="Chat returned empty text")

    return ChatResponse(response_text=response_text)


@router.post("/tts")
async def tts(request: Request, payload: TTSRequest) -> StreamingResponse:
    _, elevenlabs_service = _services(request)
    try:
        audio_bytes = await elevenlabs_service.synthesize(payload.text)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"TTS failed: {exc}") from exc

    return StreamingResponse(
        io.BytesIO(audio_bytes),
        media_type="audio/wav",
        headers={"Content-Disposition": 'attachment; filename="response.wav"'},
    )


@router.post("/turn", response_model=TurnResponse)
async def turn(request: Request, file: UploadFile = File(...)) -> TurnResponse:
    openai_service, elevenlabs_service = _services(request)
    try:
        transcript = await openai_service.transcribe(file)
        if not transcript:
            raise HTTPException(status_code=502, detail="Transcription returned empty text")

        response_text = await openai_service.chat(transcript)
        if not response_text:
            raise HTTPException(status_code=502, detail="Chat returned empty text")

        await elevenlabs_service.synthesize(response_text)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Voice turn failed: {exc}") from exc

    return TurnResponse(transcript=transcript, response_text=response_text, tts_ready=True)
