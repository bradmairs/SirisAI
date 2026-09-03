"""Real-socket tests for the inbound push listener (milestone 48) --
plain sockets/HTTP, so unlike audio_io.py this doesn't need real
hardware to exercise for real.
"""

import time

import httpx

from siris_satellite.inbound import InboundServer


class _FakeAudioOutput:
    def __init__(self, fails: bool = False, play_seconds: float = 0.0) -> None:
        self.played: list[bytes] = []
        self._fails = fails
        self._play_seconds = play_seconds

    def play(self, audio: bytes) -> None:
        if self._play_seconds:
            time.sleep(self._play_seconds)
        if self._fails:
            raise RuntimeError("playback boom")
        self.played.append(audio)


def test_play_endpoint_plays_the_posted_audio() -> None:
    audio_output = _FakeAudioOutput()
    server = InboundServer(audio_output, port=0)
    server.start()
    try:
        response = httpx.post(f"http://127.0.0.1:{server.port}/play", content=b"fake-mp3-bytes", timeout=5.0)
        assert response.status_code == 200
    finally:
        server.stop()

    assert audio_output.played == [b"fake-mp3-bytes"]


def test_unknown_path_returns_404() -> None:
    audio_output = _FakeAudioOutput()
    server = InboundServer(audio_output, port=0)
    server.start()
    try:
        response = httpx.post(f"http://127.0.0.1:{server.port}/nope", content=b"x", timeout=5.0)
        assert response.status_code == 404
    finally:
        server.stop()

    assert audio_output.played == []


def test_playback_failure_does_not_crash_the_server() -> None:
    """A failed playback is logged, not surfaced as an HTTP error -- the
    pusher already got its 200 by the time playback runs. Confirmed here
    by the server staying responsive for a second request afterward."""
    audio_output = _FakeAudioOutput(fails=True)
    server = InboundServer(audio_output, port=0)
    server.start()
    try:
        first = httpx.post(f"http://127.0.0.1:{server.port}/play", content=b"fake-mp3-bytes", timeout=5.0)
        assert first.status_code == 200
        second = httpx.post(f"http://127.0.0.1:{server.port}/play", content=b"more-bytes", timeout=5.0)
        assert second.status_code == 200
    finally:
        server.stop()


def test_response_returns_before_slow_playback_finishes() -> None:
    """The actual design property this milestone needs: the pusher's HTTP
    call shouldn't block for as long as playback takes."""
    audio_output = _FakeAudioOutput(play_seconds=1.0)
    server = InboundServer(audio_output, port=0)
    server.start()
    try:
        start = time.monotonic()
        response = httpx.post(f"http://127.0.0.1:{server.port}/play", content=b"fake-mp3-bytes", timeout=5.0)
        elapsed = time.monotonic() - start

        assert response.status_code == 200
        assert elapsed < 0.5  # well under the 1s playback -- the response didn't wait for it
    finally:
        server.stop()

    assert audio_output.played == [b"fake-mp3-bytes"]
