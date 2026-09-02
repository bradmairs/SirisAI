from siris_satellite.satellite import Satellite


class _FakeAudioInput:
    def __init__(self, utterance: bytes = b"fake-audio") -> None:
        self.wake_word_calls = 0
        self.utterance = utterance
        self.record_calls = 0

    def listen_for_wake_word(self) -> None:
        self.wake_word_calls += 1

    def record_utterance(self) -> bytes:
        self.record_calls += 1
        return self.utterance


class _FakeAudioOutput:
    def __init__(self) -> None:
        self.played: list[bytes] = []

    def play(self, audio: bytes) -> None:
        self.played.append(audio)


class _FakeSirisClient:
    def __init__(self, transcript: str = "turn on the lights", reply: str = "done", speak_fails: bool = False) -> None:
        self.transcript = transcript
        self.reply = reply
        self.speak_fails = speak_fails
        self.transcribed_audio: bytes | None = None
        self.chatted_with: str | None = None
        self.spoken_text: str | None = None

    def transcribe(self, audio: bytes) -> str:
        self.transcribed_audio = audio
        return self.transcript

    def chat(self, prompt: str) -> str:
        self.chatted_with = prompt
        return self.reply

    def speak(self, text: str) -> bytes:
        self.spoken_text = text
        if self.speak_fails:
            raise RuntimeError("boom")
        return b"fake-mp3-bytes"


def test_run_once_full_cycle() -> None:
    audio_input = _FakeAudioInput(utterance=b"recorded-audio")
    audio_output = _FakeAudioOutput()
    siris = _FakeSirisClient(transcript="what's the weather", reply="it's sunny")

    Satellite(audio_input, audio_output, siris).run_once()

    assert audio_input.wake_word_calls == 1
    assert audio_input.record_calls == 1
    assert siris.transcribed_audio == b"recorded-audio"
    assert siris.chatted_with == "what's the weather"
    assert siris.spoken_text == "it's sunny"
    assert audio_output.played == [b"fake-mp3-bytes"]


def test_run_once_skips_chat_when_transcript_is_empty() -> None:
    audio_input = _FakeAudioInput()
    audio_output = _FakeAudioOutput()
    siris = _FakeSirisClient(transcript="   ")

    Satellite(audio_input, audio_output, siris).run_once()

    assert siris.chatted_with is None
    assert audio_output.played == []


def test_run_once_does_not_play_anything_if_speak_fails() -> None:
    audio_input = _FakeAudioInput()
    audio_output = _FakeAudioOutput()
    siris = _FakeSirisClient(speak_fails=True)

    Satellite(audio_input, audio_output, siris).run_once()

    assert siris.spoken_text is not None
    assert audio_output.played == []


class _StopTestLoop(BaseException):
    """Deliberately not an Exception subclass, so it isn't swallowed by
    run_forever()'s `except Exception` -- lets a test break the loop."""


def test_run_forever_continues_after_a_cycle_raises() -> None:
    calls = {"n": 0}

    class _FlakyAudioInput(_FakeAudioInput):
        def listen_for_wake_word(self) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("mic glitch")
            raise _StopTestLoop

    satellite = Satellite(_FlakyAudioInput(), _FakeAudioOutput(), _FakeSirisClient())

    try:
        satellite.run_forever()
    except _StopTestLoop:
        pass

    assert calls["n"] == 2
