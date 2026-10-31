"""真实本地 HTTP 制品服务器验证下载、完整性与安装事务，不连接公网。"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.config.settings import Settings
from app.config.worker_deployment import deployment_manifest
from app.runtime.remote_artifacts import install_opencode, package_name
from app.runtime.remote_package_config import configure_sources
from tests.platform.deployment_target import LocalTarget


@pytest.fixture
def registry():
    state = {
        "calls": [],
        "version": "1.18.33",
        "binary_version": "1.18.33",
        "corrupt": False,
        "redirect": False,
        "external": False,
        "symlink": False,
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["calls"].append(self.path)
            content = f"#!/bin/sh\nprintf '{state['binary_version']}\\n'\n".encode()
            archive = io.BytesIO()
            with tarfile.open(fileobj=archive, mode="w:gz") as packed:
                item = tarfile.TarInfo("package/bin/opencode")
                item.mode, item.size = 0o755, len(content)
                if state["symlink"]:
                    item.type, item.linkname = tarfile.SYMTYPE, "/bin/sh"
                packed.addfile(item, io.BytesIO(content))
            data = archive.getvalue()
            if self.path == "/archive.tgz":
                if state["redirect"]:
                    self.send_response(302)
                    self.send_header("Location", "https://unapproved.invalid/package.tgz")
                    self.end_headers()
                    return
                body = data + (b"bad" if state["corrupt"] else b"")
            else:
                url = f"http://127.0.0.1:{self.server.server_port}/archive.tgz"
                if state["external"]:
                    url = "https://unapproved.invalid/package.tgz"
                body = json.dumps(
                    {
                        "name": "opencode-linux-x64-baseline",
                        "version": state["version"],
                        "dist": {
                            "tarball": url,
                            "integrity": "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode(),
                        },
                    }
                ).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    manifest = {
        "opencode_version": "1.18.33",
        "npm_artifact_hosts": [],
        "npm_registry": f"http://127.0.0.1:{server.server_port}",
    }
    try:
        yield state, manifest
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


async def test_native_package_real_download_integrity_atomic_install_and_reuse(registry, tmp_path):
    state, manifest = registry
    worker = LocalTarget(tmp_path)
    await install_opencode(worker, manifest, str(tmp_path / "opencode"), arch="x64", musl=False)
    binary = tmp_path / "opencode/bin/opencode"
    assert binary.is_file()
    stored = json.loads((binary.parent.parent / "installed.json").read_text())
    assert stored["package"] == "opencode-linux-x64-baseline"
    calls = list(state["calls"])
    assert len(calls) == 2
    assert (
        await install_opencode(worker, manifest, str(tmp_path / "opencode"), arch="x64", musl=False) is False
    )
    assert state["calls"] == calls


@pytest.mark.parametrize(
    "failure,expected",
    [
        ("corrupt", "SHA512"),
        ("external", "未允许的来源"),
        ("redirect", "重定向"),
        ("symlink", "普通 OpenCode"),
        ("version", "包名或版本"),
        ("binary_version", "实际运行版本"),
    ],
)
async def test_failed_native_package_preserves_existing_install(registry, tmp_path, failure, expected):
    state, manifest = registry
    prefix = tmp_path / "opencode"
    prefix.mkdir()
    (prefix / "user-file").write_text("preserve")
    state[failure] = "wrong" if failure in {"version", "binary_version"} else True
    with pytest.raises(RuntimeError, match=expected):
        await install_opencode(LocalTarget(tmp_path), manifest, str(prefix), arch="x64", musl=False)
    assert (prefix / "user-file").read_text() == "preserve"
    assert not (prefix / "bin/opencode").exists()
    assert not list(tmp_path.glob(".opencode-stage-*"))


def test_architecture_has_one_platform_package_without_fallback():
    assert package_name("x64", False) == "opencode-linux-x64-baseline"
    assert package_name("x64", True) == "opencode-linux-x64-baseline-musl"
    assert package_name("arm64", False) == "opencode-linux-arm64"
    assert package_name("arm64", True) == "opencode-linux-arm64-musl"
    with pytest.raises(RuntimeError):
        package_name("armv7", False)


def test_legacy_root_requires_explicit_migration_and_sources_can_differ():
    with pytest.raises(ValueError, match="已停用"):
        deployment_manifest(Settings(_env_file=None, remote_download_base_url="https://old.invalid"))
    value = deployment_manifest(
        Settings(
            _env_file=None,
            remote_python_source_url="https://python.internal/src.tar.xz",
            remote_node_base_url="https://node.internal/dist",
            remote_npm_registry="https://npm.internal",
            remote_pip_index_url="https://pypi.internal/simple",
            remote_npm_artifact_hosts="files.internal",
        )
    )
    assert value["npm_artifact_hosts"] == ["files.internal"]
    assert value["node_base_url"] == "https://node.internal/dist"


@pytest.fixture
def global_sources(monkeypatch, tmp_path):
    from app.runtime import remote_package_config

    monkeypatch.setattr(remote_package_config, "_pip_paths", lambda home: [home + "/.config/pip/pip.conf"])
    return {"pip_index_url": "https://pypi.company/simple", "npm_registry": "https://npm.company"}


async def test_global_package_sources_persist_and_repeat_without_rewriting(global_sources, tmp_path):

    home = tmp_path / "user"
    await configure_sources(LocalTarget(tmp_path), global_sources, str(home), str(tmp_path / "node"))
    pip_file, npm_file = home / ".config/pip/pip.conf", home / ".npmrc"
    assert "index-url = https://pypi.company/simple" in pip_file.read_text()
    assert "registry=https://npm.company" in npm_file.read_text()
    before = (pip_file.stat().st_mtime_ns, npm_file.stat().st_mtime_ns)
    await configure_sources(LocalTarget(tmp_path), global_sources, str(home), str(tmp_path / "node"))
    assert before == (pip_file.stat().st_mtime_ns, npm_file.stat().st_mtime_ns)


@pytest.mark.parametrize("kind", ["pip", "npm", "extra-index"])
async def test_existing_global_config_conflict_is_preserved(global_sources, tmp_path, kind):

    pip_file, npm_file = tmp_path / ".config/pip/pip.conf", tmp_path / ".npmrc"
    path = npm_file if kind == "npm" else pip_file
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "registry=https://other.company\n"
        if kind == "npm"
        else "[global]\n"
        + ("extra-index-url" if kind == "extra-index" else "index-url")
        + " = https://other.company\n"
    )
    path.write_text(content)
    with pytest.raises(RuntimeError, match="冲突|额外来源"):
        await configure_sources(LocalTarget(tmp_path), global_sources, str(tmp_path), str(tmp_path / "node"))
    assert path.read_text() == content
    assert not (pip_file if kind == "npm" else npm_file).exists()


async def test_global_config_does_not_erase_other_settings(global_sources, tmp_path):

    pip_file = tmp_path / ".config/pip/pip.conf"
    pip_file.parent.mkdir(parents=True)
    pip_file.write_text("[global]\ntimeout = 120\n")
    (tmp_path / ".npmrc").write_text("fund=false\n")
    await configure_sources(LocalTarget(tmp_path), global_sources, str(tmp_path), str(tmp_path / "node"))
    assert "timeout = 120" in pip_file.read_text()
    assert "fund=false" in (tmp_path / ".npmrc").read_text()


async def test_plain_pip_and_npm_commands_read_persisted_user_defaults(global_sources, tmp_path):
    import shlex
    import shutil
    import sys

    worker = LocalTarget(tmp_path)
    await configure_sources(worker, global_sources, str(tmp_path), str(tmp_path / "node"))
    pip_value = await worker._command(
        shlex.join([sys.executable, "-m", "pip", "config", "get", "global.index-url"])
    )
    assert pip_value.rstrip("/") == global_sources["pip_index_url"]
    if shutil.which("npm"):
        npm_value = await worker._command("cd " + shlex.quote(str(tmp_path)) + "; npm config get registry")
        assert npm_value.rstrip("/") == global_sources["npm_registry"]
