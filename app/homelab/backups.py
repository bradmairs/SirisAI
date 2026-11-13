"""Backups on the SSH servers (SIRISAI_SERVER_BACKUPS), checked daily, and a
monthly test that a file can actually be read back out of each.

Each entry is `server:kind:target`, comma-separated:
- `nas:zfs:tank/data` -- the newest snapshot of that dataset;
- `nas:path:/mnt/backups/restic` -- the newest file under that folder (a
  restic or Borg repository, rsync target, or a folder of dumps).

**Daily** (SIRISAI_SERVER_BACKUPS_HOUR_LOCAL, 8am): a backup whose newest
snapshot or file is older than SIRISAI_SERVER_BACKUP_MAX_AGE_HOURS (26) is
raised in the attention feed, urgently past twice that. One with nothing in
it, or that can't be read, is raised too.

**Monthly** (on the 1st): picks a random file (under 2 GB) from the newest
snapshot (or the folder) and reads all of it, then (6.58) restores it to a
scratch folder and checks the copy's checksum, and for a ZFS snapshot
compares it with the live file (the same, changed since, or gone). Archives are listed (`tar -t`) or tested
(`gzip -t`) rather than just read. This proves the backup can be read back,
not that every file in it is intact. A failed test is urgent; a passed one is
noted in the feed.
"""

import logging
import shlex
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.hub.attention import action
from app.ssh.client import SshError

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)

STATE_KEY = "server_backups"
KINDS = ("zfs", "path")


@dataclass(frozen=True)
class Backup:
    server: str
    kind: str
    target: str

    @property
    def label(self) -> str:
        return f"{self.server}:{self.target}"


def parse(spec: str) -> list[Backup]:
    out = []
    for part in (p.strip() for p in (spec or "").split(",")):
        if not part:
            continue
        server, _, rest = part.partition(":")
        kind, _, target = rest.partition(":")
        if kind not in KINDS or not server or not target:
            raise ValueError(f"Backup '{part}' should be server:zfs:dataset or server:path:/folder")
        out.append(Backup(server.strip(), kind, target.strip()))
    return out


def newest_command(backup: Backup) -> str:
    """Prints '<epoch seconds> <what>' for the newest snapshot or file, or nothing."""
    t = shlex.quote(backup.target)
    if backup.kind == "zfs":
        return f"zfs list -H -p -t snapshot -o creation,name -s creation -d 1 {t} | tail -n 1"
    return f"find {t} -type f -printf '%T@ %p\\n' 2>/dev/null | sort -n | tail -n 1"


def test_command(backup: Backup) -> str:
    """Reads one random file out of the newest snapshot or the folder."""
    t = shlex.quote(backup.target)
    if backup.kind == "zfs":
        where = (
            f"snap=$(zfs list -H -t snapshot -o name -s creation -d 1 {t} | tail -n 1 | cut -d@ -f2); "
            f"mnt=$(zfs get -H -o value mountpoint {t}); "
            '[ -n "$snap" ] || { echo "##none"; exit 0; }; dir="$mnt/.zfs/snapshot/$snap"; echo "##from $snap"; '
        )
    else:
        where = f"dir={t}; mnt=; "
    return (
        where
        + 'f=$(find "$dir" -type f -size +0c -size -2048M 2>/dev/null | shuf -n 1); [ -n "$f" ] || { echo "##none"; exit 0; }; '
        + 'echo "##file $f"; echo "##bytes $(wc -c < "$f")"; '
        + 'case "$f" in *.tar|*.tar.gz|*.tgz|*.tar.zst|*.tar.xz) tar -tf "$f" >/dev/null 2>&1 && echo "##ok listed the archive" || echo "##failed the archive is unreadable";; '
        + '*.gz) gzip -t "$f" 2>/dev/null && echo "##ok gzip test passed" || echo "##failed gzip test failed";; '
        + '*) cat "$f" >/dev/null 2>&1 && echo "##ok read it all" || echo "##failed couldn\'t read it";; esac; '
        # 6.58: restore it to a scratch folder and check the copy, then (a snapshot) against the live file.
        + 'tmp=$(mktemp -d 2>/dev/null) && cp -p "$f" "$tmp/r" 2>/dev/null && a=$(sha256sum < "$f" | cut -c1-64) '
        + '&& b=$(sha256sum < "$tmp/r" | cut -c1-64) && [ "$a" = "$b" ] && echo "##restored $a" || echo "##restorefailed"; '
        + '[ -n "$tmp" ] && rm -rf "$tmp"; '
        + 'if [ -n "$mnt" ] && [ -n "$a" ]; then live="$mnt/${f#$dir/}"; if [ ! -f "$live" ]; then echo "##live missing"; '
        + 'elif [ "$(sha256sum < "$live" | cut -c1-64)" = "$a" ]; then echo "##live same"; '
        + 'elif [ "$live" -nt "$f" ]; then echo "##live changed"; else echo "##live differs"; fi; fi'
    )


def parse_test(stdout: str) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": False}
    for line in stdout.splitlines():
        tag, _, rest = line.partition(" ")
        if tag == "##none":
            out["why"] = "nothing to test"
        elif tag == "##from":
            out["snapshot"] = rest
        elif tag == "##file":
            out["file"] = rest
        elif tag == "##bytes" and rest.strip().isdigit():
            out["bytes"] = int(rest)
        elif tag == "##ok":
            out["ok"], out["how"] = True, rest
        elif tag == "##failed":
            out["why"] = rest
        elif tag == "##restored":
            out["restored"] = True
        elif tag == "##restorefailed":
            out["restored"] = False
        elif tag == "##live":
            out["live"] = rest.strip()
    # 6.58: a copy that doesn't match, or a snapshot that differs from a live file older than it, is a failed test.
    if out.get("ok") and out.get("restored") is False:
        out["ok"], out["why"] = False, "restoring it to a scratch folder didn't give an identical copy"
    if out.get("ok") and out.get("live") == "differs":
        out["ok"], out["why"] = False, "the snapshot's copy differs from the live file, which hasn't changed since"
    return out


def _size(n: int | None) -> str:
    n = n or 0
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} bytes"


def check(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    max_age = siris.settings.server_backup_max_age_hours
    report: dict[str, Any] = {}
    wanted: list[dict[str, Any]] = []
    for backup in parse(siris.settings.server_backups):
        key = f"server-backups:{backup.label}"
        try:
            result = siris.ssh_client.run(backup.server, newest_command(backup), timeout=120)  # type: ignore[union-attr]
        except (SshError, LookupError, AttributeError) as exc:
            report[backup.label] = {"error": str(exc)[:300]}
            wanted.append(_item(key, f"Couldn't check the backup {backup.label}", str(exc)[:300], "attention"))
            continue
        line = result["stdout"].strip().splitlines()[-1] if result["stdout"].strip() else ""
        stamp, _, what = line.partition(" " if backup.kind == "path" else "\t")
        try:
            when = datetime.fromtimestamp(float(stamp), UTC)
        except ValueError:
            error = (result["stderr"] or "").strip()[-200:]
            report[backup.label] = {"error": error or "nothing found"}
            title = f"No backups found in {backup.label}" if not error else f"Couldn't check the backup {backup.label}"
            wanted.append(_item(key, title, error or f"There's no snapshot or file in {backup.target} on {backup.server}.", "urgent" if not error else "attention"))
            continue
        hours = (now - when).total_seconds() / 3600
        report[backup.label] = {"newest": when.isoformat(), "hours_old": round(hours, 1), "what": what.strip()}
        if max_age and hours > max_age:
            ago = f"{hours / 24:.1f} days" if hours >= 48 else f"{hours:.0f} hours"
            wanted.append(
                _item(
                    key,
                    f"The backup {backup.label} is {ago} old",
                    f"Its newest {'snapshot' if backup.kind == 'zfs' else 'file'} is {what.strip()} from {when:%a %d %b %H:%M} UTC. "
                    f"Backups should be no more than {max_age:g} hours old: check the job that makes it.",
                    "urgent" if hours > 2 * max_age else "attention",
                )
            )
    siris.attention.sync("server-backups", wanted, outcome="Backups are current")
    state = dict(siris.state.get(STATE_KEY) or {})
    state["last_check"] = {"at": now.isoformat(), "report": report}
    siris.state.set(STATE_KEY, state)
    return report


def _item(key: str, title: str, body: str, severity: str) -> dict[str, Any]:
    return {"key": key, "kind": "alert", "title": title, "body": body, "severity": severity, "actions": [action("acknowledge", "Got it")]}


def restore_test(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    report: dict[str, Any] = {}
    for backup in parse(siris.settings.server_backups):
        try:
            result = siris.ssh_client.run(backup.server, test_command(backup), timeout=900)  # type: ignore[union-attr]
            found = parse_test(result["stdout"])
            if not found["ok"] and "why" not in found:
                found["why"] = (result["stderr"] or "").strip()[-200:] or f"exit {result['exit_code']}"
        except (SshError, LookupError, AttributeError) as exc:
            found = {"ok": False, "why": str(exc)[:300]}
        report[backup.label] = found
        where = f" from snapshot {found['snapshot']}" if found.get("snapshot") else ""
        if found["ok"]:
            siris.attention.raise_item(
                f"server-backups:test:{backup.label}:{now:%Y-%m}",
                "alert",
                f"Backup test passed: {backup.label}",
                source="server-backups-test",
                severity="info",
                body=f"Read {found.get('file')} ({_size(found.get('bytes'))}){where}: {found.get('how')}."
                + (" Restored it to a scratch folder and the copy matched." if found.get("restored") else "")
                + {"same": " It matches the live file.", "changed": " The live file has changed since, as expected.",
                   "missing": " The live file has gone since (deleted or moved)."}.get(found.get("live") or "", "")
                + " This proves the backup can be restored, not that every file in it is intact.",
            )
        else:
            siris.attention.raise_item(
                f"server-backups:test:{backup.label}:{now:%Y-%m}",
                "alert",
                f"Backup test failed: {backup.label}",
                source="server-backups-test",
                severity="urgent",
                body=f"Reading a file back{where} didn't work: {found.get('why')}" + (f" ({found['file']})" if found.get("file") else ""),
            )
    state = dict(siris.state.get(STATE_KEY) or {})
    state["last_test"] = {"at": now.isoformat(), "report": report}
    siris.state.set(STATE_KEY, state)
    return report
