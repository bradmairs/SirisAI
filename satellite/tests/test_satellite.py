import base64
import threading
import time

from siris_satellite.client import ConverseUnsupported
from siris_satellite.recorder import CONTINUE, DONE, TIMEOUT, UtteranceRecorder
from siris_satellite.satellite import PlaybackQueue, Satellite


class _FakeAudioInput:
    """Hands out scripted utterances; None means "nobody spoke"."""

    def __init__(self, utterances: list[bytes | None] | None = None, interrupt_after: float | None = None) -> None:
        self.utterances = list(utterances if utterances is not None else [b"fake-audio"])
        self.wake_word_calls = 0
        self.record_calls: list[float | None] = []
        self.interrupt_after = interrupt_after
        self.barge_in_listens = 0

    def listen_for_wake_word(self, stop: threading.Event | None = None) -> bool:
        if stop is None:
            self.wake_word_calls += 1
            return True
        self.barge_in_listens += 1
        if self.interrupt_after is not None:
            if stop.wait(self.interrupt_after):
                return False
            self.interrupt_after = None
            return True
        stop.wait()
        return False

    def record_utterance(self, start_timeout: float | None = None) -> bytes | None:
        self.record_calls.append(start_timeout)
        return self.utterances.pop(0) if self.utterances else None


class _FakeAudioOutput:
    def __init__(self, play_seconds: float = 0.0) -> None:
        self.played: list[bytes] = []
        self.stopped = 0
        self._play_seconds = play_seconds
        self._stop = threading.Event()

    def play(self, audio: bytes) -> None:
        self.played.append(audio)
        self._stop.wait(self._play_seconds)

    def stop(self) -> None:
        self.stopped += 1
        self._stop.set()


def _sentence(text: str) -> dict:
    return {"type": "sentence", "text": text, "audio": base64.b64encode(f"mp3:{text}".encode()).decode()}


class _FakeSirisClient:
    def __init__(self, turns: list[list[dict]] | None = None, unsupported: bool = False) -> None:
        self.turns = list(turns or [])
        self.conversed: list[bytes] = []
        self.unsupported = unsupported
        self.transcribed: list[bytes] = []
        self.chatted: list[str] = []
        self.spoken: list[str] = []
        self.closed_streams = 0

    def converse(self, audio: bytes):
        self.conversed.append(audio)
        if self.unsupported:
            raise ConverseUnsupported()
        events = self.turns.pop(0)
        try:
            for event in events:
                if event.get("type") == "_sleep":
                    time.sleep(event["seconds"])
                    continue
                yield event
        finally:
            self.closed_streams += 1

    def transcribe(self, audio: bytes) -> str:
        self.transcribed.append(audio)
        return "turn on the lights"

    def chat(self, prompt: str) -> str:
        self.chatted.append(prompt)
        return "done"

    def speak(self, text: str) -> bytes:
        self.spoken.append(text)
        return b"legacy-mp3"


def _turn(*sentences: str, expects_reply: bool = False, transcript: str = "hello") -> list[dict]:
    return [
        {"type": "transcript", "text": transcript},
        *[_sentence(s) for s in sentences],
        {"type": "final", "conversation_id": "c1", "response": " ".join(sentences), "expects_reply": expects_reply},
    ]


def test_converse_plays_each_streamed_sentence_in_order() -> None:
    audio_input = _FakeAudioInput([b"utt-1"])
    audio_output = _FakeAudioOutput()
    siris = _FakeSirisClient([_turn("It's sunny.", "High of 24.")])

    Satellite(audio_input, audio_output, siris, follow_up_seconds=0).run_once()

    assert audio_input.wake_word_calls == 1
    assert siris.conversed == [b"utt-1"]
    assert audio_output.played == [b"mp3:It's sunny.", b"mp3:High of 24."]
    assert siris.closed_streams == 1


def test_follow_up_window_listens_again_without_the_wake_word() -> None:
    audio_input = _FakeAudioInput([b"utt-1", b"utt-2", None])
    siris = _FakeSirisClient([_turn("Kitchen is on."), _turn("Hall too.")])

    Satellite(audio_input, _FakeAudioOutput(), siris, follow_up_seconds=6).run_once()

    assert audio_input.wake_word_calls == 1
    assert siris.conversed == [b"utt-1", b"utt-2"]
    # First recording uses the default wait; follow-ups use the window.
    assert audio_input.record_calls == [None, 6, 6]


def test_a_question_opens_a_reply_window_even_with_follow_up_off() -> None:
    audio_input = _FakeAudioInput([b"utt-1", b"yes", None])
    siris = _FakeSirisClient([_turn("Should I go ahead?", expects_reply=True), _turn("Done.")])

    Satellite(audio_input, _FakeAudioOutput(), siris, follow_up_seconds=0).run_once()

    assert siris.conversed == [b"utt-1", b"yes"]
    assert audio_input.record_calls == [None, 8.0]


def test_empty_transcript_ends_the_conversation() -> None:
    audio_input = _FakeAudioInput([b"noise", b"never-used"])
    siris = _FakeSirisClient([[{"type": "transcript", "text": "  "}, {"type": "final", "response": None}]])

    Satellite(audio_input, _FakeAudioOutput(), siris, follow_up_seconds=6).run_once()

    assert siris.conversed == [b"noise"]


def test_wake_word_during_a_reply_cuts_it_off_and_records_immediately() -> None:
    audio_input = _FakeAudioInput([b"long-question", b"stop", None], interrupt_after=0.05)
    audio_output = _FakeAudioOutput(play_seconds=5)
    long_turn = _turn("First sentence.", "Second sentence.", "Third sentence.")
    siris = _FakeSirisClient([long_turn, _turn("Okay.")])

    started = time.monotonic()
    Satellite(audio_input, audio_output, siris, follow_up_seconds=0).run_once()

    assert time.monotonic() - started < 3, "interrupt should not wait for playback to finish"
    assert audio_output.stopped >= 1
    assert b"mp3:Third sentence." not in audio_output.played
    assert siris.conversed == [b"long-question", b"stop"]
    # After an interruption the next recording starts with the default wait.
    assert audio_input.record_calls[:2] == [None, None]


def test_barge_in_can_be_disabled() -> None:
    audio_input = _FakeAudioInput([b"utt"])
    Satellite(audio_input, _FakeAudioOutput(), _FakeSirisClient([_turn("Hi.")]), follow_up_seconds=0, barge_in=False).run_once()
    assert audio_input.barge_in_listens == 0


def test_falls_back_to_transcribe_chat_speak_on_an_older_server() -> None:
    audio_input = _FakeAudioInput([b"recorded-audio"])
    audio_output = _FakeAudioOutput()
    siris = _FakeSirisClient(unsupported=True)

    satellite = Satellite(audio_input, audio_output, siris, follow_up_seconds=0)
    satellite.run_once()

    assert siris.transcribed == [b"recorded-audio"]
    assert siris.chatted == ["turn on the lights"]
    assert siris.spoken == ["done"]
    assert audio_output.played == [b"legacy-mp3"]
    assert satellite._streaming is False


def test_playback_queue_stop_drops_pending_clips() -> None:
    output = _FakeAudioOutput(play_seconds=5)
    player = PlaybackQueue(output)
    player.enqueue(b"one")
    player.enqueue(b"two")
    time.sleep(0.05)
    player.stop()
    player.wait()
    assert output.played == [b"one"]


class _StopTestLoop(BaseException):
    """Deliberately not an Exception subclass, so it isn't swallowed by
    run_forever()'s `except Exception` -- lets a test break the loop."""


def test_run_forever_continues_after_a_cycle_raises() -> None:
    calls = {"n": 0}

    class _FlakyAudioInput(_FakeAudioInput):
        def listen_for_wake_word(self, stop: threading.Event | None = None) -> bool:
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


# -- UtteranceRecorder


def _recorder(start_timeout: float = 0.5) -> UtteranceRecorder:
    return UtteranceRecorder(chunk_seconds=0.1, silence_timeout=0.3, max_seconds=2.0, start_timeout=start_timeout, pre_roll_seconds=0.2)


def test_recorder_times_out_when_nobody_speaks() -> None:
    recorder = _recorder()
    statuses = [recorder.feed(i, False) for i in range(5)]
    assert statuses[-1] == TIMEOUT
    assert recorder.speech_started is False


def test_recorder_keeps_pre_roll_and_stops_after_silence() -> None:
    recorder = _recorder()
    assert recorder.feed("q1", False) == CONTINUE
    assert recorder.feed("q2", False) == CONTINUE
    assert recorder.feed("s1", True) == CONTINUE
    assert recorder.feed("s2", True) == CONTINUE
    assert recorder.feed("p1", False) == CONTINUE
    assert recorder.feed("p2", False) == CONTINUE
    assert recorder.feed("p3", False) == DONE
    # Pre-roll (0.2s = 2 chunks) includes the chunk speech was detected in.
    assert recorder.frames == ["q2", "s1", "s2", "p1", "p2", "p3"]


def test_recorder_brief_pause_does_not_end_the_utterance() -> None:
    recorder = _recorder()
    recorder.feed("s", True)
    recorder.feed("p", False)
    recorder.feed("p", False)
    assert recorder.feed("s", True) == CONTINUE
    assert recorder.feed("p", False) == CONTINUE


def test_recorder_stops_at_max_length() -> None:
    recorder = _recorder()
    statuses = [recorder.feed("s", True) for _ in range(25)]
    assert DONE in statuses
