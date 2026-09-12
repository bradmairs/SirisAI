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

import speedtest


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

        result: dict = {
            "ping_ms": round(test.results.ping, 2),
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
        if direction in ("download", "both"):
            test.download()
            result["download_mbps"] = round(test.results.download / 1_000_000, 2)
        if direction in ("upload", "both"):
            test.upload()
            result["upload_mbps"] = round(test.results.upload / 1_000_000, 2)
        return result
