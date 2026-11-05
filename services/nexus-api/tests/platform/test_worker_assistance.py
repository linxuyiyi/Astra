"""数字员工到原生 Worker 的求助闭环；Runtime 使用替身，投影与归属校验是真实代码。"""

from app.task_mcp import build_mcp_server


def test_mcp_exposes_employee_assistance_tools():
    names = {tool.name for tool in build_mcp_server()._tool_manager.list_tools()}
    assert names >= {"nexus_task_assistance", "nexus_task_reply", "nexus_task_input"}
