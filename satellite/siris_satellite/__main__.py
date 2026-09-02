"""Entry point: wire up real hardware-backed implementations and run the
satellite loop.

    python -m siris_satellite
"""

import logging

from siris_satellite.audio_io import MicrophoneInput, SpeakerOutput
from siris_satellite.client import SirisClient
from siris_satellite.config import SatelliteSettings
from siris_satellite.satellite import Satellite


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")
    settings = SatelliteSettings()

    audio_input = MicrophoneInput(
        wake_word_model=settings.wake_word_model,
        wake_word_threshold=settings.wake_word_threshold,
        sample_rate=settings.sample_rate,
        silence_timeout_seconds=settings.silence_timeout_seconds,
        max_recording_seconds=settings.max_recording_seconds,
        device=settings.input_device,
    )
    audio_output = SpeakerOutput(device=settings.output_device)
    siris = SirisClient(base_url=settings.siris_base_url, api_key=settings.siris_api_key)

    try:
        Satellite(audio_input=audio_input, audio_output=audio_output, siris=siris).run_forever()
    finally:
        siris.close()


if __name__ == "__main__":
    main()
