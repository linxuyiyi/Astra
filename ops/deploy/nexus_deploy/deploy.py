from __future__ import annotations

import json
import os
import platform
import re
import shutil
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .backup import BackupManager, BackupResult, sqlite_revision
from .config import DeployConfig, load_config, parse_env_file
from .configgen import (
    bootstrap_task_config,
    config_digest,
    ensure_directories,
    ensure_secrets,
    env_line,
    non_secret_summary,
    own_generated_file,
    persist_secret,
    redact_text,
    sanitize_payload,
    secret_values,
    write_opencode_config,
    write_runtime_env,
)
from .errors import (
    MigrationError,
    PreflightError,
    PrepareError,
    ProbeError,
    RollbackBlocked,
    ServiceError,
)
from .gitops import Release, ReleaseBuilder, verify_git_environment
from .nginx import install_nginx
from .ports import OwnershipChecker, PortInspector
from .probes import ProbeResult, ProbeSuite
from .records import DeploymentRecords, atomic_json, atomic_write, read_json
from .runner import Runner, SubprocessRunner
from .services import ServiceCatalog, build_catalog
from .systemd import SystemdManager

LEGACY_CORE_UNITS = ("nexus-astra", "nexus-web")
OPTIONAL_MODULE_UNITS = {"nexus-llm-wiki-tunnel", "nexus-llm-wiki-pump"}
CORE_UNIT_NAMES = {
    "nexus-task-api",
    "nexus-runtime-beat",
    "nexus-runtime-control",
    "nexus-runtime-agent",
    "nexus-task-mcp",
    "nexus-library-mcp",
    "nexus-agent-service",
    "nexus-api",
    "nexus-gateway",
    "nexus-opencode-server",
    "nexus-assistant-opencode",
    *LEGACY_CORE_UNITS,
}


@dataclass
class DeploymentResult:
    ok: bool
    stage: str
    message: str
    commit: str
    record: dict = field(default_factory=dict)
    checks: list[dict] = field(default_factory=list)
    classification: str = ""


def _port_units(config: DeployConfig) -> dict[str, tuple[int, str]]:
    mapping = {
        "task-api": (config.ports["task_api"], "nexus-task-api"),
        "agent-service": (config.ports["agent_service"], "nexus-agent-service"),
        "nexus-api": (config.ports["nexus_api"], "nexus-api"),
        "task-mcp": (config.ports["task_mcp"], "nexus-task-mcp"),
    }
    if not config.effective("ASTRA_OPENCODE_SERVER_URL", "").strip():
        mapping["opencode-server"] = (config.ports["opencode"], "nexus-opencode-server")
    if (
        config.effective("ASTRA_ASSISTANT_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
        and not config.effective("ASTRA_ASSISTANT_OPENCODE_URL", "").strip()
    ):
        mapping["assistant-opencode"] = (
            config.ports["assistant_opencode"],
            "nexus-assistant-opencode",
        )
    if config.library_mcp_enabled:
        mapping["library-mcp"] = (config.ports["library_mcp"], "nexus-library-mcp")
    if config.gateway_enabled:
        mapping["gateway"] = (config.ports["gateway"], "nexus-gateway")
    return mapping


class Deployer:
    def __init__(
        self,
        config: DeployConfig,
        *,
        runner: Runner | None = None,
        builder: ReleaseBuilder | None = None,
        systemd: SystemdManager | None = None,
        records: DeploymentRecords | None = None,
        backup: BackupManager | None = None,
        probes: ProbeSuite | None = None,
        probe_factory: Callable[[DeployConfig], ProbeSuite] | None = None,
        check_platform: bool = True,
        check_git: bool = True,
        check_ports: bool = True,
        preflight_only: bool = False,
        skip_web_build: bool = False,
        model_probe: bool = False,
        log: Callable[[str], None] = print,
        sleep: Callable[[float], None] = time.sleep,
        retry_seconds: int = 60,
    ) -> None:
        self.config = config
        self.runner = runner or SubprocessRunner()
        self.records = records or DeploymentRecords(config)
        self.builder = builder or ReleaseBuilder(
            config,
            self.runner,
            skip_web_build=skip_web_build,
            log=log,
        )
        self.systemd = systemd or SystemdManager(config, self.runner, log=log)
        self.backup = backup or BackupManager(config, self.runner, log=log)
        self.probes = probes or ProbeSuite(config, self.runner)
        self.probe_factory = probe_factory or (lambda selected: ProbeSuite(selected, self.runner))
        self.check_platform = check_platform
        self.check_git = check_git
        self.check_ports = check_ports
        self.preflight_only = preflight_only
        self.model_probe = model_probe
        self.log = log
        self.sleep = sleep
        self.retry_seconds = retry_seconds

    # ------------------------------------------------------------------ deploy
    def install(self) -> DeploymentResult:
        with self.records.lock():
            return self._deploy()

    def _deploy(self) -> DeploymentResult:
        stage = "preflight"
        old_commit = self.records.current_commit()
        old_release_path = self.config.release_dir(old_commit) if old_commit else None
        old_catalog: ServiceCatalog | None = None
        new_catalog: ServiceCatalog | None = None
        old_revision: str | None = None
        new_revision: str | None = None
        backup_result: BackupResult | None = None
        switched = False
        stopped = False
        digest = ""
        checks: list[dict] = []

        try:
            self._log_stage("preflight")
            checks.extend(self._preflight())
            ensure_directories(self.config)
            if self.preflight_only:
                return DeploymentResult(
                    True, "preflight", "预检通过，未修改现网", self.config.commit, checks=checks
                )

            stage = "prepare"
            self._log_stage(stage)
            release = self.builder.prepare()
            copied = bootstrap_task_config(self.config, release.path)
            if copied:
                self.log(f"[config] 初始化 Task 默认配置 {copied} 个文件（未覆盖已有文件）")
            if old_commit and old_release_path and old_release_path.is_dir():
                old_catalog = build_catalog(self.config, Release(old_release_path, old_commit, {}))

            stage = "backup"
            self._log_stage(stage)
            old_revision = self._read_revision(old_commit)
            backup_result = self.backup.backup(commit=self.config.commit, revision=old_revision)
            self.log(
                f"[backup] {backup_result.status} {backup_result.database} "
                f"revision={old_revision or 'none'} verified={backup_result.restore_verified}"
            )

            stage = "config"
            self._log_stage(stage)
            secrets, secrets_changed = ensure_secrets(self.config, self.config.user_values)
            _, changed = write_runtime_env(self.config)
            write_opencode_config(self.config)
            digest = config_digest(self.config)
            for line in non_secret_summary(changed):
                self.log(f"[config] {line}")
            if secrets_changed:
                self.log("[config] 已生成或补齐缺失密钥（值不显示）")

            new_catalog = build_catalog(self.config, release)

            stage = "stop"
            self._log_stage(stage)
            self._retire_core_units(new_catalog)
            if old_commit:
                stopped = True
                self.systemd.stop(old_catalog.units() if old_catalog else new_catalog.units())

            stage = "migrate"
            self._log_stage(stage)
            self._migrate(release, secrets)
            new_revision = self._read_revision(self.config.commit)

            stage = "switch"
            self._log_stage(stage)
            self._switch_current(release)
            switched = True

            stage = "configure"
            self._log_stage(stage)
            self.systemd.write_units(new_catalog)
            self.systemd.daemon_reload()
            if self.config.nginx_enabled:
                install_nginx(self.config, self.runner)
            self.systemd.enable(new_catalog.units())

            stage = "start"
            self._log_stage(stage)
            self.systemd.restart(new_catalog.required_units())
            self._wait_started(new_catalog)

            stage = "readiness"
            self._log_stage(stage)
            self._ensure_chat_model(new_catalog, secrets)
            checks.append(self._ensure_gateway_probe_key(secrets).as_record())
            checks.append(self._ensure_gateway_route(secrets).as_record())
            checks.extend(self._readiness(new_catalog, secrets))

            stage = "record"
            checks = sanitize_payload(checks, secret_values(self.config))
            self._save_snapshot(new_catalog)
            record = self.records.write_success(
                commit=self.config.commit,
                previous_commit=old_commit,
                config_digest=digest,
                database_revision=new_revision,
                checks=checks,
                model_probe="skipped" if not self.model_probe else "skipped",
            )
            self._prune_releases()
            self.log(f"[done] Nexus {self.config.commit[:12]} 部署成功")
            return DeploymentResult(True, stage, "部署成功", self.config.commit, record, checks)
        except Exception as exc:  # noqa: BLE001 - every failure must become a record
            return self._handle_failure(
                stage=stage,
                error=exc,
                old_commit=old_commit,
                old_catalog=old_catalog,
                new_catalog=new_catalog,
                old_revision=old_revision,
                new_revision=new_revision,
                switched=switched,
                stopped=stopped,
                digest=digest,
                checks=checks,
                backup_result=backup_result,
            )

    def _handle_failure(
        self,
        *,
        stage: str,
        error: Exception,
        old_commit: str | None,
        old_catalog: ServiceCatalog | None,
        new_catalog: ServiceCatalog | None,
        old_revision: str | None,
        new_revision: str | None,
        switched: bool,
        stopped: bool,
        digest: str,
        checks: list[dict],
        backup_result: BackupResult | None,
    ) -> DeploymentResult:
        secret_canaries = secret_values(self.config)
        message = redact_text(str(error) or error.__class__.__name__, secret_canaries)
        self.log(f"[fail] 阶段 {stage}：{message}")
        rollback: dict[str, Any] = {"attempted": False, "ok": False, "detail": ""}
        blocked = False
        can_rollback = bool(old_commit and old_catalog)
        extra_checks = list(getattr(error, "checks", []) or [])
        checks = list(checks) + [item for item in extra_checks if item not in checks]
        checks = sanitize_payload(checks, secret_canaries)
        if stage == "migrate" and new_revision is None:
            new_revision = self._read_revision(self.config.commit) or old_revision

        if stopped or switched:
            if stage == "migrate":
                blocked = True
                rollback = {
                    "attempted": False,
                    "ok": False,
                    "detail": (
                        "迁移失败：数据库可能已执行部分 DDL，即使 revision 未变也不自动启动旧代码。"
                        "请停止 Nexus 服务，使用已通过完整性校验的备份恢复数据库，"
                        "再执行 rollback --restored-revision <目标成功 revision>。"
                    ),
                }
                self._stop_quietly(new_catalog)
            elif not can_rollback:
                rollback = {
                    "attempted": False,
                    "ok": False,
                    "detail": "没有上一可用发布，本次失败不写成功标记",
                }
                self._stop_quietly(new_catalog)
                if switched:
                    self._remove_current()
            elif switched and not self._database_compatible(old_revision, new_revision, new_catalog):
                blocked = True
                rollback = {
                    "attempted": False,
                    "ok": False,
                    "detail": (
                        "数据库 revision 已变化且目标版本未声明向后兼容，禁止自动回退旧代码。"
                        "人工步骤：停止 nexus-* 服务，使用最近一次备份恢复数据库，"
                        "再执行 rollback --restored-revision <目标成功 revision>。"
                    ),
                }
                self._stop_quietly(new_catalog)
            else:
                rollback = self._rollback(old_catalog, new_catalog)
        rollback = sanitize_payload(rollback, secret_canaries)
        record = self.records.write_failure(
            commit=self.config.commit,
            stage=stage,
            message=message,
            config_digest=digest,
            database_revision=new_revision or old_revision,
            rollback=rollback,
            rollback_blocked=blocked,
            checks=checks,
            backup=backup_result.as_record() if backup_result is not None else None,
        )
        return DeploymentResult(False, stage, message, self.config.commit, record, checks)

    def _rollback(self, old_catalog: ServiceCatalog, new_catalog: ServiceCatalog | None) -> dict[str, Any]:
        self.log("[rollback] 恢复上一可用发布")
        detail = ""
        try:
            old_config = self._config_from_snapshot(old_catalog.release.commit) or self.config
            old_catalog = build_catalog(old_config, old_catalog.release)
            old_probes = self.probe_factory(old_config)
            self._stop_quietly(new_catalog)
            snapshot_units = self._restore_snapshot(old_catalog.release, old_config)
            self.systemd.restart(snapshot_units)
            self._wait_started(old_catalog)
            checks = self._readiness(old_catalog, self._current_secrets(), probes=old_probes)
            failed = [item for item in checks if not item["ok"] and not item["skipped"]]
            if failed:
                detail = "上一发布已启动但 readiness 未全部通过：" + "; ".join(
                    item["name"] for item in failed
                )
                return {"attempted": True, "ok": False, "detail": detail}
            self.log("[rollback] 用户 nexus.env 未修改；下次部署会按用户配置重新生成运行配置")
            return {"attempted": True, "ok": True, "detail": "已恢复上一发布服务与快照配置"}
        except Exception as exc:  # noqa: BLE001 - report the rollback outcome verbatim
            return {"attempted": True, "ok": False, "detail": f"回退失败：{exc}"}

    # ------------------------------------------------------------------ health
    def health(
        self,
        *,
        probes: ProbeSuite | None = None,
        config: DeployConfig | None = None,
    ) -> DeploymentResult:
        suite = probes or self.probes
        selected_config = config or self.config
        commit = self.records.current_commit()
        if not commit:
            return DeploymentResult(False, "health", "没有当前发布", "", checks=[])
        release_path = self.config.release_dir(commit)
        if not release_path.is_dir():
            return DeploymentResult(False, "health", f"当前发布目录不存在：{release_path}", commit)
        release = Release(release_path, commit, {})
        catalog = build_catalog(selected_config, release)
        secrets = self._current_secrets()
        checks = [item.as_record() for item in self._process_checks(catalog)]
        checks.extend(
            item.as_record() for item in self._service_checks(catalog, secrets, probes=suite)
        )
        checks.extend(item.as_record() for item in self._infrastructure_checks(probes=suite))
        self._append_model_probe(checks)
        checks = sanitize_payload(checks, secret_values(self.config))
        failed = [
            item
            for item in checks
            if not item["ok"] and not item.get("skipped") and item.get("required", True)
        ]
        other = [
            item
            for item in checks
            if not item["ok"] and not item.get("skipped") and not item.get("required", True)
        ]
        ok = not failed
        if failed:
            classification = "failed"
            message = "未就绪：" + ", ".join(item["name"] for item in failed)
        elif other:
            classification = "degraded"
            message = "必需项通过，存在未完成项：" + ", ".join(item["name"] for item in other)
        else:
            classification = "healthy"
            message = "健康"
        return DeploymentResult(
            ok, "health", message, commit, checks=checks, classification=classification
        )

    def status(self) -> dict[str, Any]:
        commit = self.records.current_commit()
        catalog = None
        if commit:
            release_path = self.config.release_dir(commit)
            if release_path.is_dir():
                catalog = build_catalog(self.config, Release(release_path, commit, {}))
        units = catalog.units() if catalog else ()
        services = self.systemd.status(units) if units else {}
        payload = {
            "product": "nexus",
            "current_commit": commit,
            "current_link": str(self.config.current_link),
            "last_success": self.records.last_success(),
            "last_attempt": self.records.last_attempt(),
            "history": self.records.history_records(),
            "services": services,
            "database": self.config.database_description,
            "config_digest": config_digest(self.config),
        }
        return sanitize_payload(payload, secret_values(self.config))

    def restart(self, names: list[str] | None = None) -> DeploymentResult:
        with self.records.lock():
            commit = self.records.current_commit()
            if not commit:
                return DeploymentResult(False, "restart", "没有当前发布", "")
            release_path = self.config.release_dir(commit)
            if not release_path.is_dir():
                return DeploymentResult(False, "restart", f"发布目录不存在：{release_path}", commit)
            release = Release(release_path, commit, {})
            catalog = build_catalog(self.config, release)
            try:
                if names:
                    units = tuple(catalog.by_name(name).unit for name in names if catalog.by_name(name).unit)
                else:
                    units = catalog.required_units()
            except KeyError as exc:
                return DeploymentResult(False, "restart", f"未知服务：{exc.args[0]}", commit)
            self.systemd.restart(units)
            self._wait_started(catalog)
            result = self.health()
            result.stage = "restart"
            return result

    def rollback(
        self,
        target: str | None = None,
        *,
        restored_revision: str | None = None,
    ) -> DeploymentResult:
        with self.records.lock():
            return self._rollback_locked(target, restored_revision=restored_revision)

    def _rollback_locked(
        self, target: str | None, *, restored_revision: str | None
    ) -> DeploymentResult:
        current = self.records.current_commit()
        success = self.records.last_success()
        if target is None:
            target = success.get("commit") if success.get("commit") != current else success.get(
                "previous_commit"
            )
        target = str(target or "")
        if not target:
            return DeploymentResult(False, "rollback", "没有可回退的上一发布", current or "")
        target_path = self.config.release_dir(target)
        if not target_path.is_dir():
            return DeploymentResult(False, "rollback", f"目标发布不存在：{target_path}", target)
        target_release = Release(target_path, target, read_json(target_path / "release.json"))
        target_record = self.records.success_for(target)
        target_revision = target_record.get("database_revision")
        current_revision = self._read_revision(current)
        same_schema = target_revision is not None and current_revision == target_revision
        if current and not same_schema:
            current_path = self.config.release_dir(current)
            current_manifest = read_json(current_path / "release.json")
            backward_compatible = current_manifest.get("database_compatible_with_previous") is True
            restored = restored_revision is not None and restored_revision == target_revision
            if not backward_compatible:
                if not restored:
                    return DeploymentResult(
                        False,
                        "rollback",
                        "当前数据库 revision 与目标成功记录不同；请先用已验证备份恢复数据库，"
                        "再以 --restored-revision <目标成功 revision> 重新执行",
                        target,
                    )
                verified = self._read_revision(target)
                if verified != target_revision:
                    return DeploymentResult(
                        False,
                        "rollback",
                        f"数据库 revision 仍为 {verified!r}，未达到目标 {target_revision!r}",
                        target,
                    )
        current_catalog = None
        if current:
            current_path = self.config.release_dir(current)
            if current_path.is_dir():
                current_catalog = build_catalog(self.config, Release(current_path, current, {}))
        target_config = self._config_from_snapshot(target) or self.config
        target_catalog = build_catalog(target_config, target_release)
        target_probes = self.probe_factory(target_config)
        self.log(f"[rollback] {current or '<none>'} -> {target}")
        if current_catalog:
            self.systemd.stop(current_catalog.units())
        try:
            snapshot_units = self._restore_snapshot(target_release, target_config)
            self.systemd.restart(snapshot_units)
            self._wait_started(target_catalog)
            health = self.health(probes=target_probes, config=target_config)
            if not health.ok:
                self._restore_original_after_failed_rollback(current, current_catalog)
                record = self.records.write_rollback(
                    target_commit=target,
                    from_commit=current,
                    ok=False,
                    detail=f"回退后 readiness 未通过：{health.message}",
                    checks=health.checks,
                )
                return DeploymentResult(
                    False,
                    "rollback",
                    f"回退后 readiness 未通过：{health.message}",
                    target,
                    record,
                    health.checks,
                )
            record = self.records.write_rollback(
                target_commit=target,
                from_commit=current,
                ok=True,
                detail="manual rollback",
                checks=health.checks,
            )
            return DeploymentResult(True, "rollback", "回退成功", target, record, health.checks)
        except Exception as exc:  # noqa: BLE001 - record the failed rollback and recover original
            self._restore_original_after_failed_rollback(current, current_catalog)
            record = self.records.write_rollback(
                target_commit=target,
                from_commit=current,
                ok=False,
                detail=f"回退失败：{exc}",
                checks=[],
            )
            return DeploymentResult(False, "rollback", f"回退失败：{exc}", target, record)

    def _restore_original_after_failed_rollback(
        self, current: str | None, current_catalog: ServiceCatalog | None
    ) -> None:
        if not current:
            return
        release_path = self.config.release_dir(current)
        if not release_path.is_dir():
            return
        try:
            self._stop_quietly(current_catalog)
            release = Release(release_path, current, {})
            units = self._restore_snapshot(release)
            self.systemd.restart(units)
            self._wait_started(build_catalog(self.config, release))
        except Exception as exc:  # noqa: BLE001 - report the original release is not verified
            self.log(f"[rollback] 恢复原运行发布失败：{exc}")

    # --------------------------------------------------------------- internals
    def _preflight(self) -> list[dict]:
        checks: list[dict] = []
        if self.check_platform:
            checks.extend(self._platform_checks())
        if self.check_git:
            verify_git_environment(self.config, self.runner)
            checks.append({"name": "Git commit", "ok": True, "detail": self.config.commit})
        if self.check_ports:
            inspector = PortInspector(self.runner)
            checker = OwnershipChecker(
                self.config,
                inspector,
                main_pid=self.systemd.main_pid,
                control_group=self.systemd.control_group,
            )
            for status in checker.check(_port_units(self.config)):
                if not status.owned:
                    raise PreflightError(f"端口 {status.port}：{status.detail}")
                checks.append(
                    {
                        "name": f"端口 {status.port}",
                        "ok": True,
                        "detail": status.detail,
                    }
                )
        if self.config.gateway_enabled and not self.config.gateway_probe_key:
            checks.append(
                {
                    "name": "Gateway 探针 key",
                    "ok": True,
                    "detail": "首次启动后通过现有 Gateway API 幂等创建并持久保存",
                }
            )
        if self.config.secrets_file.is_file():
            mode = self.config.secrets_file.stat().st_mode & 0o777
            if mode & 0o077:
                raise PreflightError(
                    f"密钥文件权限过宽：{self.config.secrets_file} 当前 {oct(mode)}，需要 0600"
                )
        redis = self.probes.redis()
        if not redis.ok:
            raise PreflightError(redis.detail)
        checks.append(redis.as_record())
        return checks

    def _platform_checks(self) -> list[dict]:
        checks: list[dict] = []
        if platform.system() != "Linux":
            raise PreflightError("生产部署仅支持 Linux")
        if not Path("/run/systemd/system").is_dir():
            raise PreflightError("systemd 未运行；生产部署需要 systemd")
        import grp
        import pwd

        try:
            pwd.getpwnam(self.config.run_user)
            grp.getgrnam(self.config.run_group)
        except KeyError as exc:
            raise PreflightError(
                f"运行账号或组不存在：{self.config.run_user}:{self.config.run_group}"
            ) from exc
        machine = platform.machine()
        if machine not in {"x86_64", "aarch64"}:
            raise PreflightError(f"不支持的架构：{machine}")
        checks.append({"name": "平台", "ok": True, "detail": f"Linux {machine}"})
        required = {"git": "git", "python3": "python3", "npm": "npm", "systemctl": "systemctl"}
        if self.config.nginx_enabled:
            required["nginx"] = "nginx"
        if self.config.database_backend == "mysql":
            required["mysqldump"] = "mysqldump"
            required["mysql"] = "mysql"
        missing = [label for label, binary in required.items() if shutil.which(binary) is None]
        if missing:
            raise PreflightError("缺少生产依赖：" + ", ".join(missing))
        checks.append({"name": "依赖", "ok": True, "detail": ", ".join(required)})
        version = self.runner.run(["python3", "-c", "import sys; print('%d.%d' % sys.version_info[:2])"])
        if version.returncode != 0 or tuple(map(int, version.stdout.strip().split("."))) < (3, 11):
            raise PreflightError("Python 需要 3.11+")
        checks.append({"name": "Python", "ok": True, "detail": version.stdout.strip()})
        node = self.runner.run(["node", "--version"])
        node_version = node.stdout.strip().lstrip("v")
        if node.returncode != 0 or not _version_ge(node_version, "20"):
            raise PreflightError(f"Node 需要 20+，当前 {node.stdout.strip() or '未安装'}")
        checks.append({"name": "Node", "ok": True, "detail": node_version})
        glibc = self.runner.run(["ldd", "--version"])
        match = re.search(r"(\d+\.\d+)", glibc.stdout + glibc.stderr)
        if glibc.returncode != 0 or not match or not _version_ge(match.group(1), "2.28"):
            raise PreflightError("glibc 需要 2.28+")
        checks.append({"name": "glibc", "ok": True, "detail": match.group(1)})
        cpus = os.cpu_count() or 0
        if cpus < 2:
            raise PreflightError(f"CPU 需要 2 核以上，当前 {cpus}")
        checks.append({"name": "CPU", "ok": True, "detail": f"{cpus} 核"})
        disk = shutil.disk_usage(_nearest_existing(self.config.releases_dir))
        if disk.free < 10 * 1024**3:
            raise PreflightError(f"磁盘可用 {disk.free // 1024**3}GB < 10GB")
        checks.append({"name": "磁盘", "ok": True, "detail": f"{disk.free // 1024**3}GB"})
        memory_mb = _memory_mb()
        if memory_mb and memory_mb < 1900:
            raise PreflightError(f"内存 {memory_mb}MB < 1900MB")
        if memory_mb:
            checks.append({"name": "内存", "ok": True, "detail": f"{memory_mb}MB"})
        return checks

    def _ensure_chat_model(self, catalog: ServiceCatalog, secrets: dict[str, str]) -> None:
        if self.config.effective("NEXUS_AGENTSCOPE_CHAT_MODEL_CONFIG", "").strip():
            return
        if self.config.chat_model_file.is_file():
            existing = self.config.chat_model_file.read_text(encoding="utf-8")
            if "NEXUS_AGENTSCOPE_CHAT_MODEL_CONFIG=" in existing:
                return
        agent = next((item for item in catalog.services if item.name == "agent-service"), None)
        if agent is None:
            raise ServiceError("缺少 Agent Service，无法登记聊天模型")
        deadline = time.monotonic() + 90
        ready = False
        while time.monotonic() < deadline:
            probe = self.probes.service(agent, catalog, secrets)
            if probe.ok:
                ready = True
                break
            self.sleep(2)
        if not ready:
            raise ServiceError("Agent Service 未就绪，无法登记聊天模型")
        result = self.runner.run(
            [
                str(catalog.release.agent_python),
                str(catalog.release.path / "services" / "agent-service" / "bootstrap_chat_model.py"),
            ],
            cwd=catalog.release.path / "services" / "agent-service",
            env=self._service_environment(secrets),
            timeout=120,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()
            raise ServiceError(
                "聊天模型登记失败："
                + (detail[-1] if detail else f"exit={result.returncode}")
            )
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip().startswith("{")]
        if not lines:
            raise ServiceError("bootstrap_chat_model.py 未输出聊天模型配置")
        payload = json.loads(lines[-1])
        if not {"type", "credential_id", "model", "parameters"} <= set(payload):
            raise ServiceError("聊天模型配置缺少必需字段")
        atomic_write(
            self.config.chat_model_file,
            env_line(
                "NEXUS_AGENTSCOPE_CHAT_MODEL_CONFIG",
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            ),
            mode=0o600,
        )
        own_generated_file(self.config, self.config.chat_model_file, 0o600)
        self.systemd.restart(("nexus-api",))
        self._wait_started(catalog)
        self.log("[config] 已登记 AgentScope 聊天模型")

    def _ensure_gateway_probe_key(self, secrets: dict[str, str]) -> ProbeResult:
        if not self.config.gateway_enabled:
            return ProbeResult("Gateway 探针 key", True, "gateway disabled", skipped=True, required=False)
        existing = (secrets.get("NEXUS_GATEWAY_PROBE_KEY") or self.config.gateway_probe_key).strip()
        if existing:
            if not secrets.get("NEXUS_GATEWAY_PROBE_KEY"):
                persist_secret(self.config, "NEXUS_GATEWAY_PROBE_KEY", existing)
            return ProbeResult("Gateway 探针 key", True, "复用已有只读测试 key", required=False)
        base = f"http://127.0.0.1:{self.config.ports['task_api']}/api/gateway"
        payload = {
            "name": "nexus-deploy-probe",
            "allowed_models": [],
            "max_concurrency": 0,
        }
        req = urllib.request.Request(
            base + "/keys",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                body = json.loads(response.read().decode())
            raw = str(body.get("key") or "").strip()
            if not raw:
                return ProbeResult("Gateway 探针 key", False, "Gateway API 未返回 key", required=False)
            persist_secret(self.config, "NEXUS_GATEWAY_PROBE_KEY", raw)
            self.log("[gateway] 已创建并保存只读探针 key（值不显示）")
            return ProbeResult("Gateway 探针 key", True, "已创建只读探针 key", required=False)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return ProbeResult(
                "Gateway 探针 key",
                False,
                f"创建探针 key 失败：{exc}；/v1/ 路由将无法验证",
                required=False,
            )

    def _ensure_gateway_route(self, secrets: dict[str, str]) -> ProbeResult:
        if not self.config.gateway_enabled:
            return ProbeResult("Gateway 默认路由", True, "gateway disabled", skipped=True, required=False)
        api_key = secrets.get("OPENAI_API_KEY") or secrets.get("DEEPSEEK_API_KEY") or ""
        if not api_key:
            return ProbeResult(
                "Gateway 默认路由",
                False,
                "未提供模型密钥；默认路由未完成",
                required=False,
            )
        base = f"http://127.0.0.1:{self.config.ports['task_api']}/api/gateway"

        def request(method: str, path: str, payload: dict | None = None) -> object:
            data = None if payload is None else json.dumps(payload).encode()
            req = urllib.request.Request(
                base + path,
                data=data,
                headers={"Content-Type": "application/json"},
                method=method,
            )
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.loads(response.read().decode())

        try:
            pools = request("GET", "/pools")
            pool_items = pools if isinstance(pools, list) else pools.get("items", [])
            pool = next((item for item in pool_items if item.get("name") == "nexus-pool"), None)
            if pool is None:
                pool = request("POST", "/pools", {"name": "nexus-pool"})
            nodes = request("GET", "/nodes")
            node_items = nodes if isinstance(nodes, list) else nodes.get("items", [])
            if not any(item.get("name") == "nexus-upstream" for item in node_items):
                created = request(
                    "POST",
                    "/nodes",
                    {
                        "name": "nexus-upstream",
                        "base_url": self.config.openai_base_url,
                        "pool_id": pool["id"],
                        "api_key": api_key,
                        "models": [self.config.openai_model],
                        "request_timeout_seconds": 90,
                    },
                )
                node_items = [created] if isinstance(created, dict) else node_items
            routes = request("GET", "/routes")
            route_items = routes if isinstance(routes, list) else routes.get("items", [])
            if not any(item.get("model") == self.config.openai_model for item in route_items):
                request(
                    "POST",
                    "/routes",
                    {"model": self.config.openai_model, "pool_id": pool["id"]},
                )
            node_state = "unknown"
            for item in node_items:
                if item.get("name") == "nexus-upstream":
                    node_state = str(item.get("config_state") or item.get("scheduling_state") or "unknown")
            if node_state not in {"ACTIVE"}:
                return ProbeResult(
                    "Gateway 默认路由",
                    False,
                    f"节点已登记但状态为 {node_state}；需按健康门显式启用",
                    required=False,
                )
            return ProbeResult("Gateway 默认路由", True, "默认路由已就绪", required=False)
        except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
            return ProbeResult(
                "Gateway 默认路由",
                False,
                f"默认路由未完成：{exc}",
                required=False,
            )

    def _migrate(self, release: Release, secrets: dict[str, str]) -> None:
        env = self._service_environment(secrets)
        result = self.runner.run(
            [str(release.python), "-m", "alembic", "upgrade", "head"],
            cwd=release.path / "services" / "task-platform",
            env=env,
            timeout=1800,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()
            raise MigrationError(
                f"Alembic 迁移失败：{detail[-1] if detail else result.returncode}"
            )

    def _read_revision(self, commit: str | None) -> str | None:
        if self.config.database_backend == "sqlite":
            return sqlite_revision(self.config.sqlite_path)
        env = {
            "MYSQL_PWD": self.config.database_password,
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        }
        result = self.runner.run(
            [
                "mysql",
                "--batch",
                "--skip-column-names",
                "--host",
                self.config.database_host,
                "--port",
                str(self.config.database_port),
                "--user",
                self.config.database_username,
                "--execute",
                "SELECT version_num FROM alembic_version",
                self.config.database_name,
            ],
            env=env,
            timeout=30,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip().splitlines()[-1] if result.stdout.strip() else None

    def _switch_current(self, release: Release) -> None:
        link = self.config.current_link
        if link.exists() and not link.is_symlink():
            raise PrepareError(f"current 已存在且不是符号链接：{link}；请先人工迁移旧目录")
        link.parent.mkdir(parents=True, exist_ok=True)
        temporary = link.with_name(f".{link.name}.tmp.{os.getpid()}")
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()
        os.symlink(release.path, temporary)
        os.replace(temporary, link)

    def _wait_started(self, catalog: ServiceCatalog) -> None:
        deadline = time.monotonic() + 90
        pending = set(catalog.required_units())
        while pending and time.monotonic() < deadline:
            pending = {unit for unit in pending if not self.systemd.is_active(unit)}
            if pending:
                self.sleep(1)
        if pending:
            raise ServiceError("服务未进入 active：" + ", ".join(sorted(pending)))

    def _readiness(
        self,
        catalog: ServiceCatalog,
        secrets: dict[str, str],
        *,
        probes: ProbeSuite | None = None,
    ) -> list[dict]:
        suite = probes or self.probes
        checks = [item.as_record() for item in self._process_checks(catalog)]
        checks.extend(
            item.as_record() for item in self._service_checks(catalog, secrets, probes=suite)
        )
        checks.extend(item.as_record() for item in self._infrastructure_checks(probes=suite))
        self._append_model_probe(checks)
        failed = [
            item
            for item in checks
            if not item["ok"] and not item.get("skipped") and item.get("required", True)
        ]
        if failed:
            names = ", ".join(item["name"] for item in failed)
            raise ProbeError(f"readiness 未通过：{names}", checks=checks)
        return checks

    def _process_checks(self, catalog: ServiceCatalog) -> list[ProbeResult]:
        results = []
        for service in catalog.required:
            if not service.unit:
                continue
            unit = f"{service.unit}.service"
            active = self.systemd.is_active(service.unit)
            pid = self.systemd.main_pid(service.unit) if active else 0
            results.append(
                ProbeResult(
                    f"进程 {unit}",
                    active and pid > 0,
                    f"active={active} MainPID={pid}",
                )
            )
        return results

    def _service_checks(
        self,
        catalog: ServiceCatalog,
        secrets: dict[str, str],
        *,
        probes: ProbeSuite | None = None,
    ) -> list[ProbeResult]:
        suite = probes or self.probes
        results: list[ProbeResult] = []
        for service in catalog.required:
            deadline = time.monotonic() + self.retry_seconds
            while True:
                result = suite.service(service, catalog, secrets)
                if result.ok or result.skipped or time.monotonic() >= deadline:
                    break
                self.sleep(1)
            results.append(result)
        return results

    def _infrastructure_checks(self, *, probes: ProbeSuite | None = None) -> list[ProbeResult]:
        suite = probes or self.probes
        selected_config = getattr(suite, "config", self.config)
        results = [suite.redis(), suite.sql()]
        if selected_config.nginx_enabled:
            results.append(suite.nginx_config())
            results.extend(
                suite.entry(
                    gateway_enabled=selected_config.gateway_enabled,
                    agentscope_enabled=True,
                )
            )
        return results

    def _append_model_probe(self, checks: list[dict]) -> None:
        probe = self.probes.model_cost_probe(self.model_probe)
        checks.append(probe.as_record())

    def _service_environment(self, secrets: dict[str, str]) -> dict[str, str]:
        env = dict(os.environ)
        for path in (
            self.config.user_env_file,
            self.config.secrets_file,
            self.config.runtime_env_file,
            self.config.chat_model_file,
            self.config.optional_env_file,
        ):
            env.update(parse_env_file(path))
        env.update(secrets)
        return env

    def _current_secrets(self) -> dict[str, str]:
        return parse_env_file(self.config.secrets_file)

    def _database_compatible(
        self,
        old_revision: str | None,
        new_revision: str | None,
        new_catalog: ServiceCatalog | None,
    ) -> bool:
        if old_revision is None or new_revision is None:
            return False
        if old_revision == new_revision:
            return True
        if new_catalog is None:
            return False
        manifest = new_catalog.release.manifest or {}
        return manifest.get("database_compatible_with_previous", False) is True

    def _retire_core_units(self, catalog: ServiceCatalog) -> None:
        keep = set(catalog.units()) | OPTIONAL_MODULE_UNITS
        candidates = set(LEGACY_CORE_UNITS)
        candidates.update(
            {
                "nexus-task-api",
                "nexus-runtime-beat",
                "nexus-runtime-control",
                "nexus-runtime-agent",
                "nexus-task-mcp",
                "nexus-library-mcp",
                "nexus-agent-service",
                "nexus-api",
                "nexus-gateway",
                "nexus-opencode-server",
                "nexus-assistant-opencode",
            }
        )
        for unit in sorted(candidates - keep):
            path = self.config.unit_dir / f"{unit}.service"
            if path.exists():
                self.systemd.stop((unit,))
                try:
                    self.runner.run(["systemctl", "disable", f"{unit}.service"])
                except Exception:
                    pass
                path.unlink(missing_ok=True)
                self.log(f"[services] 移除过期 unit {unit}")

    def _snapshot_root(self, commit: str) -> Path:
        return self.config.state_dir / "snapshots" / commit

    def _config_from_snapshot(self, commit: str) -> DeployConfig | None:
        path = self._snapshot_root(commit) / "full.env"
        if not path.is_file():
            return None
        return load_config(
            env_file=path,
            environ={"NEXUS_CONFIG_DIR": str(self.config.config_dir)},
            require_commit=False,
            include_runtime=False,
        )

    def _save_snapshot(self, catalog: ServiceCatalog) -> None:
        root = self._snapshot_root(catalog.release.commit)
        if root.exists():
            shutil.rmtree(root)
        (root / "units").mkdir(parents=True, exist_ok=True)
        for source in (
            self.config.runtime_env_file,
            self.config.opencode_config_file,
            self.config.chat_model_file,
            self.config.optional_env_file,
            self.config.nginx_conf,
        ):
            if source.is_file():
                shutil.copy2(source, root / source.name)
        atomic_write(
            root / "full.env",
            "".join(
                env_line(key, value)
                for key, value in sorted(self.config.effective_values.items())
            ),
            mode=0o600,
        )
        for unit in catalog.units():
            path = self.config.unit_dir / f"{unit}.service"
            if path.is_file():
                shutil.copy2(path, root / "units" / path.name)
        atomic_json(
            root / "manifest.json",
            {
                "commit": catalog.release.commit,
                "units": list(catalog.units()),
                "required_units": list(catalog.required_units()),
            },
        )

    def _restore_snapshot(
        self, release: Release, old_config: DeployConfig | None = None
    ) -> tuple[str, ...]:
        root = self._snapshot_root(release.commit)
        manifest = read_json(root / "manifest.json")
        units = tuple(str(item) for item in manifest.get("units", []) if item)
        if not units or not (root / "units").is_dir():
            raise RollbackBlocked(
                f"上一发布 {release.commit[:12]} 缺少部署快照（{root}），"
                "拒绝按当前配置猜测服务集合。部署已阻断，请按人工恢复手册处理。"
            )
        target_config = old_config or self.config
        self._switch_current(release)
        restore_map = {
            "runtime.env": (
                root / "full.env" if (root / "full.env").is_file() else root / "runtime.env",
                target_config.runtime_env_file,
            ),
            "opencode.json": (root / "opencode.json", target_config.opencode_config_file),
            "chat-model.env": (root / "chat-model.env", target_config.chat_model_file),
            "optional.env": (root / "optional.env", target_config.optional_env_file),
            "nexus.conf": (root / "nexus.conf", target_config.nginx_conf),
        }
        for name, (source, target) in restore_map.items():
            if source.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                own_generated_file(
                    target_config, target, 0o600 if name != "nexus.conf" else 0o644
                )
            elif target.is_file() and name in {"chat-model.env", "optional.env"}:
                target.unlink()
        for unit in CORE_UNIT_NAMES - set(units) - OPTIONAL_MODULE_UNITS:
            path = target_config.unit_dir / f"{unit}.service"
            if path.exists():
                try:
                    self.systemd.stop((unit,))
                except Exception:
                    pass
                self.runner.run(["systemctl", "disable", f"{unit}.service"])
                path.unlink(missing_ok=True)
        for unit in units:
            source = root / "units" / f"{unit}.service"
            if source.is_file():
                shutil.copy2(source, target_config.unit_dir / source.name)
        self.systemd.daemon_reload()
        self.systemd.enable(units)
        self._reload_nginx(target_config)
        required = tuple(str(item) for item in manifest.get("required_units", []) if item) or units
        return required

    def _reload_nginx(self, config: DeployConfig | None = None) -> None:
        selected = config or self.config
        if not selected.nginx_enabled or not selected.nginx_conf.is_file():
            return
        check = self.runner.run(["nginx", "-t"])
        if check.returncode != 0:
            raise ServiceError("回退后的 Nginx 配置校验失败")
        reload = self.runner.run(["systemctl", "reload", "nginx"])
        if reload.returncode != 0:
            reload = self.runner.run(["nginx", "-s", "reload"])
        if reload.returncode != 0:
            raise ServiceError("回退后的 Nginx reload 失败")

    def _remove_current(self) -> None:
        link = self.config.current_link
        if link.is_symlink():
            link.unlink()

    def _stop_quietly(self, catalog: ServiceCatalog | None) -> None:
        if catalog is None:
            return
        try:
            self.systemd.stop(catalog.units())
        except Exception as exc:  # noqa: BLE001 - best effort before recovery
            self.log(f"[rollback] 停止服务失败：{exc}")

    def _prune_releases(self) -> None:
        if self.config.keep_releases <= 0:
            return
        directories = sorted(
            [path for path in self.config.releases_dir.iterdir() if path.is_dir()],
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        current = self.config.current_link.resolve() if self.config.current_link.exists() else None
        success = self.records.last_success()
        protected_names = {
            str(success.get("commit") or ""),
            str(success.get("previous_commit") or ""),
        }
        keep = set(directories[: self.config.keep_releases])
        if current:
            keep.add(current)
        for path in directories:
            if path.name in protected_names:
                keep.add(path)
        for path in directories:
            if path not in keep:
                shutil.rmtree(path, ignore_errors=True)

    def _log_stage(self, stage: str) -> None:
        self.log(f"[stage] {stage}")


def _version_ge(value: str, minimum: str) -> bool:
    try:
        left = tuple(int(part) for part in value.split("."))
        right = tuple(int(part) for part in minimum.split("."))
    except ValueError:
        return False
    return left >= right


def _memory_mb() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _nearest_existing(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate

