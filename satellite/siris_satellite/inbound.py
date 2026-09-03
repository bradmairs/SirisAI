"""A small inbound HTTP listener so SirisAI can push a reply to this
satellite (milestone 48) even when a different satellite heard the
request -- inverts the satellite's otherwise pull-only architecture
(client.py only ever calls out to SirisAI) for exactly this one case.
Plain stdlib http.server, not a full web framework: this only ever
needs to handle occasional single-shot "play this audio" pushes, not
real concurrency.

Deliberately its own module, imported only by __main__.py, same
boundary as audio_io.py -- keeps satellite.py's tested orchestration
logic free of anything socket/threading-related. Unlike audio_io.py
though, this module IS exercised by the test suite (satellite/tests/
test_inbound.py): it's plain sockets/HTTP, no real microphone/speaker
needed to test it for real.
"""

import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from siris_satellite.interfaces import AudioOutput

logger = logging.getLogger(__name__)


def _make_handler(audio_output: AudioOutput) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            if self.path != "/play":
                self.send_response(404)
                self.end_headers()
                return

            length = int(self.headers.get("Content-Length", 0))
            audio = self.rfile.read(length) if length else b""

            # Respond before playing -- the pusher (speak_in_current_room)
            # is waiting on this HTTP call, not on how long playback takes.
            # An explicit Content-Length: 0 matters here: without it,
            # http.server's default HTTP/1.0 behavior leaves the client
            # waiting for the connection to close to know the body is
            # done -- which wouldn't happen until this handler function
            # (including playback below) returns, silently defeating the
            # whole point of responding first.
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

            try:
                audio_output.play(audio)
            except Exception:
                logger.exception("Playback of a pushed reply failed")

    return Handler


class InboundServer:
    """Wraps HTTPServer + a background thread so __main__.py can start
    this alongside the (blocking) main satellite loop without the two
    interfering. Binding port=0 (used by tests) lets the OS pick a free
    port -- `.port` reports whichever one it actually got."""

    def __init__(self, audio_output: AudioOutput, port: int) -> None:
        self._server = HTTPServer(("0.0.0.0", port), _make_handler(audio_output))
        self.port = self._server.server_port
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
