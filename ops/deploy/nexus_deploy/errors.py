class DeployError(RuntimeError):
    """Base error for a deployment that must stop."""


class ConfigError(DeployError):
    """Invalid or incomplete deployment configuration."""


class PreflightError(DeployError):
    """A blocking preflight check failed."""


class PrepareError(DeployError):
    """Release preparation failed before touching the running product."""


class BackupError(DeployError):
    """Database or configuration backup could not be verified."""


class MigrationError(DeployError):
    """Database migration failed."""


class ServiceError(DeployError):
    """Service control failed."""


class ProbeError(DeployError):
    """A readiness or entry probe failed."""

    def __init__(self, message: str, checks: list[dict] | None = None) -> None:
        super().__init__(message)
        self.checks = checks or []


class LockError(DeployError):
    """Another deployment holds the deployment lock."""


class RollbackBlocked(DeployError):
    """Database changes make an automatic code rollback unsafe."""
