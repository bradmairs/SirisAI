"""Server watch, OS updates, disk health, Wake-on-LAN and sudo permissions
(app/servers/, app/plugins/builtin/servers.py), against a programmable
stand-in `ssh`."""

import json
import stat
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core import daily_jobs, scheduler
from app.core.config import Settings
from app.core.siris import SirisAI, get_siris
from app.db.models import AttentionItem
from app.db.session import SessionLocal
from app.hub import attention
from app.main import app
from app.servers import checks, wake, watch
from app.ssh.client import NEEDS_SUDO_EXIT, SshHost, parse_health, passwordless

FAKE_SSH = r'''#!/usr/bin/env python3
import json, os, sys
target, command = sys.argv[-2], sys.argv[-1]
with open(os.environ["FAKE_SSH_LOG"], "a") as log:
    log.write(json.dumps([target, command]) + "\n")
for rule in json.load(open(os.environ["FAKE_SSH_RULES"])):
    if rule["host"] in target and all(part in command for part in rule.get("cmd", [""])):
        sys.stdout.write(rule.get("out", ""))
        sys.stderr.write(rule.get("err", ""))
        sys.exit(rule.get("exit", 0))
sys.exit(0)
'''

HEALTH_OK = "##uptime\n0.1 0.2 0.3 1/2 3\n4\nup 2 days\n##mem\n  total used free shared buff available\nMem: 8000 2000 5000 0 1000 6000\n##disk\nFilesystem Size Used Avail Use% Mounted on\n/dev/sda1 100G 40G 60G 40% /\n##failed\n##mac\naa:bb:cc:00:00:01\n"
HEALTH_BAD = (
    "##uptime\n0.1 0.2 0.3 1/2 3\n4\nup 2 days\n##mem\n  total used free shared buff available\nMem: 8000 7800 50 0 150 100\n"
    "##disk\nFilesystem Size Used Avail Use% Mounted on\n/dev/sdb1 3.6T 3.5T 60G 98% /mnt/media\n##failed\nnginx.service loaded failed failed nginx\n##mac\naa:bb:cc:00:00:02\n"
)
APT_UPDATES = "##pm\napt\nopenssl/noble-security 3.0.13 amd64 [upgradable from: 3.0.12]\ncurl/noble-updates 8.5 amd64 [upgradable from: 8.4]\n##reboot\nyes\nlinux-image-6.8\n##last\n"
SMART_BAD = (
    "##smart\n@@sda\nSMART overall-health self-assessment test result: FAILED!\n"
    "  5 Reallocated_Sector_Ct   0x0033   090   090   010    Pre-fail  Always       -       12\n"
    "@@nvme0n1\nSMART overall-health self-assessment test result: PASSED\nPercentage Used:                    93%\nMedia and Data Integrity Errors:    0\n"
    "##zpool\n  pool: tank\n state: DEGRADED\n##md\nmd0 : active raid1 sdc1[0] sdd1[1]\n      976630336 blocks super 1.2 [2/1] [U_]\n"
)


@pytest.fixture(autouse=True)
def _clean(client: TestClient):
    with SessionLocal() as session:
        session.query(AttentionItem).delete()
        session.commit()
    siris = SirisAI(settings=Settings())
    siris.state.delete(watch.STATE_KEY)
    siris.state.delete(wake.STATE_KEY)
    yield


class Servers:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **settings: Any) -> None:
        self.rules_path, self.log_path = tmp_path / "rules.json", tmp_path / "ssh.log"
        self.rules: list[dict[str, Any]] = []
        self.save()
        self.log_path.write_text("")
        ssh = tmp_path / "ssh"
        ssh.write_text(FAKE_SSH)
        ssh.chmod(ssh.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv("FAKE_SSH_RULES", str(self.rules_path))
        monkeypatch.setenv("FAKE_SSH_LOG", str(self.log_path))
        defaults = {"ssh_hosts": "nas=brad@10.0.0.2,pi=root@10.0.0.3", "ssh_wake_timeout_seconds": 0}
        self.siris = SirisAI(settings=Settings(**{**defaults, **settings}))
        self.siris.ssh_client._ssh_binary = str(ssh)  # type: ignore[union-attr]

    def rule(self, host: str, cmd: list[str], out: str = "", exit: int = 0, err: str = "") -> None:
        self.rules.insert(0, {"host": host, "cmd": cmd, "out": out, "exit": exit, "err": err})
        self.save()

    def save(self) -> None:
        self.rules_path.write_text(json.dumps(self.rules))

    def commands(self, host: str = "") -> list[str]:
        return [c for t, c in (json.loads(line) for line in self.log_path.read_text().splitlines()) if host in t]

    def items(self, source: str) -> dict[str, dict[str, Any]]:
        return {i["title"]: i for i in self.siris.attention.list() if i["source"] == source}


@pytest.fixture
def servers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Servers:
    return Servers(tmp_path, monkeypatch)


# -- parsing ------------------------------------------------------------------------------


def test_apt_and_dnf_updates_and_the_reboot_flag_parse() -> None:
    apt = checks.parse_updates(APT_UPDATES)
    assert apt == {"package_manager": "apt", "updates": 2, "security": 1, "packages": ["openssl", "curl"], "reboot_required": True, "reboot_because": ["linux-image-6.8"]}
    dnf = checks.parse_updates("##pm\ndnf\nkernel.x86_64 6.9.1-200.fc40 updates\nLast metadata expiration check: 0:10 ago\n##reboot\n##last\n1\nE: broken\n")
    assert dnf["updates"] == 1 and dnf["packages"] == ["kernel"] and not dnf["reboot_required"]
    assert dnf["last_install"] == {"exit_code": 1, "log_tail": "E: broken"}
    assert checks.parse_updates("##pm\nnone\n##reboot\n")["updates"] == 0


def test_install_and_restart_commands_check_exactly_what_they_run() -> None:
    brad, root = SshHost("nas", "brad", "10.0.0.2"), SshHost("pi", "root", "10.0.0.3")
    apt = checks.install_updates_command(brad, "apt")
    assert f"{passwordless(checks.APT_UPDATE)} || exit {NEEDS_SUDO_EXIT}" in apt
    assert f"{passwordless(checks.APT_UPGRADE)} || exit {NEEDS_SUDO_EXIT}" in apt
    assert f"nohup sh -c 'sudo -n {checks.APT_UPDATE} && sudo -n {checks.APT_UPGRADE} </dev/null" in apt and apt.endswith("& echo started")
    assert "sudo" not in checks.install_updates_command(root, "dnf")
    with pytest.raises(ValueError):
        checks.install_updates_command(root, "pacman")
    assert checks.restart_unit_command(brad, "nginx.service") == (
        f"{passwordless('systemctl restart nginx.service')} || exit {NEEDS_SUDO_EXIT}; sudo -n systemctl restart nginx.service && echo restarted"
    )
    for bad in ("nginx; reboot", "a b", "$(id)", ""):
        with pytest.raises(ValueError):
            checks.restart_unit_command(root, bad)


def test_disk_health_reads_smart_nvme_zfs_and_raid() -> None:
    found = checks.parse_disks(SMART_BAD)
    assert found["problems"] == [
        "sda: SMART health FAILED!, 12 reallocated sectors",
        "nvme0n1: 93% of its rated life used",
        "ZFS pool tank is DEGRADED",
        "RAID md0 is degraded (U_)",
    ]
    healthy = checks.parse_disks("##smart\n@@sda\nSMART overall-health self-assessment test result: PASSED\n  5 Reallocated_Sector_Ct 0x0033 100 100 010 Pre-fail Always - 0\n##zpool\nall pools are healthy\n##md\nmd0 : active raid1 [2/2] [UU]\n")
    assert healthy["problems"] == [] and healthy["zfs_healthy"]
    assert checks.parse_disks("##smart\nNOSUDO\n##zpool\n##md\n")["smart_note"] == "SMART needs passwordless sudo for smartctl"
    assert checks.parse_disks("##smart\nNOSMARTCTL\n")["smart_note"].startswith("smartctl isn't installed")
    assert "sudo -n -l smartctl" in checks.disks_command(SshHost("nas", "brad", "x")) and "sudo" not in checks.disks_command(SshHost("pi", "root", "x"))


def test_the_health_snapshot_learns_the_mac() -> None:
    assert parse_health(HEALTH_OK)["mac"] == "aa:bb:cc:00:00:01"
    assert "mac" not in parse_health("##mac\n00:00:00:00:00:00\n")


# -- Wake-on-LAN ----------------------------------------------------------------------------


def test_magic_packets_macs_and_broadcasts(servers: Servers) -> None:
    packet = wake.magic_packet("aa:bb:cc:dd:ee:ff")
    assert len(packet) == 102 and packet[:6] == b"\xff" * 6 and packet[6:12] == bytes.fromhex("aabbccddeeff")
    with pytest.raises(ValueError):
        wake.magic_packet("nope")
    s = servers.siris
    nas = s.ssh_client.find("nas")  # type: ignore[union-attr]
    assert wake.mac_for(s, nas) is None
    wake.remember_mac(s, nas, "aa:bb:cc:00:00:09")
    assert wake.mac_for(s, nas) == "aa:bb:cc:00:00:09"
    assert wake.broadcasts_for(s, nas) == ["255.255.255.255", "10.0.0.255"]
    s2 = SirisAI(settings=Settings(ssh_hosts="nas=brad@10.0.0.2", ssh_wake_macs="NAS=AA-BB-CC-00-00-07,bad=zz", ssh_wake_broadcast="10.0.0.127"))
    assert wake.configured_macs(s2) == {"nas": "aa:bb:cc:00:00:07"}
    assert wake.broadcasts_for(s2, nas) == ["10.0.0.127"]
    cmd = wake.relay_command("aa:bb:cc:00:00:09", ["255.255.255.255", "10.0.0.255", "evil;rm"])
    assert "'aabbcc000009'*16" in cmd and "'10.0.0.255'" in cmd and "evil" not in cmd


def test_waking_sends_every_way_and_needs_a_mac(servers: Servers, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(wake, "send_direct", lambda mac, addresses, port=9: sent.append((mac, addresses)))

    class FakeHA:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        def call_service(self, domain: str, service: str, **data: Any) -> None:
            self.calls.append((domain, service, data))

    s = SirisAI(settings=Settings(ssh_hosts="nas=brad@10.0.0.2,pi=root@10.0.0.3", ssh_wake_relay="pi", ssh_wake_timeout_seconds=0))
    s.ssh_client._ssh_binary = servers.siris.ssh_client._ssh_binary  # type: ignore[union-attr]
    s._home_assistant_client = FakeHA()  # type: ignore[assignment]
    assert s.run_tool("ssh_wake", {"servers": "nas"}, confirmed=True)[0]["error"].startswith("I don't know nas's MAC")
    wake.remember_mac(s, s.ssh_client.find("nas"), "aa:bb:cc:00:00:01")  # type: ignore[union-attr]
    result = s.run_tool("ssh_wake", {"servers": "nas"}, confirmed=True)[0]
    assert result["sent_via"] == ["relay pi", "Home Assistant", "direct broadcast"] and result["watching"] is False
    assert s._home_assistant_client.calls == [("wake_on_lan", "send_magic_packet", {"mac": "aa:bb:cc:00:00:01"})]  # type: ignore[union-attr]
    assert sent == [("aa:bb:cc:00:00:01", ["255.255.255.255", "10.0.0.255"])]
    assert any("python3 -c" in c for c in servers.commands("root@10.0.0.3"))


def test_after_waking_it_says_whether_the_server_came_back(servers: Servers) -> None:
    s = servers.siris
    nas = s.ssh_client.find("nas")  # type: ignore[union-attr]
    assert wake.wait_until_up(s, nas, timeout=1, poll=0.05) is True
    assert "nas is awake" in {i["title"] for i in s.attention.list()}
    servers.rule("brad@", [""], exit=255, err="No route to host")
    assert wake.wait_until_up(s, nas, timeout=0.2, poll=0.05) is False
    assert "nas didn't wake up" in {i["title"] for i in s.attention.list()}


# -- the watch ----------------------------------------------------------------------------------


def test_the_watch_raises_and_clears_problems_and_learns_macs(servers: Servers) -> None:
    servers.rule("brad@", ["##uptime"], HEALTH_BAD)
    servers.rule("root@", ["##uptime"], HEALTH_OK)
    report = watch.check_health(servers.siris)
    assert report["pi"]["reachable"] and report["nas"]["failed_services"] == ["nginx.service"]
    items = servers.items("servers")
    assert set(items) == {"/mnt/media on nas is 98% full", "nas is out of memory (99% used)", "nginx.service has failed on nas"}
    assert items["/mnt/media on nas is 98% full"]["severity"] == "urgent"
    assert [a["id"] for a in items["nginx.service has failed on nas"]["actions"]] == ["restart", "dismiss"]
    assert wake.mac_for(servers.siris, servers.siris.ssh_client.find("pi")) == "aa:bb:cc:00:00:01"  # type: ignore[union-attr]

    servers.rule("brad@", ["##uptime"], HEALTH_OK)
    watch.check_health(servers.siris)
    assert servers.items("servers") == {}  # all cleared up


def test_a_server_that_stops_answering_is_raised_after_two_misses_with_wake(servers: Servers) -> None:
    servers.rule("root@", ["##uptime"], HEALTH_OK)
    watch.check_health(servers.siris)  # learns pi's MAC
    servers.rule("root@", [""], exit=255, err="Connection timed out")
    watch.check_health(servers.siris)
    assert "pi isn't answering" not in servers.items("servers")  # one miss isn't enough
    watch.check_health(servers.siris)
    item = servers.items("servers")["pi isn't answering"]
    assert "Connection timed out" in item["body"] and [a["id"] for a in item["actions"]] == ["wake", "acknowledge"]
    servers.rule("brad@", [""], exit=255, err="refused")
    watch.check_health(servers.siris)
    watch.check_health(servers.siris)
    assert [a["id"] for a in servers.items("servers")["nas isn't answering"]["actions"]] == ["acknowledge"]  # no MAC known: no Wake


def test_updates_and_disks_reach_the_feed(servers: Servers) -> None:
    servers.rule("brad@", ["##pm"], APT_UPDATES)
    servers.rule("root@", ["##pm"], "##pm\napt\n##reboot\n##last\n1\nE: dpkg was interrupted\n")
    servers.rule("brad@", ["##smart"], SMART_BAD)
    servers.rule("root@", ["##smart"], "##smart\nNOSMARTCTL\n##zpool\n##md\n")
    watch.check_updates(servers.siris)
    updates = servers.items("server-updates")
    assert updates["2 updates waiting on nas (1 security)"]["severity"] == "attention"
    assert [a["id"] for a in updates["nas needs a reboot"]["actions"]] == ["reboot", "dismiss"]
    assert updates["Installing updates on pi failed"]["body"] == "E: dpkg was interrupted"
    report = watch.check_disks(servers.siris)
    assert report["pi"]["smart_note"].startswith("smartctl isn't installed")
    disks = servers.items("server-disks")
    assert disks["Disk trouble on nas: sda: SMART health FAILED!, 12 reallocated sectors"]["severity"] == "urgent"
    assert disks["Disk trouble on nas: ZFS pool tank is DEGRADED"]["severity"] == "urgent"
    assert len(disks) == 4


def test_the_feed_buttons_restart_install_reboot_and_wake(servers: Servers, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wake, "send_direct", lambda mac, addresses, port=9: None)
    s = servers.siris
    servers.rule("brad@", ["##uptime"], HEALTH_BAD)
    servers.rule("brad@", ["##pm"], APT_UPDATES)
    servers.rule("brad@", ["systemctl restart nginx.service"], "restarted\n")
    servers.rule("brad@", ["systemctl reboot"], "started\n")
    watch.check_health(s)
    watch.check_updates(s)
    attention.act(s, servers.items("servers")["nginx.service has failed on nas"]["id"], "restart")
    attention.act(s, servers.items("server-updates")["2 updates waiting on nas (1 security)"]["id"], "install")
    attention.act(s, servers.items("server-updates")["nas needs a reboot"]["id"], "reboot")
    sent = servers.commands("brad@")
    assert any(c.endswith("sudo -n systemctl restart nginx.service && echo restarted") for c in sent)
    assert any(checks.APT_UPGRADE in c and "nohup" in c for c in sent)
    assert any("systemctl reboot" in c for c in sent)
    audit = {e["tool_name"]: e for e in reversed(s.audit.list_recent(limit=20))}  # the newest of each
    assert audit["ssh_power"]["trigger"].startswith("attention feed: the user chose 'reboot'")

    wake.remember_mac(s, s.ssh_client.find("nas"), "aa:bb:cc:00:00:02")  # type: ignore[union-attr]
    s.attention.raise_item("servers:down:nas", "alert", "nas isn't answering", source="servers", actions=[attention.action("wake", "Wake")], payload={"server": "nas"})
    _, result = attention.act(s, servers.items("servers")["nas isn't answering"]["id"], "wake")
    assert result[0]["sent_via"] == ["direct broadcast"]


def test_a_restart_without_sudo_says_the_exact_rule(servers: Servers) -> None:
    s = servers.siris
    servers.rule("brad@", ["##uptime"], HEALTH_BAD)
    servers.rule("brad@", ["systemctl restart"], exit=NEEDS_SUDO_EXIT)
    watch.check_health(s)
    with pytest.raises(PermissionError, match=r"brad ALL=\(root\) NOPASSWD: /usr/bin/systemctl restart nginx.service"):
        attention.act(s, servers.items("servers")["nginx.service has failed on nas"]["id"], "restart")


# -- permissions --------------------------------------------------------------------------------


def test_permissions_say_what_is_missing_and_the_sudoers_line(servers: Servers) -> None:
    servers.rule("brad@", ["sudo -n -l"], "ok 0\nok 1\nno 2\nno 3\nok 4\n")
    rows = {r["server"]: r for r in servers.siris.run_tool("ssh_server_permissions", {})}
    assert rows["pi"]["root"] is True and rows["pi"]["cannot"] == []
    nas = rows["nas"]
    assert nas["can"] == ["reboot", "shut down", "read SMART"] and nas["cannot"] == ["delayed power / cancel", "install updates (apt)"]
    assert nas["sudoers"] == (
        "brad ALL=(root) NOPASSWD: /usr/sbin/shutdown, /usr/bin/apt-get -y -q update, "
        "/usr/bin/apt-get -y -q -o Dpkg\\:\\:Options\\:\\:\\=--force-confdef -o Dpkg\\:\\:Options\\:\\:\\=--force-confold upgrade"
    )
    assert "*" not in nas["sudoers"]  # sudo-rs takes no wildcards in arguments
    assert "nginx.service" in nas["restart_services"]


# pi-1 (Ubuntu 25.10, sudo-rs): sudo allows apt-get only with a password, but a few rules need none.
_SUDO_RS = """#!/bin/sh
if [ "$1 $2" = "-n true" ]; then echo "sudo-rs: interactive authentication is required" >&2; exit 1; fi
if [ "$1 $2" = "-n -l" ] && [ $# -eq 2 ]; then
  echo "User brad may run the following commands on pi-1:"
  echo "    (ALL : ALL) ALL"
  echo "    (ALL) NOPASSWD: /usr/sbin/reboot"
  echo "    (root) NOPASSWD: /usr/bin/systemctl reboot, /usr/bin/systemctl poweroff, /usr/sbin/shutdown"
  exit 0
fi
[ "$1 $2" = "-n -l" ] && exit 0   # listing one command: allowed (with a password)
exit 1
"""


def test_passwordless_means_no_password_not_just_allowed(tmp_path: Path) -> None:
    import subprocess

    sudo = tmp_path / "sudo"
    sudo.write_text(_SUDO_RS)
    sudo.chmod(0o755)
    env = {"PATH": f"{tmp_path}:/usr/bin:/bin"}
    ok = lambda cmd: subprocess.run(["sh", "-c", passwordless(cmd)], env=env).returncode == 0  # noqa: E731
    assert not ok(checks.APT_UPDATE)          # allowed, but only with a password: it would fail in the background
    assert ok("systemctl reboot") and ok("shutdown")
    assert not ok("smartctl")
    sudo.write_text("#!/bin/sh\nexit 0\n")     # NOPASSWD: ALL
    assert ok(checks.APT_UPDATE)


def test_a_failed_install_says_why(servers: Servers) -> None:
    from app.servers.watch import install_failure, update_rule

    host = SshHost("pi-1", "brad", "192.168.0.127")
    assert "sudo visudo -f /etc/sudoers.d/sirisai" in install_failure(host, {"exit_code": 1, "log_tail": "sudo-rs: interactive authentication is required"})
    assert update_rule(host).startswith("brad ALL=(root) NOPASSWD: /usr/bin/apt-get -y -q update, /usr/bin/apt-get -y -q -o")
    assert install_failure(host, {"exit_code": 1, "log_tail": ""}) == "It stopped with exit code 1, and its log (/tmp/sirisai-updates.log) is gone from pi-1."
    assert install_failure(host, {"exit_code": 100, "log_tail": "E: dpkg was interrupted"}) == "E: dpkg was interrupted"


def test_install_updates_asks_first_and_reports_missing_sudo(servers: Servers) -> None:
    from app.plugins.base import ConfirmationRequired

    s = servers.siris
    with pytest.raises(ConfirmationRequired):
        s.run_tool("ssh_install_updates", {"server": "nas"})
    servers.rule("brad@", ["##pm"], "##pm\napt\n##reboot\n##last\n")
    assert s.run_tool("ssh_install_updates", {"server": "nas"}, confirmed=True)["status"] == "nothing to install"
    servers.rule("brad@", ["##pm"], APT_UPDATES)
    servers.rule("brad@", ["nohup"], exit=NEEDS_SUDO_EXIT)
    with pytest.raises(PermissionError, match="needs passwordless sudo"):
        s.run_tool("ssh_install_updates", {"server": "nas"}, confirmed=True)


# -- wiring -------------------------------------------------------------------------------------


def test_routes_jobs_and_the_scheduler(client: TestClient, servers: Servers) -> None:
    servers.rule("", ["##uptime"], HEALTH_OK)
    app.dependency_overrides[get_siris] = lambda: servers.siris
    try:
        assert client.post("/siris/servers/watch").json()["nas"]["reachable"] is True
        listing = {r["name"]: r for r in client.get("/siris/servers").json()}
        assert listing["nas"]["mac"] == "aa:bb:cc:00:00:01" and listing["nas"]["failures"] == 0
        assert client.post("/siris/servers/updates").status_code == 200 and client.post("/siris/servers/disks").status_code == 200
    finally:
        app.dependency_overrides.pop(get_siris, None)
    no_ssh = SirisAI(settings=Settings())
    app.dependency_overrides[get_siris] = lambda: no_ssh
    try:
        assert client.get("/siris/servers").status_code == 404
    finally:
        app.dependency_overrides.pop(get_siris, None)
    assert scheduler.run_server_watch(no_ssh) is None
    jobs = {j.name: j for j in daily_jobs.JOBS}
    assert jobs["server_updates"].hour(no_ssh) is None and jobs["server_updates"].hour(servers.siris) == 7
    assert jobs["server_disks"].hour(servers.siris) == 6


# -- waking: always asks, and works for computers that aren't SSH servers ------------------------


def test_waking_asks_first(servers: Servers) -> None:
    from app.plugins.base import ConfirmationRequired

    with pytest.raises(ConfirmationRequired):
        servers.siris.run_tool("ssh_wake", {"servers": "nas"})


def test_a_wake_only_computer_can_be_woken_by_name_and_checked_on_its_address(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(wake, "send_direct", lambda mac, addresses, port=9: sent.append((mac, addresses)))
    started: list[Any] = []
    monkeypatch.setattr(wake.threading, "Thread", lambda target, args, daemon: type("T", (), {"start": lambda self: started.append(args)})())
    s = SirisAI(settings=Settings(ssh_hosts="nas=brad@10.0.0.2",
                                  ssh_wake_macs="Desktop=AA-BB-CC-00-00-09@10.0.0.50,laptop=aa:bb:cc:00:00:0a"))

    assert [m.name for m in wake.wake_only_machines(s)] == ["desktop", "laptop"]
    assert "desktop, laptop" in s.tools.get("ssh_wake").description

    result = s.run_tool("ssh_wake", {"servers": "Desktop"}, confirmed=True)[0]
    assert result["server"] == "desktop" and result["mac"] == "aa:bb:cc:00:00:09" and result["watching"] is True
    assert sent[-1] == ("aa:bb:cc:00:00:09", ["255.255.255.255", "10.0.0.255"])
    assert started and started[-1][1].host == "10.0.0.50"

    result = s.run_tool("ssh_wake", {"servers": "laptop"}, confirmed=True)[0]
    assert result["watching"] is False                       # no address: nothing to check it by


def test_an_unknown_machine_names_both_kinds(monkeypatch: pytest.MonkeyPatch) -> None:
    s = SirisAI(settings=Settings(ssh_hosts="nas=brad@10.0.0.2", ssh_wake_macs="desktop=aa:bb:cc:00:00:09"))
    with pytest.raises(ValueError, match="Servers: nas; other computers: desktop"):
        s.run_tool("ssh_wake", {"servers": "toaster"}, confirmed=True)


def test_a_wake_only_computer_is_up_when_it_answers_on_any_port(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    def refused(address: tuple[str, int], timeout: float) -> Any:
        raise ConnectionRefusedError
    monkeypatch.setattr(socket, "create_connection", refused)
    assert wake.answers("10.0.0.50") is True                 # a refusal still means it's running

    def silent(address: tuple[str, int], timeout: float) -> Any:
        raise TimeoutError
    monkeypatch.setattr(socket, "create_connection", silent)
    assert wake.answers("10.0.0.50") is False

    s = SirisAI(settings=Settings(ssh_wake_macs="desktop=aa:bb:cc:00:00:09@10.0.0.50"))
    monkeypatch.setattr(wake, "answers", lambda address, timeout=2.0: address == "10.0.0.50")
    desktop = wake.wake_only_machines(s)[0]
    assert wake.wait_until_up(s, desktop, timeout=1, poll=0.01) is True
    assert "desktop is awake" in {i["title"] for i in s.attention.list()}


# -- reboot follow-up (5.73) ----------------------------------------------------------------

OLD_BOOT = "11111111-2222-3333-4444-555555555555"
NEW_BOOT = "99999999-8888-7777-6666-555555555555"


def _reboot(servers: Servers, power_out: str = "started\n", power_exit: int = 0, power_err: str = "") -> dict[str, Any]:
    from app.servers import power

    servers.siris.state.delete(power.STATE_KEY)
    servers.rule("brad@", ["random/boot_id"], OLD_BOOT + "\n")
    servers.rule("brad@", ["systemctl reboot"], power_out, exit=power_exit, err=power_err)
    return servers.siris.run_tool("ssh_power", {"action": "reboot", "servers": "nas"}, confirmed=True)


def test_a_reboot_is_followed_until_the_server_is_back(servers: Servers) -> None:
    from datetime import UTC, datetime, timedelta

    from app.servers import power

    result = _reboot(servers)
    assert result["summary"] == "Rebooting nas. I'll tell you when it's back."
    start = datetime.now(UTC)
    assert [p["boot_id"] for p in power.pending(servers.siris)] == [OLD_BOOT]
    servers.rule("brad@", ["random/boot_id"], exit=255, err="ssh: connect to host 10.0.0.2: No route to host")
    assert power.check(servers.siris, start + timedelta(seconds=30)) == []  # down: still waiting
    servers.rule("brad@", ["random/boot_id"], NEW_BOOT + "\n")
    done = power.check(servers.siris, start + timedelta(seconds=100))
    assert [d["outcome"] for d in done] == ["back"] and power.pending(servers.siris) == []
    assert "nas rebooted and is back (1m 40s)" in servers.items("servers-power")


def test_a_dropped_connection_is_the_reboot_starting(servers: Servers) -> None:
    result = _reboot(servers, power_out="", power_exit=255, power_err="Connection to 10.0.0.2 closed by remote host.")
    assert result["results"] == [{"server": "nas", "status": "started", "self": False}]


def test_a_reboot_that_never_happened_is_an_alert(servers: Servers) -> None:
    from datetime import UTC, datetime, timedelta

    from app.servers import power

    _reboot(servers)
    start = datetime.now(UTC)
    assert power.check(servers.siris, start + timedelta(minutes=1)) == []
    done = power.check(servers.siris, start + timedelta(minutes=4))
    assert [d["outcome"] for d in done] == ["not rebooted"]
    assert servers.items("servers-power")["nas didn't reboot"]["kind"] == "alert"


def test_a_server_that_doesnt_come_back_is_an_alert(servers: Servers) -> None:
    from datetime import UTC, datetime, timedelta

    from app.servers import power

    _reboot(servers)
    servers.rule("brad@", ["random/boot_id"], exit=255, err="No route to host")
    assert power.check(servers.siris, datetime.now(UTC) + timedelta(minutes=11))[0]["outcome"] == "not back"
    assert "nas hasn't come back after rebooting" in servers.items("servers-power")


def test_the_feed_reboot_button_fails_loudly_when_nothing_happened(servers: Servers) -> None:
    servers.rule("brad@", ["##pm"], APT_UPDATES)
    watch.check_updates(servers.siris)
    servers.rule("brad@", ["systemctl reboot"], exit=NEEDS_SUDO_EXIT)
    item = servers.items("server-updates")["nas needs a reboot"]
    with pytest.raises(Exception, match="passwordless sudo"):
        attention.act(servers.siris, item["id"], "reboot")


def test_the_reboot_command_runs_in_a_real_shell(tmp_path: Path) -> None:
    """The command as sent, against stand-in sudo and systemctl: the reboot is
    asked for in the foreground and nothing is left running behind it."""
    import subprocess

    from app.ssh.client import power_command

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    for name, body in {
        "sudo": 'echo "sudo $*" >> "$CALLS"; [ "$1" = -n ] && shift; [ "$1" = -l ] && { echo "(root) NOPASSWD: /usr/bin/systemctl reboot"; exit 0; }; [ "$1" = true ] && exit 1; "$@"',
        "systemctl": 'echo "systemctl $*" >> "$CALLS"',
    }.items():
        path = bin_dir / name
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)
    command = power_command(SshHost("nas", "brad", "10.0.0.2"), "reboot")
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "CALLS": str(calls)}
    out = subprocess.run(["sh", "-c", command], capture_output=True, text=True, env=env, timeout=10, stdin=subprocess.DEVNULL)
    assert out.returncode == 0 and out.stdout.strip().splitlines()[-1] == "started"
    assert "systemctl reboot" in calls.read_text().splitlines()[-1]


# -- 6.38: reboots at a set time, one at a time, and each server's timeline ------------------


@pytest.fixture
def clean_power(servers: Servers):  # type: ignore[no-untyped-def]
    from app.servers import power, timeline

    for key in (power.STATE_KEY, power.SCHEDULED_KEY, power.ROLLING_KEY, timeline.STATE_KEY, timeline.SEEN_KEY, watch.REBOOT_KEY):
        servers.siris.state.delete(key)
    yield servers


def test_when_words_become_times(clean_power: Servers) -> None:
    from datetime import UTC, datetime

    from app.servers import power

    s = clean_power.siris
    s.settings.timezone = "Australia/Sydney"
    now = datetime(2026, 11, 11, 1, 0, tzinfo=UTC)  # Wed 12:00 in Sydney (+11)
    assert power.parse_when(s, "03:00", now).isoformat() == "2026-11-11T16:00:00+00:00"  # Thu 03:00
    assert power.parse_when(s, "sunday 3am", now).isoformat() == "2026-11-14T16:00:00+00:00"
    assert power.parse_when(s, "tomorrow 2:30pm", now).isoformat() == "2026-11-12T03:30:00+00:00"
    assert power.parse_when(s, "2026-11-20T03:00", now).isoformat() == "2026-11-19T16:00:00+00:00"
    with pytest.raises(ValueError):
        power.parse_when(s, "whenever", now)
    with pytest.raises(ValueError, match="passed"):
        power.parse_when(s, "2026-11-01T03:00", now)


def test_a_reboot_at_a_set_time_runs_then_and_can_be_cancelled(clean_power: Servers) -> None:
    from datetime import UTC, datetime, timedelta

    from app.servers import power

    s = clean_power.siris
    clean_power.rule("brad@", ["random/boot_id"], OLD_BOOT + "\n")
    clean_power.rule("brad@", ["systemctl reboot"], "started\n")
    out = s.run_tool("ssh_power", {"action": "reboot", "servers": "nas", "at": "03:00"}, confirmed=True)
    assert out["summary"].startswith("Rebooting nas at ") and out["summary"].endswith("I'll check it comes back.")
    assert not any("systemctl reboot" in c for c in clean_power.commands("brad@"))  # not yet
    when = datetime.fromisoformat(power.scheduled(s)[0]["at"])
    power.tick(s, when - timedelta(minutes=1))
    assert power.scheduled(s)
    power.tick(s, when + timedelta(seconds=30))
    assert power.scheduled(s) == [] and any("systemctl reboot" in c for c in clean_power.commands("brad@"))
    assert [p["server"] for p in power.pending(s)] == ["nas"]
    assert s.audit.list_recent(limit=1)[0]["trigger"].startswith("scheduled by you for ")

    s.run_tool("ssh_power", {"action": "reboot", "servers": "nas", "at": "04:00"}, confirmed=True)
    cancelled = s.run_tool("ssh_power", {"action": "cancel", "servers": "nas"}, confirmed=True)
    assert "Cancelled the scheduled reboot of nas" in cancelled["summary"] and power.scheduled(s) == []


def test_a_scheduled_reboot_missed_while_down_is_not_done_late(clean_power: Servers) -> None:
    from datetime import UTC, datetime, timedelta

    from app.servers import power

    s = clean_power.siris
    at = datetime.now(UTC) + timedelta(hours=1)
    power.schedule(s, "reboot", ["nas"], at)
    power.tick(s, at + timedelta(hours=2))
    assert not any("systemctl reboot" in c for c in clean_power.commands("brad@"))
    assert "The reboot of nas didn't happen" in clean_power.items("servers-power")


def test_servers_reboot_one_at_a_time(clean_power: Servers) -> None:
    from datetime import UTC, datetime, timedelta

    from app.servers import power

    s = clean_power.siris
    s.settings.ssh_self_server = "nas"
    clean_power.rule("", ["random/boot_id"], OLD_BOOT + "\n")
    clean_power.rule("", ["systemctl reboot"], "started\n")
    out = s.run_tool("ssh_reboot_needed", {"servers": "nas,pi"}, confirmed=True)
    assert out["servers"] == ["pi", "nas"]  # SirisAI's own server last
    assert s.state.get(power.ROLLING_KEY)["current"] == "pi" and not any("systemctl reboot" in c for c in clean_power.commands("brad@"))
    now = datetime.now(UTC)
    power.tick(s, now + timedelta(seconds=30))
    assert s.state.get(power.ROLLING_KEY)["current"] == "pi"  # pi isn't back yet
    clean_power.rule("root@", ["random/boot_id"], NEW_BOOT + "\n")
    power.tick(s, now + timedelta(seconds=90))
    assert s.state.get(power.ROLLING_KEY)["current"] == "nas" and any("systemctl reboot" in c for c in clean_power.commands("brad@"))
    clean_power.rule("brad@", ["random/boot_id"], NEW_BOOT + "\n")
    power.tick(s, now + timedelta(seconds=150))
    assert s.state.get(power.ROLLING_KEY) is None
    assert "Rebooted pi, nas, one at a time; all back" in clean_power.items("servers-power")
    assert [e["text"] for e in s.run_tool("server_timeline", {"server": "pi"})["events"]][:2] == ["rebooted and came back", "reboot started"]


def test_one_that_doesnt_come_back_stops_the_rest(clean_power: Servers) -> None:
    from datetime import UTC, datetime, timedelta

    from app.servers import power

    s = clean_power.siris
    clean_power.rule("", ["random/boot_id"], OLD_BOOT + "\n")
    clean_power.rule("", ["systemctl reboot"], "started\n")
    s.run_tool("ssh_reboot_needed", {"servers": "nas,pi"}, confirmed=True)
    first = s.state.get(power.ROLLING_KEY)["current"]
    power.tick(s, datetime.now(UTC) + timedelta(minutes=5))  # still the old boot id: didn't reboot
    assert s.state.get(power.ROLLING_KEY) is None
    assert any(t.startswith(f"Stopped rebooting the servers: {first}") for t in clean_power.items("servers-power"))


def test_two_servers_needing_a_reboot_offer_one_at_a_time(clean_power: Servers) -> None:
    clean_power.rule("", ["##pm"], APT_UPDATES)
    watch.check_updates(clean_power.siris)
    assert watch.needs_reboot(clean_power.siris) == ["nas", "pi"]
    item = clean_power.items("server-updates")["2 servers need a reboot: nas, pi"]
    assert [a["id"] for a in item["actions"]] == ["reboot_all", "dismiss"]
    from app.voice.confirmations import NEVER_BY_VOICE

    assert "ssh_reboot_needed" in NEVER_BY_VOICE


def test_the_timeline_keeps_warnings_once(clean_power: Servers) -> None:
    s = clean_power.siris
    clean_power.rule("brad@", ["##uptime"], HEALTH_BAD)
    watch.check_health(s)
    watch.check_health(s)
    texts = [e["text"] for e in s.run_tool("server_timeline", {"server": "nas"})["events"]]
    assert "nginx.service has failed on nas" in texts and texts.count("nginx.service has failed on nas") == 1
    assert "nas, the last 7 days (newest first):" in s.run_tool("server_timeline", {"server": "nas"})["text"]
    clean_power.rule("brad@", ["##uptime"], HEALTH_OK)
    watch.check_health(s)
    clean_power.rule("brad@", ["##uptime"], HEALTH_BAD)
    watch.check_health(s)  # failed again after clearing: a new event
    texts = [e["text"] for e in s.run_tool("server_timeline", {"server": "nas"})["events"]]
    assert texts.count("nginx.service has failed on nas") == 2
