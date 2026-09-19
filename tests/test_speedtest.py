from typing import Any

from app.plugins.base import ToolPermission
from app.plugins.builtin.speedtest import RunSpeedTestTool
from app.speedtest.client import SpeedTestClient


class _FakeResults:
    ping: float | None

    def __init__(self) -> None:
        self.ping = 12.345
        self.server = {"sponsor": "Test ISP", "name": "Testville", "country": "Testland"}
        self.download = 100_000_000.0  # 100 Mbps in bits/sec
        self.upload = 20_000_000.0  # 20 Mbps in bits/sec


class _FakeSpeedtest:
    def __init__(self, calls: list[str], **kwargs: Any) -> None:
        self._calls = calls
        self.results = _FakeResults()
        self.servers: dict[float, list[dict[str, str]]] = {
            10.0: [{"country": "Testland", "name": "Testville", "sponsor": "Test ISP"}],
            20.0: [{"country": "Otherland", "name": "Otherville", "sponsor": "Other ISP"}],
        }

    def get_servers(self) -> None:
        self._calls.append("get_servers")

    def get_best_server(self, servers: list[dict[str, str]] | None = None) -> None:
        self._calls.append(f"get_best_server({len(servers) if servers else 'auto'})")

    def download(self) -> None:
        self._calls.append("download")

    def upload(self) -> None:
        self._calls.append("upload")


def _fake_factory(calls: list[str]) -> Any:
    return lambda **kwargs: _FakeSpeedtest(calls, **kwargs)


def test_run_both_directions_calls_download_and_upload() -> None:
    calls: list[str] = []
    client = SpeedTestClient(speedtest_factory=_fake_factory(calls))

    result = client.run(direction="both")

    assert calls == ["get_best_server(auto)", "download", "upload"]
    assert result == {
        "ping_ms": 12.35,
        "server": "Test ISP, Testville, Testland",
        "download_mbps": 100.0,
        "upload_mbps": 20.0,
    }


def test_run_download_only_skips_upload() -> None:
    calls: list[str] = []
    client = SpeedTestClient(speedtest_factory=_fake_factory(calls))

    result = client.run(direction="download")

    assert calls == ["get_best_server(auto)", "download"]
    assert "download_mbps" in result
    assert "upload_mbps" not in result


def test_run_upload_only_skips_download() -> None:
    calls: list[str] = []
    client = SpeedTestClient(speedtest_factory=_fake_factory(calls))

    result = client.run(direction="upload")

    assert calls == ["get_best_server(auto)", "upload"]
    assert "upload_mbps" in result
    assert "download_mbps" not in result


def test_tool_defaults_to_both_directions() -> None:
    calls: list[str] = []
    client = SpeedTestClient(speedtest_factory=_fake_factory(calls))
    tool = RunSpeedTestTool(client)

    result = tool.run()

    assert "download_mbps" in result
    assert "upload_mbps" in result


def test_run_with_no_location_skips_server_lookup() -> None:
    calls: list[str] = []
    client = SpeedTestClient(speedtest_factory=_fake_factory(calls))

    result = client.run(direction="download")

    assert "get_servers" not in calls
    assert "location_requested" not in result
    assert "location_matched" not in result


def test_run_with_matching_location_filters_servers() -> None:
    calls: list[str] = []
    client = SpeedTestClient(speedtest_factory=_fake_factory(calls))

    result = client.run(direction="download", location="Testland")

    assert calls[:2] == ["get_servers", "get_best_server(1)"]
    assert result["location_requested"] == "Testland"
    assert result["location_matched"] is True
    assert "note" not in result


def test_run_with_unmatched_location_falls_back_and_says_so() -> None:
    """The bug this fixes: silently substituting a server nowhere near
    what was asked for, with no way to tell the fallback happened."""
    calls: list[str] = []
    client = SpeedTestClient(speedtest_factory=_fake_factory(calls))

    result = client.run(direction="download", location="Nonexistentland")

    assert calls[:2] == ["get_servers", "get_best_server(auto)"]
    assert result["location_requested"] == "Nonexistentland"
    assert result["location_matched"] is False
    assert "Nonexistentland" in result["note"]


def test_tool_passes_location_through() -> None:
    calls: list[str] = []
    client = SpeedTestClient(speedtest_factory=_fake_factory(calls))
    tool = RunSpeedTestTool(client)

    result = tool.run(direction="download", location="Testland")

    assert result["location_matched"] is True


def test_tool_is_safe_action_not_read() -> None:
    """Unlike every other READ tool here, this one has a real-world side
    effect (bandwidth consumption) even without touching persistent
    state -- SAFE_ACTION reflects that, not READ."""
    assert RunSpeedTestTool(SpeedTestClient()).permission == ToolPermission.SAFE_ACTION


def test_unusable_ping_sentinel_is_reported_as_none_with_a_note() -> None:
    """speedtest-cli's ~1,800,000 ms "every latency probe failed" value must
    never be shown to a user as if it were a real ping."""

    class _AllProbesFailed(_FakeSpeedtest):
        def __init__(self, calls: list[str], **kwargs: Any) -> None:
            super().__init__(calls, **kwargs)
            self.results.ping = 1_800_000.0

    calls: list[str] = []
    client = SpeedTestClient(speedtest_factory=lambda **kw: _AllProbesFailed(calls, **kw))

    result = client.run(direction="download")

    assert result["ping_ms"] is None
    assert "ping_note" in result
    assert result["download_mbps"] == 100.0


def test_a_real_ping_has_no_ping_note() -> None:
    calls: list[str] = []
    result = SpeedTestClient(speedtest_factory=_fake_factory(calls)).run(direction="download")

    assert result["ping_ms"] == 12.35
    assert "ping_note" not in result
