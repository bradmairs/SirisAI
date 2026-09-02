# SirisAI Voice Satellite

A wake-word voice satellite for SirisAI: listens for a wake word, records
your utterance, sends it to a running SirisAI server for
transcribe -> chat -> speak, and plays the reply back. Meant to run on a
small always-on device near you (a Raspberry Pi with a USB mic/speaker is
the reference target) that talks to your main SirisAI instance over HTTP.

This is a **separate deployable** from the main FastAPI app, not part of
the Docker image in the repo root. It has its own dependencies
(`sounddevice`, `openwakeword`) that don't belong in a headless server
container, and it's meant to be installed directly on the device with the
microphone attached.

## Why a separate directory

The SirisAI server already exposes the raw primitives a voice client needs
(`POST /siris/voice/transcribe`, `POST /siris/chat`, `POST /siris/voice/speak`
-- the same three calls the built-in web UI makes). The satellite is just
another frontend against those endpoints: nothing on the server side changes
to support it. Keeping it out of `app/` keeps the server's dependency list
free of audio/hardware libraries it will never use.

## Architecture

- `siris_satellite/interfaces.py` -- `AudioInput`/`AudioOutput` `Protocol`
  classes. This is the boundary that makes the orchestration logic testable
  without real hardware.
- `siris_satellite/satellite.py` -- `Satellite`, the orchestration loop
  (wake word -> record -> transcribe -> chat -> speak -> play). Depends only
  on the two protocols above and a `SirisClient`, so it's fully covered by
  `tests/test_satellite.py` using fakes -- no microphone required.
- `siris_satellite/client.py` -- `SirisClient`, a thin `httpx` wrapper
  around the three SirisAI endpoints. Threads `conversation_id` across
  calls so multi-turn context and tool-call/confirmation-required responses
  come back as spoken-friendly text. Covered by `tests/test_client.py` via
  `httpx.MockTransport`.
- `siris_satellite/audio_io.py` -- the real hardware implementation:
  `MicrophoneInput` (sounddevice capture + openWakeWord wake-word detection,
  crude amplitude-based silence detection to know when you've stopped
  talking) and `SpeakerOutput` (shells out to `mpg123` to play the MP3 the
  server returns). **Not covered by the test suite** -- there's no real
  microphone/speaker in CI or this dev environment to test it against. This
  is the one piece you should sanity-check by ear once it's running on
  actual hardware.
- `siris_satellite/__main__.py` -- wires the real implementations above
  together and runs `Satellite.run_forever()`. Entry point: `python -m
  siris_satellite`.
- `siris_satellite/config.py` -- `SatelliteSettings`, env-driven
  (`SIRISAI_SATELLITE_` prefix), same pattern as the main app's
  `app/core/config.py`.

## Setup (Raspberry Pi or any Linux box with a mic/speaker)

System dependencies (PortAudio for `sounddevice`, `mpg123` for playback):

```bash
sudo apt update
sudo apt install -y portaudio19-dev mpg123
```

Python dependencies:

```bash
cd satellite
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Configuration -- create `satellite/.env`:

```bash
SIRISAI_SATELLITE_SIRIS_BASE_URL=http://your-sirisai-host:8000
SIRISAI_SATELLITE_SIRIS_API_KEY=your-sirisai-api-key

# Optional tuning (defaults shown):
SIRISAI_SATELLITE_WAKE_WORD_MODEL=hey_jarvis
SIRISAI_SATELLITE_WAKE_WORD_THRESHOLD=0.5
SIRISAI_SATELLITE_SAMPLE_RATE=16000
SIRISAI_SATELLITE_SILENCE_TIMEOUT_SECONDS=1.5
SIRISAI_SATELLITE_MAX_RECORDING_SECONDS=15.0
# SIRISAI_SATELLITE_INPUT_DEVICE=1
# SIRISAI_SATELLITE_OUTPUT_DEVICE=0
```

`wake_word_model` accepts any of openWakeWord's bundled models (e.g.
`hey_jarvis`, `alexa`) or a path to a custom `.onnx`/`.tflite` model --
see the [openWakeWord docs](https://github.com/dscripka/openWakeWord) for
training your own wake word.

If you need to pick a specific input/output device on a Pi with more than
one audio interface, run `python -m sounddevice` (from inside the venv) to
list device indices, then set `SIRISAI_SATELLITE_INPUT_DEVICE` /
`SIRISAI_SATELLITE_OUTPUT_DEVICE`.

Run it:

```bash
python -m siris_satellite
```

It will log each cycle (wake word heard, transcript, reply) and keep
running until stopped; a single bad cycle (mic error, network blip) is
caught and logged without killing the process, so it should self-recover.

## Running the test suite

The tests only exercise the orchestration logic (`satellite.py`,
`client.py`) via fakes and `httpx.MockTransport` -- they don't need
PortAudio, a real mic, or openWakeWord, so they run anywhere:

```bash
cd satellite
python3 -m venv .venv-test
source .venv-test/bin/activate
pip install -r requirements-test.txt
PYTHONPATH=. pytest tests/ -q
```

This is intentionally a separate virtualenv/requirements file from
`requirements.txt` -- see the comment at the top of
`requirements-test.txt`.
