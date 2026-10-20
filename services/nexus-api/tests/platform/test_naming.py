"""E2E-19：没有旧产品入口。ASTRA_* 变量名仍在。"""

from pathlib import Path

from fastapi.testclient import TestClient

from app.config.settings import Settings
from app.main import app

REPO = Path(__file__).resolve().parents[4]


def test_product_entry_is_nexus_and_astra_env_names_stay():
    assert Settings.model_config["env_prefix"] == "ASTRA_"
    exceptions = (REPO / "openspec/changes/nexus-stage3/naming-exceptions.md").read_text()
    assert "ASTRA_GATEWAY_MASTER_KEY" in exceptions
    assert "测试夹具" in exceptions

    with TestClient(app) as client:
        assert client.get("/openapi.json").json()["info"]["title"] == "Nexus API"
        assert client.get("/api/assistant/health").status_code == 404
        assert client.get("/api/health").status_code == 200

    router = (REPO / "services/nexus-api/web/src/router.ts").read_text()
    assert "/assistant" not in router
    help_text = (REPO / "services/nexus-api/web/src/views/settings/EmployeeConfig.vue").read_text()
    assert "助手端口" not in help_text
    gateway = (REPO / "services/nexus-api/app/gateway/server.py").read_text()
    assert 'title="Nexus API Gateway"' in gateway
