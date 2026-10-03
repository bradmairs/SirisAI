"""Read-only GitHub activity for the second brain's nightly upkeep
(app/second_brain/jobs.py): which of Brad's repos moved since the last run,
and what changed in them -- merged pull requests and commits on the default
branch -- so each project's note gets a dated update in plain words.

Only ever reads. A fine-grained token with read access to contents, pull
requests and metadata (SIRISAI_GITHUB_TOKEN) is enough, and is needed for
private repos; without one only public repos are visible, so the upkeep
job skips this step rather than report half the picture.
"""

from datetime import datetime
from typing import Any

import httpx

API = "https://api.github.com"


class GitHubActivityClient:
    def __init__(self, token: str, owner: str, transport: httpx.BaseTransport | None = None, timeout: float = 20.0) -> None:
        self.owner = owner
        self._http = httpx.Client(
            base_url=API,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
            timeout=timeout,
            transport=transport,
        )

    def _get(self, path: str, **params: Any) -> Any:
        response = self._http.get(path, params=params)
        response.raise_for_status()
        return response.json()

    def repos_pushed_since(self, since: datetime) -> list[dict]:
        """The owner's own (not forked, not archived) repos pushed to since `since`, newest first."""
        repos = self._get("/user/repos", affiliation="owner", sort="pushed", direction="desc", per_page=50)
        return [r for r in repos
                if r.get("owner", {}).get("login", "").lower() == self.owner.lower()
                and not r.get("fork") and not r.get("archived")
                and r.get("pushed_at") and datetime.fromisoformat(r["pushed_at"].replace("Z", "+00:00")) >= since]

    def activity(self, repo: dict, since: datetime) -> dict:
        """What happened in one repo since `since`: merged PRs (title, body, link) and
        non-merge commits on the default branch (first line of the message, link)."""
        full = repo["full_name"]
        stamp = since.isoformat().replace("+00:00", "Z")
        commits = [
            {"sha": c["sha"][:7], "message": (c["commit"]["message"] or "").splitlines()[0][:200], "url": c["html_url"]}
            for c in self._get(f"/repos/{full}/commits", since=stamp, per_page=50)
            if len(c.get("parents") or []) < 2
        ]
        merged = [
            {"number": p["number"], "title": p["title"], "body": (p.get("body") or "")[:800], "url": p["html_url"]}
            for p in self._get(f"/repos/{full}/pulls", state="closed", sort="updated", direction="desc", per_page=30)
            if p.get("merged_at") and datetime.fromisoformat(p["merged_at"].replace("Z", "+00:00")) >= since
        ]
        return {"repo": repo["name"], "full_name": full, "url": repo["html_url"],
                "description": repo.get("description") or "", "commits": commits, "merged_prs": merged}

    def close(self) -> None:
        self._http.close()
