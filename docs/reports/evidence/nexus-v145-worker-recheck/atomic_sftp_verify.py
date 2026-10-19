"""Real SSH/SFTP atomic-write verification in an isolated /tmp directory.

Credentials are read in-process and never printed. Only canary content is used.
"""

from __future__ import annotations

import json
import shlex
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import paramiko

CREDENTIALS = Path("/tmp/nexus-v145-qa/credentials.json")
OUT = Path("/tmp/nexus-atomic-sftp-result.json")


def main() -> int:
    credentials = json.loads(CREDENTIALS.read_text(encoding="utf-8"))
    ssh = credentials["ssh"]
    root = ssh["users"]["root"]
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=ssh["host"],
        port=int(ssh["port"]),
        username=root["username"],
        password=root["password"],
        timeout=20,
        banner_timeout=20,
        auth_timeout=20,
        allow_agent=False,
        look_for_keys=False,
    )
    sftp = client.open_sftp()
    result: dict = {"phase": "real-ssh-sftp-atomic-write", "generated_at": datetime.now(UTC).isoformat()}
    write_calls = {"count": 0}
    remote_dir = ""

    def run(command: str):
        _stdin, stdout, stderr = client.exec_command(command, timeout=30)
        rc = stdout.channel.recv_exit_status()
        return rc, stdout.read().decode("utf-8", "replace"), stderr.read().decode("utf-8", "replace")

    def read(path: str):
        try:
            with sftp.open(path, "rb") as handle:
                return handle.read()
        except FileNotFoundError:
            return None

    def write(path: str, data: bytes, mode: int):
        write_calls["count"] += 1
        with sftp.open(path, "wb") as handle:
            handle.write(data)
        sftp.chmod(path, mode)

    def mode_of(path: str) -> str:
        rc, out, _ = run(f"stat -c %a {shlex.quote(path)}")
        return out.strip() if rc == 0 else ""

    def temp_files() -> list[str]:
        rc, out, _ = run(f"find {shlex.quote(remote_dir)} -maxdepth 1 -name '*.tmp-*' -print")
        return [line for line in out.splitlines() if line.strip()] if rc == 0 else ["find-failed"]

    def atomic(path: str, data: bytes, mode: int, *, backup: bool = False, fault: str = ""):
        existing = read(path)
        if existing == data:
            run(f"chmod {mode:o} {shlex.quote(path)}")
            return False
        temp = f"{path}.tmp-{uuid.uuid4().hex}"
        try:
            if backup and existing is not None:
                rc, _, _ = run(
                    f"cp -p {shlex.quote(path)} {shlex.quote(path + '.nexus-backup')}"
                )
                if rc:
                    raise RuntimeError("backup failed")
            write(temp, data if fault != "verify" else data[:-1], mode)
            if fault == "partial":
                write(temp, data[: max(1, len(data) // 2)], mode)
                raise RuntimeError("injected write interruption")
            if read(temp) != data:
                raise RuntimeError("verification failed")
            run(f"chmod {mode:o} {shlex.quote(temp)}")
            destination = f"{remote_dir}/missing-dir/target" if fault == "rename" else path
            rc, _, err = run(f"mv -f {shlex.quote(temp)} {shlex.quote(destination)}")
            if rc:
                raise RuntimeError("rename failed: " + err.strip()[:120])
        finally:
            run(f"rm -f {shlex.quote(temp)}")
        return True

    try:
        rc, out, err = run(f"mktemp -d /tmp/nexus-atomic-{uuid.uuid4().hex[:12]}-XXXXXX")
        if rc:
            raise RuntimeError("mktemp failed: " + err.strip()[:120])
        remote_dir = out.strip()
        run(f"chmod 700 {shlex.quote(remote_dir)}")
        result["remote_dir_prefix"] = str(Path(remote_dir).parent)

        worker = f"{remote_dir}/worker 1.env"
        write(worker, b"old-canary\n", 0o644)
        unchanged_mtime = run(f"stat -c %Y {shlex.quote(worker)}")[1].strip()
        result["normal_write_changed"] = atomic(worker, b"new-canary\n", 0o600, backup=True)
        result["normal_target"] = read(worker) == b"new-canary\n"
        result["normal_backup"] = read(worker + ".nexus-backup") == b"old-canary\n"
        result["normal_mode"] = mode_of(worker)
        result["normal_temp_cleaned"] = temp_files() == []

        calls_before = write_calls["count"]
        result["same_content_changed"] = atomic(worker, b"new-canary\n", 0o600, backup=True)
        result["same_content_no_sftp_write"] = write_calls["count"] == calls_before
        unchanged_mtime_after = run(f"stat -c %Y {shlex.quote(worker)}")[1].strip()
        result["same_content_mtime_unchanged"] = unchanged_mtime == unchanged_mtime_after
        run(f"chmod 644 {shlex.quote(worker)}")
        atomic(worker, b"new-canary\n", 0o600)
        result["same_content_mode"] = mode_of(worker)

        interrupted = f"{remote_dir}/interrupted.env"
        write(interrupted, b"keep-old\n", 0o600)
        try:
            atomic(interrupted, b"never-visible\n", 0o600, backup=True, fault="partial")
        except RuntimeError as exc:
            result["interruption_error"] = "injected write interruption" in str(exc)
        result["interruption_old_target"] = read(interrupted) == b"keep-old\n"
        result["interruption_backup"] = read(interrupted + ".nexus-backup") == b"keep-old\n"
        result["interruption_mode"] = mode_of(interrupted)

        corrupt = f"{remote_dir}/corrupt.env"
        write(corrupt, b"keep-old\n", 0o600)
        try:
            atomic(corrupt, b"never-visible\n", 0o600, backup=True, fault="verify")
        except RuntimeError as exc:
            result["verification_error"] = "verification failed" in str(exc)
        result["verification_old_target"] = read(corrupt) == b"keep-old\n"
        result["verification_backup"] = read(corrupt + ".nexus-backup") == b"keep-old\n"

        rename = f"{remote_dir}/rename.env"
        write(rename, b"keep-old\n", 0o600)
        try:
            atomic(rename, b"never-visible\n", 0o600, backup=True, fault="rename")
        except RuntimeError as exc:
            result["rename_error"] = "rename failed" in str(exc)
        result["rename_old_target"] = read(rename) == b"keep-old\n"
        result["rename_backup"] = read(rename + ".nexus-backup") == b"keep-old\n"

        fresh = f"{remote_dir}/fresh.env"
        result["fresh_write_changed"] = atomic(fresh, b"fresh\n", 0o600)
        result["fresh_mode"] = mode_of(fresh)
        result["fresh_backup_absent"] = read(fresh + ".nexus-backup") is None
        result["all_temp_cleaned"] = temp_files() == []
        result["arch"] = run("uname -m")[1].strip()
        checks = [
            "normal_write_changed",
            "normal_target",
            "normal_backup",
            "normal_temp_cleaned",
            "same_content_no_sftp_write",
            "same_content_mtime_unchanged",
            "interruption_error",
            "interruption_old_target",
            "interruption_backup",
            "verification_error",
            "verification_old_target",
            "verification_backup",
            "rename_error",
            "rename_old_target",
            "rename_backup",
            "fresh_write_changed",
            "fresh_backup_absent",
            "all_temp_cleaned",
        ]
        result["checks"] = {key: result.get(key) is True for key in checks}
        result["checks"].update(
            {
                "normal_mode_600": result.get("normal_mode") == "600",
                "same_content_mode_600": result.get("same_content_mode") == "600",
                "interruption_mode_600": result.get("interruption_mode") == "600",
                "fresh_mode_600": result.get("fresh_mode") == "600",
            }
        )
        result["normal_write_changed"] = result["normal_write_changed"] is True
    finally:
        if remote_dir:
            run(f"rm -rf -- {shlex.quote(remote_dir)}")
            result["cleaned_own_dir"] = read(remote_dir) is None
        sftp.close()
        client.close()
    result["checks"]["cleaned_own_dir"] = result.get("cleaned_own_dir") is True
    result["status"] = "passed" if all(result["checks"].values()) else "failed"
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
