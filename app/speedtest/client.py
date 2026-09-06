"""Thin wrapper around the `speedtest-cli` PyPI package (unofficial but
well-established Speedtest.net client, MIT-licensed) for real internet
speed measurement -- SirisAI's own suggestion via report_capability_gap
after a user asked what the download speed was with no tool available
for it.

Deliberately no "location" parameter, despite the model's own suggested
tool spec for this capability including one: the library auto-selects
the nearest/fastest test server by round-trip latency, matching how a
person would run a speed test anyway, and there's no simple "give me a
city name" override in the underlying library worth building against an
LLM's unverified guess at what that parameter should even mean.

Note the module name collision: the PyPI package is `speedtest-cli` but
importable as plain `speedtest` -- this file is `app/speedtest/client.py`
(a different name, `app.speedtest.client`), not `app/speedtest.py`, so it
doesn't shadow the real import inside this package.
"""

from typing import Any, Callable

import speedtest


class SpeedTestClient:
    def __init__(self, speedtest_factory: Callable[..., Any] = speedtest.Speedtest) -> None:
        # Injectable, same reasoning as every other client here taking an
        # optional `transport` -- speedtest.Speedtest does real network
        # I/O with no injection point of its own, so tests fake this
        # factory instead of hitting the real internet on every run.
        self._speedtest_factory = speedtest_factory

    def run(self, direction: str = "both") -> dict:
        test = self._speedtest_factory(secure=True)
        test.get_best_server()

        result: dict = {
            "ping_ms": round(test.results.ping, 2),
            "server": (
                f"{test.results.server['sponsor']}, {test.results.server['name']}, "
                f"{test.results.server['country']}"
            ),
        }
        if direction in ("download", "both"):
            test.download()
            result["download_mbps"] = round(test.results.download / 1_000_000, 2)
        if direction in ("upload", "both"):
            test.upload()
            result["upload_mbps"] = round(test.results.upload / 1_000_000, 2)
        return result
