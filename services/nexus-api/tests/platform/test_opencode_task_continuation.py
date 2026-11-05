"""跨阶段 MCP 续答复用原 Task / Session，员工保留整体完成后的关单能力。"""

from app.task_mcp import build_mcp_server


def test_confirm_remains_available():
    names = {tool.name for tool in build_mcp_server()._tool_manager.list_tools()}
    assert "nexus_task_confirm" in names
