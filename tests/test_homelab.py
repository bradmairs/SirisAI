"""Homelab upkeep (app/homelab/): container updates with roll-back, server
backups and their monthly test, new network devices, the internet outage
log, and expiring certificates and domains."""

import json
import socket
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core import approvals, daily_jobs
from app.core.config import Settings
from app.core.siris import SirisAI
from app.db.models import AttentionItem, KnownNetworkDevice
from app.db.session import SessionLocal
from app.docker.client import DockerClient, _split_ref
from app.homelab import backups, containers, devices, expiry, internet

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clean(client: TestClient):
    with SessionLocal() as session:
        session.query(AttentionItem).delete()
        session.query(KnownNetworkDevice).filter(KnownNetworkDevice.entity_id.like("unifi:%")).delete(synchronize_session=False)
        session.commit()
    s = SirisAI(settings=Settings())
    for key in (containers.STATE_KEY, backups.STATE_KEY, internet.STATE_KEY, devices.NAMES_KEY):
        s.state.delete(key)
    yield


def _items(siris: SirisAI, source: str) -> dict[str, dict[str, Any]]:
    return {i["title"]: i for i in siris.attention.list() if i["source"] == source}


# -- container updates --------------------------------------------------------------------


class FakeDocker:
    """Enough of the Engine API for checking and recreating containers."""

    def __init__(self) -> None:
        self.images: dict[str, dict[str, Any]] = {}
        self.tags: dict[str, str] = {}  # ref -> image id
        self.remote: dict[str, str] = {}  # ref -> registry digest
        self.pullable: dict[str, dict[str, Any]] = {}  # ref -> image the next pull brings
        self.containers: dict[str, dict[str, Any]] = {}
        self.new_exits = False
        self.calls: list[str] = []
        self._n = 0

    def image(self, ref: str, image_id: str, digest: str | None, env: list[str] | None = None, cmd: list[str] | None = None) -> None:
        repo = ref.rsplit(":", 1)[0]
        self.images[image_id] = {"Id": image_id, "RepoDigests": [f"{repo}@{digest}"] if digest else [], "Config": {"Env": env or [], "Cmd": cmd or ["run"], "Labels": {"maker": "x"}}}
        self.tags[ref] = image_id

    def container(self, name: str, ref: str, labels: dict[str, str] | None = None, env: list[str] | None = None) -> str:
        self._n += 1
        cid = f"{self._n:012d}" + "f" * 52
        image = self.images[self.tags[ref]]
        self.containers[cid] = {
            "Id": cid,
            "Name": "/" + name,
            "Image": image["Id"],
            "Config": {
                "Image": ref,
                "Env": [*image["Config"]["Env"], *(env or [])],
                "Cmd": image["Config"]["Cmd"],
                "Labels": {**image["Config"]["Labels"], **(labels or {})},
                "Hostname": cid[:12],
            },
            "HostConfig": {"Binds": ["/srv/x:/config"], "RestartPolicy": {"Name": "unless-stopped"}},
            "NetworkSettings": {"Networks": {"media": {"Aliases": [name, cid[:12]]}, "proxy": {}}},
            "State": {"Running": True, "ExitCode": 0},
            "connected": [],
        }
        return cid

    def _find(self, ref: str) -> dict[str, Any] | None:
        for c in self.containers.values():
            if c["Id"] == ref or c["Id"].startswith(ref) or c["Name"] == "/" + ref:
                return c
        return None

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        self.calls.append(f"{method} {path}")
        if path == "/containers/json":
            return httpx.Response(200, json=[{"Id": c["Id"], "Names": [c["Name"]], "Image": c["Config"]["Image"], "State": "running"} for c in self.containers.values() if c["State"]["Running"]])
        if path.startswith("/distribution/"):
            return httpx.Response(200, json={"Descriptor": {"digest": self.remote[path.removeprefix("/distribution/").removesuffix("/json")]}})
        if path.startswith("/images/") and path.endswith("/json"):
            ref = path.removeprefix("/images/").removesuffix("/json")
            image = self.images.get(ref) or self.images.get(self.tags.get(ref, ""))
            return httpx.Response(200, json=image) if image else httpx.Response(404, json={})
        if path == "/images/create":
            ref = f"{request.url.params['fromImage']}:{request.url.params['tag']}"
            if ref in self.pullable:
                new = self.pullable.pop(ref)
                self.images[new["Id"]] = new
                self.tags[ref] = new["Id"]
            return httpx.Response(200, text='{"status":"done"}\n')
        if path == "/containers/create":
            body = json.loads(request.content)
            self._n += 1
            cid = f"{self._n:012d}" + "e" * 52
            self.containers[cid] = {"Id": cid, "Name": "/" + request.url.params["name"], "Image": self.tags.get(body["Image"], body["Image"]), "Config": body, "HostConfig": body["HostConfig"], "NetworkSettings": {"Networks": {}}, "State": {"Running": False}, "connected": []}
            return httpx.Response(201, json={"Id": cid})
        parts = path.split("/")
        if parts[1] == "networks" and parts[3] == "connect":
            body = json.loads(request.content)
            self._find(body["Container"])["connected"].append(parts[2])  # type: ignore[index]
            return httpx.Response(200)
        c = self._find(parts[2]) if len(parts) > 2 else None
        if c is None:
            return httpx.Response(404, json={"message": "no such container"})
        action = parts[3] if len(parts) > 3 else ""
        if method == "DELETE":
            del self.containers[c["Id"]]
            return httpx.Response(204)
        if action == "json":
            return httpx.Response(200, json=c)
        if action == "stop":
            c["State"] = {"Running": False, "ExitCode": 0}
            return httpx.Response(204)
        if action == "start":
            fails = self.new_exits and c["Id"].endswith("e" * 52)
            c["State"] = {"Running": not fails, "ExitCode": 1 if fails else 0}
            return httpx.Response(204)
        if action == "rename":
            c["Name"] = "/" + request.url.params["name"]
            return httpx.Response(204)
        return httpx.Response(404, json={})


@pytest.fixture
def docker() -> tuple[SirisAI, FakeDocker]:
    fake = FakeDocker()
    siris = SirisAI(settings=Settings(container_update_health_seconds=0.05, container_update_exclude="db", container_update_watch_minutes=0))
    siris._docker_client = DockerClient("http://docker", transport=httpx.MockTransport(fake.handler))
    from app.plugins.builtin.homelab import DockerCheckUpdatesTool, DockerUpdateContainersTool

    siris.tools.register(DockerCheckUpdatesTool(siris))
    siris.tools.register(DockerUpdateContainersTool(siris))
    return siris, fake


def _setup_containers(fake: FakeDocker) -> None:
    fake.image("lscr.io/linuxserver/plex:latest", "sha256:old", "sha256:aaa", env=["PATH=/usr/bin", "VERSION=1"])
    fake.remote["lscr.io/linuxserver/plex:latest"] = "sha256:bbb"
    fake.pullable["lscr.io/linuxserver/plex:latest"] = {"Id": "sha256:new", "RepoDigests": ["lscr.io/linuxserver/plex@sha256:bbb"], "Config": {"Env": ["PATH=/usr/bin", "VERSION=2"], "Cmd": ["run2"], "Labels": {"maker": "x"}}}
    fake.container("plex", "lscr.io/linuxserver/plex:latest", env=["TZ=Australia/Melbourne"])
    fake.image("nginx:1.27", "sha256:ng", "sha256:same")
    fake.remote["nginx:1.27"] = "sha256:same"
    fake.container("web", "nginx:1.27")
    fake.image("local/app:dev", "sha256:local", None)
    fake.container("app", "local/app:dev")
    fake.image("postgres:16", "sha256:pg", "sha256:p1")
    fake.remote["postgres:16"] = "sha256:p2"
    fake.container("db", "postgres:16")
    fake.image("redis:7", "sha256:rd", "sha256:r1")
    fake.remote["redis:7"] = "sha256:r2"
    fake.container("cache", "redis:7", labels={"com.centurylinklabs.watchtower.enable": "false"})


def test_image_refs_split_into_name_and_tag() -> None:
    assert _split_ref("lscr.io/linuxserver/plex:latest") == ("lscr.io/linuxserver/plex", "latest")
    assert _split_ref("registry:5000/app") == ("registry:5000/app", "latest")
    assert _split_ref("nginx") == ("nginx", "latest")


def test_checking_finds_updates_and_says_why_others_were_skipped(docker) -> None:
    siris, fake = docker
    _setup_containers(fake)
    report = containers.check(siris, NOW)
    assert [u["container"] for u in report["updates"]] == ["plex"]
    assert report["up_to_date"] == ["web"]
    assert report["skipped"] == {"app": "built locally", "db": "excluded (SIRISAI_CONTAINER_UPDATE_EXCLUDE)", "cache": "opted out by label"}
    item = next(iter(_items(siris, "containers").values()))
    assert item["title"] == "1 container update: plex" and [a["id"] for a in item["actions"]] == ["update", "dismiss"]


def test_updating_recreates_with_the_same_settings_and_new_image_defaults(docker) -> None:
    siris, fake = docker
    _setup_containers(fake)
    [result] = containers.update_many(siris, ["plex"])
    assert result["status"] == "updated", result
    [plex] = [c for c in fake.containers.values() if c["Name"] == "/plex"]
    assert plex["Image"] == "sha256:new" and plex["State"]["Running"]
    config = plex["Config"]
    # Only the container's own settings were carried over; the image's defaults come from the new image.
    assert config["Env"] == ["TZ=Australia/Melbourne"] and "Cmd" not in config and "Hostname" not in config
    assert config["HostConfig"]["Binds"] == ["/srv/x:/config"]
    assert config["NetworkingConfig"] == {"EndpointsConfig": {"media": {"Aliases": ["plex"]}}} and plex["connected"] == ["proxy"]
    assert not any(c["Name"].endswith(containers.ASIDE) for c in fake.containers.values())  # the old one's gone


def test_a_new_container_that_fails_rolls_back(docker) -> None:
    siris, fake = docker
    _setup_containers(fake)
    fake.new_exits = True
    [result] = containers.update_many(siris, ["plex"])
    assert result["status"] == "rolled back" and "stopped" in result["why"]
    [plex] = [c for c in fake.containers.values() if c["Name"] == "/plex"]
    assert plex["Image"] == "sha256:old" and plex["State"]["Running"]
    assert len([c for c in fake.containers.values() if c["Config"]["Image"].startswith("lscr.io")]) == 1
    assert "The new version didn't come up: it stopped" in _items(siris, "containers-failed")["plex's update was rolled back"]["body"]


def test_update_all_from_the_feed(docker) -> None:
    siris, fake = docker
    _setup_containers(fake)
    containers.check(siris, NOW)
    item = next(iter(_items(siris, "containers").values()))
    from app.hub import attention

    closed, result = attention.act(siris, item["id"], "update")
    # Answers at once and works in the background (3.52), then says how it went.
    assert result["started"] == 1 and closed["outcome"].startswith("Updating 1 in the background")
    containers._worker.join(10)
    assert not _items(siris, "containers")  # nothing left to update
    assert "Updated 1 of 1 containers" in _items(siris, "containers-done")
    # Where it had got to was in the feed while it ran (3.72), and closed when it finished.
    progress = [i for i in siris.attention.list(None) if i["source"] == "containers-progress"]
    assert [i["title"] for i in progress] == ["Updating 1 of 1: plex"] and progress[0]["status"] == "done"
    with_pin = SirisAI(settings=Settings(approval_pin="1234"))
    assert approvals.action_needs_pin(with_pin, item, "update", {})


def test_a_container_that_falls_over_after_its_update_goes_back(docker) -> None:
    siris, fake = docker
    _setup_containers(fake)
    [result] = containers.update_many(siris, ["plex"])
    assert result["status"] == "updated"
    [plex] = [c for c in fake.containers.values() if c["Name"] == "/plex"]
    assert plex["Image"] == "sha256:new"
    plex["State"] = {"Running": False, "ExitCode": 137}  # crashes a few minutes in
    [rolled] = containers.watch(siris, ["plex"], seconds=0.2, poll=0.05)
    assert rolled == {"container": "plex", "status": "rolled back", "why": "it stopped (exit code 137)"}
    [plex] = [c for c in fake.containers.values() if c["Name"] == "/plex"]
    assert plex["Image"] == "sha256:old" and plex["State"]["Running"]
    assert plex["Config"]["Env"] == ["TZ=Australia/Melbourne"] and plex["connected"] == ["proxy"]
    assert "It's back on the image it had before" in _items(siris, "containers-failed")["plex was rolled back after its update"]["body"]


def test_a_healthy_update_is_left_alone_by_the_watch(docker) -> None:
    siris, fake = docker
    _setup_containers(fake)
    containers.update_many(siris, ["plex"])
    assert containers.watch(siris, ["plex"], seconds=0.1, poll=0.05) == []
    assert [c["Image"] for c in fake.containers.values() if c["Name"] == "/plex"] == ["sha256:new"]


def test_the_front_doors_update_last() -> None:
    assert containers.front_doors_last(["sirisos-https", "mealie", "sirisai-https-1", "plex"]) == ["mealie", "plex", "sirisos-https", "sirisai-https-1"]


# -- server backups -----------------------------------------------------------------------


class FakeSsh:
    def __init__(self, replies: dict[str, dict[str, Any]]) -> None:
        self.replies = replies
        self.sent: list[tuple[str, str]] = []
        self.hosts: list[Any] = []

    def run(self, name: str, command: str, timeout: float | None = None) -> dict[str, Any]:
        self.sent.append((name, command))
        for needle, reply in self.replies.items():
            if needle in command and needle.split("|")[0]:
                return {"stdout": "", "stderr": "", "exit_code": 0, **reply}
        return {"stdout": "", "stderr": "", "exit_code": 0}


def _backup_siris(replies: dict[str, dict[str, Any]]) -> tuple[SirisAI, FakeSsh]:
    siris = SirisAI(settings=Settings(server_backups="nas:zfs:tank/data, nas:path:/mnt/backup/restic, pi:path:/var/backups"))
    fake = FakeSsh(replies)
    siris.ssh_client = fake  # type: ignore[assignment]
    return siris, fake


def test_backup_entries_parse() -> None:
    assert backups.parse("nas:zfs:tank/data,pi:path:/var/b") == [backups.Backup("nas", "zfs", "tank/data"), backups.Backup("pi", "path", "/var/b")]
    with pytest.raises(ValueError):
        backups.parse("nas:rsync:/x")


def test_stale_and_missing_backups_reach_the_feed() -> None:
    fresh = (NOW - timedelta(hours=5)).timestamp()
    old = (NOW - timedelta(hours=60)).timestamp()
    siris, fake = _backup_siris(
        {
            "zfs list -H -p": {"stdout": f"{int(fresh)}\ttank/data@auto-2026-10-29\n"},
            "/mnt/backup/restic": {"stdout": f"{old} /mnt/backup/restic/data/ab/1234\n"},
            "/var/backups": {"stdout": ""},
        }
    )
    report = backups.check(siris, NOW)
    assert report["nas:tank/data"]["hours_old"] == 5.0
    items = _items(siris, "server-backups")
    assert items["The backup nas:/mnt/backup/restic is 2.5 days old"]["severity"] == "urgent"
    assert items["No backups found in pi:/var/backups"]["severity"] == "urgent"
    assert "nas:tank/data" not in " ".join(items)
    assert "'/mnt/backup/restic'" in fake.sent[1][1] or "/mnt/backup/restic" in fake.sent[1][1]


def test_the_monthly_backup_test_reads_a_file_back() -> None:
    siris, fake = _backup_siris(
        {
            "zfs get": {"stdout": "##from auto-2026-10-29\n##file /tank/data/.zfs/snapshot/auto-2026-10-29/photos/a.jpg\n##bytes 2400000\n##ok read it all\n"},
            "dir='/mnt/backup/restic'": {"stdout": "##file /mnt/backup/restic/data/ab/x.tar.gz\n##bytes 10\n##failed the archive is unreadable\n"},
            "dir=/var/backups": {"stdout": "##none\n"},
        }
    )
    report = backups.restore_test(siris, NOW)
    assert report["nas:tank/data"]["ok"] and report["nas:tank/data"]["bytes"] == 2400000
    items = _items(siris, "server-backups-test")
    assert "a.jpg (2.3 MB) from snapshot auto-2026-10-29" in items["Backup test passed: nas:tank/data"]["body"]
    assert items["Backup test failed: nas:/mnt/backup/restic"]["severity"] == "urgent"
    assert "nothing to test" in items["Backup test failed: pi:/var/backups"]["body"]


# -- new devices --------------------------------------------------------------------------


class FakeUniFi:
    def __init__(self) -> None:
        self.clients = [{"mac": "aa:aa:aa:00:00:01", "name": "Brad's iPhone", "hostname": "iphone", "vendor": "Apple", "network": "Main", "ssid": "Home", "ip": "10.0.0.5", "wired": False}]
        self.blocked: list[str] = []

    def client_details(self) -> list[dict[str, Any]]:
        return list(self.clients)

    def list_clients(self) -> list[dict[str, Any]]:
        return [{"id": f"id-{c['mac']}", "mac": c["mac"], "name": c["name"]} for c in self.clients]

    def set_blocked(self, mac: str, blocked: bool) -> dict[str, Any]:
        self.blocked.append(mac)
        return {"mac": mac, "blocked": blocked}


def _device_siris() -> tuple[SirisAI, FakeUniFi]:
    from app.plugins.builtin.unifi import UniFiBlockClientTool

    siris = SirisAI(settings=Settings())
    fake = FakeUniFi()
    siris._unifi_client = fake  # type: ignore[assignment]
    siris.tools.register(UniFiBlockClientTool(fake))  # type: ignore[arg-type]
    return siris, fake


def test_a_new_device_is_raised_after_the_baseline_with_name_and_block() -> None:
    from app.hub import attention

    siris, fake = _device_siris()
    assert devices.check(siris, NOW)["baseline"] == 1 and not _items(siris, "devices")
    fake.clients.append({"mac": "bb:bb:bb:00:00:02", "name": None, "hostname": None, "vendor": "Espressif", "network": "IoT", "ssid": "IoT", "ip": "10.0.20.9", "wired": False})
    fake.clients.append({"mac": "cc:cc:cc:00:00:03", "name": None, "hostname": "printer", "vendor": "HP", "network": "Main", "ssid": None, "ip": "10.0.0.9", "wired": True})
    assert len(devices.check(siris, NOW)["new"]) == 2
    items = _items(siris, "devices")
    esp = items["New device: Espressif on IoT"]
    assert "MAC: bb:bb:bb:00:00:02" in esp["body"] and [a["id"] for a in esp["actions"]] == ["name", "block"]
    assert devices.check(siris, NOW)["new"] == []  # each one only once

    attention.act(siris, esp["id"], "name", body="Kitchen plug")
    assert devices.names(siris)["bb:bb:bb:00:00:02"] == "Kitchen plug"
    printer = items["New device: printer on Main"]
    closed, _ = attention.act(siris, printer["id"], "block")
    assert fake.blocked == ["cc:cc:cc:00:00:03"] and closed["outcome"] == "Blocked cc:cc:cc:00:00:03"
    assert any(e["tool_name"] == "unifi_block" for e in siris.audit.list_recent(limit=10))

    assert devices.find_mac(siris, "kitchen plug") == "bb:bb:bb:00:00:02"
    assert devices.find_mac(siris, "10.0.0.5") == "aa:aa:aa:00:00:01"
    with_pin = SirisAI(settings=Settings(approval_pin="1234"))
    assert approvals.action_needs_pin(with_pin, printer, "block", {})


def test_a_device_unifi_has_known_for_a_while_is_never_new() -> None:
    siris, fake = _device_siris()
    devices.check(siris, NOW)
    long_ago = NOW.timestamp() - 90 * 86400
    # Asleep at the baseline, back now: UniFi first saw it months ago, so it's quietly recorded.
    fake.clients.append({"mac": "dd:dd:dd:00:00:04", "name": None, "hostname": "Lounge", "vendor": "Apple, Inc.", "network": "Main",
                         "ssid": "Main", "ip": "192.168.0.11", "wired": False, "first_seen": long_ago})
    # Named in UniFi: you know it.
    fake.clients.append({"mac": "ee:ee:ee:00:00:05", "name": "Garage camera", "hostname": None, "vendor": "Ubiquiti", "network": "IoT",
                         "ssid": None, "ip": "10.0.20.3", "wired": True, "first_seen": NOW.timestamp() - 60})
    # Really new to the gateway: alerted.
    fake.clients.append({"mac": "ff:ff:ff:00:00:06", "name": None, "hostname": None, "vendor": "Tuya", "network": "IoT",
                         "ssid": "IoT", "ip": "10.0.20.44", "wired": False, "first_seen": NOW.timestamp() - 600})
    assert [d["mac"] for d in devices.check(siris, NOW)["new"]] == ["ff:ff:ff:00:00:06"]
    assert list(_items(siris, "devices")) == ["New device: Tuya on IoT"]


def test_an_alert_raised_before_for_an_old_device_is_closed() -> None:
    siris, fake = _device_siris()
    devices.check(siris, NOW)
    fake.clients.append({"mac": "dd:dd:dd:00:00:04", "name": None, "hostname": "Lounge", "vendor": "Apple", "network": "Main", "ssid": "Main",
                         "ip": "192.168.0.11", "wired": False})  # an older UniFi without first_seen: alerted
    devices.check(siris, NOW)
    assert "New device: Lounge on Main" in _items(siris, "devices")
    fake.clients[-1]["first_seen"] = NOW.timestamp() - 30 * 86400
    devices.check(siris, NOW)
    assert not _items(siris, "devices")


# -- internet outage log -------------------------------------------------------------------


def test_an_outage_is_logged_raised_and_closed() -> None:
    siris = SirisAI(settings=Settings(timezone="UTC", internet_probe_seconds=60))
    t = NOW
    internet.probe(siris, t, up=True)
    internet.probe(siris, t + timedelta(minutes=1), up=False)
    assert not _items(siris, "internet")
    internet.probe(siris, t + timedelta(minutes=2), up=False)
    assert _items(siris, "internet")["The internet is down (since 12:01)"]["severity"] == "urgent"
    for minute in range(3, 26):
        internet.probe(siris, t + timedelta(minutes=minute), up=False)
    internet.probe(siris, t + timedelta(minutes=26), up=True)
    assert not _items(siris, "internet")
    [outage] = siris.state.get(internet.STATE_KEY)["outages"]
    assert outage["seconds"] == 25 * 60

    # A short blip, then SirisAI stops mid-outage: that one ends at the last probe seen.
    internet.probe(siris, t + timedelta(hours=1), up=False)
    internet.probe(siris, t + timedelta(hours=1, minutes=1), up=True)
    internet.probe(siris, t + timedelta(hours=2), up=False)
    internet.probe(siris, t + timedelta(hours=5), up=True)
    outages = siris.state.get(internet.STATE_KEY)["outages"]
    assert [o["seconds"] for o in outages] == [1500, 60, 60]


def test_the_weekly_summary_counts_drops_and_speed_dips() -> None:
    siris = SirisAI(settings=Settings(timezone="UTC"))
    state = {
        "outages": [
            {"start": (NOW - timedelta(days=2)).isoformat(), "end": (NOW - timedelta(days=2) + timedelta(minutes=25)).isoformat(), "seconds": 1500},
            {"start": (NOW - timedelta(days=3)).isoformat(), "end": (NOW - timedelta(days=3)).isoformat(), "seconds": 600},
            {"start": (NOW - timedelta(days=20)).isoformat(), "end": (NOW - timedelta(days=20)).isoformat(), "seconds": 9999},
        ],
        "speeds": [{"at": (NOW - timedelta(days=d)).isoformat(), "down": down, "up": 40} for d, down in [(1, 500), (2, 480), (3, 200), (10, 500), (12, 510)]],
    }
    siris.state.set(internet.STATE_KEY, state)
    found = internet.weekly(siris, NOW)
    assert found["drops"] == 2 and found["minutes_down"] == 35.0 and len(found["dips"]) == 1
    assert found["text"] == "Internet last week: 2 drops, 35 minutes in total, the worst Tue 12:00 for 25 minutes. Speed averaged 393/40 Mbps with 1 dip below half the usual."
    assert next(iter(_items(siris, "internet-weekly").values()))["title"] == found["text"]
    assert internet.summary(siris, 30, NOW)["drops"] == 3


def test_online_is_any_target_answering() -> None:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    dead = closed.getsockname()[1]
    closed.close()
    try:
        port = server.getsockname()[1]
        assert internet.online(SirisAI(settings=Settings(internet_probe_targets=f"127.0.0.1:{dead},127.0.0.1:{port}")), timeout=1)
        assert not internet.online(SirisAI(settings=Settings(internet_probe_targets=f"127.0.0.1:{dead}")), timeout=1)
    finally:
        server.close()


# -- certificates and domains -------------------------------------------------------------


def test_domain_expiry_comes_from_rdap() -> None:
    def rdap(request: httpx.Request) -> httpx.Response:
        if "example.com" in request.url.path:
            return httpx.Response(200, json={"events": [{"eventAction": "registration", "eventDate": "2000-01-01T00:00:00Z"}, {"eventAction": "expiration", "eventDate": "2026-11-05T00:00:00Z"}]})
        return httpx.Response(404)

    found = expiry.domain_expiry("Example.com.", NOW, transport=httpx.MockTransport(rdap))
    assert found == {"domain": "example.com", "expires": "2026-11-05T00:00:00+00:00", "days_left": 6.5}
    assert "error" in expiry.domain_expiry("nope.example", NOW, transport=httpx.MockTransport(rdap))


def test_expiring_domains_and_unreachable_cert_hosts_reach_the_feed(monkeypatch: pytest.MonkeyPatch) -> None:
    siris = SirisAI(settings=Settings(security_domains="example.com,later.net", security_cert_hosts="127.0.0.1:1", expiry_check_servers=False))
    monkeypatch.setattr(expiry, "domain_expiry", lambda d, now=None: {"domain": d, "expires": "2026-11-05T00:00:00+00:00", "days_left": 6.5 if d == "example.com" else 200})
    expiry.check(siris, NOW)
    items = _items(siris, "expiry")
    assert items["The domain example.com expires in 6 days"]["severity"] == "urgent"
    assert "Couldn't check the certificate on 127.0.0.1:1" in items
    assert not any("later.net" in t for t in items)


def test_the_jobs_are_scheduled() -> None:
    names = {job.name: job for job in daily_jobs.JOBS}
    assert {"container_updates", "server_backups", "server_backup_test", "internet_weekly", "expiry_check"} <= set(names)
    assert names["server_backup_test"].monthday == 1 and names["internet_weekly"].weekday(SirisAI(settings=Settings())) == 0
    assert time.monotonic() > 0
