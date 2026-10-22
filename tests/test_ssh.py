"""SSH to servers on the home network (app/ssh/client.py, app/plugins/builtin/ssh.py),
including reboot and shutdown, against a stand-in `ssh` binary."""

import stat
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core import taint
from app.core.config import Settings
from app.core.siris import SirisAI
from app.plugins.base import ConfirmationRequired
from app.plugins.builtin.ssh import SshPowerTool, SshRunTool
from app.ssh.client import NEEDS_SUDO_EXIT, SshClient, SshError, SshHost, is_read_only, parse_health, parse_ssh_hosts, power_command
from app.voice.confirmations import describe_call, voice_confirmable

HEALTH_OUTPUT = """##uptime
0.42 3.10 2.80 2/345 6789
2
up 3 weeks, 2 days
##mem
               total        used        free      shared  buff/cache   available
Mem:            7820        7300         120          10         400         350
Swap:              0           0           0
##disk
Filesystem      Size  Used Avail Use% Mounted on
/dev/sda1        58G   20G   36G  36% /
/dev/sdb1       3.6T  3.4T  120G  97% /mnt/media
##failed
plex.service loaded failed failed Plex Media Server
"""


def _fake_ssh(tmp_path: Path, script: str) -> str:
    path = tmp_path / "ssh"
    path.write_text("#!/bin/sh\n" + script)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def test_hosts_parse_with_or_without_names_users_and_ports() -> None:
    hosts = parse_ssh_hosts(" nas=brad@192.168.0.127, pi=root@192.168.0.83:2222 ,10.0.0.5, Box=10.0.0.6 ,")
    assert hosts == [
        SshHost("nas", "brad", "192.168.0.127"),
        SshHost("pi", "root", "192.168.0.83", 2222),
        SshHost("10.0.0.5", "", "10.0.0.5"),
        SshHost("box", "", "10.0.0.6"),
    ]
    assert hosts[0].target == "brad@192.168.0.127" and hosts[2].target == "10.0.0.5"
    with pytest.raises(ValueError):
        parse_ssh_hosts("nas=brad@")


@pytest.mark.parametrize(
    "command",
    [
        "uptime",
        "df -h",
        "free -m",
        "systemctl status nginx --no-pager",
        "systemctl --failed",
        "journalctl -u plex -n 50 --no-pager",
        "docker ps",
        "docker logs --tail 50 plex",
        "docker stats --no-stream",
        "tail -n 100 /var/log/syslog",
        "ip addr",
        "ping -c 3 192.168.0.1",
        "zpool status",
        "ps aux",
        "ps -ef",
        "hostname",
        "date",
    ],
)
def test_look_only_commands_are_read_only(command: str) -> None:
    assert is_read_only(command)


@pytest.mark.parametrize(
    "command",
    [
        "",
        "rm -rf /tmp/x",
        "systemctl restart nginx",
        "docker restart plex",
        "docker exec plex sh",
        "sudo df -h",
        "uptime; rm -rf ~",
        "uptime && reboot",
        "cat /etc/hosts > /tmp/x",
        "df -h | mail me",
        "echo $(id)",
        "ls `whoami`",
        "cat ~/.ssh/id_ed25519",
        "cat .env",
        "grep -r password /etc",
        "tail -f /var/log/syslog",
        "journalctl --follow",
        "ping 192.168.0.1",
        "docker stats",
        "ip link set eth0 down",
        "ip route add default via 10.0.0.1",
        "apt install htop",
        "reboot",
        "unbalanced 'quote",
        "docker inspect plex",
        "systemctl show plex",
        "ps eww",
        "cat /proc/1/environ",
        "cat ~/.bash_history",
        "hostname evil",
        "date -s 2020-01-01",
        "mount /dev/sdb1 /mnt",
    ],
)
def test_anything_that_could_change_or_leak_is_not(command: str) -> None:
    assert not is_read_only(command)


def test_extra_safe_prefixes_count_as_look_only() -> None:
    assert not is_read_only("pihole status")
    assert is_read_only("pihole status", ["pihole status"])
    assert not is_read_only("pihole restartdns", ["pihole status"])
    assert not is_read_only("pihole status; reboot", ["pihole status"])


def test_a_command_runs_with_batch_mode_on_the_named_host(tmp_path: Path) -> None:
    ssh = _fake_ssh(tmp_path, 'echo "ARGS:$*"; echo oops >&2; exit 3\n')
    client = SshClient(parse_ssh_hosts("nas=brad@10.0.0.2:2200"), ssh_binary=ssh)
    result = client.run("NAS", "uptime")
    assert result["host"] == "nas" and result["exit_code"] == 3 and result["stderr"] == "oops\n"
    assert "-p 2200 -o BatchMode=yes" in result["stdout"] and result["stdout"].strip().endswith("brad@10.0.0.2 uptime")
    assert client.find("10.0.0.2").name == "nas"  # by address too
    with pytest.raises(ValueError, match="Unknown server 'prod'. Configured: nas."):
        client.run("prod", "uptime")


def test_connection_failures_and_timeouts_are_clear(tmp_path: Path) -> None:
    refused = SshClient(parse_ssh_hosts("pi=root@10.0.0.3"), ssh_binary=_fake_ssh(tmp_path, 'echo "Connection refused" >&2; exit 255\n'))
    with pytest.raises(SshError, match="Couldn't connect to pi .*Connection refused"):
        refused.run("pi", "uptime")
    slow = SshClient(parse_ssh_hosts("pi=root@10.0.0.3"), timeout=0.2, connect_timeout=0.1, ssh_binary=_fake_ssh(tmp_path, "sleep 5\n"))
    with pytest.raises(SshError, match="didn't finish"):
        slow.run("pi", "uptime")
    missing = SshClient(parse_ssh_hosts("pi=root@10.0.0.3"), ssh_binary=str(tmp_path / "nope"))
    with pytest.raises(SshError, match="isn't installed"):
        missing.run("pi", "uptime")


def test_long_output_keeps_the_end(tmp_path: Path) -> None:
    client = SshClient(parse_ssh_hosts("pi=root@10.0.0.3"), ssh_binary=_fake_ssh(tmp_path, "seq 1 5000\n"))
    out = client.run("pi", "journalctl -n 5000")["stdout"]
    assert out.startswith("[first ") and out.rstrip().endswith("5000") and len(out) < 6100


def test_the_health_snapshot_reads_load_memory_disks_and_failed_services() -> None:
    health = parse_health(HEALTH_OUTPUT)
    assert health["load"] == [0.42, 3.1, 2.8] and health["cpus"] == 2 and health["up"] == "3 weeks, 2 days"
    assert health["memory"] == {"total_mb": 7820, "used_percent": 96}
    assert health["disks"][1] == {"mount": "/mnt/media", "size": "3.6T", "used_percent": 97, "free": "120G"}
    assert health["failed_services"] == ["plex.service"]
    assert health["warnings"] == [
        "load is high (3.1 over 5 minutes on 2 CPUs)",
        "memory is 96% used",
        "/mnt/media is 97% full (120G free)",
        "plex.service has failed",
    ]
    assert parse_health("")["warnings"] == []


def _siris(tmp_path: Path, **settings: Any) -> SirisAI:
    siris = SirisAI(settings=Settings(**{"ssh_hosts": "nas=brad@10.0.0.2", **settings}))
    siris.ssh_client._ssh_binary = _fake_ssh(tmp_path, 'case "$*" in *"##uptime"*) cat <<EOF\n' + HEALTH_OUTPUT + "EOF\n;; *) echo \"ran: $*\";; esac\n")  # type: ignore[union-attr]
    return siris


def test_tools_register_only_with_hosts_and_fall_back_to_the_cron_hosts(client: TestClient) -> None:
    assert SirisAI(settings=Settings()).tools.get("ssh_run") is None
    via_cron = SirisAI(settings=Settings(cron_hosts="brad@192.168.0.127"))
    assert via_cron.run_tool("ssh_list_servers", {}) == [{"name": "192.168.0.127", "target": "brad@192.168.0.127", "port": 22}]
    assert {"ssh_run", "ssh_server_health"} <= taint.EXTERNAL_CONTENT_TOOLS


def test_look_only_runs_straight_away_and_changes_ask_first(client: TestClient, tmp_path: Path) -> None:
    siris = _siris(tmp_path)
    assert siris.run_tool("ssh_run", {"server": "nas", "command": "df -h"})["stdout"].endswith("brad@10.0.0.2 df -h\n")
    with pytest.raises(ConfirmationRequired):
        siris.run_tool("ssh_run", {"server": "nas", "command": "systemctl restart plex"})
    held = siris.audit.find_recent("ssh_run", limit=1)[0]
    assert held["confirmation_required"] and held["arguments"]["command"] == "systemctl restart plex"
    ran = siris.run_tool("ssh_run", {"server": "nas", "command": "systemctl restart plex"}, confirmed=True)
    assert ran["exit_code"] == 0 and "systemctl restart plex" in ran["stdout"]


def test_changes_can_be_switched_off_entirely(tmp_path: Path) -> None:
    tool = SshRunTool(SshClient(parse_ssh_hosts("nas=brad@10.0.0.2"), ssh_binary=_fake_ssh(tmp_path, "echo ok\n")), allow_changes=False)
    assert tool.run(server="nas", command="uptime")["stdout"] == "ok\n"
    with pytest.raises(PermissionError):
        tool.run(server="nas", command="reboot")


def test_server_health_over_one_or_all(client: TestClient, tmp_path: Path) -> None:
    siris = _siris(tmp_path, ssh_hosts="nas=brad@10.0.0.2,pi=root@10.0.0.3")
    one = siris.run_tool("ssh_server_health", {"server": "nas"})
    assert one["server"] == "nas" and "plex.service has failed" in one["warnings"]
    assert [h["server"] for h in siris.run_tool("ssh_server_health", {})] == ["nas", "pi"]


# -- reboot and shut down -------------------------------------------------------------------


def test_power_commands_use_sudo_only_for_non_root_and_detach_immediate_ones() -> None:
    brad, root = SshHost("nas", "brad", "10.0.0.2"), SshHost("pi", "root", "10.0.0.3")
    assert power_command(root, "reboot") == "nohup sh -c 'sleep 2; systemctl reboot' >/dev/null 2>&1 & echo started"
    assert power_command(brad, "shutdown") == (
        f"sudo -n -l systemctl poweroff >/dev/null 2>&1 || exit {NEEDS_SUDO_EXIT}; "
        "nohup sh -c 'sleep 2; sudo -n systemctl poweroff' >/dev/null 2>&1 & echo started"
    )
    assert power_command(brad, "reboot", 15).endswith("sudo -n shutdown -r +15 'SirisAI: reboot requested' && echo scheduled")
    assert power_command(root, "cancel") == "shutdown -c && echo cancelled"
    with pytest.raises(ValueError):
        power_command(root, "halt")


def _power_tool(tmp_path: Path, script: str, hosts: str = "nas=brad@10.0.0.2,pi=root@10.0.0.3,box=root@10.0.0.4", **kwargs: Any) -> SshPowerTool:
    return SshPowerTool(SshClient(parse_ssh_hosts(hosts), ssh_binary=_fake_ssh(tmp_path, script)), **kwargs)


def test_power_reaches_one_several_or_all_with_its_own_server_last(tmp_path: Path) -> None:
    log = tmp_path / "calls"
    tool = _power_tool(tmp_path, f'echo "$*" >> {log}; echo started\n', self_server="nas")
    result = tool.run(action="reboot", servers="all")
    assert [r["server"] for r in result["results"]] == ["pi", "box", "nas"]  # nas is SirisAI's own: last
    assert result["results"][-1]["self"] is True and result["summary"] == "Rebooting pi, box, nas"
    assert len(log.read_text().splitlines()) == 3
    several = tool.run(action="shutdown", servers="pi, BOX, pi", delay_minutes=999)
    assert [r["server"] for r in several["results"]] == ["pi", "box"] and several["delay_minutes"] == 240
    assert several["summary"] == "Shutting down pi, box in 240 minutes"
    with pytest.raises(ValueError, match="Unknown server 'prod'"):
        tool.run(action="reboot", servers="pi,prod")  # nothing runs on a bad name
    assert len(log.read_text().splitlines()) == 5
    with pytest.raises(ValueError):
        tool.run(action="reboot", servers=" ")


def test_power_failures_are_per_server_and_say_why(tmp_path: Path) -> None:
    script = (
        'case "$*" in\n'
        f'  *brad@*) exit {NEEDS_SUDO_EXIT};;\n'
        '  *10.0.0.3*) echo "Connection refused" >&2; exit 255;;\n'
        '  *) echo "Failed to talk to init" >&2; exit 1;;\n'
        "esac\n"
    )
    result = _power_tool(tmp_path, script).run(action="reboot", servers="all")
    by = {r["server"]: r for r in result["results"]}
    assert "brad needs passwordless sudo" in by["nas"]["detail"]
    assert "Couldn't connect to pi" in by["pi"]["detail"]
    assert by["box"] == {"server": "box", "status": "failed", "detail": "Failed to talk to init"}
    assert result["summary"] == "Nothing was done."


def test_power_can_be_turned_off(tmp_path: Path) -> None:
    with pytest.raises(PermissionError):
        _power_tool(tmp_path, "echo started\n", allowed=False).run(action="reboot", servers="all")


def test_power_always_asks_never_by_voice_and_not_on_its_own(client: TestClient, tmp_path: Path) -> None:
    siris = SirisAI(settings=Settings(ssh_hosts="nas=brad@10.0.0.2"))
    siris.ssh_client._ssh_binary = _fake_ssh(tmp_path, "echo started\n")  # type: ignore[union-attr]
    with pytest.raises(ConfirmationRequired):
        siris.run_tool("ssh_power", {"action": "reboot", "servers": "nas"})
    with pytest.raises(ConfirmationRequired):
        siris.run_tool("ssh_power", {"action": "reboot", "servers": "nas"}, trigger="routine: 'nightly' (at 03:00)")
    assert siris.run_tool("ssh_power", {"action": "reboot", "servers": "nas"}, confirmed=True)["summary"] == "Rebooting nas"
    assert not voice_confirmable("ssh_power", {"action": "reboot", "servers": "nas"}, set())
    assert describe_call("ssh_power", {"action": "shutdown", "servers": "all", "delay_minutes": 10}) == "shutdown all in 10 minutes"
    assert describe_call("ssh_power", {"action": "cancel", "servers": "nas"}) == "cancel the pending shutdown on nas"
    assert SirisAI(settings=Settings(ssh_hosts="nas=brad@10.0.0.2", ssh_allow_changes=False)).tools.get("ssh_power")._allowed is False  # type: ignore[union-attr]
