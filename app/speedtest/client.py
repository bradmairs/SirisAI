"""Thin wrapper around the `speedtest-cli` PyPI package (unofficial but
well-established Speedtest.net client, MIT-licensed) for real internet
speed measurement -- SirisAI's own suggestion via report_capability_gap
after a user asked what the download speed was with no tool available
for it.

Optional "location" parameter to filter test servers by geographic
location (country, city, or ISP name) before picking the fastest by
latency. Worth flagging why this is honest rather than a solid guarantee:
speedtest.net's own server-list endpoint (what get_servers() calls) has
been observed live to return a small, non-deterministic, geographically
narrow candidate set -- not a full global list -- so an arbitrary
location like "Melbourne" can easily match nothing even though a real
Melbourne server exists on speedtest.net. run() reports that plainly via
location_matched rather than silently substituting a server nowhere near
what was asked for.

Note the module name collision: the PyPI package is `speedtest-cli` but
importable as plain `speedtest` -- this file is `app/speedtest/client.py`
(a different name, `app.speedtest.client`), not `app/speedtest.py`, so it
doesn't shadow the real import inside this package.
"""

from typing import Any, Callable

import socket
import time

import speedtest

# speedtest-cli reports a server's latency as ~1,800,000 ms when every
# latency probe to it failed (found live, 2026-09-19: a location-filtered
# pick returned exactly that as "ping_ms" while download still measured a
# real 297 Mbps) -- the server was usable for throughput, just not for a
# ping reading. Anything at or above a minute is that sentinel, never a
# real round trip.
_UNUSABLE_PING_MS = 60_000


def _tcp_ping_ms(host_port: str | None, attempts: int = 3) -> float | None:
    """Average TCP connect time to the test server, used when speedtest-cli's
    own latency probe failed (it does intermittently: 3 of 4 runs on the
    home server) so the result still carries a real reading."""
    if not host_port:
        return None
    host, _, port = host_port.partition(":")
    samples = []
    for _ in range(attempts):
        started = time.perf_counter()
        try:
            with socket.create_connection((host, int(port) if port else 80), timeout=3):
                pass
        except (OSError, ValueError):
            continue
        samples.append((time.perf_counter() - started) * 1000)
    return round(sum(samples) / len(samples), 2) if samples else None


class SpeedTestClient:
    def __init__(self, speedtest_factory: Callable[..., Any] = speedtest.Speedtest) -> None:
        # Injectable, same reasoning as every other client here taking an
        # optional `transport` -- speedtest.Speedtest does real network
        # I/O with no injection point of its own, so tests fake this
        # factory instead of hitting the real internet on every run.
        self._speedtest_factory = speedtest_factory

    def run(self, direction: str = "both", location: str | None = None) -> dict:
        test = self._speedtest_factory(secure=True)
        location_matched: bool | None = None
        if location:
            test.get_servers()
            needle = location.lower()
            matches = [
                server
                for servers_at_distance in test.servers.values()
                for server in servers_at_distance
                if needle in server.get("country", "").lower()
                or needle in server.get("name", "").lower()
                or needle in server.get("sponsor", "").lower()
            ]
            location_matched = bool(matches)
            test.get_best_server(servers=matches if matches else None)
        else:
            test.get_best_server()

        ping = test.results.ping
        ping_usable = ping is not None and ping < _UNUSABLE_PING_MS
        estimated_ping = None if ping_usable else _tcp_ping_ms(test.results.server.get("host"))
        result: dict = {
            "ping_ms": round(ping, 2) if ping_usable else estimated_ping,
            "server": (
                f"{test.results.server['sponsor']}, {test.results.server['name']}, "
                f"{test.results.server['country']}"
            ),
        }
        if location:
            result["location_requested"] = location
            result["location_matched"] = location_matched
            if not location_matched:
                result["note"] = (
                    f"No test server matched '{location}' among the servers "
                    "speedtest.net returned for this candidate pool (which can be a "
                    "small, non-deterministic regional subset, not the full global "
                    "list) -- used automatic nearest-server selection instead."
                )
        if not ping_usable and estimated_ping is not None:
            result["ping_note"] = (
                "The speed test's own latency probe failed, so this ping is the TCP connect time to "
                "the test server instead."
            )
        elif not ping_usable:
            result["ping_note"] = (
                "Latency probes to this test server all failed, so no ping reading is "
                "available -- download/upload were still measured."
            )
        if direction in ("download", "both"):
            test.download()
            result["download_mbps"] = round(test.results.download / 1_000_000, 2)
        if direction in ("upload", "both"):
            test.upload()
            result["upload_mbps"] = round(test.results.upload / 1_000_000, 2)
        return result
