"""Entry point: wire up real hardware-backed implementations and run the
satellite loop.

    python -m siris_satellite
"""

import logging

from siris_satellite.audio_io import MicrophoneInput, SpeakerOutput
from siris_satellite.client import SirisClient
from siris_satellite.config import SatelliteSettings
from siris_satellite.inbound import InboundServer
from siris_satellite.satellite import Satellite

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")
    settings = SatelliteSettings()

    audio_input = MicrophoneInput(
        wake_word_model=settings.wake_word_model,
        wake_word_threshold=settings.wake_word_threshold,
        sample_rate=settings.sample_rate,
        silence_timeout_seconds=settings.silence_timeout_seconds,
        max_recording_seconds=settings.max_recording_seconds,
        speech_start_timeout_seconds=settings.speech_start_timeout_seconds,
        vad=settings.vad,
        vad_threshold=settings.vad_threshold,
        device=settings.input_device,
    )
    audio_output = SpeakerOutput(device=settings.output_device)
    siris = SirisClient(
        base_url=settings.siris_base_url, api_key=settings.siris_api_key, room=settings.room, user=settings.user
    )

    # Room/presence-aware routing (milestone 48) -- both room and
    # callback_url are opt-in (empty by default): without them, this
    # satellite never registers or opens the inbound listener, and
    # behaves exactly as it did before this existed (pull-only:
    # chat() -> speak() -> play locally).
    inbound: InboundServer | None = None
    if settings.room and settings.callback_url:
        inbound = InboundServer(audio_output, settings.inbound_port)
        inbound.start()
        try:
            siris.register(settings.room, settings.callback_url)
        except Exception:
            logger.exception("Registering with SirisAI failed -- continuing without room-push")

    try:
        Satellite(
            audio_input=audio_input,
            audio_output=audio_output,
            siris=siris,
            follow_up_seconds=settings.follow_up_seconds,
            barge_in=settings.barge_in,
        ).run_forever()
    finally:
        if inbound is not None:
            inbound.stop()
        siris.close()


if __name__ == "__main__":
    main()
