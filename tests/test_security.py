"""Security briefing and backup verification (app/security/,
app/skills/builtin/security.py)."""

import json
import socket
import ssl
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.scheduler import run_security_watch
from app.core.siris import SirisAI
from app.cron.client import CronHost, CronHostsClient
from app.security import backups, checks
from app.synology.client import SynologyClient


def _tls_server(tmp_path: Path, days: int) -> tuple[int, threading.Event]:
    key, cert = tmp_path / "key.pem", tmp_path / "cert.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert), "-days", str(days), "-subj", "/CN=nas.home"],
        check=True,
        capture_output=True,
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    stop = threading.Event()

    def serve() -> None:
        listener.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except OSError:
                continue
            try:
                with context.wrap_socket(conn, server_side=True):
                    pass
            except (ssl.SSLError, OSError):
                pass
        listener.close()

    threading.Thread(target=serve, daemon=True).start()
    return listener.getsockname()[1], stop


@pytest.mark.parametrize("without_cryptography", [False, True])
def test_certificate_days_left_including_self_signed(tmp_path: Path, without_cryptography: bool) -> None:
    port, stop = _tls_server(tmp_path, days=10)
    try:
        modules = {"cryptography": None} if without_cryptography else {}
        with patch.dict(sys.modules, modules):
            result = checks.check_certificate(f"127.0.0.1:{port}")
    finally:
        stop.set()
    assert "error" not in result, result
    assert 9 < result["days_left"] <= 10
    assert "nas.home" in result["subject"]


def test_unreachable_certificate_host_is_an_error_entry_not_an_exception() -> None:
    result = checks.check_certificate("127.0.0.1:1", timeout=1)
    assert result["target"] == "127.0.0.1:1" and "error" in result


def test_failed_ssh_logins_parses_counts_per_source() -> None:
    client = CronHostsClient([CronHost(user="root", host="pi"), CronHost(user="brad", host="server")])

    def fake_run(host: CronHost, command: str, input_text: str | None = None) -> str:
        assert "Failed password" in command
        if host.host == "server":
            from app.cron.client import CronHostError

            raise CronHostError("Timed out connecting to brad@server")
        return "     41 from 203.0.113.9\n      3 from 198.51.100.7\n__done__\n"

    with patch.object(client, "_run", side_effect=fake_run):
        results = checks.failed_ssh_logins(client)
    assert results[0] == {"host": "root@pi", "failed": 44, "top_sources": [{"ip": "203.0.113.9", "attempts": 41}, {"ip": "198.51.100.7", "attempts": 3}]}
    assert "error" in results[1]


def test_trivy_scan_summarises_severities(tmp_path: Path) -> None:
    report = {"Results": [{"Vulnerabilities": [
        {"VulnerabilityID": "CVE-1", "Severity": "CRITICAL", "PkgName": "openssl", "InstalledVersion": "3.0.1", "FixedVersion": "3.0.2"},
        {"VulnerabilityID": "CVE-2", "Severity": "HIGH", "PkgName": "zlib", "InstalledVersion": "1.2"},
    ]}]}
    fake = tmp_path / "trivy"
    fake.write_text(f"#!/bin/sh\necho '{json.dumps(report)}'\n")
    fake.chmod(0o755)
    result = checks.scan_image("plex:latest", binary=str(fake))
    assert result == {"image": "plex:latest", "critical": 1, "high": 1, "fixable": 1, "examples": ["CVE-1 in openssl 3.0.1", "CVE-2 in zlib 1.2"]}


# -- backups


def test_backup_evaluation_flags_failed_stale_and_missing() -> None:
    zone = ZoneInfo("Australia/Melbourne")
    now = datetime(2026, 10, 1, 9, 0, tzinfo=zone)
    tasks = [
        {"name": "Photos to B2", "last_bkp_result": "done", "last_bkp_success_time": "2026/10/01 03:10"},
        {"name": "Docs to USB", "last_bkp_result": "failed", "last_bkp_end_time": "2026/10/01 02:00"},
        {"name": "VMs", "last_bkp_result": "done", "last_bkp_success_time": "2026/09/27 03:00"},
        {"name": "Never ran"},
        {"name": "Running now", "state": "backingup"},
    ]
    result = {t["name"]: t for t in backups.evaluate(tasks, now, 26, zone)}
    assert result["Photos to B2"]["ok"] is True
    assert result["Docs to USB"]["problem"] == "last run failed"
    assert result["VMs"]["problem"] == "last successful run was 102 hours ago"
    assert result["Never ran"]["problem"] == "no successful run on record"
    assert result["Running now"]["ok"] is True


def _synology(tasks: list[dict[str, Any]], statuses: dict[int, dict[str, Any]]) -> SynologyClient:
    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if params.get("method") == "list":
            return httpx.Response(200, json={"success": True, "data": {"tasks": tasks}})
        if params.get("method") == "status":
            return httpx.Response(200, json={"success": True, "data": statuses[int(params["task_id"])]})
        return httpx.Response(200, json={"success": False})

    return SynologyClient(base_url="http://nas", username="u", password="p", transport=httpx.MockTransport(handler))


def test_backup_check_and_security_briefing_through_the_tools(client: TestClient) -> None:
    siris = SirisAI(settings=Settings(timezone="UTC"))
    siris._synology_client = _synology(
        [{"task_id": 1, "name": "Photos"}, {"task_id": 2, "name": "Docs"}],
        {1: {"last_bkp_result": "done", "last_bkp_success_time": (datetime.now(UTC) - timedelta(hours=3)).strftime("%Y/%m/%d %H:%M")},
         2: {"last_bkp_result": "failed"}},
    )
    from app.skills.base import SkillTool
    from app.skills.builtin.security import BackupCheckSkill

    siris.tools.register(SkillTool(BackupCheckSkill(), siris))
    backup = siris.run_tool("backup_check", {})
    assert [t["ok"] for t in backup["tasks"]] == [True, False]
    briefing = siris.run_tool("security_briefing", {})
    assert briefing["urgent"] == ["Backup 'Docs' needs attention: last run failed"]
    assert "Security check: Backup 'Docs'" in briefing["summary"]


def test_security_watch_pushes_urgent_and_queues_the_rest(client: TestClient, tmp_path: Path) -> None:
    port, stop = _tls_server(tmp_path, days=20)
    try:
        siris = SirisAI(settings=Settings(security_cert_hosts=f"127.0.0.1:{port}", security_cert_warn_days=30, ntfy_topic="t"))
        siris._synology_client = _synology([{"task_id": 1, "name": "VMs"}], {1: {"last_bkp_result": "failed"}})
        sent: list[dict[str, Any]] = []

        class Pushes:
            def send(self, message: str, title: str | None = None, actions: Any = None, priority: str | None = None) -> None:
                sent.append({"message": message, "priority": priority})

            def close(self) -> None:
                pass

        siris.tools.get("notify")._client = Pushes()  # type: ignore[union-attr]
        siris.pending_notifications.clear()
        report = run_security_watch(siris)
    finally:
        stop.set()
    assert report is not None
    assert sent == [{"message": "Backup 'VMs' needs attention: last run failed", "priority": "urgent"}]
    queued = [n["message"] for n in siris.pending_notifications.list_pending() if n["category"] == "security"]
    assert len(queued) == 1
    assert "expires in 19 days" in queued[0] or "expires in 20 days" in queued[0]


def test_all_clear_when_nothing_is_configured(client: TestClient) -> None:
    report = SirisAI(settings=Settings()).run_tool("security_briefing", {})
    assert report["summary"] == "Security check: all clear."
