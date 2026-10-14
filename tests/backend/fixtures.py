# -*- coding: utf-8 -*-
"""测试夹具：公开 ``SkillHubBase`` 实现与真实本地 MCP 声明。

这些夹具只在测试构建时注入，不是产品默认资源，也不代表模型能力。
"""
from __future__ import annotations

import io
import tarfile
from pathlib import Path

from agentscope.app.hub import (
    SkillArchive,
    SkillCard,
    SkillHubBase,
    SkillHubPage,
)

FIXTURE_SKILL_HUB_ID = "fixturehub"
FIXTURE_SKILL_CARD_ID = "compute-skill"
FIXTURE_SKILL_NAME = "compute-skill"

MCP_FIXTURE_NAME = "nexus-compute"
HERE = Path(__file__).resolve().parent
MCP_FIXTURE_SCRIPT = HERE / "mcp_fixture_stdio.py"


def _skill_archive_bytes() -> bytes:
    markdown = (
        "---\n"
        f"name: {FIXTURE_SKILL_NAME}\n"
        "description: Fixture skill used to verify capability binding.\n"
        "---\n"
        "# Compute Skill\n"
        "This fixture skill exists only for capability-binding verification.\n"
    ).encode("utf-8")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo("SKILL.md")
        info.size = len(markdown)
        tar.addfile(info, io.BytesIO(markdown))
    return buffer.getvalue()


class FixtureSkillHub(SkillHubBase):
    """返回一张固定 Skill 卡与真实 tar.gz 归档的公开 Hub 实现。"""

    def __init__(self) -> None:
        super().__init__(
            hub_id=FIXTURE_SKILL_HUB_ID,
            display_name="Fixture Skill Hub",
            description="Test-only skill hub.",
        )

    def _card(self) -> SkillCard:
        return SkillCard(
            hub_id=self.hub_id,
            id=FIXTURE_SKILL_CARD_ID,
            name=FIXTURE_SKILL_NAME,
            description="Fixture skill for capability binding.",
            version="1.0.0",
        )

    async def list_skills(
        self,
        user_id: str,
        q: str | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> SkillHubPage:
        return SkillHubPage(cards=[self._card()], next_cursor=None)

    async def get_skill(self, user_id: str, card_id: str) -> SkillCard:
        if card_id != FIXTURE_SKILL_CARD_ID:
            raise KeyError(card_id)
        card = self._card()
        card.markdown = (
            "---\n"
            f"name: {FIXTURE_SKILL_NAME}\n"
            "description: Fixture skill for capability binding.\n"
            "---\n"
            "# Compute Skill\n"
        )
        return card

    async def download(
        self,
        user_id: str,
        card_id: str,
        version: str | None = None,
    ) -> SkillArchive:
        if card_id != FIXTURE_SKILL_CARD_ID:
            raise KeyError(card_id)
        payload = _skill_archive_bytes()

        async def stream():
            yield payload

        return SkillArchive(format="tar.gz", stream=stream())
