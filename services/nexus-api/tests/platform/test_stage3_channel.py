"""S3-10 gaps: SSH stays on install and CLI fallback, and offline is not available."""

from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"
SSH_ALLOW = {
    ("app/runtime/environment.py", "SSHSession("),
    ("app/runtime/environment.py", ".open_session("),
    ("app/runtime/worker_connection.py", ".open_session("),
    ("app/runtime/remote_provisioning.py", ".open_session("),
    ("app/runtime/ssh_client.py", ".open_session("),
}


def test_runtime_ssh_connections_are_owned_by_the_pool():
    found = set()
    for path in APP.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        relative = path.relative_to(APP.parent).as_posix()
        for needle in ("SSHSession(", ".open_session(", ".run_remote("):
            if needle in text:
                found.add((relative, needle))
    assert found == SSH_ALLOW
