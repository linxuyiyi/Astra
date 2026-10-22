"""部署模板必须跟随统一服务的目录与端口。

背景：前端已迁移到 ``services/nexus-api/web/dist``，任务/Worker/网关控制面
与 ``/api/v1`` 合并到同一个 18120 进程；``nginx-nexus.conf`` 仍指向
``/opt/nexus/web/nexus/dist`` 和已退役的 18100，按模板发布就会静态资源 404、
控制面接口 502。这里做配置一致性检查，真实 nginx 行为仍需部署验收。
"""

from __future__ import annotations

import re
from pathlib import Path

CONFIG = Path(__file__).resolve().parents[4] / "ops" / "deploy" / "nginx-nexus.conf"


def _upstreams(text: str) -> dict[str, str]:
    """location -> proxy_pass 上游（仅一级 location 块）。"""

    upstreams: dict[str, str] = {}
    locations = re.finditer(r"(?m)^  location\s+(.+?)\s*\{", text)
    for match in locations:
        start = match.end()
        end = text.find("\n  }", start)
        block = text[start:end if end != -1 else len(text)]
        proxy = re.search(r"proxy_pass\s+(\S+);", block)
        if proxy:
            upstreams[match.group(1).strip()] = proxy.group(1)
    return upstreams


def test_static_root_points_at_unified_web_build():
    text = CONFIG.read_text(encoding="utf-8")
    assert "root /opt/nexus/services/nexus-api/web/dist;" in text
    assert "/opt/nexus/web/nexus/dist" not in text


def test_control_plane_and_docs_use_unified_service():
    upstreams = _upstreams(CONFIG.read_text(encoding="utf-8"))
    for location in ("/api/", "/api/v1/", "= /docs", "= /openapi.json"):
        assert upstreams[location].startswith("http://127.0.0.1:18120"), location


def test_voice_transcription_is_proxied_to_the_asr_service():
    text = CONFIG.read_text(encoding="utf-8")
    upstreams = _upstreams(text)
    assert upstreams["= /api/asr/transcribe"].startswith("http://127.0.0.1:19899")
    assert upstreams["= /api/asr/models"].startswith("http://127.0.0.1:19899")
    assert "client_max_body_size 12m;" in text


def test_retired_port_and_infrastructure_proxy_boundaries():
    text = CONFIG.read_text(encoding="utf-8")
    assert "18100" not in text
    upstreams = _upstreams(text)
    assert upstreams["/v1/"].startswith("http://127.0.0.1:18110")
    assert upstreams["/agentscope-api/"] == "http://127.0.0.1:18120"
