"""唯一生成器必须维持内部拓扑和入口安全边界。真实 Nginx 覆盖见隔离验收。"""

import pytest

from tests.platform.deployment_platform import deploy


@pytest.fixture
def network_config(tmp_path):
    values = {
        "NEXUS_API_PORT": "19120",
        "ASTRA_SERVICE_GATEWAY_PORT": "19110",
        "NEXUS_MCP_PORT": "19201",
        "NEXUS_DEPLOY_HTTPS_PORT": "19443",
        "NEXUS_DEPLOY_WORKER_PORT": "19130",
        "NEXUS_DEPLOY_PUBLIC_IP": "7.1.2.3",
        "NEXUS_DEPLOY_WORKER_ALLOW": "7.0.0.0/8",
        "NEXUS_DEPLOY_TLS_CERT": str(tmp_path / "cert"),
        "NEXUS_DEPLOY_TLS_KEY": str(tmp_path / "key"),
    }
    return deploy.nginx_config(tmp_path, tmp_path, values)


def test_web_and_gateway_use_one_https_entry_and_loopback_upstreams(network_config):
    assert "listen 19443 ssl;" in network_config
    assert "proxy_pass http://127.0.0.1:19120" in network_config
    assert "proxy_pass http://127.0.0.1:19110" in network_config
    assert "proxy_buffering off;" in network_config
    assert "proxy_set_header Upgrade $http_upgrade;" in network_config
    assert "proxy_set_header Connection $nexus_connection;" in network_config


def test_worker_http_is_acl_limited_and_management_paths_are_not_proxied(network_config):
    worker = network_config[network_config.index("listen 7.1.2.3:19130") :]
    assert "allow 7.0.0.0/8;" in worker and "deny all;" in worker
    assert "location = /api/worker-gateway/connect" in worker
    assert "location = /mcp" in worker
    assert "location / { return 404; }" in worker
    assert "location ^~ /api/worker-gateway/data/" in worker
    assert "location /api/" not in worker
    assert "Authorization" not in network_config  # 原请求鉴权由上游验证，不由代理伪造。


def test_company_profile_scopes_tls_and_dependency_minimum(tmp_path):
    from tests.platform.deployment_platform import DEPLOY
    company = deploy.source_manifest(DEPLOY / "sources/huawei.json")
    public = deploy.source_manifest(DEPLOY / "sources/public.json")
    assert company["nginx_min"] == "1.12" and company["tls_protocols"] == ["TLSv1.2"]
    assert public["nginx_min"] == "1.20" and public["tls_protocols"] == ["TLSv1.2", "TLSv1.3"]
    assert company["pip_index"].startswith("http://mirrors.tools.huawei.com/")
    assert company["npm_registry"].startswith("http://mirrors.tools.huawei.com/")
