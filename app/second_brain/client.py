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
        self._maybe_pull()
        with self._lock:
            results = self._engine_call(["learn", "--json", "-", "--output", "json"], stdin=json.dumps(items))
            if self.git_sync:
                self._commit(results)
        if self.git_sync:
            self._schedule_push()
        return results

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

    def render_map(self) -> str:
        """The vault's interactive mind map as one self-contained HTML page,
        drawn fresh from the notes as they are now."""
        self._maybe_pull()
        with self._lock:
            self._engine_call(["graph"], raw=True)
            return (self.path / "_site" / "index.html").read_text(encoding="utf-8")

    def learned_today(self, date: str) -> list[dict[str, str]]:
        """What the brain learned or updated on `date` (YYYY-MM-DD), read from
        that day's daily note log ("🧠 Learned [[X]]" lines the engine writes)."""
        note = self.get_note(date)
        if note is None or note.get("title") != date:
            return []
        seen: dict[str, str] = {}
        for action, title in re.findall(r"🧠 (Learned|Updated) \[\[([^\]|#]+)", note.get("body", "")):
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

    def _commit(self, results: list[dict]) -> None:
        self._git("add", "-A")
        titles = ", ".join(dict.fromkeys(r["title"] for r in results)) or "notes"
        proc = self._git("commit", "--quiet", "-m", f"siris: learned {titles}"[:200])
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
