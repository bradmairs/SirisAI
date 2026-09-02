from app.plugins.builtin.system_info import SystemInfoTool


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
