from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    text: str = Field(min_length=1)


class ChatResponse(BaseModel):
    response_text: str


class TranscriptionResponse(BaseModel):
    text: str


class TTSRequest(BaseModel):
    text: str = Field(min_length=1)


class TurnResponse(BaseModel):
    transcript: str
    response_text: str
    tts_ready: bool
