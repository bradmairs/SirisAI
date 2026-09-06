from typing import Any

from app.plugins.base import ToolPermission
from app.plugins.builtin.speedtest import RunSpeedTestTool
from app.speedtest.client import SpeedTestClient


class _FakeResults:
    def __init__(self) -> None:
        self.ping = 12.345
        self.server = {"sponsor": "Test ISP", "name": "Testville", "country": "Testland"}
        self.download = 100_000_000.0  # 100 Mbps in bits/sec
        self.upload = 20_000_000.0  # 20 Mbps in bits/sec


class _FakeSpeedtest:
    def __init__(self, calls: list[str], **kwargs: Any) -> None:
        self._calls = calls
        self.results = _FakeResults()

    def get_best_server(self) -> None:
        self._calls.append("get_best_server")

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

    assert calls == ["get_best_server", "download", "upload"]
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

    assert calls == ["get_best_server", "download"]
    assert "download_mbps" in result
    assert "upload_mbps" not in result


def test_run_upload_only_skips_download() -> None:
    calls: list[str] = []
    client = SpeedTestClient(speedtest_factory=_fake_factory(calls))

    result = client.run(direction="upload")

    assert calls == ["get_best_server", "upload"]
    assert "upload_mbps" in result
    assert "download_mbps" not in result


def test_tool_defaults_to_both_directions() -> None:
    calls: list[str] = []
    client = SpeedTestClient(speedtest_factory=_fake_factory(calls))
    tool = RunSpeedTestTool(client)

    result = tool.run()

    assert "download_mbps" in result
    assert "upload_mbps" in result


def test_tool_is_safe_action_not_read() -> None:
    """Unlike every other READ tool here, this one has a real-world side
    effect (bandwidth consumption) even without touching persistent
    state -- SAFE_ACTION reflects that, not READ."""
    assert RunSpeedTestTool(SpeedTestClient()).permission == ToolPermission.SAFE_ACTION
