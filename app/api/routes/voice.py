"""Voice endpoints: raw speech-to-text and text-to-speech primitives, plus
POST /converse -- a whole spoken turn (audio in, speech out one sentence at
a time) for voice satellites, so a reply starts playing while the rest of
it is still being written.

/transcribe and /speak stay as they were, deliberately separate: the web UI
(or any client) can still compose them with POST /siris/chat itself.
"""

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from app.api.routes.siris import ChatRequest, _resolve_conversation
from app.core.auth import require_api_key
from app.core.siris import SirisAI, get_siris
from app.voice.base import NullSpeechToTextClient, NullTextToSpeechClient
from app.voice.pronunciation import for_speech
from app.voice.turn import VoiceTurn
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


@router.post("/converse")
async def converse(
    file: UploadFile | None = File(None),
    text: str | None = Form(None),
    conversation_id: str | None = Form(None),
    room: str | None = Form(None),
    synthesize: bool = Form(True),
    siris: SirisAI = Depends(get_siris),
) -> StreamingResponse:
    """One spoken turn, streamed back as newline-delimited JSON:

    - `{"type": "transcript", "text"}` -- what was heard.
    - `{"type": "sentence", "text", "audio"}` -- the next sentence of the
      reply, `audio` a base64 MP3 (null when TTS isn't configured or
      `synthesize` is false). Arrives while later sentences are still being
      written; play them in order.
    - `{"type": "tool_start" | "tool_end", "name", ...}` -- progress.
    - `{"type": "final", "conversation_id", "response", "confirmation_required",
      "expects_reply"}` -- always last on success. `expects_reply` is true
      when SirisAI asked something (including "should I go ahead?"), so a
      satellite should listen for an answer without the wake word.
    - `{"type": "error", "detail"}`.

    Send the recording as `file`, or plain `text` instead (typed input, and
    tests). A pending spoken confirmation in this conversation is answered
    by a short "yes"/"no" reply (app/voice/confirmations.py)."""
    audio = await file.read() if file is not None else None
    typed = (text or "").strip()
    if audio is None and not typed:
        raise HTTPException(status_code=422, detail="Send audio as 'file' or text as 'text'.")
    assert siris.stt is not None
    if audio is not None and isinstance(siris.stt, NullSpeechToTextClient):
        raise HTTPException(status_code=501, detail="Speech-to-text isn't configured (set SIRISAI_STT_PROVIDER).")
    filename = (file.filename if file is not None else None) or "audio.wav"
    content_type = (file.content_type if file is not None else None) or "audio/wav"

    async def events() -> AsyncIterator[dict[str, Any]]:
        transcript = typed
        if audio is not None:
            try:
                transcript = (await siris.stt.transcribe(audio, filename=filename, content_type=content_type)).strip()
            except Exception:
                logger.exception("STT transcribe() failed during converse")
                yield {"type": "error", "detail": "Speech-to-text backend call failed."}
                return
        yield {"type": "transcript", "text": transcript}
        if not transcript:
            yield {"type": "final", "conversation_id": conversation_id, "response": None, "expects_reply": False}
            return

        conv_id = _resolve_conversation(siris, ChatRequest(prompt=transcript, conversation_id=conversation_id, room=room))
        async for event in VoiceTurn(siris, conv_id, transcript, synthesize=synthesize).run():
            yield event

    async def ndjson() -> AsyncIterator[str]:
        async for event in events():
            yield json.dumps(event) + "\n"

    return StreamingResponse(ndjson(), media_type="application/x-ndjson", headers={"Cache-Control": "no-cache"})
