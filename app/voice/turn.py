"""One spoken turn, streamed: run a chat turn through Cortex and turn its
answer into speech one sentence at a time while the model is still writing
the rest (POST /siris/voice/converse).

What gets spoken has to end up matching the answer the conversation
records, so text is tracked per segment (reset whenever a tool starts --
anything said before a tool call is a preamble like "let me check"): once
the final answer arrives, only the part of it not already spoken is added.
That covers a trailing correction Cortex appends after streaming finished
(_finalize), and a backend that doesn't stream at all.
"""

import asyncio
import base64
import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from app.cortex.orchestrator import Cortex, CortexResult
from app.voice.base import NullTextToSpeechClient
from app.voice.confirmations import (
    PendingVoiceConfirmation,
    classify_reply,
    describe_call,
    pending_voice_confirmations,
    voice_confirmable,
)
from app.voice.pronunciation import for_speech
from app.voice.sentences import SentenceChunker, speakable, strip_spoken_prefix

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)

# Held until done so a satellite hanging up mid-reply doesn't cancel a turn
# halfway through running a tool or persisting the conversation.
_running: set["asyncio.Task[None]"] = set()


class VoiceTurn:
    def __init__(self, siris: "SirisAI", conversation_id: str, transcript: str, *, synthesize: bool = True) -> None:
        self.siris = siris
        self.conversation_id = conversation_id
        self.transcript = transcript
        self.synthesize = synthesize and siris.tts is not None and not isinstance(siris.tts, NullTextToSpeechClient)
        self._chunker = SentenceChunker()

    async def _sentence_event(self, sentence: str) -> dict[str, Any] | None:
        text = speakable(sentence)
        if not text:
            return None
        audio = None
        if self.synthesize:
            assert self.siris.tts is not None
            try:
                audio = base64.b64encode(await self.siris.tts.synthesize(for_speech(text))).decode("ascii")
            except Exception:
                logger.exception("TTS failed for a voice sentence")
        return {"type": "sentence", "text": text, "audio": audio}

    async def _speak_all(self, sentences: list[str]) -> AsyncIterator[dict[str, Any]]:
        for sentence in sentences:
            event = await self._sentence_event(sentence)
            if event is not None:
                yield event

    async def run(self) -> AsyncIterator[dict[str, Any]]:
        siris = self.siris
        pending = pending_voice_confirmations.pop(self.conversation_id)
        decision = classify_reply(self.transcript) if pending else None

        if pending is not None and decision == "no":
            siris.conversation.add_message(self.conversation_id, role="user", content=self.transcript)
            reply = "Okay, I won't do that."
            siris.conversation.add_message(self.conversation_id, role="assistant", content=reply)
            async for event in self._speak_all([reply]):
                yield event
            yield {"type": "final", "conversation_id": self.conversation_id, "response": reply, "expects_reply": False}
            return

        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        async def on_event(event: dict[str, Any]) -> None:
            await queue.put(event)

        cortex = Cortex(on_event=on_event)
        if pending is not None and decision == "yes":
            siris.conversation.add_message(self.conversation_id, role="user", content=self.transcript)
            work = cortex.confirm(
                siris, self.conversation_id, pending.tool_name, pending.arguments, use_planner=pending.used_planner
            )
        else:
            work = cortex.process(
                siris, self.conversation_id, self.transcript, use_planner=siris.settings.default_use_planner
            )

        async def runner() -> None:
            try:
                await work
            except Exception:
                logger.exception("Voice turn failed")
                await queue.put({"type": "error", "detail": "Voice turn failed."})
            finally:
                await queue.put(None)

        task = asyncio.create_task(runner())
        _running.add(task)
        task.add_done_callback(_running.discard)

        while True:
            event = await queue.get()
            if event is None:
                return
            kind = event.get("type")
            if kind == "content":
                async for spoken in self._speak_all(self._chunker.feed(event.get("delta", ""))):
                    yield spoken
            elif kind == "tool_start":
                async for spoken in self._speak_all(self._chunker.flush()):
                    yield spoken
                self._chunker = SentenceChunker()
                yield {"type": "tool_start", "name": event.get("name")}
            elif kind == "tool_end":
                yield {"type": "tool_end", "name": event.get("name"), "status": event.get("status")}
            elif kind == "error":
                async for spoken in self._speak_all(["Sorry, something went wrong."]):
                    yield spoken
                yield {"type": "error", "detail": event.get("detail")}
                return
            elif kind == "final":
                async for spoken in self._finish(event["result"]):
                    yield spoken
                return

    async def _finish(self, result: CortexResult) -> AsyncIterator[dict[str, Any]]:
        already_spoken = self._chunker.consumed
        self._chunker = SentenceChunker()
        expects_reply = False
        confirmation = None
        if result.error:
            text = "Sorry, I couldn't do that." if not result.permission_denied else "I'm not allowed to do that."
            async for event in self._speak_all([text]):
                yield event
            yield {"type": "final", "conversation_id": self.conversation_id, "response": None, "error": result.error, "expects_reply": False}
            return

        response = result.response or ""
        if result.pending_confirmation is not None:
            name, arguments = result.pending_confirmation
            confirmation = {"name": name, "arguments": arguments}
            blocked = self.siris.settings.voice_confirmation_blocked_domain_set()
            description = describe_call(name, arguments)
            if self.siris.settings.voice_confirmation_enabled and voice_confirmable(name, arguments, blocked):
                pending_voice_confirmations.put(
                    self.conversation_id,
                    PendingVoiceConfirmation(tool_name=name, arguments=arguments, used_planner=result.used_planner),
                )
                response = f"I need your OK to {description}. Should I go ahead?"
                expects_reply = True
            else:
                response = f"To {description}, please confirm it in the SirisAI app."
            self.siris.conversation.add_message(self.conversation_id, role="assistant", content=response)
            sentences = self._chunker.feed(response) + self._chunker.flush()
        else:
            remainder = strip_spoken_prefix(already_spoken, response)
            sentences = self._chunker.feed(remainder) + self._chunker.flush()
            expects_reply = response.rstrip().endswith("?")

        async for event in self._speak_all(sentences):
            yield event
        yield {
            "type": "final",
            "conversation_id": self.conversation_id,
            "response": response,
            "confirmation_required": confirmation,
            "expects_reply": expects_reply,
        }
