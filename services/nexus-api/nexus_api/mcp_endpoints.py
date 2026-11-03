"""独立 MCP 服务的部署地址；不向员工自动挂载资源。"""

import os


def task_mcp_url() -> str:
    from app.config.settings import get_settings

    explicit = os.environ.get("NEXUS_TASK_MCP_URL", "").strip()
    return explicit or f"http://127.0.0.1:{get_settings().task_mcp_port}/mcp"


def schedule_mcp_url() -> str:
    from app.config.settings import get_settings

    explicit = os.environ.get("NEXUS_SCHEDULE_MCP_URL", "").strip()
    return explicit or f"http://127.0.0.1:{get_settings().schedule_mcp_port}/mcp"
