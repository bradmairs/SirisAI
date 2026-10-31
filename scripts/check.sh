#!/usr/bin/env bash
# What CI checks, before you push (4.03). The pre-push hook runs it; turn the
# hook on once per clone with:  git config core.hooksPath .githooks
#
# - Clears Python's bytecode caches first: a stale one once made
#   `python -m app.hub.contract` write the schema from old code.
# - The hub contract's schema must match the models (python -m app.hub.contract).
# - The whole suite, on Python 3.12 (CI's version) when it's installed.
# SKIP_CHECKS=1 git push skips it, for a push that only touches docs.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
find app tests scripts -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
PY=python
if command -v python3.12 >/dev/null && python3.12 -c "import fastapi, pydantic, pytest" 2>/dev/null; then
  PY=python3.12
fi
if ! $PY -m app.hub.contract --check; then
  echo "The hub contract schema is out of date: run python -m app.hub.contract (and HUB_WRITE_EXAMPLES=1 pytest tests/test_hub_examples.py)." >&2
  exit 1
fi
echo "Running the suite on $($PY --version)..."
$PY -m pytest -q -x -p no:cacheprovider
