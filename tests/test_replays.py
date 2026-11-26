"""Replays of tool calls that failed in real use (4.28.0, app/learning/tool_failures.py).

Each line of tests/replays/*.jsonl is a real call: the tool and the exact
arguments the model gave it (secrets already masked). Export more with
GET /siris/learning/tool-failures?format=replay. Each must still get through
argument checking -- types converted, nothing missing or out of range -- so
the bug that call found can't come back the same way. The service's reply
isn't recorded, so this checks how the call is made, not what comes back."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core import tool_args
from app.core.siris import SirisAI
from app.evals.setup import fully_configured_settings

CASES = [json.loads(line) for f in sorted((Path(__file__).parent / "replays").glob("*.jsonl")) for line in f.read_text().splitlines() if line.strip()]


@pytest.mark.parametrize("case", CASES, ids=[f"{c['tool']}:{i}" for i, c in enumerate(CASES)])
def test_a_real_failing_call_now_gets_through(case: dict, client: TestClient) -> None:
    siris = SirisAI(settings=fully_configured_settings())
    tool = siris.tools.get(case["tool"])
    assert tool is not None, f"{case['tool']} isn't a tool any more: update or remove the replay"
    schema = getattr(tool, "parameters", None)
    fixed = tool_args.fit_enums(tool_args.coerce(case["arguments"], schema), schema)
    assert tool_args.problems(fixed, schema) == []
    for name, spec in ((schema or {}).get("properties") or {}).items():
        if name in fixed and spec.get("type") == "integer":
            assert isinstance(fixed[name], int) and not isinstance(fixed[name], bool), name
