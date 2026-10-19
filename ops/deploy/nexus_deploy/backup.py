from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .config import DeployConfig
from .errors import BackupError
from .runner import Runner


@dataclass
class BackupResult:
    status: str
    database: str
    database_revision: str | None = None
    path: str | None = None
    restore_verified: bool | str = False
    detail: str = ""
    extra: dict = field(default_factory=dict)

    def as_record(self) -> dict:
        return {
            "status": self.status,
            "database": self.database,
            "database_revision": self.database_revision,
            "path": self.path,
            "restore_verified": self.restore_verified,
            "detail": self.detail,
            **self.extra,
        }


def sqlite_revision(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        with sqlite3.connect(path) as connection:
            row = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='alembic_version'"
            ).fetchone()
            if row is None:
                return None
            value = connection.execute("SELECT version_num FROM alembic_version").fetchone()
            return str(value[0]) if value else None
    except sqlite3.Error:
        return None


class BackupManager:
    def __init__(
        self,
        config: DeployConfig,
        runner: Runner,
        *,
        which: Callable[[str], str | None] = shutil.which,
        log: Callable[[str], None] = print,
    ) -> None:
        self.config = config
        self.runner = runner
        self.which = which
        self.log = log

    def backup(self, *, commit: str, revision: str | None = None) -> BackupResult:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        directory = self.config.backup_dir / f"{stamp}-{commit[:12]}"
        directory.mkdir(parents=True, exist_ok=True)
        self._copy_config(directory)
        if self.config.database_backend == "sqlite":
            result = self._backup_sqlite(directory, revision)
        else:
            result = self._backup_mysql(directory, revision)
        (directory / "backup.json").write_text(
            json.dumps(result.as_record(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return result

    def _copy_config(self, directory: Path) -> None:
        for source in (
            self.config.user_env_file,
            self.config.secrets_file,
            self.config.runtime_env_file,
            self.config.opencode_config_file,
            self.config.chat_model_file,
            self.config.optional_env_file,
        ):
            if not source.is_file():
                continue
            target = directory / source.name
            shutil.copy2(source, target)
            target.chmod(0o600)

    def _backup_sqlite(self, directory: Path, revision: str | None) -> BackupResult:
        databases = [
            ("task-platform", self.config.sqlite_path, directory / "nexus.db"),
            ("nexus-api", self.config.nexus_api_sqlite_path, directory / "nexus-api.sqlite3"),
        ]
        backed_up: list[dict] = []
        for label, source, target in databases:
            if not source.is_file():
                backed_up.append({"name": label, "status": "skipped", "path": None})
                continue
            backed_up.append(self._backup_one_sqlite(label, source, target))
        if not any(item["status"] == "ok" for item in backed_up):
            return BackupResult(
                status="skipped",
                database="sqlite",
                database_revision=revision,
                detail="数据库尚未初始化，首个发布不需要备份",
                extra={"sqlite_databases": backed_up},
            )
        return BackupResult(
            status="ok",
            database="sqlite",
            database_revision=revision,
            path=str(directory / "nexus.db"),
            restore_verified=True,
            detail="sqlite backup API + integrity_check（Task 与 Nexus API）",
            extra={"sqlite_databases": backed_up},
        )

    def _backup_mysql(self, directory: Path, revision: str | None) -> BackupResult:
        if not self.which("mysqldump"):
            raise BackupError("MySQL 备份需要 mysqldump；请安装客户端后重试")
        if not self.which("mysql"):
            raise BackupError("MySQL 恢复验证需要 mysql 客户端；请安装后重试")
        if not self.config.mysql_backup_acknowledged:
            raise BackupError(
                "MySQL 自动恢复验证未启用。请设置 NEXUS_MYSQL_BACKUP_ACKNOWLEDGED=1 "
                "确认按人工恢复手册演练后再升级"
            )
        target = directory / "nexus.sql"
        env = {
            "MYSQL_PWD": self.config.database_password,
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        }
        command = [
            "mysqldump",
            "--single-transaction",
            "--quick",
            "--routines",
            "--events",
            "--host",
            self.config.database_host,
            "--port",
            str(self.config.database_port),
            "--user",
            self.config.database_username,
            "--result-file",
            str(target),
            "--databases",
            self.config.database_name,
        ]
        result = self.runner.run(command, env=env)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()
            raise BackupError(
                f"mysqldump 失败：{detail[-1] if detail else result.returncode}"
            )
        if not target.is_file() or target.stat().st_size == 0:
            raise BackupError("mysqldump 未生成非空备份文件")
        content = target.read_text(encoding="utf-8", errors="ignore")
        if "CREATE TABLE" not in content:
            raise BackupError("MySQL 备份不包含建表语句，拒绝视为可恢复备份")
        api_sqlite = self._backup_one_sqlite(
            "nexus-api", self.config.nexus_api_sqlite_path, directory / "nexus-api.sqlite3"
        )
        return BackupResult(
            status="ok",
            database="mysql",
            database_revision=revision,
            path=str(target),
            restore_verified="manual",
            detail="mysqldump 已生成；Nexus API SQLite 已备份并校验；自动恢复演练未执行，按人工手册验证",
            extra={"sqlite_databases": [api_sqlite]},
        )

    def _backup_one_sqlite(self, label: str, source: Path, target: Path) -> dict:
        if not source.is_file():
            return {"name": label, "status": "skipped", "path": None}
        try:
            with sqlite3.connect(source) as origin, sqlite3.connect(target) as copy:
                origin.backup(copy)
                check = copy.execute("PRAGMA integrity_check").fetchone()
        except sqlite3.Error as exc:
            raise BackupError(f"SQLite 备份失败（{label}）：{exc}") from exc
        verdict = str(check[0]) if check else "unknown"
        if verdict.lower() != "ok":
            raise BackupError(f"SQLite 备份 integrity_check 失败（{label}）：{verdict}")
        return {"name": label, "status": "ok", "path": str(target), "source": str(source)}
