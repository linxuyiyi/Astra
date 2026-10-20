"""Opt-in real OpenCode verification for the Phase 1A Session fact path.

Runs only when explicitly enabled because it starts a real OpenCode Server,
calls a real model, and writes to a temporary workspace:

    ASTRA_REAL_OPENCODE=1 ASTRA_REAL_OPENCODE_MODEL=deepseek/deepseek-flash \
        pytest tests/test_real_opencode_phase1a.py
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPOSITORY_ROOT / "tools" / "e2e" / "phase1a_real_opencode.py"


@pytest.mark.skipif(
    os.environ.get("ASTRA_REAL_OPENCODE") != "1",
    reason="real OpenCode verification is opt-in (set ASTRA_REAL_OPENCODE=1)",
)
def test_phase1a_real_opencode_key_paths():
    model = os.environ.get("ASTRA_REAL_OPENCODE_MODEL", "deepseek/deepseek-flash")
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--model", model],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr