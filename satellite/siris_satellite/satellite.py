"""The satellite's main loop: wake word -> record -> transcribe -> chat ->
speak -> repeat. Depends only on the AudioInput/AudioOutput protocols
(interfaces.py) and SirisClient (client.py) -- no direct dependency on
sounddevice, openWakeWord, or any hardware, which is what makes this
logic testable without a real microphone or speaker (see
satellite/tests/test_satellite.py).
"""

import logging

from siris_satellite.client import SirisClient
from siris_satellite.interfaces import AudioInput, AudioOutput

logger = logging.getLogger(__name__)


class Satellite:
    def __init__(self, audio_input: AudioInput, audio_output: AudioOutput, siris: SirisClient) -> None:
        self._audio_input = audio_input
        self._audio_output = audio_output
        self._siris = siris

    def run_once(self) -> None:
        """One full wake -> respond cycle. Split out from run_forever() so
        it can be driven deterministically, once, in tests."""
        self._audio_input.listen_for_wake_word()
        logger.info("Wake word detected, listening...")

        audio = self._audio_input.record_utterance()
        text = self._siris.transcribe(audio)
        logger.info("Heard: %s", text)
        if not text.strip():
            return

        reply = self._siris.chat(text)
        logger.info("Replying: %s", reply)

        try:
            reply_audio = self._siris.speak(reply)
        except Exception:
            logger.exception("Text-to-speech failed; nothing to play back")
            return

        self._audio_output.play(reply_audio)

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
