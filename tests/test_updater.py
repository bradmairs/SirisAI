"""deploy/updater.sh, run against stand-in git, docker and curl: it applies a
requested update, refuses with local edits, and rolls back when the new
version isn't healthy or its tool-selection eval got worse."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

FAKE_GIT = r"""#!/usr/bin/env bash
echo "git $*" >> "$FAKE_STATE/calls"
args="$*"
case "$args" in
  *"status --porcelain"*) [ -f "$FAKE_STATE/dirty" ] && echo " M app/main.py"; exit 0 ;;
  *"rev-parse HEAD"*) echo "old-sha"; exit 0 ;;
  *"rev-parse -q --verify refs/tags/v"*) [ -f "$FAKE_STATE/no_tag" ] && exit 1; exit 0 ;;
  *"merge -q --ff-only v"*) echo "${args##*--ff-only v}" > "$FAKE_STATE/version"; exit 0 ;;
  *"reset -q --hard"*) cat "$FAKE_STATE/from" > "$FAKE_STATE/version"; touch "$FAKE_STATE/rolled_back"; exit 0 ;;
  *) exit 0 ;;
esac
"""

FAKE_DOCKER = r"""#!/usr/bin/env bash
echo "docker $*" >> "$FAKE_STATE/calls"
if [ "$2" = "build" ] && [ -f "$FAKE_STATE/build_fails" ] && [ ! -f "$FAKE_STATE/rolled_back" ]; then exit 1; fi
exit 0
"""

FAKE_CURL = r"""#!/usr/bin/env bash
url="${@: -1}"
case "$url" in
  */health) exit 0 ;;
  */siris/status)
    v=$(cat "$FAKE_STATE/version")
    [ -f "$FAKE_STATE/unhealthy" ] && [ ! -f "$FAKE_STATE/rolled_back" ] && v="still-starting"
    echo "{\"version\": \"$v\"}" ;;
  */siris/evals/run*)
    acc=$(cat "$FAKE_STATE/accuracy" 2>/dev/null || echo 100.0)
    echo "{\"selection\": {\"accuracy\": $acc}}" ;;
esac
"""


@pytest.fixture
def stage(tmp_path: Path):
    if shutil.which("python3") is None:
        pytest.skip("needs python3")
    deploy = tmp_path / "SirisAI" / "deploy"
    deploy.mkdir(parents=True)
    shutil.copy(ROOT / "deploy" / "updater.sh", deploy / "updater.sh")
    (tmp_path / "SirisAI" / ".git").mkdir()
    (tmp_path / "SirisOS" / ".git").mkdir(parents=True)
    bin_dir, state = tmp_path / "bin", tmp_path / "state"
    bin_dir.mkdir()
    state.mkdir()
    for name, body in (("git", FAKE_GIT), ("docker", FAKE_DOCKER), ("curl", FAKE_CURL)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    (state / "version").write_text("1.2")
    (state / "from").write_text("1.2")
    update_dir = tmp_path / "update"
    update_dir.mkdir()
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_STATE": str(state),
        "UPDATE_DIR": str(update_dir),
        "SIRISOS_SRC": str(tmp_path / "SirisOS"),
        # Whole seconds (date +%s): 1 could expire before the first check when
        # the run starts late in a second, rolling back a healthy update.
        "HEALTH_TIMEOUT": "3",
        "HEALTH_POLL": "0.1",
    }

    def run(request: dict | None = None) -> dict | None:
        if request is not None:
            (update_dir / "request.json").write_text(json.dumps({"version": "1.3", "from_version": "1.2", "baseline_selection_accuracy": 90.0, **request}))
        subprocess.run(["bash", str(deploy / "updater.sh"), "once"], env=env, check=True, timeout=30)
        path = update_dir / "status.json"
        return json.loads(path.read_text()) if path.exists() else None

    run.state = state  # type: ignore[attr-defined]
    run.update_dir = update_dir  # type: ignore[attr-defined]
    return run


def _calls(stage) -> str:
    return (stage.state / "calls").read_text()


def test_nothing_to_do_without_a_request(stage) -> None:
    assert stage() is None
    assert not (stage.state / "calls").exists()


def test_an_update_is_applied_and_checked(stage) -> None:
    status = stage({})
    assert status["state"] == "updated" and status["version"] == "1.3" and status["from_version"] == "1.2"
    assert "tool selection 100.0%" in status["detail"] and status["finished_at"]
    calls = _calls(stage)
    assert "merge -q --ff-only v1.3" in calls and "pull -q --ff-only" in calls
    assert "docker compose build" in calls and "docker compose up -d" in calls and "reset" not in calls
    assert not (stage.update_dir / "request.json").exists() and not (stage.update_dir / "request.processing.json").exists()
    assert not (stage.update_dir / ".lock").exists()


def test_local_edits_are_never_overwritten(stage) -> None:
    (stage.state / "dirty").touch()
    status = stage({})
    assert status["state"] == "skipped" and "local edits" in status["detail"]
    assert "merge" not in _calls(stage) and "docker" not in _calls(stage)


def test_a_missing_tag_is_skipped(stage) -> None:
    (stage.state / "no_tag").touch()
    assert stage({})["detail"] == "there's no v1.3 tag"


def test_an_unhealthy_new_version_is_rolled_back(stage) -> None:
    (stage.state / "unhealthy").touch()
    status = stage({})
    assert status["state"] == "rolled_back" and "didn't come up healthy" in status["detail"] and "rolled back to 1.2" in status["detail"]
    assert "reset -q --hard old-sha" in _calls(stage)
    assert (stage.state / "version").read_text().strip() == "1.2"


def test_a_failed_build_is_rolled_back(stage) -> None:
    (stage.state / "build_fails").touch()
    assert stage({})["detail"].startswith("the build failed")


def test_worse_tool_selection_is_rolled_back(stage) -> None:
    (stage.state / "accuracy").write_text("80.0")
    status = stage({})
    assert status["state"] == "rolled_back" and "tool selection got worse on 1.3 (90.0% -> 80.0%)" in status["detail"]


def test_no_baseline_skips_the_eval_gate(stage) -> None:
    (stage.state / "accuracy").write_text("10.0")
    assert stage({"baseline_selection_accuracy": None})["state"] == "updated"
