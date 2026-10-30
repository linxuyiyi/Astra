"""Nexus owns process decisions; the target only probes, spawns, and signals verified identities."""

from __future__ import annotations

import asyncio
import json
import shlex


async def _python(worker, source: str, *args: str) -> str:
    from app.runtime.remote_provisioning import agent_python

    return await worker._command(shlex.join([agent_python(worker.environment.id), "-I", "-c", source, *args]))


async def identity(worker, pid: int) -> dict:
    source = (
        "import json,sys;from app.worker_agent.process_manager import process_identity;"
        "print(json.dumps(process_identity(int(sys.argv[1]))))"
    )
    return json.loads(await _python(worker, source, str(pid)))


async def signal_owned(worker, expected: dict) -> None:
    source = (
        "import os,json,signal,sys\n"
        "from app.worker_agent.process_manager import process_identity\n"
        "expected=json.loads(sys.argv[1]);pid=expected['pid']\n"
        "if pid<=1 or pid==os.getpid() or expected.get('pgid')!=pid or process_identity(pid)!=expected:\n"
        " raise SystemExit('受管进程身份已变化，未发送停止信号')\n"
        "os.killpg(pid,signal.SIGTERM)\n"
    )
    await worker._ensure_safe()
    await _python(worker, source, json.dumps(expected))


async def manage(worker, name: str, *, restart: bool) -> None:
    from app.runtime.remote_provisioning import WORKER_SECRET_DIR, agent_python, worker_install_dir

    await worker._ensure_safe()
    root = worker_install_dir(worker.environment.id)
    record_path, log_path = f"{root}/{name}-process.json", f"{root}/{name}-process.log"
    raw = await worker.ssh.read_file(record_path)
    record = None
    if raw:
        try:
            record = json.loads(raw)
            expected = record["identity"]
            pid = expected["pid"]
            if not isinstance(pid, int) or pid <= 1 or expected.get("pgid") != pid:
                raise ValueError("invalid identity")
        except (ValueError, KeyError, TypeError) as exc:
            raise RuntimeError("进程身份记录无效，已保留现场") from exc
    current = await identity(worker, record["identity"]["pid"]) if record else {}
    if current and current != record["identity"]:
        raise RuntimeError("受管进程身份已变化，拒绝停止或覆盖记录")
    if current and current["cwd"] != worker.environment.workspace_root:
        canonical = await worker._command(shlex.join(["realpath", worker.environment.workspace_root]))
        if current["cwd"] != canonical:
            raise RuntimeError("受管进程工作区不一致，拒绝停止")
    command = (
        [
            worker.target_binary,
            "serve",
            "--hostname",
            "127.0.0.1",
            "--port",
            str(worker.environment.opencode_server_port),
        ]
        if name == "opencode"
        else [agent_python(worker.environment.id), "-m", "app.worker_agent"]
    )
    if name == "opencode":
        source = (
            "import json,os,sys\n"
            "from app.worker_agent.channel_switch import _probe_listener,_listener_pid\n"
            "port=int(sys.argv[1]);active=_probe_listener(port);pid=_listener_pid(port) if active else None\n"
            "print(json.dumps({'active':active,'pgid':os.getpgid(pid) if pid else None}))\n"
        )
        listener = json.loads(await _python(worker, source, str(worker.environment.opencode_server_port)))
        if listener["active"] and (not current or listener["pgid"] != current["pid"]):
            raise RuntimeError("OpenCode 端口不属于受管进程，拒绝停止或重复启动")
    if current and not restart:
        if record["command"] != command:
            raise RuntimeError("受管进程启动参数已变化，需要显式重启")
        return
    if current:
        await worker._native_idle(strict=True)
        await signal_owned(worker, current)
        deadline = asyncio.get_running_loop().time() + 5
        while await identity(worker, current["pid"]):
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError("受管进程未在时限内退出，保留身份记录")
            await asyncio.sleep(0.1)
    if name == "opencode":
        deadline = asyncio.get_running_loop().time() + 5
        probe = (
            "from app.worker_agent.channel_switch import _probe_listener;import sys;"
            "print('active' if _probe_listener(int(sys.argv[1])) else 'stopped')"
        )
        while await _python(worker, probe, str(worker.environment.opencode_server_port)) != "stopped":
            await worker._ensure_safe()
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError("OpenCode 端口仍有监听，拒绝重复启动")
            await asyncio.sleep(0.1)
        await worker._native_idle(strict=True)
    elif record is None:
        from sqlalchemy import select

        from app.database import SessionLocal
        from app.models import WorkerInstance

        async with SessionLocal() as session:
            instance = await session.scalar(
                select(WorkerInstance).where(WorkerInstance.environment_id == worker.environment.id)
            )
        if instance is not None and instance.status == "online":
            raise RuntimeError("在线 Worker 缺少受管进程记录，不自动启动重复 Agent")
    # This fixed primitive starts the exact command chosen above and atomically records its identity.
    source = (
        "import os,json,subprocess,sys,time\n"
        "from pathlib import Path\n"
        "from app.worker_agent.process_manager import process_identity,read_environment\n"
        "from app.worker_agent.process_env import child_environ\n"
        "record,log,envfile,workspace,raw,kind=sys.argv[1:];command=json.loads(raw)\n"
        "env=read_environment(Path(envfile));env=child_environ(env) if kind=='opencode' else env\n"
        "with open(log,'ab') as output:\n"
        " os.chmod(log,0o600)\n"
        " process=subprocess.Popen(command,cwd=workspace,env=env,stdin=subprocess.DEVNULL,"
        "stdout=output,stderr=subprocess.STDOUT,start_new_session=True,close_fds=True)\n"
        "time.sleep(0.1);actual=process_identity(process.pid)\n"
        "if process.poll() is not None or not actual:raise SystemExit('进程启动后立即退出，请检查私有日志')\n"
        "temp=record+'.tmp';Path(temp).write_text(json.dumps({'identity':actual,'command':command}))\n"
        "os.chmod(temp,0o600);os.replace(temp,record)\n"
    )
    await worker._ensure_safe()
    await _python(
        worker,
        source,
        record_path,
        log_path,
        f"{WORKER_SECRET_DIR}/{name}-{worker.environment.id}.env",
        worker.environment.workspace_root,
        json.dumps(command),
        name,
    )
