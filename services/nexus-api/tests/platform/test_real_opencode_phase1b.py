"""Opt-in real assistant + MCP acceptance over execution-session fixtures.

    ASTRA_REAL_OPENCODE=1 ASTRA_REAL_OPENCODE_MODEL=deepseek/deepseek-flash \
        pytest tests/test_real_opencode_phase1b.py
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPOSITORY_ROOT / "tools" / "e2e" / "phase1b_real_experience.py"


@pytest.mark.skipif(
    os.environ.get("ASTRA_REAL_OPENCODE") != "1",
    reason="real OpenCode verification is opt-in (set ASTRA_REAL_OPENCODE=1)",
)
def test_phase1b_real_experience_chain():
    model = os.environ.get("ASTRA_REAL_OPENCODE_MODEL")
    command = [sys.executable, str(SCRIPT)]
    if model:
        command.extend(["--model", model])
    completed = subprocess.run(
        command,
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr