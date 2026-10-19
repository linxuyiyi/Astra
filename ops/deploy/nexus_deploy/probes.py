from __future__ import annotations

import ast
import base64
import json
import os
import socket
import sqlite3
import ssl
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .config import DeployConfig, parse_env_file
from .runner import Runner
from .services import ServiceCatalog, ServiceSpec

MCP_INITIALIZE = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "nexus-deploy", "version": "1.4.4"},
        },
    }
).encode()


@dataclass
class ProbeResult:
    name: str
    ok: bool
    detail: str = ""
    skipped: bool = False
    required: bool = True

    def as_record(self) -> dict:
        return {
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "skipped": self.skipped,
            "required": self.required,
        }


def _request(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = 10,
    method: str = "GET",
    body: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    request = Request(url, method=method, data=body, headers=dict(headers or {}))
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()
    except URLError as exc:
        raise ConnectionError(str(exc.reason)) from exc


def _json_body(raw: bytes) -> object:
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise ValueError("响应不是有效 JSON") from exc


def http_json_probe(
    name: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    required_keys: tuple[str, ...] = (),
    expected: Mapping[str, object] | None = None,
    require_dict: bool = True,
) -> ProbeResult:
    try:
        status, content_type, raw = _request(url, headers=headers)
    except ConnectionError as exc:
        return ProbeResult(name, False, f"连接失败：{exc}")
    if not 200 <= status < 300:
        return ProbeResult(name, False, f"HTTP {status}")
    if "json" not in content_type.get("Content-Type", "").lower():
        return ProbeResult(name, False, f"Content-Type 不是 JSON：{content_type.get('Content-Type', '')}")
    try:
        payload = _json_body(raw)
    except ValueError as exc:
        return ProbeResult(name, False, str(exc))
    if require_dict and not isinstance(payload, dict):
        return ProbeResult(name, False, "响应不是 JSON 对象")
    if not isinstance(payload, dict):
        return ProbeResult(name, False, "响应不是 JSON 对象")
    missing = [key for key in required_keys if key not in payload]
    if missing:
        return ProbeResult(name, False, f"缺少字段：{', '.join(missing)}")
    for key, value in (expected or {}).items():
        if payload.get(key) != value:
            return ProbeResult(name, False, f"字段 {key}={payload.get(key)!r}，期望 {value!r}")
    return ProbeResult(name, True, f"HTTP {status}")


def http_ok_probe(
    name: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
) -> ProbeResult:
    """Accept any 2xx with a non-empty body; validate JSON when it is JSON."""
    try:
        status, content_type, raw = _request(url, headers=headers)
    except ConnectionError as exc:
        return ProbeResult(name, False, f"连接失败：{exc}")
    if not 200 <= status < 300:
        return ProbeResult(name, False, f"HTTP {status}")
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return ProbeResult(name, False, "响应为空")
    if "json" in content_type.get("Content-Type", "").lower():
        try:
            payload = _json_body(raw)
        except ValueError as exc:
            return ProbeResult(name, False, str(exc))
        if not isinstance(payload, dict):
            return ProbeResult(name, False, "JSON 响应不是对象")
    return ProbeResult(name, True, f"HTTP {status}")


def agent_health_probe(
    name: str, url: str, *, headers: Mapping[str, str] | None = None
) -> ProbeResult:
    """Validate the AgentScope 2.0.10 /health protocol, not just any 2xx."""
    try:
        status, content_type, raw = _request(url, headers=headers)
    except ConnectionError as exc:
        return ProbeResult(name, False, f"连接失败：{exc}")
    if status != 200:
        return ProbeResult(name, False, f"HTTP {status}")
    if "json" not in content_type.get("Content-Type", "").lower():
        return ProbeResult(name, False, f"Content-Type 不是 JSON：{content_type.get('Content-Type', '')}")
    try:
        payload = _json_body(raw)
    except ValueError as exc:
        return ProbeResult(name, False, str(exc))
    if not isinstance(payload, dict):
        return ProbeResult(name, False, "响应不是 JSON 对象")
    if payload.get("status") != "ok":
        return ProbeResult(name, False, f"status={payload.get('status')!r}")
    if not str(payload.get("version") or "").strip():
        return ProbeResult(name, False, "缺少 version")
    components = payload.get("components")
    if not isinstance(components, dict) or not components:
        return ProbeResult(name, False, "缺少 components")
    not_ready = sorted(key for key, value in components.items() if value == "not_ready")
    if not_ready:
        return ProbeResult(name, False, "组件未就绪：" + ", ".join(not_ready))
    return ProbeResult(name, True, f"HTTP 200 status=ok components={len(components)}")


def opencode_health_probe(
    name: str,
    url: str,
    *,
    expected_version: str,
    version_check: bool,
    username: str = "",
    password: str = "",
) -> ProbeResult:
    headers: dict[str, str] = {}
    if password:
        token = base64.b64encode(f"{username or 'opencode'}:{password}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    try:
        status, content_type, raw = _request(url.rstrip("/") + "/global/health", headers=headers)
    except ConnectionError as exc:
        return ProbeResult(name, False, f"连接失败：{exc}")
    if status != 200:
        return ProbeResult(name, False, f"HTTP {status}")
    if "json" not in content_type.get("Content-Type", "").lower():
        return ProbeResult(name, False, "Content-Type 不是 JSON")
    try:
        payload = _json_body(raw)
    except ValueError as exc:
        return ProbeResult(name, False, str(exc))
    if not isinstance(payload, dict):
        return ProbeResult(name, False, "响应不是 JSON 对象")
    if payload.get("healthy") is not True:
        return ProbeResult(name, False, f"healthy={payload.get('healthy')!r}")
    version = str(payload.get("version") or "").strip().removeprefix("v")
    if not version:
        return ProbeResult(name, False, "缺少 version")
    expected = expected_version.strip().removeprefix("v")
    if version_check and version != expected:
        return ProbeResult(name, False, f"版本 {version} != {expected}")
    return ProbeResult(name, True, f"HTTP 200 healthy version={version}")


def _celery_payload(text: str) -> dict:
    try:
        value = ast.literal_eval(text.strip())
    except (ValueError, SyntaxError):
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            return {}
    if isinstance(value, dict):
        return value
    return {}


def _celery_value(payload: dict, node: str) -> object:
    if node in payload:
        return payload[node]
    for key, value in payload.items():
        if str(key).startswith(node):
            return value
    return None


def _parse_mcp_response(text: str, content_type: str) -> dict:
    candidates: list[str] = []
    if "text/event-stream" in content_type.lower() or text.lstrip().startswith("event:"):
        for line in text.splitlines():
            if line.startswith("data:"):
                candidates.append(line[5:].strip())
    else:
        candidates.append(text.strip())
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise ValueError("MCP 响应不是可解析的 JSON 或 SSE data")


def mcp_probe(
    name: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
) -> ProbeResult:
    merged = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        **(headers or {}),
    }
    try:
        status, _content_type, raw = _request(
            url, headers=merged, method="POST", body=MCP_INITIALIZE, timeout=15
        )
    except ConnectionError as exc:
        return ProbeResult(name, False, f"连接失败：{exc}")
    if status == 401:
        return ProbeResult(name, False, "MCP 认证失败（401）")
    if not 200 <= status < 300:
        return ProbeResult(name, False, f"HTTP {status}")
    text = raw.decode("utf-8", errors="replace")
    try:
        payload = _parse_mcp_response(text, _content_type.get("Content-Type", ""))
    except ValueError as exc:
        return ProbeResult(name, False, str(exc))
    if payload.get("jsonrpc") != "2.0" or payload.get("id") != 1:
        return ProbeResult(name, False, "MCP 响应 jsonrpc/id 不匹配")
    if "error" in payload:
        return ProbeResult(name, False, f"MCP initialize 错误：{payload['error']}")
    result = payload.get("result")
    if not isinstance(result, dict):
        return ProbeResult(name, False, "MCP 响应缺少 result 对象")
    if not str(result.get("protocolVersion") or "").strip():
        return ProbeResult(name, False, "MCP result 缺少 protocolVersion")
    server_info = result.get("serverInfo")
    if not isinstance(server_info, dict) or not server_info.get("name"):
        return ProbeResult(name, False, "MCP result 缺少 serverInfo.name")
    return ProbeResult(name, True, f"HTTP {status} initialize")


class ProbeSuite:
    def __init__(self, config: DeployConfig, runner: Runner) -> None:
        self.config = config
        self.runner = runner

    def redis(self, *, required: bool = True) -> ProbeResult:
        parsed = urlparse(self.config.redis_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 6379
        password = parsed.password or ""
        use_ssl = parsed.scheme == "rediss"
        try:
            with socket.create_connection((host, port), timeout=5) as raw:
                stream = (
                    ssl.create_default_context().wrap_socket(raw, server_hostname=host)
                    if use_ssl
                    else raw
                )
                if password:
                    stream.sendall(f"AUTH {password}\r\n".encode())
                    if not stream.recv(128).startswith(b"+OK"):
                        return ProbeResult("Redis", False, "AUTH 失败", required=required)
                stream.sendall(b"PING\r\n")
                reply = stream.recv(128)
        except OSError as exc:
            return ProbeResult("Redis", False, f"连接失败：{exc}", required=required)
        ok = reply.startswith(b"+PONG")
        return ProbeResult(
            "Redis", ok, reply.decode("utf-8", errors="replace").strip(), required=required
        )

    def sql(self, *, required: bool = True) -> ProbeResult:
        if self.config.database_backend == "sqlite":
            if not self.config.sqlite_path.is_file():
                return ProbeResult(
                    "SQL", False, f"数据库不存在：{self.config.sqlite_path}", required=required
                )
            try:
                with sqlite3.connect(self.config.sqlite_path) as connection:
                    value = connection.execute("SELECT 1").fetchone()
            except sqlite3.Error as exc:
                return ProbeResult("SQL", False, str(exc), required=required)
            return ProbeResult("SQL", bool(value and value[0] == 1), "sqlite SELECT 1", required=required)
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
                "SELECT 1",
                self.config.database_name,
            ],
            env=env,
            timeout=20,
        )
        if result.returncode != 0:
            return ProbeResult("SQL", False, (result.stderr or result.stdout).strip(), required=required)
        return ProbeResult(
            "SQL", result.stdout.strip().splitlines()[-1:] == ["1"], "mysql SELECT 1", required=required
        )

    def celery(self, service: ServiceSpec, catalog: ServiceCatalog) -> ProbeResult:
        env = {key: value for key, value in os.environ.items()}
        for path in (
            self.config.user_env_file,
            self.config.secrets_file,
            self.config.runtime_env_file,
            self.config.chat_model_file,
            self.config.optional_env_file,
        ):
            env.update(parse_env_file(path))
        env.update({
            "ASTRA_DATA_DIR": str(self.config.task_data_dir),
            "ASTRA_WORKSPACE_ROOT": str(self.config.task_workspace_root),
            "ASTRA_RUNTIME_DIR": str(self.config.task_runtime_dir),
            "ASTRA_CONFIG_DIR": str(self.config.task_config_dir),
            "ASTRA_LOG_DIR": str(self.config.task_log_dir),
            "ASTRA_REDIS_URL": self.config.redis_url,
            "ASTRA_DATABASE_BACKEND": self.config.database_backend,
            "ASTRA_DATABASE_HOST": self.config.database_host,
            "ASTRA_DATABASE_PORT": str(self.config.database_port),
            "ASTRA_DATABASE_NAME": self.config.database_name,
            "ASTRA_DATABASE_USERNAME": self.config.database_username,
            "ASTRA_DATABASE_PASSWORD": self.config.database_password,
            "ASTRA_ENVIRONMENT": "production",
        })
        if self.config.database_backend == "mysql":
            env["ASTRA_DATABASE_PASSWORD"] = self.config.database_password
        python = str(catalog.release.path / "services" / "task-platform" / ".venv" / "bin" / "python")
        ping = self.runner.run(
            [
                python,
                "-m",
                "celery",
                "-A",
                "app.runtime.celery_app:celery",
                "inspect",
                "ping",
                "-d",
                service.celery_node,
                "--timeout",
                "10",
            ],
            cwd=catalog.release.path / "services" / "task-platform",
            env=env,
            timeout=30,
        )
        ping_payload = _celery_payload(ping.stdout)
        ping_ok = _celery_value(ping_payload, service.celery_node) is not None
        if isinstance(_celery_value(ping_payload, service.celery_node), dict):
            ping_ok = _celery_value(ping_payload, service.celery_node).get("ok") == "pong"
        else:
            ping_ok = ping_ok and "pong" in f"{ping.stdout}\n{ping.stderr}".lower()
        if ping.returncode != 0 or not ping_ok:
            return ProbeResult(f"Celery {service.celery_node}", False, "worker 未响应 ping")
        queues = self.runner.run(
            [
                python,
                "-m",
                "celery",
                "-A",
                "app.runtime.celery_app:celery",
                "inspect",
                "active_queues",
                "-d",
                service.celery_node,
                "--timeout",
                "10",
            ],
            cwd=catalog.release.path / "services" / "task-platform",
            env=env,
            timeout=30,
        )
        queue_payload = _celery_payload(queues.stdout)
        node_queues = _celery_value(queue_payload, service.celery_node)
        names = {
            str(item.get("name"))
            for item in node_queues or []
            if isinstance(item, dict) and item.get("name")
        }
        if service.celery_queue not in names:
            return ProbeResult(
                f"Celery {service.celery_node}",
                False,
                f"队列 {service.celery_queue} 未注册",
            )
        return ProbeResult(f"Celery {service.celery_node}", True, "ping + active_queues")

    def service(self, service: ServiceSpec, catalog: ServiceCatalog, secrets: dict[str, str]) -> ProbeResult:
        if service.probe in {"celery", "celery-beat"}:
            return self.celery(service, catalog)
        port = catalog.port_for(service)
        if service.probe == "opencode":
            url = service.base_url or f"http://127.0.0.1:{port}"
            prefix = (
                "ASTRA_ASSISTANT_OPENCODE"
                if service.name == "assistant-opencode"
                else "ASTRA_OPENCODE_SERVER"
            )
            password = secrets.get(f"{prefix}_PASSWORD", "")
            username = (
                secrets.get(f"{prefix}_USERNAME", "")
                or self.config.effective(f"{prefix}_USERNAME", "")
                or "opencode"
            )
            version_check = self.config.effective(
                "ASTRA_OPENCODE_SERVER_VERSION_CHECK_ENABLED", "true"
            ).lower() in {"1", "true", "yes", "on"}
            return opencode_health_probe(
                service.description,
                url,
                expected_version=self.config.effective(
                    "ASTRA_OPENCODE_SERVER_VERSION", "1.18.35"
                ),
                version_check=version_check,
                username=username,
                password=password,
            )
        if service.probe == "mcp":
            key = secrets.get("ASTRA_TASK_MCP_KEY" if service.name == "task-mcp" else "ASTRA_MCP_API_KEY", "")
            headers = {"Authorization": f"Bearer {key}"}
            if service.name == "task-mcp":
                headers["x-nexus-employee-id"] = "nexus-deploy-probe"
            return mcp_probe(
                service.description,
                f"http://127.0.0.1:{port}{service.http_path}",
                headers=headers,
            )
        if service.probe == "http":
            headers = {}
            if service.name == "agent-service":
                return agent_health_probe(
                    service.description,
                    f"http://127.0.0.1:{port}{service.http_path}",
                    headers={"X-User-ID": "nexus"},
                )
            required_keys: tuple[str, ...] = ("status",)
            expected: dict[str, object] = {"status": "ok"}
            if service.name == "nexus-api":
                required_keys = ("status", "product")
                expected = {"status": "ok", "product": "nexus"}
            if service.name == "gateway":
                required_keys = ("status", "runtime_healthy")
                expected = {"status": "ok", "runtime_healthy": True}
            return http_json_probe(
                service.description,
                f"http://127.0.0.1:{port}{service.http_path}",
                headers=headers,
                required_keys=required_keys,
                expected=expected,
            )
        return ProbeResult(service.description, False, "该服务没有配置 readiness 探针")

    def entry(self, *, gateway_enabled: bool, agentscope_enabled: bool = True) -> list[ProbeResult]:
        base = self.config.entry_url
        results = [
            self._web_entry(base),
            http_json_probe(
                "Nginx /api/health",
                f"{base}api/health",
                required_keys=("status",),
                expected={"status": "ok"},
            ),
            http_json_probe(
                "Nginx /api/v1/me",
                f"{base}api/v1/me",
                headers={"X-Remote-User": "nexus-deploy-probe"},
                required_keys=("uid",),
                expected={"uid": "nexus-deploy-probe"},
            ),
        ]
        if agentscope_enabled:
            results.append(
                agent_health_probe(
                    "Nginx /agentscope-api/health",
                    f"{base}agentscope-api/health",
                    headers={"X-User-ID": "nexus"},
                )
            )
        if gateway_enabled:
            results.append(self._gateway_entry(base))
        return results

    def _web_entry(self, base: str) -> ProbeResult:
        try:
            status, headers, raw = _request(base)
        except ConnectionError as exc:
            return ProbeResult("Nginx Web", False, f"连接失败：{exc}")
        content_type = headers.get("Content-Type", "").lower()
        if status != 200:
            return ProbeResult("Nginx Web", False, f"HTTP {status}")
        if "html" not in content_type:
            return ProbeResult("Nginx Web", False, f"Content-Type 不是 HTML：{content_type}")
        text = raw.decode("utf-8", errors="replace")
        if 'id="app"' not in text and "<div id='app'" not in text:
            return ProbeResult("Nginx Web", False, "首页缺少应用挂载点")
        return ProbeResult("Nginx Web", True, "HTTP 200 HTML app")

    def _gateway_probe_key(self) -> str:
        key = self.config.gateway_probe_key.strip()
        if key:
            return key
        return parse_env_file(self.config.secrets_file).get("NEXUS_GATEWAY_PROBE_KEY", "").strip()

    def _gateway_entry(self, base: str) -> ProbeResult:
        probe_key = self._gateway_probe_key()
        if not probe_key:
            return ProbeResult(
                "Nginx /v1/",
                False,
                "未配置只读测试 key（NEXUS_GATEWAY_PROBE_KEY），必需路由未验证",
            )
        try:
            status, headers, raw = _request(
                f"{base}v1/models",
                headers={"Authorization": f"Bearer {probe_key}"},
            )
        except ConnectionError as exc:
            return ProbeResult("Nginx /v1/", False, f"连接失败：{exc}")
        content_type = headers.get("Content-Type", "").lower()
        if "html" in content_type:
            return ProbeResult("Nginx /v1/", False, "路由返回 SPA 页面而不是网关响应")
        if status == 200:
            try:
                payload = _json_body(raw)
            except ValueError as exc:
                return ProbeResult("Nginx /v1/", False, str(exc))
            if not isinstance(payload, dict) or "data" not in payload:
                return ProbeResult("Nginx /v1/", False, "200 响应缺少 data")
            return ProbeResult("Nginx /v1/", True, "HTTP 200 models")
        return ProbeResult("Nginx /v1/", False, f"HTTP {status}")

    def nginx_config(self) -> ProbeResult:
        if not self.config.nginx_enabled:
            return ProbeResult("Nginx 配置", True, "已禁用", skipped=True, required=False)
        result = self.runner.run(["nginx", "-t"])
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()
            return ProbeResult("Nginx 配置", False, detail[-1] if detail else "nginx -t 失败")
        return ProbeResult("Nginx 配置", True, "nginx -t")

    def model_cost_probe(self, enabled: bool) -> ProbeResult:
        if not enabled:
            return ProbeResult("模型计费探测", True, "skipped（未启用）", skipped=True, required=False)
        return ProbeResult(
            "模型计费探测",
            True,
            "skipped（未配置真实计费请求）",
            skipped=True,
            required=False,
        )
