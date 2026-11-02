"""真实 HTTP 索引/registry 验证安装隔离、构建依赖与冻结下载。"""

import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

from app.config.worker_deployment import pip_install_options, pip_trusted_host
from app.runtime.remote_package_config import configure_sources
from tests.platform.deployment_target import LocalTarget


def wheel(name, files):
    output = io.BytesIO()
    dist = name + "-0.0.1.dist-info"
    with zipfile.ZipFile(output, "w") as archive:
        content = {
            **files,
            dist + "/METADATA": "Metadata-Version: 2.1\nName: "
            + name.replace("_", "-")
            + "\nVersion: 0.0.1\n",
            dist + "/WHEEL": "Wheel-Version: 1.0\nGenerator: e2e\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        }
        content[dist + "/RECORD"] = "".join(p + ",,\n" for p in [*content, dist + "/RECORD"])
        for path, text in content.items():
            archive.writestr(path, text)
    return output.getvalue()


@pytest.fixture
def http_artifacts():
    state = {"files": {}, "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"].append(self.path)
            path = urlsplit(self.path).path
            body = state["files"].get(path)
            self.send_response(200 if body is not None else 404)
            self.send_header("Content-Length", str(len(body or b"")))
            self.send_header(
                "Content-Type", "text/html" if path.startswith("/simple/") else "application/octet-stream"
            )
            self.end_headers()
            self.wfile.write(body or b"")

        def do_CONNECT(self):
            state["requests"].append("CONNECT " + self.path)
            self.send_error(403)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


@pytest.mark.parametrize(
    "index,trusted",
    [
        ("http://mirror.test/simple", "mirror.test"),
        ("http://mirror.test:8080/simple", "mirror.test:8080"),
        ("https://mirror.test/simple", ""),
    ],
)
def test_http_trust_is_explicit_and_port_scoped(index, trusted):
    assert pip_trusted_host(index) == trusted
    options = pip_install_options(index)
    assert ("--trusted-host" in options) == bool(trusted)
    if trusted:
        assert options[-1] == trusted


@pytest.mark.parametrize(
    "index",
    [
        "http:///simple",
        "http://user:secret@host/simple",
        "http://host:bad/simple",
        "http://host/simple?token=x",
    ],
)
def test_invalid_source_is_rejected_before_building_trust(index):
    with pytest.raises(ValueError):
        pip_install_options(index)


async def test_http_user_defaults_are_idempotent_and_same_origin_only(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.runtime.remote_package_config._pip_paths", lambda home: [home + "/.config/pip/pip.conf"]
    )
    manifest = {"pip_index_url": "http://mirror.test:8080/simple", "npm_registry": "http://mirror.test/npm"}
    target = LocalTarget(tmp_path, env={"PIP_TRUSTED_HOST": "mirror.test:8080 mirror.test:8080"})
    await configure_sources(target, manifest, str(tmp_path), str(tmp_path / "node"))
    path = tmp_path / ".config/pip/pip.conf"
    before = path.read_bytes(), path.stat().st_mtime_ns
    assert "trusted-host = mirror.test:8080" in path.read_text()
    assert "strict-ssl=false" not in (tmp_path / ".npmrc").read_text()
    await configure_sources(target, manifest, str(tmp_path), str(tmp_path / "node"))
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


@pytest.mark.parametrize("where", ["environment", "install", "global", "default", "npm"])
async def test_conflicting_source_or_tls_setting_never_writes(tmp_path, monkeypatch, where):
    monkeypatch.setattr(
        "app.runtime.remote_package_config._pip_paths", lambda home: [home + "/.pip/pip.conf"]
    )
    manifest = {"pip_index_url": "http://mirror.test:8080/simple", "npm_registry": "http://mirror.test/npm"}
    env = {"PIP_TRUSTED_HOST": "mirror.test:8080 other.test"} if where == "environment" else {}
    path = tmp_path / ".pip/pip.conf"
    if where in {"install", "global", "default"}:
        path.parent.mkdir()
        section = "DEFAULT" if where == "default" else where
        path.write_text(f"[{section}]\ntrusted-host = mirror.test\n")
    if where == "npm":
        (tmp_path / ".npmrc").write_text("strict-ssl=false\n")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(RuntimeError, match="可信主机|strict-ssl"):
        await configure_sources(
            LocalTarget(tmp_path, env=env), manifest, str(tmp_path), str(tmp_path / "node")
        )
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def test_real_pip_isolated_build_fetches_backend_from_configured_http_index(tmp_path, http_artifacts):
    state, base = http_artifacts
    backend = """def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
 import base64,pathlib
 data=base64.b64decode(PAYLOAD)
 name='nexus_probe-0.0.1-py3-none-any.whl'
 pathlib.Path(wheel_directory,name).write_bytes(data)
 return name
""".replace(
        "PAYLOAD",
        repr(
            base64.b64encode(
                wheel("nexus_probe", {"nexus_probe.py": 'proof="isolated-http-build"\n'})
            ).decode()
        ),
    )
    archive = wheel("nexus_e2e_backend", {"e2e_backend.py": backend})
    filename = "nexus_e2e_backend-0.0.1-py3-none-any.whl"
    state["files"]["/packages/" + filename] = archive
    state["files"]["/simple/nexus-e2e-backend/"] = (
        f'<a href="{base}/packages/{filename}">{filename}</a>'.encode()
    )
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        '[build-system]\nrequires=["nexus-e2e-backend==0.0.1"]\nbuild-backend="e2e_backend"\n'
    )
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PIP_")}
    env.update(
        PIP_CONFIG_FILE=os.devnull, PIP_INDEX_URL=base + "/simple", PIP_TRUSTED_HOST=pip_trusted_host(base)
    )
    destination = tmp_path / "installed"
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-cache-dir",
        "--target",
        str(destination),
        *pip_install_options(base + "/simple"),
        str(project),
    ]
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stderr
    assert (destination / "nexus_probe.py").read_text() == 'proof="isolated-http-build"\n'
    assert "/simple/nexus-e2e-backend/" in state["requests"]
    assert "/packages/" + filename in state["requests"]


def test_real_npm_ci_rewrites_public_registry_without_changing_locked_version(tmp_path, http_artifacts):
    if not shutil.which("npm"):
        pytest.skip("npm unavailable")
    state, base = http_artifacts
    package = {
        "name": "nexus-gateway-proof",
        "version": "1.0.0",
        "scripts": {"postinstall": "node -e \"require('fs').writeFileSync('ran-script','bad')\""},
    }
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        data = json.dumps(package).encode()
        info = tarfile.TarInfo("package/package.json")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    data = output.getvalue()
    tarball = "/nexus-gateway-proof/-/nexus-gateway-proof-1.0.0.tgz"
    state["files"][tarball] = data
    root = {"name": "fixture", "version": "1.0.0", "dependencies": {"nexus-gateway-proof": "1.0.0"}}
    (tmp_path / "package.json").write_text(json.dumps(root))
    lock = {
        "name": "fixture",
        "version": "1.0.0",
        "lockfileVersion": 3,
        "packages": {
            "": root,
            "node_modules/nexus-gateway-proof": {
                "version": "1.0.0",
                "resolved": "https://registry.npmjs.org" + tarball,
                "integrity": "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode(),
                "hasInstallScript": True,
            },
        },
    }
    path = tmp_path / "package-lock.json"
    path.write_text(json.dumps(lock))
    before = path.read_bytes()
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("NPM_CONFIG_")}
    env.update(
        npm_config_replace_registry_host="npmjs",
        npm_config_audit="false",
        npm_config_fund="false",
        npm_config_fetch_retries="0",
        npm_config_https_proxy=base,
        npm_config_strict_ssl="true",
        npm_config_userconfig=str(tmp_path / "npmrc"),
        npm_config_globalconfig=str(tmp_path / "npmrc.global"),
    )
    result = subprocess.run(
        ["npm", "ci", "--ignore-scripts", "--registry", base, "--cache", str(tmp_path / "cache")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stderr
    installed = tmp_path / "node_modules/nexus-gateway-proof"
    assert json.loads((installed / "package.json").read_text())["version"] == "1.0.0"
    assert not (installed / "ran-script").exists()
    assert path.read_bytes() == before
    assert tarball in [urlsplit(path).path for path in state["requests"]]
    assert all(not path.startswith("CONNECT ") and urlsplit(path).netloc in {"", urlsplit(base).netloc}
               for path in state["requests"])
    root["dependencies"]["nexus-gateway-proof"] = "2.0.0"
    (tmp_path / "package.json").write_text(json.dumps(root))
    mismatch = subprocess.run(
        ["npm", "ci", "--ignore-scripts", "--registry", base, "--cache", str(tmp_path / "cache")],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=45,
    )
    assert mismatch.returncode != 0
    assert path.read_bytes() == before
