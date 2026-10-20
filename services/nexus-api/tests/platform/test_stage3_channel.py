"""S3-10 gaps: SSH stays on install and CLI fallback, and offline is not available."""

from pathlib import Path

from app.runtime.presentation import project_channel_health

APP = Path(__file__).resolve().parents[2] / "app"
SSH_ALLOW = {
    ("app/runtime/environment.py", "SSHSession("),
    ("app/runtime/environment.py", ".open_session("),
    ("app/runtime/maintenance.py", ".open_session("),
    ("app/runtime/remote_provisioning.py", ".open_session("),
    ("app/runtime/ssh_client.py", ".open_session("),
    ("app/runtime/worker_health.py", ".open_session("),
    ("app/executors/opencode_remote.py", ".open_session("),
}


def test_daily_workspace_and_health_do_not_open_ssh():
    found = set()
    for path in APP.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        relative = path.relative_to(APP.parent).as_posix()
        for needle in ("SSHSession(", ".open_session(", ".run_remote("):
            if needle in text:
                found.add((relative, needle))
    assert found == SSH_ALLOW


def test_offline_worker_is_not_shown_available_and_does_not_encode_a_task_change():
    health = project_channel_health(
        mode="LINUX_REMOTE",
        worker_channel="worker",
        environment_status="AVAILABLE",
        worker_status="offline",
        repository_id=None,
        employee_status="WORKING",
        task_statuses=["RUNNING"],
        runtime_connections=["CONNECTED"],
    )
    assert health["availability"] == "offline"
    assert health["worker_online"] is False
    assert health["opencode_reachable"] is False
    assert health["executing"] is True
    assert health["waiting_human"] is False
    assert "status" not in health


def test_waiting_human_and_repository_stay_separate_from_worker_health():
    health = project_channel_health(
        mode="LINUX_REMOTE",
        worker_channel="worker",
        environment_status="AVAILABLE",
        worker_status="online",
        repository_id="repo-1",
        employee_status="WAIT_HUMAN",
        task_statuses=["WAITING"],
        runtime_connections=["CONNECTED"],
    )
    assert health["availability"] == "AVAILABLE"
    assert health["opencode_reachable"] is True
    assert health["repository_bound"] is True
    assert health["waiting_human"] is True
    assert health["executing"] is False
