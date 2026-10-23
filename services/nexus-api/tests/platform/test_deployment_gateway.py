"""部署环境必须能脱离安装进程恢复，不依赖本机 OpenCode 或厂商 key。"""

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.deployment import check_wiki_project, register_gateway_key
from app.gateway.models import GatewayKey
from app.gateway.security import gateway_key_digest
from app.gateway.urls import gateway_base

ROOT = Path(__file__).resolve().parents[4]
INSTALLER = ROOT / "ops/deploy/install_nexus.sh"


def installer(tmp_path, body, *, extra=None):
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        # Explicit env wins over the checkout's local ops/env/.env; this test
        # verifies installer persistence, not this developer's run mode.
        "ASTRA_EXECUTOR_TYPE": "worker",
        "LC_ALL": "C",
        "NEXUS_ROOT": str(tmp_path),
        "NEXUS_STATE_DIR": str(tmp_path),
        "ASTRA_DATABASE_URL": "mysql+asyncmy://test:abc%40pass@localhost/nexus?charset=utf8mb4",
        "AGENTSCOPE_SQL_URL": "mysql+asyncmy://test@localhost/agent",
        "ASTRA_GATEWAY_MASTER_KEY": "test-master",
        "NEXUS_MCP_API_KEY": "test-mcp",
        "NEXUS_GATEWAY_OPENAI_BASE": "http://gateway.test:18110/v1",
        "ASTRA_SERVICE_GATEWAY_HOST": "0.0.0.0",
        "ASTRA_SERVICE_GATEWAY_PORT": "19110",
        "NEXUS_REMOTE_GATEWAY_LOCAL_URL": "http://10.0.0.8:18110/v1",
        "NEXUS_KB_EMBEDDING_JSON": '{"model_route_id":"embed-route","dimensions":1024}',
        **(extra or {}),
    }
    script = f"source {shlex.quote(str(INSTALLER))}\n" + body
    return subprocess.run(["bash", "-c", script], env=env, text=True, capture_output=True, timeout=10)


def test_generated_environment_survives_clean_process_and_reinstall(tmp_path):
    read = (
        "import json,os; print(json.dumps({k:os.getenv(k) for k in "
        "['ASTRA_EXECUTOR_TYPE','NEXUS_GATEWAY_API_KEY','NEXUS_GATEWAY_OPENAI_BASE',"
        "'ASTRA_DATABASE_URL','NEXUS_KB_EMBEDDING_JSON','NEXUS_RUNTIME_ENV_FILE',"
        "'ASTRA_SERVICE_GATEWAY_HOST','ASTRA_SERVICE_GATEWAY_PORT']}))"
    )
    script = """step_secrets
step_runenv
before=$(get_kv "$SECRETS" NEXUS_GATEWAY_API_KEY)
step_secrets
[ "$before" = "$(get_kv "$SECRETS" NEXUS_GATEWAY_API_KEY)" ]
"""
    script += (
        'env -i PATH="$PATH" bash -c \'set -a; . "$1"; . "$2"; set +a; exec "$3" -c "$4"\' '
        f'-- "$RUNENV" "$SECRETS" {shlex.quote(sys.executable)} {shlex.quote(read)}'
    )
    result = installer(tmp_path, script)
    assert result.returncode == 0, result.stderr
    saved = json.loads(result.stdout.splitlines()[-1])
    assert saved["ASTRA_EXECUTOR_TYPE"] == "worker"
    assert saved["ASTRA_SERVICE_GATEWAY_HOST"] == "0.0.0.0"
    assert saved["ASTRA_SERVICE_GATEWAY_PORT"] == "19110"
    assert saved["NEXUS_GATEWAY_API_KEY"].startswith("agw_")
    assert saved["NEXUS_GATEWAY_OPENAI_BASE"] == "http://gateway.test:18110/v1"
    assert saved["ASTRA_DATABASE_URL"].endswith("?charset=utf8mb4")
    assert json.loads(saved["NEXUS_KB_EMBEDDING_JSON"])["model_route_id"] == "embed-route"
    assert Path(saved["NEXUS_RUNTIME_ENV_FILE"]).is_file()
    assert (tmp_path / "secrets.env").stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / ".config/opencode/opencode.json").exists()
    assert "DEEPSEEK" not in (tmp_path / "nexus.env").read_text()


def test_existing_wiki_never_invents_credentials(tmp_path):
    result = installer(tmp_path, "WITH_WIKI=1\nstep_secrets")
    assert result.returncode != 0 and "llm_wiki token" in result.stderr
    assert "ASTRA_LLM_WIKI_API_TOKEN=" not in (tmp_path / "secrets.env").read_text()


def test_remote_bootstrap_settings_survive_clean_process(tmp_path):
    expected = {
        "NEXUS_REMOTE_DOWNLOAD_BASE_URL": "https://mirror.company.test",
        "NEXUS_REMOTE_NPM_REGISTRY": "https://mirror.company.test/repository/npm/",
        "NEXUS_REMOTE_PIP_INDEX_URL": "https://mirror.company.test/repository/pypi/simple",
        "NEXUS_REMOTE_PYTHON": "/opt/company/python3.11",
        "NEXUS_REMOTE_DEPLOY_TIMEOUT_SECONDS": "2400",
    }
    read = "import json,os; print(json.dumps({k:os.getenv(k) for k in " + repr(list(expected)) + "}))"
    body = "step_runenv\n" + (
        'env -i PATH="$PATH" bash -c \'set -a; . "$1"; set +a; exec "$2" -c "$3"\' '
        f'-- "$RUNENV" {shlex.quote(sys.executable)} {shlex.quote(read)}'
    )
    result = installer(tmp_path, body, extra=expected)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1]) == expected


def test_systemd_reads_the_same_shell_environment(tmp_path):
    result = installer(
        tmp_path,
        """SUDO=""
tee() { cat > "$NEXUS_ROOT/unit"; }
svc_unit nexus-test simple "exec python3 app.py"
""",
    )
    assert result.returncode == 0, result.stderr
    unit = (tmp_path / "unit").read_text()
    assert '. "' + str(tmp_path / "nexus.env") + '"' in unit
    assert '. "' + str(tmp_path / "secrets.env") + '"' in unit


@pytest.mark.parametrize("url", ["http://gateway.test:18110", "http://gateway.test:18110/v1/"])
def test_gateway_clients_share_canonical_v1(url):
    assert gateway_base(url) == "http://gateway.test:18110/v1"


@pytest.mark.parametrize(
    "url",
    ["http://gateway.test/v2", "http://user:secret@gateway.test", "bad", "http://gateway.test?key=secret"],
)
def test_invalid_gateway_url_does_not_echo_credentials(url):
    with pytest.raises(ValueError) as exc:
        gateway_base(url)
    assert "secret" not in str(exc.value)


async def test_platform_key_registration_is_idempotent_and_never_revives_revoked_key(monkeypatch):
    raw = "agw_test_" + uuid4().hex
    monkeypatch.setenv("NEXUS_GATEWAY_API_KEY", raw)
    monkeypatch.setenv("NEXUS_GATEWAY_OPENAI_BASE", "http://gateway.test/v1")
    await register_gateway_key()
    await register_gateway_key()
    async with SessionLocal() as session:
        rows = (
            await session.scalars(select(GatewayKey).where(GatewayKey.key_digest == gateway_key_digest(raw)))
        ).all()
        assert len(rows) == 1
        rows[0].enabled = False
        await session.commit()
    with pytest.raises(RuntimeError, match="禁用"):
        await register_gateway_key()


async def test_wiki_health_cannot_mask_project_auth_failure(monkeypatch):
    from app.config.settings import get_settings
    from app.llm_wiki import LlmWikiError

    monkeypatch.setattr(get_settings(), "llm_wiki_enabled", True)
    monkeypatch.setattr(get_settings(), "llm_wiki_api_base_url", "http://wiki.test/api/v1")
    monkeypatch.setattr(get_settings(), "llm_wiki_api_token", "test-token")
    monkeypatch.setattr(get_settings(), "llm_wiki_project_id", "test-project")
    original = httpx.AsyncClient
    paths = []

    def handler(request):
        paths.append(request.url.path)
        assert request.headers["Authorization"] == "Bearer test-token"
        if request.url.path.endswith("/health"):
            return httpx.Response(200, json={"ok": True, "status": "running"})
        return httpx.Response(403)

    monkeypatch.setattr(
        "app.llm_wiki.httpx.AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw)
    )
    with pytest.raises(LlmWikiError, match="Token"):
        await check_wiki_project()
    assert paths == ["/api/v1/health", "/api/v1/projects/test-project/search"]


def test_local_managed_runtime_cannot_use_vendor_provider_or_small_model():
    from app.runtime.opencode_model import gateway_environment

    original = {
        "NEXUS_GATEWAY_API_KEY": "agw_test_local",
        "NEXUS_GATEWAY_OPENAI_BASE": "http://gateway.test:18110",
        "OPENCODE_CONFIG_CONTENT": json.dumps(
            {
                "mcp": {"custom": {"type": "remote", "url": "http://mcp.test"}},
                "provider": {"vendor": {}},
                "small_model": "vendor/direct",
            }
        ),
    }
    result = gateway_environment("alias-model", original)
    config = json.loads(result["OPENCODE_CONFIG_CONTENT"])
    assert config["model"] == config["small_model"] == "nexus-gateway/alias-model"
    assert config["enabled_providers"] == ["nexus-gateway"]
    assert config["provider"]["nexus-gateway"]["options"]["baseURL"] == "http://gateway.test:18110/v1"
    assert config["mcp"]["custom"]["url"] == "http://mcp.test"
    assert "agw_test_local" not in result["OPENCODE_CONFIG_CONTENT"]
    assert json.loads(original["OPENCODE_CONFIG_CONTENT"])["small_model"] == "vendor/direct"


def test_installer_reads_quoted_json_and_urls_without_executing_input(tmp_path):
    config = tmp_path / "input.env"
    config.write_text(
        'REVIEW_JSON=\'{"model_route_id":"embed","dimensions":2}\'\n'
        'REVIEW_URL="http://gateway.test/v1?value=%40"\n'
        "REVIEW_LITERAL=literal$(touch sentinel) # comment\n"
    )
    result = installer(
        tmp_path,
        'load_env_file "$NEXUS_ROOT/input.env"\n'
        'printf \'%s\\n\' "$REVIEW_JSON" "$REVIEW_URL" "$REVIEW_LITERAL"',
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert json.loads(lines[-3])["dimensions"] == 2
    assert lines[-2] == "http://gateway.test/v1?value=%40"
    assert lines[-1] == "literal$(touch sentinel)"
    assert not (ROOT / "sentinel").exists()


@pytest.mark.parametrize("field,value", [
    ("small_model", "vendor/small"), ("model", "vendor/main"),
    ("enabled_providers", ["nexus-gateway", "vendor"]),
])
async def test_shared_serve_checks_actual_config_before_ready(monkeypatch, field, value):
    from app.config.settings import Settings
    from app.runtime.opencode_model import gateway_environment
    from app.runtime.opencode_server import OpenCodeServerManager

    source = {"NEXUS_GATEWAY_API_KEY": "agw_test"}
    config = json.loads(gateway_environment("default", source)["OPENCODE_CONFIG_CONTENT"])
    config[field] = value
    monkeypatch.delenv("NEXUS_GATEWAY_OPENAI_BASE", raising=False)
    original = httpx.AsyncClient

    def handler(request):
        if request.url.path == "/global/health":
            return httpx.Response(200, json={"healthy": True, "version": "1.18.35"})
        assert request.url.path == "/config"
        return httpx.Response(200, json=config)

    monkeypatch.setattr("app.runtime.opencode_server.httpx.AsyncClient",
                        lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    manager = OpenCodeServerManager(Settings(seed_demo=False))
    with pytest.raises(RuntimeError, match="统一网关"):
        await manager.wait_until_ready()
    config[field] = json.loads(gateway_environment("default", source)["OPENCODE_CONFIG_CONTENT"])[field]
    assert (await manager.wait_until_ready()).healthy


def test_custom_runtime_and_review_settings_survive_restart(tmp_path):
    result = installer(tmp_path, 'step_secrets\nstep_runenv', extra={
        "ASTRA_OPENCODE_MODEL": "nexus-gateway/custom",
        "NEXUS_EXPERIENCE_REVIEW_GATEWAY_URL": "http://gateway.test:19110/v1/chat/completions",
        "NEXUS_EXPERIENCE_REVIEW_GATEWAY_KEY": "agw_review_test",
    })
    assert result.returncode == 0, result.stderr
    runtime = (tmp_path / "nexus.env").read_text()
    assert "ASTRA_OPENCODE_MODEL=nexus-gateway/custom" in runtime
    assert "NEXUS_EXPERIENCE_REVIEW_GATEWAY_URL=http://gateway.test:19110/v1/chat/completions" in runtime
    assert "agw_review_test" not in runtime
    assert "NEXUS_EXPERIENCE_REVIEW_GATEWAY_KEY=agw_review_test" in (tmp_path / "secrets.env").read_text()
