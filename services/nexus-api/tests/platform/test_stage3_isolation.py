"""Stage 3 defaults stay off the names v0.9 already binds."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "ops" / "deploy" / "check_parallel.py"


def test_process_defaults_do_not_collide_with_v09():
    env = {key: value for key, value in os.environ.items() if not key.startswith("ASTRA_")}
    completed = subprocess.run(
        [sys.executable, str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    for label in (
        "database name",
        "database user",
        "redis db",
        "redis prefix",
        "celery queue",
        "managed processes",
        "opt and etc paths",
        "ports",
        "lock directory",
    ):
        assert f"ok  {label}:" in completed.stdout
