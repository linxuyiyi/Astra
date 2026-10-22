"""公司容器问题单回归：传输、进程身份、运行事实与分类发现。"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import socket
import ssl
import subprocess
import sys
import tarfile
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import mysql, sqlite

from app.worker_agent import channel_switch, process_manager
from app.worker_agent.local_opencode import gateway_http_base
from app.worker_agent.process_env import child_environ
from app.worker_agent.transport import connect_websocket, websocket_url
from tests.platform.test_remote_install_v145 import _functions
from tests.platform.test_task_responsibility import session  # noqa: F401


@pytest.mark.parametrize("scheme,ws,http", [
    ("http", "ws", "http"), ("https", "wss", "https"),
    ("ws", "ws", "http"), ("wss", "wss", "https"),
])
def test_control_and_data_scheme(scheme, ws, http):
    url = f"{scheme}://worker.test:8443/api/worker-gateway/connect?x=1"
    assert websocket_url(url) == f"{ws}://worker.test:8443/api/worker-gateway/connect?x=1"
    assert gateway_http_base(url) == f"{http}://worker.test:8443"


@pytest.mark.parametrize("url", ["ftp://worker.test", "https://", "https://u:p@worker.test", "wss://a/#x"])
def test_invalid_gateway_rejected(url):
    with pytest.raises(ValueError):
        websocket_url(url)
    with pytest.raises(ValueError):
        gateway_http_base(url)


async def test_websocket_retains_default_tls_verification(monkeypatch):
    calls = []

    async def connect(url, **kwargs):
        calls.append((url, kwargs))
        return object()

    monkeypatch.setattr("websockets.connect", connect)
    await connect_websocket("https://worker.test/connect", max_size=1234)
    assert calls == [("wss://worker.test/connect", {"max_size": 1234})]


@pytest.mark.skipif(not shutil.which("openssl"), reason="需要 openssl 生成临时测试证书")
async def test_self_signed_tls_requires_trusted_ca(tmp_path, monkeypatch):
    import websockets

    from app.worker_agent.transport import WorkerTransportError

    certificate = tmp_path / "ca.pem"
    private_key = tmp_path / "key.pem"
    subprocess.run([
        "openssl", "req", "-newkey", "rsa:2048", "-nodes", "-x509", "-days", "1",
        "-keyout", str(private_key), "-out", str(certificate), "-subj", "/CN=qa-worker",
        "-addext", "subjectAltName=IP:127.0.0.1",
    ], check=True, capture_output=True)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, private_key)

    async def handler(connection, *_unused):
        await connection.wait_closed()

    async with websockets.serve(handler, "127.0.0.1", 0, ssl=context) as server:
        url = f"https://127.0.0.1:{server.sockets[0].getsockname()[1]}/connect"
        with pytest.raises(WorkerTransportError, match="SSLCertVerificationError"):
            await connect_websocket(url, max_size=1234)
        monkeypatch.setenv("SSL_CERT_FILE", str(certificate))
        connected = await connect_websocket(url, max_size=1234)
        await connected.close()


def test_config_environment_is_preserved_without_worker_credentials():
    result = child_environ({
        "PATH": "/bin", "OPENCODE_CONFIG": "/etc/nexus/opencode.jsonc",
        "OPENCODE_CONFIG_DIR": "/etc/nexus", "NEXUS_WORKER_TOKEN": "canary",
        "NEXUS_WORKER_GATEWAY_URL": "https://worker.test", "SSL_CERT_FILE": "/etc/ca.pem",
        "GIT_SSL_CAINFO": "/etc/ca.pem",
    })
    assert result["OPENCODE_CONFIG"] == "/etc/nexus/opencode.jsonc"
    assert result["OPENCODE_CONFIG_DIR"] == "/etc/nexus"
    assert result["SSL_CERT_FILE"] == result["GIT_SSL_CAINFO"] == "/etc/ca.pem"
    assert not any(key.startswith("NEXUS_") for key in result)


def test_container_listener_restart_actually_starts_new_process(tmp_path, monkeypatch):
    monkeypatch.setattr(channel_switch, "has_systemd", lambda: False)
    monkeypatch.setattr(channel_switch, "assert_native_idle", lambda *a, **kw: True)
    listeners = iter([42, None])
    monkeypatch.setattr(channel_switch, "_listener_pid", lambda _p: next(listeners))
    monkeypatch.setattr(channel_switch, "_opencode_listener", lambda _p: True)
    identity = {"pid": 42, "cwd": str(tmp_path.resolve()), "starttime": "123"}
    monkeypatch.setattr(channel_switch, "process_identity", lambda _p: identity)
    actions = []
    monkeypatch.setattr(channel_switch.os, "kill", lambda *a: actions.append(("stop", a)))
    monkeypatch.setattr(channel_switch.subprocess, "Popen", lambda *a, **kw: actions.append(("start", a)))
    monkeypatch.setattr(channel_switch, "_wait_until_healthy", lambda *a, **kw: actions.append(("health", a)))
    channel_switch.restart_local_opencode(
        str(tmp_path), 14096,
        environ={"NEXUS_WORKER_OPENCODE_UNIT": "nexus-opencode-1.service"},
    )
    assert [item[0] for item in actions] == ["stop", "start", "health"]


def _config(gateway="http://10.0.0.8:8110", mcp="http://10.0.0.8:8201/mcp"):
    return {
        "provider": {"nexus-gateway": {"options": {"baseURL": gateway, "apiKey": "test-key"}}},
        "mcp": {"nexus-library": {"url": mcp}},
    }


def test_effective_config_waits_after_health_without_disk_fallback(tmp_path, monkeypatch):
    path = tmp_path / "opencode.jsonc"
    path.write_text(json.dumps(_config()))
    monkeypatch.setattr(channel_switch, "assert_native_idle", lambda *a, **kw: False)
    monkeypatch.setattr(channel_switch.time, "sleep", lambda _t: None)
    reads = iter([{}, RuntimeError("读取生效配置失败"), {}, _config()])

    def effective():
        result = next(reads)
        if isinstance(result, Exception):
            raise result
        return result

    result = channel_switch.apply_channel_switch(
        workspace=str(tmp_path), port=14096, config_path=path, probe=lambda _url: True,
        environ={"NEXUS_REMOTE_GATEWAY_URL": "http://10.0.0.8:8110",
                 "NEXUS_MCP_RESOURCE_URL": "http://10.0.0.8:8201/mcp"},
        reload=lambda: None, read_effective=effective,
    )
    assert result["verified"] is True
    assert result["reloaded"] is True


def test_missing_effective_fields_cannot_be_verified_from_disk(tmp_path, monkeypatch):
    path = tmp_path / "opencode.jsonc"
    path.write_text(json.dumps(_config()))
    monkeypatch.setattr(channel_switch, "assert_native_idle", lambda *a, **kw: False)
    monkeypatch.setattr(channel_switch.time, "sleep", lambda _t: None)
    counter = iter(range(0, 100, 2))
    monkeypatch.setattr(channel_switch.time, "monotonic", lambda: next(counter))
    with pytest.raises(RuntimeError, match="生效配置缺少"):
        channel_switch.apply_channel_switch(
            workspace=str(tmp_path), port=14096, config_path=path, probe=lambda _url: True,
            environ={"NEXUS_REMOTE_GATEWAY_URL": "http://10.0.0.8:8110",
                     "NEXUS_MCP_RESOURCE_URL": "http://10.0.0.8:8201/mcp"},
            reload=lambda: None, read_effective=lambda: {},
        )


def test_stale_process_record_never_signals(tmp_path, monkeypatch):
    record = tmp_path / "process.json"
    identity = {"pid": 123, "starttime": "old", "cwd": "/work", "argv": ["worker"], "pgid": 123}
    record.write_text(json.dumps({"identity": identity, "command": ["worker"]}))
    monkeypatch.setattr(process_manager, "process_identity", lambda _pid: {**identity, "starttime": "new"})
    monkeypatch.setattr(process_manager.os, "killpg", lambda *_a: pytest.fail("不能向复用的 PID 发信号"))
    with pytest.raises(RuntimeError, match="身份已变化"):
        process_manager._stop(record)
    assert record.exists()


@pytest.mark.skipif(sys.platform != "linux", reason="身份读取使用 Linux /proc")
def test_detached_process_eof_idempotency_restart_and_environment(tmp_path):
    record = tmp_path / "opencode-process.json"
    env_file = tmp_path / "worker.env"
    observed = tmp_path / "observed.json"
    env_file.write_text("OPENCODE_CONFIG=/managed/config.jsonc\nNEXUS_WORKER_TOKEN=canary\n")
    code = (
        "import os,json,time,pathlib;"
        f"pathlib.Path({str(observed)!r}).write_text(json.dumps(dict(os.environ)));"
        "time.sleep(60)"
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    command = [sys.executable, "-c", code, "--port", str(port)]
    base = [
        sys.executable, "-m", "app.worker_agent.process_manager", "ensure",
        "--record", str(record), "--workspace", str(tmp_path), "--log", str(tmp_path / "process.log"),
        "--env-file", str(env_file), "--opencode", "--", *command,
    ]
    try:
        # capture_output 只有 SSH 等效标准流到 EOF 才能返回。
        first = subprocess.run(base, capture_output=True, text=True, timeout=5)
        assert first.returncode == 0, first.stderr
        identity = json.loads(record.read_text())["identity"]
        observed_env = json.loads(observed.read_text())
        assert observed_env["OPENCODE_CONFIG"] == "/managed/config.jsonc"
        assert "NEXUS_WORKER_TOKEN" not in observed_env
        again = subprocess.run(base, capture_output=True, text=True, timeout=5)
        assert again.returncode == 0, again.stderr
        assert json.loads(record.read_text())["identity"] == identity
        base[3] = "restart"
        restarted = subprocess.run(base, capture_output=True, text=True, timeout=10)
        assert restarted.returncode == 0, restarted.stderr
        assert json.loads(record.read_text())["identity"]["pid"] != identity["pid"]
    finally:
        if record.exists():
            process_manager._stop(record)


def test_bootstrap_reads_source_with_missing_target_python(tmp_path):
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps({"python_source_tgz": str(tmp_path / "missing-source.tgz")}))
    source = (
        "set -euo pipefail\nfail() { echo \"$*\" >&2; exit 1; }\n"
        + _functions()
        + f"\npython_bin={shlex.quote(str(tmp_path / 'missing-python'))}\n"
        + f"payload={shlex.quote(str(payload))}\nbootstrap_python\n"
    )
    result = subprocess.run(["bash", "-c", source], capture_output=True, text=True, timeout=5)
    assert result.returncode != 0
    assert "远端 Python 源码包不存在" in result.stderr
    assert "未配置" not in result.stderr


@pytest.mark.skipif(sys.platform != "linux", reason="源码构建需要 Linux flock")
def test_bootstrap_source_build_returns_actual_python_and_reuses_it(tmp_path):
    source_dir = tmp_path / "Python-source"
    source_dir.mkdir()
    configure = source_dir / "configure"
    configure.write_text(
        '#!/bin/sh\nfor arg in "$@"; do case "$arg" in --prefix=*) '
        'echo "${arg#--prefix=}" > prefix ;; esac; done\n'
    )
    configure.chmod(0o700)
    archive = tmp_path / "Python.tgz"
    with tarfile.open(archive, "w:gz") as out:
        out.add(source_dir, arcname="Python-source")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    make = fake_bin / "make"
    make.write_text(
        '#!/bin/sh\nif test "$1" = install; then prefix=$(cat prefix); '
        'mkdir -p "$prefix/bin"; ln -s '
        + shlex.quote(sys.executable) + ' "$prefix/bin/python3"; fi\n'
    )
    make.chmod(0o700)
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps({"python_source_tgz": str(archive)}))
    source = (
        'set -euo pipefail\nfail() { echo "$*" >&2; exit 1; }\nstep() { :; }\n'
        + _functions()
        + f"\npython_bin={shlex.quote(str(tmp_path / 'missing-python'))}\n"
        + f"payload={shlex.quote(str(payload))}\ncache={shlex.quote(str(tmp_path / 'cache'))}\n"
        + 'bootstrap_python\ntest -x "$python_bin"\nfirst=$python_bin\n'
        + 'bootstrap_python\ntest "$first" = "$python_bin"\n'
    )
    env = {**os.environ, "HOME": str(tmp_path), "PATH": f"{fake_bin}:{os.environ['PATH']}"}
    result = subprocess.run(["bash", "-c", source], env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_process_restart_gate_loss_prevents_new_start(tmp_path, monkeypatch):
    record = tmp_path / "process.json"
    record.write_text(json.dumps({"identity": {"pid": 123, "cwd": str(tmp_path.resolve())}}))
    monkeypatch.setattr(process_manager, "managed_running", lambda _p: True)
    stopped = []
    monkeypatch.setattr(process_manager, "_stop", lambda _p: stopped.append(True))
    monkeypatch.setattr(process_manager.subprocess, "Popen", lambda *a, **kw: pytest.fail("租约失效不能启动"))

    def gate():
        if stopped:
            raise RuntimeError("控制命令已失效")

    with pytest.raises(RuntimeError, match="已失效"):
        process_manager.manage_process(
            record, command=["worker"], workspace=str(tmp_path), log_path=tmp_path / "worker.log",
            environ={}, restart=True, gate=gate,
        )
    assert stopped == [True]


def test_json_snapshot_uses_mysql_json_value():
    from app.runtime.maintenance import status_detail_matches

    value = {"nested": {"a": 1}, "enabled": True}
    for dialect in (mysql.dialect(), sqlite.dialect()):
        fake = SimpleNamespace(get_bind=lambda dialect=dialect: SimpleNamespace(dialect=dialect))
        compiled = str(status_detail_matches(fake, value).compile(dialect=dialect))
        assert ("CAST(" in compiled) == (dialect.name == "mysql")


@pytest.mark.skipif(not os.environ.get("NEXUS_TEST_MYSQL_SOCKET"), reason="需独立 MySQL 测试 socket")
def test_mysql_json_snapshot_accepts_match_and_rejects_stale():
    from sqlalchemy import create_engine, text, update
    from sqlalchemy.engine import URL
    from sqlalchemy.orm import Session

    from app.models import OpenCodeExecutionEnvironment as Environment
    from app.runtime.maintenance import status_detail_matches

    engine = create_engine(URL.create(
        "mysql+pymysql", username="root", database="nexus_container_qa",
        query={"unix_socket": os.environ["NEXUS_TEST_MYSQL_SOCKET"]},
    ))
    value = {"nested": {"b": True, "a": [1, 2]}, "state": "old"}
    try:
        with engine.begin() as connection:
            connection.execute(text(
                "CREATE TEMPORARY TABLE t_nexus_opencode_execution_environments "
                "(id INT PRIMARY KEY, status VARCHAR(32), status_detail JSON, updated_at DATETIME(6))"
            ))
            connection.execute(text(
                "INSERT INTO t_nexus_opencode_execution_environments VALUES (1, 'UNKNOWN', :detail, NOW(6))"
            ), {"detail": json.dumps(value)})
            session = Session(bind=connection)
            statement = update(Environment).where(
                Environment.id == 1, status_detail_matches(session, value)
            ).values(status="AVAILABLE")
            assert connection.execute(statement).rowcount == 1
            connection.execute(text(
                "UPDATE t_nexus_opencode_execution_environments "
                "SET status_detail = JSON_OBJECT('changed', true) WHERE id = 1"
            ))
            assert connection.execute(statement).rowcount == 0
    finally:
        engine.dispose()


async def test_categories_are_authenticated_enabled_ordered_and_current(session):  # noqa: F811
    from app.models import TaskCategory
    from app.task_mcp import build_mcp_server
    from app.task_tools import TaskTools, nexus_employee_id

    session.add_all([
        TaskCategory(id="later", name="后", sort_order=2),
        TaskCategory(id="first", name="先", sort_order=1, task_prompt="执行模板", question_prompt="问题模板"),
        TaskCategory(id="disabled", name="禁用", enabled=False),
    ])
    await session.commit()
    tools = TaskTools(session)
    assert (await tools.categories())["ok"] is False
    token = nexus_employee_id.set("emp-test")
    try:
        result = await tools.categories()
        assert [row["id"] for row in result["categories"]] == ["first", "later"]
        assert result["categories"][0]["task_prompt"] == "执行模板"
        assert result["categories"][0]["question_prompt"] == "问题模板"
        first = await session.get(TaskCategory, "first")
        first.enabled = False
        await session.commit()
        assert [row["id"] for row in (await tools.categories())["categories"]] == ["later"]
    finally:
        nexus_employee_id.reset(token)
    assert "nexus_task_categories" in {tool.name for tool in build_mcp_server()._tool_manager.list_tools()}
