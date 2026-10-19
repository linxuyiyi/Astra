from __future__ import annotations

import fcntl
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import DeployConfig
from .errors import LockError


def utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def atomic_write(path: Path, content: str, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if tmp.exists():
            tmp.unlink()


def atomic_json(path: Path, payload: dict[str, Any], *, mode: int = 0o644) -> None:
    atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", mode=mode)


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


@dataclass
class DeploymentLock:
    path: Path
    _handle: Any = None

    def __enter__(self) -> DeploymentLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._handle.close()
            self._handle = None
            raise LockError(f"另一个部署正在运行（锁：{self.path}）") from exc
        self._handle.seek(0)
        self._handle.truncate()
        self._handle.write(f"{os.getpid()}\n")
        self._handle.flush()
        return self

    def __exit__(self, *_: object) -> None:
        if self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None


class DeploymentRecords:
    def __init__(self, config: DeployConfig) -> None:
        self.config = config
        self.root = config.state_dir
        self.history = self.root / "deployments"
        self.latest = self.root / "latest.json"
        self.success = self.root / "success.json"
        self.installed = self.root / "installed"

    def lock(self) -> DeploymentLock:
        return DeploymentLock(self.root / "deploy.lock")

    def write_failure(
        self,
        *,
        commit: str,
        stage: str,
        message: str,
        config_digest: str = "",
        database_revision: str | None = None,
        rollback: dict[str, Any] | None = None,
        rollback_blocked: bool = False,
        checks: list[dict[str, Any]] | None = None,
        backup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "status": "failed",
            "product": "nexus",
            "commit": commit,
            "stage": stage,
            "message": message,
            "config_digest": config_digest,
            "database_revision": database_revision,
            "rollback": rollback or {},
            "rollback_blocked": rollback_blocked,
            "checks": checks or [],
            "backup": backup or {},
            "finished_at": utcnow(),
        }
        atomic_json(self._history_path(commit, "failed"), payload)
        atomic_json(self.latest, payload)
        return payload

    def write_success(
        self,
        *,
        commit: str,
        previous_commit: str | None,
        config_digest: str,
        database_revision: str | None,
        checks: list[dict[str, Any]],
        rollback: dict[str, Any] | None = None,
        model_probe: str = "skipped",
    ) -> dict[str, Any]:
        payload = {
            "status": "succeeded",
            "product": "nexus",
            "commit": commit,
            "previous_commit": previous_commit,
            "config_digest": config_digest,
            "database_revision": database_revision,
            "checks": checks,
            "rollback": rollback or {},
            "model_probe": model_probe,
            "finished_at": utcnow(),
        }
        atomic_json(self._history_path(commit, "success"), payload)
        atomic_json(self.success, payload)
        atomic_json(self.latest, payload)
        atomic_write(self.installed, f"{commit}\n")
        return payload

    def write_rollback(
        self,
        *,
        target_commit: str,
        from_commit: str | None,
        ok: bool,
        detail: str,
        checks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        payload = {
            "status": "rolled_back" if ok else "rollback_failed",
            "product": "nexus",
            "commit": target_commit,
            "previous_commit": from_commit,
            "rollback": {"attempted": True, "ok": ok, "detail": detail},
            "checks": checks,
            "finished_at": utcnow(),
        }
        atomic_json(self._history_path(target_commit, payload["status"]), payload)
        atomic_json(self.latest, payload)
        return payload

    def current_commit(self) -> str | None:
        target = self.config.current_link
        if not target.exists() and not target.is_symlink():
            return None
        try:
            return target.resolve().name
        except OSError:
            return None

    def last_success(self) -> dict[str, Any]:
        return read_json(self.success)

    def last_attempt(self) -> dict[str, Any]:
        return read_json(self.latest)

    def success_for(self, commit: str) -> dict[str, Any]:
        for record in self.history_records(limit=100):
            if record.get("status") == "succeeded" and record.get("commit") == commit:
                return record
        current = self.last_success()
        return current if current.get("commit") == commit else {}

    def history_records(self, limit: int = 10) -> list[dict[str, Any]]:
        if not self.history.is_dir():
            return []
        records = [read_json(path) for path in self.history.glob("*.json")]
        records = [record for record in records if record]
        records.sort(key=lambda item: str(item.get("finished_at", "")), reverse=True)
        return records[:limit]

    def _history_path(self, commit: str, outcome: str) -> Path:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        short = commit[:12] or "unknown"
        return self.history / f"{stamp}-{short}-{outcome}.json"
