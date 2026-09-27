"""Voice endpoints: raw speech-to-text and text-to-speech primitives.

Deliberately not a single combined "voice chat" endpoint -- the web UI (or
any client) composes these with POST /siris/chat itself (transcribe -> chat
-> speak), same as any other tool. Keeps each piece independently useful
and testable.
"""

import logging

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from app.core.auth import require_api_key
from app.core.siris import SirisAI, get_siris
from app.voice.base import NullSpeechToTextClient, NullTextToSpeechClient
from app.voice.pronunciation import for_speech
from app.core.user_context import resolve_user_id

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/siris/voice", tags=["voice"], dependencies=[Depends(require_api_key), Depends(resolve_user_id)]
)


class TranscribeResponse(BaseModel):
    text: str


@router.post("/transcribe", response_model=TranscribeResponse)
async def transcribe(file: UploadFile = File(...), siris: SirisAI = Depends(get_siris)) -> TranscribeResponse:
    assert siris.stt is not None
    if isinstance(siris.stt, NullSpeechToTextClient):
        raise HTTPException(status_code=501, detail="Speech-to-text isn't configured (set SIRISAI_STT_PROVIDER).")
    audio = await file.read()
    try:
        text = await siris.stt.transcribe(
            audio, filename=file.filename or "audio.wav", content_type=file.content_type or "audio/wav"
        )
    except Exception:
        logger.exception("STT transcribe() failed")
        raise HTTPException(status_code=502, detail="Speech-to-text backend call failed.") from None

    return TranscribeResponse(text=text)


class SpeakRequest(BaseModel):
    text: str


@router.post("/speak")
async def speak(request: SpeakRequest, siris: SirisAI = Depends(get_siris)) -> Response:
    assert siris.tts is not None
    if isinstance(siris.tts, NullTextToSpeechClient):
        raise HTTPException(status_code=501, detail="Text-to-speech isn't configured (set SIRISAI_TTS_PROVIDER).")
    try:
        audio = await siris.tts.synthesize(for_speech(request.text))
    except Exception:
        logger.exception("TTS synthesize() failed")
        raise HTTPException(status_code=502, detail="Text-to-speech backend call failed.") from None

    return Response(content=audio, media_type="audio/mpeg")
