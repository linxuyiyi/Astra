"""真实 v1.5.0 公共 API / Worker / OpenCode 验收；需显式指定隔离执行环境。"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]


@pytest.mark.skipif(
    not (os.environ.get("NEXUS_V150_E2E_URL") and os.environ.get("NEXUS_V150_E2E_WORKER")),
    reason="需要 NEXUS_V150_E2E_URL 与 NEXUS_V150_E2E_WORKER 指定可执行的隔离 Worker/模型环境",
)
def test_real_worker_task_and_native_session():
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/e2e/nexus_v150_task_worker.py"),
            "--base-url",
            os.environ["NEXUS_V150_E2E_URL"],
            "--worker-id",
            os.environ["NEXUS_V150_E2E_WORKER"],
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stdout + result.stderr
