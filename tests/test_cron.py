"""Tests for the cron overview feature (app/cron/client.py,
app/plugins/builtin/cron.py, GET /siris/cron_jobs)."""

import tempfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.siris import SirisAI, get_siris
from app.cron.client import CronHost, CronHostError, CronHostsClient, parse_cron_hosts
from app.plugins.base import ToolPermission


# -- parse_cron_hosts


def test_parse_cron_hosts_basic() -> None:
    hosts = parse_cron_hosts("brad@192.168.0.127,root@192.168.0.83")
    assert hosts == [
        CronHost(user="brad", host="192.168.0.127", port=22),
        CronHost(user="root", host="192.168.0.83", port=22),
    ]


def test_parse_cron_hosts_with_port_and_blank_entries() -> None:
    hosts = parse_cron_hosts(" brad@192.168.0.127:2222 , , root@192.168.0.83,")
    assert hosts == [
        CronHost(user="brad", host="192.168.0.127", port=2222),
        CronHost(user="root", host="192.168.0.83", port=22),
    ]


def test_parse_cron_hosts_empty_spec_returns_empty_list() -> None:
    assert parse_cron_hosts("") == []


def test_cron_host_label() -> None:
    assert CronHost(user="root", host="192.168.0.83").label == "root@192.168.0.83"


# -- CronHostsClient.list_jobs


class _FakeCompletedProcess:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


_TEST_CRONTAB = """# a comment, not a job
0 3 * * * /usr/bin/backup.sh --full
*/15 * * * * /usr/local/bin/check_temp.sh
#0 4 * * 0 /usr/bin/old_disabled_job.sh
30 2 * * 1 /home/pi/scripts/rotate_logs.sh
@reboot /usr/local/bin/on_boot.sh
PATH=/usr/bin:/bin
"""


def test_list_jobs_parses_schedule_command_and_enabled_state() -> None:
    host = CronHost(user="root", host="pi.local")
    client = CronHostsClient([host])
    with patch("app.cron.client.subprocess.run", return_value=_FakeCompletedProcess(stdout=_TEST_CRONTAB)):
        jobs = client.list_jobs()

    by_command = {j["command"]: j for j in jobs}
    assert by_command["/usr/bin/backup.sh --full"]["schedule"] == "0 3 * * *"
    assert by_command["/usr/bin/backup.sh --full"]["enabled"] is True
    assert by_command["/usr/local/bin/check_temp.sh"]["enabled"] is True
    assert by_command["/usr/bin/old_disabled_job.sh"]["enabled"] is False
    assert by_command["/usr/bin/old_disabled_job.sh"]["schedule"] == "0 4 * * 0"
    assert by_command["/usr/local/bin/on_boot.sh"]["schedule"] == "@reboot"
    assert all(j["host"] == "root@pi.local" for j in jobs)
    # The plain comment and the bare PATH= line aren't schedule-shaped --
    # neither should be reported as a job.
    assert "PATH=/usr/bin:/bin" not in [j["command"] for j in jobs]
    assert len(jobs) == 5


def test_list_jobs_skips_no_crontab_silently() -> None:
    host = CronHost(user="nocrontabuser", host="pi.local")
    client = CronHostsClient([host])
    with patch(
        "app.cron.client.subprocess.run",
        return_value=_FakeCompletedProcess(returncode=1, stderr="no crontab for nocrontabuser"),
    ):
        jobs = client.list_jobs()
    assert jobs == []


def test_list_jobs_reports_unreachable_host_without_failing_others() -> None:
    import subprocess

    good_host = CronHost(user="root", host="pi-good")
    bad_host = CronHost(user="root", host="pi-unreachable")
    client = CronHostsClient([bad_host, good_host])

    def fake_run(args, **kwargs):
        if "pi-unreachable" in args[-2]:
            raise subprocess.TimeoutExpired(cmd=args, timeout=10)
        return _FakeCompletedProcess(stdout="0 3 * * * /bin/true\n")

    with patch("app.cron.client.subprocess.run", side_effect=fake_run):
        jobs = client.list_jobs()

    errored = [j for j in jobs if "error" in j]
    ok = [j for j in jobs if "error" not in j]
    assert len(errored) == 1
    assert errored[0]["host"] == "root@pi-unreachable"
    assert len(ok) == 1
    assert ok[0]["host"] == "root@pi-good"


# -- CronHostsClient.set_job_enabled


def test_set_job_enabled_disables_a_matching_line() -> None:
    host = CronHost(user="root", host="pi.local")
    client = CronHostsClient([host])
    calls = []

    def fake_run(args, input=None, **kwargs):
        calls.append((args, input))
        if input is None:
            return _FakeCompletedProcess(stdout="0 3 * * * /usr/bin/backup.sh --full\n")
        return _FakeCompletedProcess(stdout="")

    with patch("app.cron.client.subprocess.run", side_effect=fake_run):
        result = client.set_job_enabled("root@pi.local", "0 3 * * * /usr/bin/backup.sh --full", enabled=False)

    assert result == {"host": "root@pi.local", "line": "0 3 * * * /usr/bin/backup.sh --full", "enabled": False}
    # Second call is the write (`crontab -` with piped input); confirm the
    # line was actually commented out in what got piped back.
    write_input = calls[1][1]
    assert write_input == "# 0 3 * * * /usr/bin/backup.sh --full\n"


def test_set_job_enabled_reenables_a_disabled_line() -> None:
    host = CronHost(user="root", host="pi.local")
    client = CronHostsClient([host])

    def fake_run(args, input=None, **kwargs):
        if input is None:
            return _FakeCompletedProcess(stdout="# 0 4 * * 0 /usr/bin/old_disabled_job.sh\n")
        return _FakeCompletedProcess(stdout="")

    with patch("app.cron.client.subprocess.run", side_effect=fake_run) as mock_run:
        client.set_job_enabled("root@pi.local", "0 4 * * 0 /usr/bin/old_disabled_job.sh", enabled=True)

    write_input = mock_run.call_args_list[1].kwargs["input"]
    assert write_input == "0 4 * * 0 /usr/bin/old_disabled_job.sh\n"


def test_set_job_enabled_raises_for_no_matching_line() -> None:
    host = CronHost(user="root", host="pi.local")
    client = CronHostsClient([host])
    with patch("app.cron.client.subprocess.run", return_value=_FakeCompletedProcess(stdout="0 3 * * * /bin/true\n")):
        try:
            client.set_job_enabled("root@pi.local", "5 5 * * * /bin/nonexistent", enabled=False)
            raise AssertionError("expected ValueError")
        except ValueError:
            pass


def test_set_job_enabled_raises_for_unknown_host() -> None:
    client = CronHostsClient([CronHost(user="root", host="pi.local")])
    try:
        client.set_job_enabled("root@some-other-host", "0 3 * * * /bin/true", enabled=False)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_run_raises_cron_host_error_on_nonzero_exit() -> None:
    host = CronHost(user="root", host="pi.local")
    client = CronHostsClient([host])
    with patch(
        "app.cron.client.subprocess.run",
        return_value=_FakeCompletedProcess(returncode=255, stderr="Permission denied (publickey)."),
    ):
        try:
            client.list_jobs()
        except CronHostError:
            raise AssertionError("list_jobs should swallow per-host errors, not raise") from None


# -- Tool registration and permission tiers


def test_cron_tools_not_registered_without_cron_hosts() -> None:
    siris = SirisAI(settings=Settings())
    names = {tool.name for tool in siris.tools.list()}
    assert not names & {"cron_list_jobs", "cron_set_job_enabled"}
    assert siris.cron_client is None


def test_cron_tools_registered_with_expected_permissions_when_cron_hosts_configured() -> None:
    siris = SirisAI(settings=Settings(cron_hosts="root@192.168.0.83"))
    list_tool = siris.tools.get("cron_list_jobs")
    set_tool = siris.tools.get("cron_set_job_enabled")
    assert list_tool is not None and list_tool.permission is ToolPermission.READ
    assert set_tool is not None and set_tool.permission is ToolPermission.CONFIRM_REQUIRED
    assert siris.cron_client is not None


def test_cron_tools_categorized_under_cron() -> None:
    siris = SirisAI(settings=Settings(cron_hosts="root@192.168.0.83"))
    categories = siris.capabilities()
    assert "cron_list_jobs" in categories.get("cron", [])
    assert "cron_set_job_enabled" in categories.get("cron", [])


# -- GET /siris/cron_jobs


def test_list_cron_jobs_route_returns_empty_list_without_cron_hosts_configured(client: TestClient) -> None:
    # The shared `client` fixture's app never has SIRISAI_CRON_HOSTS set
    # (see conftest.py) -- same "empty = feature not engaged" convention
    # as MetricsDashboardResponse.power_draw.
    response = client.get("/siris/cron_jobs")
    assert response.status_code == 200
    assert response.json() == []


def test_list_cron_jobs_route_reflects_the_configured_cron_client() -> None:
    siris = SirisAI(settings=Settings(cron_hosts="root@pi.local"))
    with patch("app.cron.client.subprocess.run", return_value=_FakeCompletedProcess(stdout="0 3 * * * /bin/true\n")):
        jobs = siris.cron_client.list_jobs()
    assert jobs == [
        {"host": "root@pi.local", "schedule": "0 3 * * *", "command": "/bin/true", "line": "0 3 * * * /bin/true", "enabled": True}
    ]


def test_missing_ssh_binary_is_reported_per_host_not_raised() -> None:
    """Found live: a container with no `ssh` made GET /siris/cron_jobs 500
    instead of showing each host's error in the tab."""
    client = CronHostsClient(
        [CronHost(user="brad", host="192.168.0.127"), CronHost(user="root", host="192.168.0.83")],
        ssh_binary="/nonexistent/ssh",
    )

    jobs = client.list_jobs()

    assert [j["host"] for j in jobs] == ["brad@192.168.0.127", "root@192.168.0.83"]
    assert all("isn't installed" in j["error"] for j in jobs)


# -- the overview, schedules in words, adding servers (5.73) -------------------------------


def test_schedules_in_words_and_their_next_run() -> None:
    from datetime import UTC, datetime

    from app.cron.schedule import describe, next_run

    assert describe("0 3 * * *") == "Every day at 03:00"
    assert describe("*/5 * * * *") == "Every 5 minutes"
    assert describe("30 2 * * 1") == "Every Monday at 02:30"
    assert describe("0 8 * * 1-5") == "Every weekday at 08:00"
    assert describe("0 22 * * 6,0") == "Weekends at 22:00"
    assert describe("0 0 1 * *") == "On the 1st of every month at 00:00"
    assert describe("@reboot") == "When the server starts"
    assert describe("0 9 * 1,7 *") == "Every day in January and July at 09:00"
    now = datetime(2026, 11, 11, 1, 0, tzinfo=UTC)  # 12:00 in +11:00
    assert next_run("0 3 * * *", now, 660).isoformat() == "2026-11-12T03:00:00+11:00"
    assert next_run("30 2 * * 1", now).isoformat() == "2026-11-16T02:30:00+00:00"
    assert next_run("@reboot", now) is None and next_run("nonsense", now) is None


OVERVIEW_OUTPUT = """##info
nas
+1100
up 3 days, 2 hours
##user
# backup
0 3 * * * /usr/bin/backup.sh --full
# 0 4 * * 0 /usr/bin/old.sh
##file /etc/crontab
SHELL=/bin/sh
17 *\t* * *\troot\tcd / && run-parts --report /etc/cron.hourly
##file /etc/cron.d/certbot
0 */12 * * * root certbot -q renew
##log
2026-11-11T03:00:01+11:00 nas CRON[123]: (brad) CMD (/usr/bin/backup.sh --full)
2026-11-10T03:00:01+11:00 nas CRON[99]: (brad) CMD (/usr/bin/backup.sh --full)
2026-11-11T11:17:01+11:00 nas CRON[5]: (root) CMD (cd / && run-parts --report /etc/cron.hourly)
"""


def test_the_overview_has_every_job_with_details() -> None:
    from datetime import UTC, datetime

    from app.cron.client import overview

    client = CronHostsClient([CronHost(user="brad", host="nas"), CronHost(user="pi", host="pi.local")])

    def fake_run(args, **kwargs):  # type: ignore[no-untyped-def]
        if "pi@pi.local" in args:
            return _FakeCompletedProcess(returncode=255, stderr="No route to host")
        return _FakeCompletedProcess(stdout=OVERVIEW_OUTPUT)

    with patch("app.cron.client.subprocess.run", side_effect=fake_run):
        hosts = overview(client, datetime(2026, 11, 11, 1, 0, tzinfo=UTC))
    nas, pi = hosts
    assert (nas["hostname"], nas["utc_offset"], nas["uptime"], nas["log_available"]) == ("nas", "+1100", "up 3 days, 2 hours", True)
    jobs = {j["command"]: j for j in nas["jobs"]}
    backup = jobs["/usr/bin/backup.sh --full"]
    assert backup["description"] == "Every day at 03:00" and backup["editable"] and backup["run_as"] == "brad"
    assert backup["next_run"] == "2026-11-12T03:00:00+11:00" and backup["last_run"] == "2026-11-11T03:00:01+11:00"
    assert jobs["/usr/bin/old.sh"]["enabled"] is False and jobs["/usr/bin/old.sh"]["next_run"] is None
    hourly = jobs["cd / && run-parts --report /etc/cron.hourly"]
    assert (hourly["source"], hourly["run_as"], hourly["editable"]) == ("/etc/crontab", "root", False)
    assert hourly["last_run"] == "2026-11-11T11:17:01+11:00"
    assert jobs["certbot -q renew"]["source"] == "/etc/cron.d/certbot" and jobs["certbot -q renew"]["last_run"] is None
    assert len(nas["jobs"]) == 4  # the SHELL= line and comments aren't jobs
    assert pi["reachable"] is False and "No route to host" in pi["error"]


def test_servers_can_be_added_and_removed(client: TestClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.core import settings_overrides

    siris = get_siris()
    monkeypatch.setattr(settings_overrides, "path", lambda: Path(tempfile.mkdtemp()) / "settings.json")
    monkeypatch.setattr(siris.settings, "cron_hosts", "")
    before = (siris.cron_client, siris.tools._tools.get("cron_list_jobs"), siris.tools._tools.get("cron_set_job_enabled"))
    try:
        assert client.post("/siris/cron_jobs/hosts", json={"target": "not a host"}).status_code == 422
        with patch("app.cron.client.subprocess.run", return_value=_FakeCompletedProcess(returncode=255, stderr="brad@10.0.0.9: Permission denied (publickey).")):
            refused = client.post("/siris/cron_jobs/hosts", json={"target": "brad@10.0.0.9"})
        assert refused.status_code == 502 and "doesn't accept SirisAI's" in refused.json()["detail"]
        assert siris.settings.cron_hosts == ""
        with patch("app.cron.client.subprocess.run", return_value=_FakeCompletedProcess(stdout="nas\n3\n")):
            added = client.post("/siris/cron_jobs/hosts", json={"target": "brad@10.0.0.9:2222"})
        assert added.status_code == 200 and added.json()["hostname"] == "nas"
        assert siris.settings.cron_hosts == "brad@10.0.0.9:2222" and siris.cron_client.hosts[0].port == 2222
        assert siris.tools.get("cron_list_jobs") is not None
        assert client.post("/siris/cron_jobs/hosts", json={"target": "brad@10.0.0.9"}).status_code == 422  # already there
        assert client.delete("/siris/cron_jobs/hosts/brad@10.0.0.9").json()["hosts"] == []
        assert siris.cron_client is None and client.get("/siris/cron_jobs/overview").json()["hosts"] == []
        assert client.delete("/siris/cron_jobs/hosts/brad@10.0.0.9").status_code == 404
    finally:
        siris.cron_client = before[0]
        for name, tool in (("cron_list_jobs", before[1]), ("cron_set_job_enabled", before[2])):
            if tool is None:
                siris.tools._tools.pop(name, None)
            else:
                siris.tools._tools[name] = tool
