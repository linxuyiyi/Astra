"""Worker Gateway 管理接口的内部服务密钥。

任务平台的 HTTP 路由没有统一登录。图书馆身份来自受信任的上游头，MCP 密钥只保护 MCP。
管理接口因此单独使用一把内部密钥。密钥由启动脚本在拉起 API 之前生成一次。
API 进程发现密钥为空时只记录日志并拒绝管理调用，不在每个进程里各自生成。
WebSocket 仍用 Worker token，健康检查不带这把密钥。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from secrets import token_urlsafe

ENV_NAME = "NEXUS_WORKER_GATEWAY_ADMIN_KEY"
logger = logging.getLogger(__name__)
_warned = False


def warn_if_admin_key_missing(key: str) -> None:
    global _warned
    if (key or "").strip() or _warned:
        return
    _warned = True
    logger.warning("管理密钥未配置，拒绝管理调用")


def provision_worker_gateway_admin_key(env_path: Path) -> str | None:
    """加锁生成一次。已经注入的环境变量优先，目录不可写时不抛异常。"""

    injected = os.environ.get(ENV_NAME, "").strip()
    if injected:
        return injected
    try:
        env_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("管理密钥目录不可写，拒绝在进程内生成：%s", exc)
        return _remember(_read_key_safe(env_path))
    lock_path = env_path.with_name(env_path.name + ".gateway-admin.lock")
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        logger.warning("管理密钥锁不可用：%s", exc)
        return _remember(_read_key_safe(env_path))
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        _lock_exclusive(fd)
        injected = os.environ.get(ENV_NAME, "").strip()
        if injected:
            return injected
        existing = _read_key_safe(env_path)
        if existing:
            return _remember(existing)
        generated = f"wgk_{token_urlsafe(32)}"
        try:
            _atomic_upsert(env_path, generated)
        except OSError as exc:
            logger.warning("管理密钥写入失败，API 将拒绝管理调用：%s", exc)
            return None
        written = _read_key_safe(env_path)
        return _remember(written or generated)
    finally:
        try:
            _unlock(fd)
        except OSError:
            logger.debug("管理密钥锁释放失败", exc_info=True)
        os.close(fd)


def _remember(value: str) -> str | None:
    if not value:
        return None
    os.environ[ENV_NAME] = value
    return value


def _lock_exclusive(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        return
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_EX)


def _unlock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)


def _read_key(path: Path) -> str:
    if not path.exists():
        return ""
    prefix = f"{ENV_NAME}="
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(prefix):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _read_key_safe(path: Path) -> str:
    try:
        return _read_key(path)
    except OSError as exc:
        logger.warning("管理密钥无法读取：%s", exc)
        return ""


def _atomic_upsert(path: Path, value: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    prefix = f"{ENV_NAME}="
    replaced = False
    updated: list[str] = []
    for line in lines:
        if line.startswith(prefix):
            updated.append(f"{ENV_NAME}={value}")
            replaced = True
        else:
            updated.append(line)
    if not replaced:
        if updated and updated[-1] != "":
            updated.append("")
        updated.append(f"{ENV_NAME}={value}")
    payload = ("\n".join(updated).rstrip() + "\n").encode("utf-8")
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        logger.debug("管理密钥文件权限未能收紧", exc_info=True)
