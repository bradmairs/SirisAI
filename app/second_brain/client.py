"""Client for Siris' Second Brain -- Brad's Markdown knowledge vault
(https://github.com/bradmairs/Siris-Second-Brain), checked out on disk.

Not an HTTP API, unlike every other integration here: the vault is a folder
of Obsidian-compatible notes that ships its own engine, `tools/brain.py`
(stdlib-only Python), which owns how notes are named, linked and updated.
This client shells out to that engine rather than re-implementing it, so
the vault's linking rules live in exactly one place and SirisAI never
writes a note file itself:

- `search`/`get_note` are read-only.
- `learn` is additive: a new title creates a linked note, an existing
  title (or alias -- "Dom" finds "Dom Mairs") gets a dated update
  appended. Nothing is ever overwritten, which is why the tool wrapping it
  can be SAFE_ACTION where obsidian_create_or_update_note (a full PUT
  replace) is CONFIRM_REQUIRED.
- `file_note`/`clear_memory_inbox`/`relink` are the nightly upkeep's
  writes (app/second_brain/jobs.py): they move inbox captures to where
  the router says they belong and archive filed memory entries, never
  deleting anything.

Optional git sync (`git_sync=True`): reads and writes always use the
local checkout straight away. At most every `pull_interval` seconds, one of
them also starts a pull on a background thread, and each learn makes a
local commit and starts a push on a background thread, so a reply never
waits on the network. A push that meets someone else's commits (a Claude
Code session, another device) rebases onto them once. Sync failures never
fail a read or a write -- the local checkout stays usable and the error is
kept on `last_sync_error` for the health check.
"""

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

Runner = Callable[..., subprocess.CompletedProcess]


class SecondBrainError(RuntimeError):
    """The engine refused or failed a request (bad vault path, engine error)."""


class SecondBrainClient:
    def __init__(
        self,
        path: str,
        git_sync: bool = False,
        pull_interval: float = 60.0,
        python: str = sys.executable,
        timeout: float = 30.0,
        author_name: str = "SirisAI",
        author_email: str = "sirisai@localhost",
        engine_env: dict[str, str] | None = None,
        runner: Runner = subprocess.run,
    ) -> None:
        self.path = Path(path)
        self.git_sync = git_sync
        self._pull_interval = pull_interval
        self._python = python
        self._timeout = timeout
        self._author = (author_name, author_email)
        self._run = runner
        # e.g. BRAIN_EMBED_URL / BRAIN_EMBED_MODEL, so the engine links by meaning with the same Ollama.
        self._engine_env = dict(engine_env or {})
        self._lock = threading.Lock()  # one engine write or git operation at a time
        self._last_pull = 0.0
        self._push_pending = threading.Event()
        self._push_thread: threading.Thread | None = None
        self._pull_thread: threading.Thread | None = None
        self.last_sync_error: str | None = None

    @property
    def engine(self) -> Path:
        return self.path / "tools" / "brain.py"

    # ---------- engine ----------

    def _engine_call(self, args: list[str], stdin: str | None = None, raw: bool = False) -> Any:
        if not self.engine.exists():
            raise SecondBrainError(
                f"No brain engine at {self.engine} -- is SIRISAI_BRAIN_PATH a checkout of Siris-Second-Brain?"
            )
        proc = self._run(
            [self._python, str(self.engine), *args],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=self._timeout,
            cwd=str(self.path),
            env={**os.environ, **self._engine_env},
        )
        if proc.returncode != 0:
            raise SecondBrainError((proc.stderr or proc.stdout or f"brain.py exited {proc.returncode}").strip())
        return proc.stdout if raw else json.loads(proc.stdout)

    def search(self, query: str, limit: int = 10) -> list[dict]:
        """Notes matching `query`, best first: each has `title`, `path`
        (cite it), `type`, `tags` and readable `excerpts`."""
        self._maybe_pull()
        # Options before "--" so a query starting with "-" is never read as a flag.
        return self._engine_call(["search", "--limit", str(limit), "--output", "json", "--", query])

    def get_note(self, title: str) -> dict | None:
        """One note -- body, frontmatter, aliases, tags, links out and
        backlinks -- by title, alias, or a close title match. None if
        nothing matches."""
        self._maybe_pull()
        return self._engine_call(["show", "--output", "json", "--", title])

    def learn(self, items: list[dict[str, Any]]) -> list[dict]:
        """Add knowledge: each item is {title, content, tags?, source?,
        type?, links?, aliases?}. Returns what the engine did per item
        (`action` created/updated, `title`, `path`, `links`)."""
        results = self._write(["learn", "--json", "-", "--output", "json"], stdin=json.dumps(items),
                              message=lambda r: "siris: learned " + (", ".join(dict.fromkeys(x["title"] for x in r)) or "notes"))
        return results

    def _write(self, args: list[str], message: Callable[[Any], str], stdin: str | None = None, raw: bool = False) -> Any:
        """Run an engine command that changes the vault, then commit and push it."""
        self._maybe_pull()
        with self._lock:
            result = self._engine_call(args, stdin=stdin, raw=raw)
            if self.git_sync:
                self._commit(message(result))
        if self.git_sync:
            self._schedule_push()
        return result

    ATTACHMENTS = "Attachments"

    def save_attachment(self, name: str, data: bytes) -> str:
        """Write a file (a captured photo) into the vault's Attachments/
        folder and return its path inside the vault, for an Obsidian embed
        (`![[Attachments/name]]`). It's committed with the next write."""
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in name).strip("-.") or "attachment"
        with self._lock:
            folder = self.path / self.ATTACHMENTS
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / safe
            stem, suffix, n = target.stem, target.suffix, 2
            while target.exists():
                target = folder / f"{stem}-{n}{suffix}"
                n += 1
            target.write_bytes(data)
        return f"{self.ATTACHMENTS}/{target.name}"

    def inbox(self) -> dict:
        """What's waiting in 00-Inbox: `memory` (the memory inbox's entries: `id`,
        `date`, `text`, `plain`) and `notes` (other captures: `title`, `path`, `type`,
        `created`, `source`, `routing`, `text`)."""
        self._maybe_pull()
        return self._engine_call(["inbox", "--output", "json"])

    def activity(self, days: int) -> list[dict]:
        """The vault's activity log for the last `days` days ({date, at, line}),
        e.g. "🧠 Learned [[X]]". Empty for a vault from before the log."""
        self._maybe_pull()
        try:
            return self._engine_call(["log", "--days", str(days), "--output", "json"])
        except SecondBrainError:
            return []

    def review_queue(self, count: int = 3) -> list[dict]:
        """Notes Siris wrote that Brad hasn't checked, most connected first:
        `title`, `path`, `links`, `as_of` and an `excerpt`."""
        self._maybe_pull()
        return self._engine_call(["review", "--count", str(count), "--output", "json"])

    def mark_reviewed(self, title: str) -> dict:
        """Brad read this note and it's right; it leaves the review queue."""
        return self._write(["review", "--output", "json", "--", title],
                           message=lambda r: f"siris: {r.get('title', title)} reviewed by Brad")

    def recall_check(self) -> dict:
        """The vault's recall check (`brain.py eval --record`): how many of its
        known questions search answers with the right note in the top 3
        (`score`, `found`, `total`, `misses`). Recorded for the mind map."""
        return self._write(["eval", "--record", "--output", "json"],
                           message=lambda r: f"siris: recall check {round(r.get('score', 0) * 100)}%")

    def file_note(self, title: str, type: str = "auto", into: str = "", folder: str = "", new_title: str = "") -> dict:
        """File one inbox capture where the router says it belongs, or merge it
        into the existing note `into`. `new_title` files it under a better name
        (the old one stays an alias). `action` is filed (moved), merged (added
        to that note; the capture is archived), left (unsure, with `reasons`)
        or missing."""
        args = ["file", "--type", type, "--output", "json"] + (["--into", into] if into else []) \
            + (["--folder", folder] if folder else []) + (["--title", new_title] if new_title else [])
        return self._write([*args, "--", title],
                           message=lambda r: f"siris: filed {r.get('title', title)}")

    def archive_split(self, title: str, into: list[str]) -> dict:
        """An inbox capture whose pieces were added to the notes `into`
        (the nightly sort's split): moved to 04-Archive/Inbox with
        `split_into` recording where they went. `action` is split, left
        or missing."""
        args = ["file", "--output", "json"] + [a for t in into for a in ("--split-into", t)]
        return self._write([*args, "--", title], message=lambda r: f"siris: split {r.get('title', title)}")

    def clear_memory_inbox(self, filed: list[dict[str, str]]) -> dict:
        """Archive memory-inbox entries ({"id", "filed_to"}) once they're filed.
        They move to 04-Archive/Inbox; nothing is deleted."""
        return self._write(["clear-memory-inbox", "--json", "-", "--output", "json"], stdin=json.dumps(filed),
                           message=lambda r: f"siris: filed {r.get('archived', 0)} memory inbox entries")

    def link(self, a: str, b: str) -> dict:
        """Connect two notes both ways, acting on an insights "could be linked"
        suggestion. `action` is linked or already-linked; an unknown note raises."""
        return self._write(["link", "--output", "json", "--", a, b],
                           message=lambda r: f"siris: linked {r.get('a', a)} and {r.get('b', b)}")

    def unrelate(self, a: str, b: str) -> dict:
        """Dismiss a "could be linked" suggestion: the pair is remembered in the
        vault and never suggested again. Neither note changes."""
        return self._write(["unrelate", "--output", "json", "--", a, b],
                           message=lambda r: f"siris: {r.get('a', a)} and {r.get('b', b)} aren't related")

    def duplicates(self) -> list[dict]:
        """Pairs of notes that look like one topic written twice: [{a, b, score, why}]."""
        self._maybe_pull()
        return self._engine_call(["duplicates", "--output", "json"])

    def merge(self, source: str, into: str) -> dict:
        """Fold note `source` into note `into` (same topic): its text becomes a dated
        "Merged from" update, links to it point at `into`, and it's archived in
        04-Archive/Merged. Its title stays findable as an alias. Unknown notes raise."""
        return self._write(["merge", "--output", "json", "--", source, into],
                           message=lambda r: f"siris: merged {r.get('from', source)} into {r.get('into', into)}")

    def distinct(self, a: str, b: str) -> dict:
        """Two notes that look alike are different things: never suggest merging them again."""
        return self._write(["distinct", "--output", "json", "--", a, b],
                           message=lambda r: f"siris: {r.get('a', a)} and {r.get('b', b)} are different notes")

    def project_pages(self) -> list[dict]:
        """Refresh the page for each project a #project-list note names (GVW Projects):
        [{project, action created/updated/unchanged, path, mentions, tasks, people}]."""
        return self._write(["project-pages", "--apply", "--output", "json"],
                           message=lambda r: "siris: refreshed project pages ("
                           + ", ".join(p["project"] for p in r if p["action"] != "unchanged")[:150] + ")")

    def autolink(self) -> dict:
        """Link every pair of notes the engine is confident about (a calibrated
        similarity threshold, 80% by default); unsure pairs stay as insights'
        suggestions. Returns `linked` [{a, b, confidence, why}], `deferred` and
        `thresholds`."""
        return self._write(["autolink", "--output", "json"],
                           message=lambda r: f"siris: auto-linked {len(r.get('linked') or [])} note pairs")

    def done(self, note: str, task: str | None = None) -> dict:
        """Mark something done: tick `task` in `note` (`- [x] … ✅ date`), or, with no
        task, mark the project itself finished, which takes its deadline and leftover
        tasks out of insights. `action` is done, project-done or already-done; an
        unknown note or task raises."""
        args = ["done", "--output", "json", "--", note, *([task] if task else [])]
        return self._write(args, message=lambda r: f"siris: done {r.get('task') or r.get('note', note)}")

    def unlink(self, a: str, b: str) -> dict:
        """Disconnect two notes both ways (connection bullets removed, inline links
        turned back into plain words) and remember they're not related, so nothing
        automatic links them again. `action` is unlinked or not-linked."""
        return self._write(["unlink", "--output", "json", "--", a, b],
                           message=lambda r: f"siris: unlinked {r.get('a', a)} and {r.get('b', b)}")

    def delete_note(self, title: str, permanent: bool = False) -> dict:
        """Delete a note Brad asked to delete. It moves to the vault's .deleted/
        folder (out of search, links and the map) and links to it in other notes
        are removed; restore_note() undoes it. `permanent` removes the file
        instead (it stays in git history). Unknown notes and the brain's hubs
        raise SecondBrainError."""
        args = ["delete", "--output", "json", *(["--permanent"] if permanent else []), "--", title]
        return self._write(args, message=lambda r: f"siris: deleted {r.get('title', title)}"
                           + (" permanently" if permanent else ""))

    def restore_note(self, title: str) -> dict:
        """Bring back a deleted note where it was, reconnecting the notes that linked to it."""
        return self._write(["restore", "--output", "json", "--", title],
                           message=lambda r: f"siris: restored {r.get('title', title)}")

    def purge_note(self, title: str) -> dict:
        """Permanently remove a note that's already deleted (it stays in git history)."""
        return self._write(["purge", "--output", "json", "--", title],
                           message=lambda r: f"siris: deleted {r.get('title', title)} permanently")

    def deleted_notes(self) -> list[dict]:
        """Deleted notes, newest first: `title`, `type`, `path`, `date`, `linked_from`, `restorable`."""
        self._maybe_pull()
        return self._engine_call(["deleted", "--output", "json"])

    def relink(self) -> int:
        """Link plain mentions of note titles across the vault; returns how many links were added."""
        out = self._write(["relink"], raw=True, message=lambda r: "siris: relink")
        match = re.search(r"Added (\d+) links", out or "")
        return int(match.group(1)) if match else 0

    def route(self, title: str, content: str, type: str = "auto") -> dict:
        """Where the vault's router would file this, without writing:
        `action` (create/update), `title`, `type`, `folder`, `areas`,
        `confidence` and `reasons`."""
        self._maybe_pull()
        return self._engine_call(["route", "--type", type, "--output", "json", "-c", content, "--", title])

    def list_notes(self) -> list[dict]:
        """Every note's `title`, `type`, `aliases` and `path`."""
        self._maybe_pull()
        return self._engine_call(["list", "--output", "json"])

    def gaps(self) -> list[dict]:
        """What the vault knows it doesn't know: `title`, `kind`
        (fill_in / confirm / unwritten) and `detail`."""
        self._maybe_pull()
        return self._engine_call(["gaps", "--output", "json"])

    def insights(self, days: int = 30) -> dict:
        """What the vault can tell about itself, worked out by the engine
        without an LLM: `highlights` (ready-made sentences), `summary`
        counts, daily `activity`, growing `topics`, `hubs`, `deadlines`,
        `overdue_tasks`, `stale_projects`, `inbox`, `gaps` and
        `suggested_links` (notes that read alike but aren't linked)."""
        self._maybe_pull()
        return self._engine_call(["insights", "--days", str(days), "--output", "json"])

    def tasks(self) -> list[dict]:
        """Every open task in the vault, soonest due first: {task, note, due,
        type}. Needs the vault engine 1.41 or later (`brain.py tasks`)."""
        self._maybe_pull()
        return self._engine_call(["tasks", "--output", "json"])

    def render_map(self) -> str:
        """The vault's interactive mind map as one self-contained HTML page,
        drawn fresh from the notes as they are now."""
        self._maybe_pull()
        with self._lock:
            self._engine_call(["graph"], raw=True)
            return (self.path / "_site" / "index.html").read_text(encoding="utf-8")

    def learned_today(self, date: str) -> list[dict[str, str]]:
        """What the brain learned or updated on `date` (YYYY-MM-DD), from the
        "🧠 Learned [[X]]" lines in the vault's activity log (`brain.py log`).
        A vault from before the activity log kept them in that day's daily
        note instead, so that's read when the engine has no `log` command."""
        self._maybe_pull()
        try:
            text = "\n".join(e.get("line", "") for e in self._engine_call(["log", "--date", date, "--output", "json"]))
        except SecondBrainError:
            note = self.get_note(date)
            if note is None or note.get("title") != date:
                return []
            text = note.get("body", "")
        seen: dict[str, str] = {}
        for action, title in re.findall(r"🧠 (Learned|Updated) \[\[([^\]|#]+)", text):
            seen.setdefault(title, action.lower())
        return [{"title": t, "action": a} for t, a in seen.items()]

    # ---------- git sync ----------

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        name, email = self._author
        return self._run(
            ["git", "-C", str(self.path), "-c", f"user.name={name}", "-c", f"user.email={email}", *args],
            capture_output=True,
            text=True,
            timeout=self._timeout,
        )

    def _sync_failed(self, what: str, proc: subprocess.CompletedProcess) -> None:
        self.last_sync_error = f"git {what} failed: {(proc.stderr or proc.stdout).strip()[:300]}"
        logger.warning("Second brain %s", self.last_sync_error)

    def _maybe_pull(self) -> None:
        """Start a background pull if one is due and none is running."""
        if not self.git_sync or time.monotonic() - self._last_pull < self._pull_interval:
            return
        if self._pull_thread is not None and self._pull_thread.is_alive():
            return
        self._last_pull = time.monotonic()  # claim this interval now, so callers don't start a second pull
        self._pull_thread = threading.Thread(target=self._locked_pull, name="second-brain-pull", daemon=True)
        self._pull_thread.start()

    def _locked_pull(self) -> None:
        with self._lock:
            self._pull()

    def _pull(self) -> None:
        proc = self._git("pull", "--rebase", "--autostash", "--quiet")
        self._last_pull = time.monotonic()
        if proc.returncode != 0:
            self._sync_failed("pull", proc)

    def _commit(self, message: str) -> None:
        self._git("add", "-A")
        proc = self._git("commit", "--quiet", "-m", message[:200])
        if proc.returncode != 0 and "nothing to commit" not in (proc.stdout + proc.stderr):
            self._sync_failed("commit", proc)

    def _schedule_push(self) -> None:
        """Coalesce pushes: a learn while a push is running just marks
        another push as due, rather than queueing one push per write."""
        self._push_pending.set()
        if self._push_thread is None or not self._push_thread.is_alive():
            self._push_thread = threading.Thread(target=self._push_loop, name="second-brain-push", daemon=True)
            self._push_thread.start()

    def _push_loop(self) -> None:
        while self._push_pending.is_set():
            self._push_pending.clear()
            with self._lock:
                proc = self._git("push", "--quiet")
                if proc.returncode != 0:
                    # Someone else pushed first (a Claude Code session, another device): rebase onto it once.
                    self._pull()
                    proc = self._git("push", "--quiet")
                if proc.returncode == 0:
                    self.last_sync_error = None
                else:
                    self._sync_failed("push", proc)

    def flush(self, timeout: float = 30.0) -> None:
        """Wait for background pulls and pushes to finish (tests, shutdown)."""
        for thread in (self._pull_thread, self._push_thread):
            if thread is not None:
                thread.join(timeout)
        # A push started after a pull we just waited on: wait for that too.
        if self._push_thread is not None:
            self._push_thread.join(timeout)

    def close(self) -> None:
        self.flush(timeout=10.0)
