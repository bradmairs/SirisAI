"""The satellite's main loop: wake word -> converse -> back to waiting.

A conversation is a loop, not a single exchange:

- The reply streams back one sentence at a time (POST /siris/voice/converse)
  and each sentence plays as soon as it arrives, instead of after the whole
  answer is written.
- Barge-in: while a reply plays, the wake word is still being listened for.
  Saying it cuts the reply off and starts recording straight away.
- Follow-up: after a reply, the satellite keeps listening for a few seconds
  without needing the wake word ("...and the kitchen too"), longer when
  SirisAI asked a question ("should I go ahead?").

Depends only on the AudioInput/AudioOutput protocols (interfaces.py) and
SirisClient (client.py) -- no sounddevice or wake-word model -- so all of
this is testable with fakes (satellite/tests/test_satellite.py). Falls back
to the old transcribe -> chat -> speak calls against a SirisAI server that
predates /siris/voice/converse.
"""

import base64
import logging
import queue
import threading

from siris_satellite.client import ConverseUnsupported, SirisClient
from siris_satellite.interfaces import AudioInput, AudioOutput

logger = logging.getLogger(__name__)

DONE = "done"
EXPECTS_REPLY = "expects_reply"
INTERRUPTED = "interrupted"
SILENT = "silent"

_MIN_REPLY_WINDOW_SECONDS = 8.0


class PlaybackQueue:
    """Plays clips in order on a background thread, so the next sentence can
    be downloading while the current one plays. stop() drops everything
    queued and cuts off the clip that's playing."""

    def __init__(self, output: AudioOutput) -> None:
        self._output = output
        self._clips: "queue.Queue[bytes | None]" = queue.Queue()
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while True:
            clip = self._clips.get()
            if clip is None or self._stopped.is_set():
                return
            try:
                self._output.play(clip)
            except Exception:
                logger.exception("Playback failed")

    def enqueue(self, clip: bytes) -> None:
        if not self._stopped.is_set():
            self._clips.put(clip)

    def close(self) -> None:
        self._clips.put(None)

    def wait(self) -> None:
        self._thread.join()

    def stop(self) -> None:
        self._stopped.set()
        self._clips.put(None)
        try:
            self._output.stop()
        except Exception:
            logger.exception("Stopping playback failed")


class Satellite:
    def __init__(
        self,
        audio_input: AudioInput,
        audio_output: AudioOutput,
        siris: SirisClient,
        follow_up_seconds: float = 6.0,
        barge_in: bool = True,
    ) -> None:
        self._audio_input = audio_input
        self._audio_output = audio_output
        self._siris = siris
        self._follow_up_seconds = follow_up_seconds
        self._barge_in = barge_in
        self._streaming = True

    def run_once(self) -> None:
        """Wait for the wake word, then hold a conversation until it goes
        quiet. Split out from run_forever() so tests can drive it once."""
        self._audio_input.listen_for_wake_word()
        logger.info("Wake word detected, listening...")
        self.converse()

    def converse(self) -> None:
        start_timeout: float | None = None
        while True:
            audio = self._audio_input.record_utterance(start_timeout=start_timeout)
            if audio is None:
                return
            outcome = self._respond(audio)
            if outcome == INTERRUPTED:
                start_timeout = None
                continue
            if outcome == SILENT:
                return
            if outcome == EXPECTS_REPLY:
                start_timeout = max(self._follow_up_seconds, _MIN_REPLY_WINDOW_SECONDS)
            elif self._follow_up_seconds > 0:
                start_timeout = self._follow_up_seconds
            else:
                return
            logger.info("Listening for a follow-up (%.0fs)...", start_timeout)

    def _respond(self, audio: bytes) -> str:
        if not self._streaming:
            return self._respond_legacy(audio)

        player = PlaybackQueue(self._audio_output)
        reply_done = threading.Event()
        interrupted = threading.Event()
        watcher: threading.Thread | None = None
        if self._barge_in:

            def watch() -> None:
                try:
                    if self._audio_input.listen_for_wake_word(stop=reply_done):
                        logger.info("Interrupted by the wake word")
                        interrupted.set()
                        player.stop()
                except Exception:
                    logger.exception("Listening for an interruption failed")

            watcher = threading.Thread(target=watch, daemon=True)
            watcher.start()

        transcript: str | None = None
        final: dict | None = None
        events = self._siris.converse(audio)
        try:
            for event in events:
                if interrupted.is_set():
                    break
                kind = event.get("type")
                if kind == "transcript":
                    transcript = event.get("text") or ""
                    logger.info("Heard: %s", transcript)
                elif kind == "sentence" and event.get("audio"):
                    player.enqueue(base64.b64decode(event["audio"]))
                elif kind == "final":
                    final = event
                    logger.info("Replied: %s", event.get("response"))
                elif kind == "error":
                    logger.error("SirisAI error: %s", event.get("detail"))
            player.close()
            player.wait()
        except ConverseUnsupported:
            logger.info("This SirisAI server has no /siris/voice/converse; using transcribe -> chat -> speak")
            self._streaming = False
            player.stop()
            reply_done.set()
            if watcher is not None:
                watcher.join(timeout=2)
            return self._respond_legacy(audio)
        finally:
            events.close()
            reply_done.set()
            if watcher is not None:
                watcher.join(timeout=2)

        if interrupted.is_set():
            return INTERRUPTED
        if transcript is not None and not transcript.strip():
            return SILENT
        if final is not None and final.get("expects_reply"):
            return EXPECTS_REPLY
        return DONE

    def _respond_legacy(self, audio: bytes) -> str:
        text = self._siris.transcribe(audio)
        logger.info("Heard: %s", text)
        if not text.strip():
            return SILENT
        reply = self._siris.chat(text)
        logger.info("Replying: %s", reply)
        try:
            reply_audio = self._siris.speak(reply)
        except Exception:
            logger.exception("Text-to-speech failed; nothing to play back")
            return DONE
        self._audio_output.play(reply_audio)
        return EXPECTS_REPLY if reply.rstrip().endswith("?") else DONE

    def run_forever(self) -> None:
        """Run run_once() forever. A failure in one cycle (a network blip,
        a backend error) is logged and the satellite goes back to
        listening for the wake word, rather than crashing."""
        logger.info("Satellite ready, listening for the wake word...")
        while True:
            try:
                self.run_once()
            except Exception:
                logger.exception("Error during satellite cycle; continuing to listen")
