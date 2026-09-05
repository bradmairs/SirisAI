import time

from app.plugins.builtin.system_info import SystemInfoTool


def test_system_info_does_not_block_on_cpu_sampling() -> None:
    """Performance hardening pass: this used to call psutil.cpu_percent(
    interval=0.2), a guaranteed 200ms block on nearly every chat turn
    (ContextEngine.build() calls this tool). Regression guard: a real
    call, run twice back to back, should complete in well under 200ms
    each time -- a blocking interval reintroduced here would fail this."""
    tool = SystemInfoTool(disk_paths=["/"])

    start = time.monotonic()
    tool.run()
    tool.run()
    elapsed = time.monotonic() - start

    assert elapsed < 0.15  # well under the 0.2s a single blocking call used to take


def test_system_info_returns_expected_shape() -> None:
    tool = SystemInfoTool(disk_paths=["/"])
    result = tool.run()

    assert isinstance(result["cpu_percent"], float)
    assert isinstance(result["cpu_count"], int)
    assert "/" in result["disk"]
    assert result["disk"]["/"]["total_gb"] > 0
    assert 0 <= result["disk"]["/"]["percent_used"] <= 100
    assert result["memory"]["total_gb"] > 0
    assert isinstance(result["temperatures_celsius"], dict)


def test_system_info_skips_unreadable_disk_paths() -> None:
    tool = SystemInfoTool(disk_paths=["/", "/no/such/path/exists"])
    result = tool.run()
    assert "/" in result["disk"]
    assert "/no/such/path/exists" not in result["disk"]
