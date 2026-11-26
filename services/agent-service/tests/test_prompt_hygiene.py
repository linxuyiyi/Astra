"""验证数字员工底层 Workspace 与 Auto-Memory 提示词净化，以及记忆能力完整可用。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from agentscope.app.workspace_manager import IsolationPolicy
from agentscope.workspace import LocalWorkspace

from agent_service import NEXUS_MEMORY_INSTRUCTIONS, attach_memory
from schedule_workspace import NEXUS_WORKSPACE_INSTRUCTIONS, ScheduleWorkspaceManager


def test_workspace_instructions_are_concise_and_free_of_coding_boilerplate(tmp_path: Path):
    manager = ScheduleWorkspaceManager(
        basedir=str(tmp_path),
        isolation=IsolationPolicy.PER_SESSION,
    )

    async def run():
        ws = await manager.get_workspace("nexus", "agent-1", "ses-1")
        inst = await ws.get_instructions()
        await manager.close_all()
        return ws.workdir, inst

    workdir, inst = asyncio.run(run())
    assert workdir in inst
    assert "read_chat_attachment" in inst
    assert inst == NEXUS_WORKSPACE_INSTRUCTIONS.format(workdir=workdir)
    for forbidden in ("git worktree", "uv venv", "README.md", ".gitignore"):
        assert forbidden not in inst


def test_memory_instructions_and_end_to_end_recall(tmp_path: Path):
    ws = LocalWorkspace(workdir=str(tmp_path))

    async def run():
        await ws.initialize()
        middlewares = attach_memory(ws, key="agent-mem-test")
        assert len(middlewares) == 1
        mw = middlewares[0]

        # 1. 初次调用 on_system_prompt 自动创建 Memory/MEMORY.md 并替换 {memory_dir}
        prompt = await mw.on_system_prompt(None, "基础人设提示词")
        assert "基础人设提示词" in prompt
        assert "## MEMORY.md" in prompt
        assert str(tmp_path / "Memory") in prompt
        assert "{memory_dir}" not in prompt
        for forbidden in ("CLAUDE.md", "git blame", "mock the database", "bundled PR", "Grafana"):
            assert forbidden not in prompt
            assert forbidden not in NEXUS_MEMORY_INSTRUCTIONS
        assert (tmp_path / "Memory" / "MEMORY.md").is_file()

        # 2. 按新提示词的两步格式写入中文主题记忆与 MEMORY.md 索引，验证官方解析与召回
        topic_file = tmp_path / "Memory" / "user_design_pref.md"
        topic_file.write_text(
            "---\n"
            "name: 设计沟通偏好\n"
            "description: 当同事找你讨论界面或交互方案时回忆此条偏好\n"
            "type: user\n"
            "---\n\n"
            "同事希望先听核心判断再聊细节，重点关注桌面端 1440px 布局。\n",
            encoding="utf-8",
        )
        (tmp_path / "Memory" / "MEMORY.md").write_text(
            "- [设计沟通偏好](user_design_pref.md) — 讨论界面方案时先给结论、聚焦桌面端 1440px\n",
            encoding="utf-8",
        )

        # 验证 system prompt 正确带上 MEMORY.md 索引
        prompt_with_mem = await mw.on_system_prompt(None, "基础人设提示词")
        assert "user_design_pref.md" in prompt_with_mem
        assert "设计沟通偏好" in prompt_with_mem

        # 验证官方 frontmatter 解析与文件扫描无异常
        inner = mw._inner
        headers = await inner._list_md_files()
        assert len(headers) == 1
        assert headers[0].filename == "user_design_pref.md"
        assert headers[0].type == "user"
        assert "讨论界面或交互方案" in (headers[0].description or "")

        # 验证异步召回路径 (_retrieve_relevant_files) 正确读取中文主题文件并格式化注入
        class _FakeModel:
            async def generate_structured_output(self, messages, structured_model):
                text = messages[1].get_text_content() or ""
                assert "user_design_pref.md" in text
                assert "[user]" in text
                return SimpleNamespace(content={"selected_files": ["user_design_pref.md"]})

        fake_agent = SimpleNamespace(model=_FakeModel())
        recalled = await inner._retrieve_relevant_files(fake_agent, "帮我看看新页面布局")
        assert recalled is not None
        assert "桌面端 1440px 布局" in recalled
        await ws.close()

    asyncio.run(run())
