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
SIRISAI_SATELLITE_SILENCE_TIMEOUT_SECONDS=1.2
SIRISAI_SATELLITE_MAX_RECORDING_SECONDS=15.0
SIRISAI_SATELLITE_SPEECH_START_TIMEOUT_SECONDS=5.0
# Speech detection: "silero" (bundled with openWakeWord; ignores fans/TV hum)
# or "amplitude" (loudness only). Falls back to amplitude if the Silero model
# isn't downloaded -- see "Streaming conversation" below.
SIRISAI_SATELLITE_VAD=silero
SIRISAI_SATELLITE_VAD_THRESHOLD=0.5
# Keep listening this long after a reply for a follow-up with no wake word
# (0 = only when SirisAI asked a question).
SIRISAI_SATELLITE_FOLLOW_UP_SECONDS=6.0
# Say the wake word while a reply is playing to cut it off and talk.
SIRISAI_SATELLITE_BARGE_IN=true
# Optional fixed identity for this satellite (X-Siris-User). Leave unset to
# let SirisAI recognise who's speaking by voice (enrolled voiceprints).
# SIRISAI_SATELLITE_USER=brad
# SIRISAI_SATELLITE_INPUT_DEVICE=1
# SIRISAI_SATELLITE_OUTPUT_DEVICE=0

# Room/presence-aware routing (milestone 48) -- both empty by default,
# meaning this satellite stays pull-only (asks, gets its own reply,
# plays it locally) exactly as before this existed. Set both to let
# SirisAI push a reply here even when a *different* satellite heard the
# request (see the main README's milestone 48 entry): ROOM registers
# this satellite and is sent on every chat turn as "where the user just
# spoke from"; CALLBACK_URL must be reachable FROM the SirisAI host
# (behind your router/VPN as needed -- same "you configure it
# explicitly" reasoning as the main app's SIRISAI_PUBLIC_BASE_URL).
# INBOUND_PORT is which port this satellite listens on for pushes; only
# needs to differ from CALLBACK_URL's port if you're port-forwarding.
# SIRISAI_SATELLITE_ROOM=kitchen
# SIRISAI_SATELLITE_CALLBACK_URL=http://192.168.1.50:8090
SIRISAI_SATELLITE_INBOUND_PORT=8090
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

## Streaming conversation

The satellite talks to `POST /siris/voice/converse`, which streams the reply
back a sentence at a time (each with its audio), so the first sentence plays
while SirisAI is still writing the rest. Around that:

- **Barge-in** -- the wake word is still listened for while a reply plays;
  saying it stops playback immediately (mpg123 is killed) and starts
  recording what you say next.
- **Follow-up** -- after a reply, the satellite listens for
  `FOLLOW_UP_SECONDS` without the wake word. When SirisAI asked something
  ("Should I go ahead?") it listens for at least 8 seconds even if follow-up
  is off.
- **Spoken confirmation** -- an action that needs your OK is asked out loud;
  "yes"/"go ahead" runs it, "no"/"cancel" drops it. Locks, covers (garage
  doors) and alarms are never confirmable by voice -- you're told to confirm
  those in the app (server setting `SIRISAI_VOICE_CONFIRMATION_BLOCKED_DOMAINS`).
- **Speech detection** -- recording starts when speech is detected (keeping a
  0.3s pre-roll so the first syllable isn't clipped) and stops after
  `SILENCE_TIMEOUT_SECONDS` of non-speech. Silero VAD's model is fetched by
  `python -c "import openwakeword.utils; openwakeword.utils.download_models()"`,
  the same command that fetches the wake-word models.

No acoustic echo cancellation is attempted: a satellite's own speaker is
rarely loud enough at the mic to trigger the wake word, but if yours does,
set `BARGE_IN=false` or raise `WAKE_WORD_THRESHOLD`.

Talking to a SirisAI server older than `/siris/voice/converse`, the satellite
notices the 404 and falls back to transcribe -> chat -> speak.

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
