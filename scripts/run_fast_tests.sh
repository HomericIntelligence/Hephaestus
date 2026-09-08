#!/usr/bin/env bash
# Run the shared local-commit and pull-request test selection.

set -euo pipefail

exec uv run pytest tests/unit tests/integration \
    --override-ini="addopts=" -v --strict-markers -m precommit
