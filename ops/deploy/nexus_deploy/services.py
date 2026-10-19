from __future__ import annotations

import socket
from dataclasses import dataclass
from pathlib import Path

from .config import DeployConfig
from .gitops import Release


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    unit: str
    description: str
    command: tuple[str, ...]
    cwd: Path
    required: bool = True
    port_key: str | None = None
    probe: str = "process"
    http_path: str = ""
    celery_node: str = ""
    celery_queue: str = ""
    base_url: str = ""

    def display(self) -> str:
        return f"{self.unit} ({self.description})"


@dataclass(frozen=True)
class ServiceCatalog:
    config: DeployConfig
    release: Release
    services: tuple[ServiceSpec, ...]

    @property
    def required(self) -> tuple[ServiceSpec, ...]:
        return tuple(service for service in self.services if service.required)

    @property
    def optional(self) -> tuple[ServiceSpec, ...]:
        return tuple(service for service in self.services if not service.required)

    def by_name(self, name: str) -> ServiceSpec:
        for service in self.services:
            if service.name == name or service.unit == name:
                return service
        raise KeyError(name)

    def units(self, *, required_only: bool = False) -> tuple[str, ...]:
        selected = self.required if required_only else self.services
        return tuple(service.unit for service in selected if service.unit)

    def required_units(self) -> tuple[str, ...]:
        return self.units(required_only=True)

    def port_for(self, service: ServiceSpec) -> int | None:
        if not service.port_key:
            return None
        return self.config.ports[service.port_key]


def _task_python(release: Release) -> str:
    return str(release.path / "services" / "task-platform" / ".venv" / "bin" / "python")


def _nexus_api_python(release: Release) -> str:
    return str(release.path / "services" / "nexus-api" / ".venv" / "bin" / "python")


def _agent_python(release: Release) -> str:
    return str(
        release.path / "services" / "agent-service" / ".venv-agentscope" / "bin" / "python"
    )


def _opencode_binary(config: DeployConfig, release: Release) -> str:
    configured = config.effective("ASTRA_OPENCODE_BINARY", "").strip()
    if configured:
        return configured
    return str(release.opencode_binary)


def build_catalog(config: DeployConfig, release: Release) -> ServiceCatalog:
    task_root = release.path / "services" / "task-platform"
    nexus_api_root = release.path / "services" / "nexus-api"
    agent_root = release.path / "services" / "agent-service"
    hostname = socket.gethostname()
    task_python = _task_python(release)
    task_mcp_binary = task_root / ".venv" / "bin" / "astra-task-mcp"

    services: list[ServiceSpec] = [
        ServiceSpec(
            name="task-api",
            unit="nexus-task-api",
            description="Task API",
            command=(
                task_python,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                config.bind_host,
                "--port",
                str(config.ports["task_api"]),
            ),
            cwd=task_root,
            port_key="task_api",
            probe="http",
            http_path="/api/health",
        ),
    ]

    execution_url = config.effective("ASTRA_OPENCODE_SERVER_URL", "").strip()
    if execution_url:
        services.append(
            ServiceSpec(
                name="opencode-server",
                unit="",
                description="Employee OpenCode Server (external)",
                command=(),
                cwd=task_root,
                port_key=None,
                probe="opencode",
                base_url=execution_url,
            )
        )
    else:
        services.append(
            ServiceSpec(
                name="opencode-server",
                unit="nexus-opencode-server",
                description="Employee OpenCode Server",
                command=(task_python, "-m", "app.runtime.opencode_server"),
                cwd=task_root,
                port_key="opencode",
                probe="opencode",
            )
        )

    assistant_enabled = config.effective("ASTRA_ASSISTANT_ENABLED", "true").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    assistant_url = config.effective("ASTRA_ASSISTANT_OPENCODE_URL", "").strip()
    if assistant_enabled and assistant_url:
        services.append(
            ServiceSpec(
                name="assistant-opencode",
                unit="",
                description="Platform Assistant OpenCode Server (external)",
                command=(),
                cwd=task_root,
                port_key=None,
                probe="opencode",
                base_url=assistant_url,
            )
        )
    elif assistant_enabled:
        services.append(
            ServiceSpec(
                name="assistant-opencode",
                unit="nexus-assistant-opencode",
                description="Platform Assistant OpenCode Server",
                command=(task_python, "-m", "app.assistant.server"),
                cwd=task_root,
                port_key="assistant_opencode",
                probe="opencode",
            )
        )

    services.extend([
        ServiceSpec(
            name="runtime-beat",
            unit="nexus-runtime-beat",
            description="Celery Beat",
            command=(
                task_python,
                "-m",
                "celery",
                "-A",
                "app.runtime.celery_app:celery",
                "beat",
                "--loglevel=INFO",
                f"--schedule={config.celery_schedule}",
            ),
            cwd=task_root,
            probe="celery-beat",
            celery_node=f"nexus-control@{hostname}",
            celery_queue="nexus-control",
        ),
        ServiceSpec(
            name="runtime-control",
            unit="nexus-runtime-control",
            description="Celery Control Worker",
            command=(
                task_python,
                "-m",
                "celery",
                "-A",
                "app.runtime.celery_app:celery",
                "worker",
                "--loglevel=INFO",
                "--pool=solo",
                "--queues=nexus-control",
                f"--hostname=nexus-control@{hostname}",
            ),
            cwd=task_root,
            probe="celery",
            celery_node=f"nexus-control@{hostname}",
            celery_queue="nexus-control",
        ),
        ServiceSpec(
            name="runtime-agent",
            unit="nexus-runtime-agent",
            description="Celery Agent Worker",
            command=(
                task_python,
                "-m",
                "celery",
                "-A",
                "app.runtime.celery_app:celery",
                "worker",
                "--loglevel=INFO",
                "--pool=solo",
                "--queues=agent",
                f"--hostname=nexus-agent@{hostname}",
            ),
            cwd=task_root,
            probe="celery",
            celery_node=f"nexus-agent@{hostname}",
            celery_queue="agent",
        ),
        ServiceSpec(
            name="task-mcp",
            unit="nexus-task-mcp",
            description="Task MCP",
            command=(
                (str(task_mcp_binary),)
                if task_mcp_binary.is_file()
                else (task_python, "-c", "from app.task_mcp import main; main()")
            ),
            cwd=task_root,
            port_key="task_mcp",
            probe="mcp",
            http_path="/mcp",
            celery_queue="",
        ),
        ServiceSpec(
            name="agent-service",
            unit="nexus-agent-service",
            description="AgentScope Agent Service",
            command=(_agent_python(release), str(agent_root / "agent_service.py")),
            cwd=agent_root,
            port_key="agent_service",
            probe="http",
            http_path="/health",
        ),
        ServiceSpec(
            name="nexus-api",
            unit="nexus-api",
            description="Nexus API",
            command=(
                _nexus_api_python(release),
                "-m",
                "uvicorn",
                "nexus_api.main:app",
                "--app-dir",
                str(nexus_api_root),
                "--host",
                config.bind_host,
                "--port",
                str(config.ports["nexus_api"]),
            ),
            cwd=nexus_api_root,
            port_key="nexus_api",
            probe="http",
            http_path="/api/health",
        ),
    ])

    if config.library_mcp_enabled:
        services.append(
            ServiceSpec(
                name="library-mcp",
                unit="nexus-library-mcp",
                description="Library MCP",
                command=(task_python, "-m", "app.mcp_server"),
                cwd=task_root,
                port_key="library_mcp",
                probe="mcp",
                http_path="/mcp",
            )
        )

    if config.gateway_enabled:
        services.append(
            ServiceSpec(
                name="gateway",
                unit="nexus-gateway",
                description="Gateway",
                command=(
                    task_python,
                    "-m",
                    "uvicorn",
                    "app.gateway.server:app",
                    "--host",
                    config.bind_host,
                    "--port",
                    str(config.ports["gateway"]),
                ),
                cwd=task_root,
                port_key="gateway",
                probe="http",
                http_path="/healthz",
            )
        )

    return ServiceCatalog(config=config, release=release, services=tuple(services))


def opencode_binary(config: DeployConfig, release: Release) -> str:
    return _opencode_binary(config, release)
