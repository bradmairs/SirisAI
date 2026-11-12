"""Voice endpoints: raw speech-to-text and text-to-speech primitives, plus
POST /converse -- a whole spoken turn (audio in, speech out one sentence at
a time) for voice satellites, so a reply starts playing while the rest of
it is still being written.

/transcribe and /speak stay as they were, deliberately separate: the web UI
(or any client) can still compose them with POST /siris/chat itself.
"""

import asyncio
import base64
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from app.api.routes.siris import ChatRequest, _resolve_conversation
from app.core.auth import require_api_key
from app.core.siris import SirisAI, get_siris
from app.voice.base import NullSpeechToTextClient, NullTextToSpeechClient
from app.voice.pronunciation import for_speech
from app.voice.sentences import strip_wake
from app.voice.turn import VoiceTurn
from app.core.user_context import get_current_user_id, resolve_user_id, role_for, set_current_user_id, set_turn_role

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
    wake: bool = Form(False),
    x_siris_user: str | None = Header(default=None),
    siris: SirisAI = Depends(get_siris),
) -> StreamingResponse:
    """One spoken turn, streamed back as newline-delimited JSON:

    - `{"type": "transcript", "text"}` -- what was heard.
    - `{"type": "speaker", "user_id", "score"}` -- who said it, when voices
      are enrolled and no X-Siris-User header was sent; the turn then runs
      as that user (their memories, goals, "my ...").
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
        heard: str | None = None
        if wake and audio is not None:
            # Listening for "Hey Siris" (5.8): transcribe first, and drop anything not addressed to SirisAI.
            try:
                heard = (await siris.stt.transcribe(audio, filename=filename, content_type=content_type)).strip()
            except Exception:
                logger.exception("STT transcribe() failed while listening for the wake word")
                yield {"type": "ignored"}
                return
            request = strip_wake(heard)
            if request is None:
                yield {"type": "ignored"}
                return
            if not request:
                yield {"type": "transcript", "text": heard}
                yield await _yes_event(siris, synthesize)
                yield {"type": "final", "conversation_id": conversation_id, "response": "Yes?", "expects_reply": True}
                return
            heard = request
        if audio is not None and not (x_siris_user and x_siris_user.strip()):
            match = await asyncio.to_thread(siris.identify_speaker, audio)
            if match is not None and match["user_id"]:
                set_current_user_id(match["user_id"])
                role = role_for(match["user_id"], siris.settings.voice_roles)
                set_turn_role(role)
                yield {"type": "speaker", "user_id": match["user_id"], "score": match["score"], "role": role}
            elif match is not None and siris.settings.voice_unknown_as_guest and siris.voiceprints.list_voiceprints():
                # A voice nobody enrolled (5.0): a guest for this turn.
                set_turn_role("guest")
                yield {"type": "speaker", "user_id": None, "score": match["score"], "role": "guest"}
        if heard is not None:
            transcript = heard
        elif audio is not None:
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


async def _yes_event(siris: SirisAI, synthesize: bool) -> dict[str, Any]:
    """A short "Yes?" when only the wake phrase was said; the reply expects an answer, so the HUD listens."""
    audio = None
    if synthesize and siris.tts is not None:
        try:
            audio = base64.b64encode(await siris.tts.synthesize("Yes?")).decode("ascii")
        except Exception:  # noqa: BLE001
            logger.warning("TTS failed for the wake reply")
    return {"type": "sentence", "text": "Yes?", "audio": audio}


class VoiceprintInfo(BaseModel):
    user_id: str
    samples: int
    updated_at: str


class EnrollResponse(BaseModel):
    user_id: str
    samples: int


def _require_encoder(siris: SirisAI) -> None:
    if siris.speaker_encoder() is None:
        raise HTTPException(
            status_code=501,
            detail="Speaker recognition isn't installed (run scripts/fetch_voice_encoder.py; see SIRISAI_SPEAKER_ID_MODEL_PATH).",
        )


@router.get("/voiceprints", response_model=list[VoiceprintInfo])
def list_voiceprints(siris: SirisAI = Depends(get_siris)) -> list[VoiceprintInfo]:
    return [VoiceprintInfo(**v) for v in siris.voiceprints.list_voiceprints()]


@router.post("/voiceprints/enroll", response_model=EnrollResponse)
async def enroll_voiceprint(
    file: UploadFile = File(...), user: str | None = Form(None), siris: SirisAI = Depends(get_siris)
) -> EnrollResponse:
    """Add one sample of someone's voice (a few seconds of natural speech,
    WAV). Enrol three or four samples per person for reliable recognition.
    `user` defaults to whoever the request is acting as (X-Siris-User)."""
    _require_encoder(siris)
    user_id = (user or "").strip() or get_current_user_id()
    encoder = siris.speaker_encoder()
    assert encoder is not None
    embedding = await asyncio.to_thread(encoder.embed, await file.read())
    if embedding is None:
        raise HTTPException(status_code=422, detail="Couldn't hear enough speech in that recording (send a few seconds of WAV audio).")
    return EnrollResponse(**siris.voiceprints.enroll(user_id, embedding))


@router.delete("/voiceprints/{user_id}")
def delete_voiceprint(user_id: str, siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    if not siris.voiceprints.delete(user_id):
        raise HTTPException(status_code=404, detail="No voiceprint for that user.")
    return {"user_id": user_id, "deleted": True}


@router.post("/identify")
async def identify_speaker(file: UploadFile = File(...), siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    """Who is speaking in a recording: the matched user (or null) and every
    enrolled person's similarity score -- for checking enrolment works."""
    _require_encoder(siris)
    match = await asyncio.to_thread(siris.identify_speaker, await file.read())
    if match is None:
        raise HTTPException(status_code=422, detail="Couldn't hear enough speech in that recording.")
    return match
