import asyncio
from uuid import uuid4

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.mcp_server import mcp
from app.models import KnowledgeItem


def unique(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:10]}"


def test_library_web_api_browses_drafts_publishes_and_enforces_review_permission():
    with TestClient(app) as client:
        spaces = client.get("/api/library/spaces")
        assert spaces.status_code == 200
        assert [space["name"] for space in spaces.json()][:2] == ["公共知识库", "测试领域知识"]
        space_id = spaces.json()[0]["id"]

        created = client.post(
            "/api/library/knowledge",
            json={
                "space_id": space_id,
                "title": unique("流水线创建错误 50004 处理经验"),
                "type": "experience",
                "summary": "用于验证精确错误码检索",
                "content_md": "## 问题现象\n\n返回 `50004`。\n\n## 处理步骤\n\n1. 检查参数。",
                "tags": ["流水线", "50004"],
            },
        )
        assert created.status_code == 201
        assert created.json()["status"] == "draft"
        knowledge_id = created.json()["id"]

        viewer_list = client.get(
            "/api/library/knowledge",
            params={"keyword": created.json()["title"]},
            headers={"X-Astra-User-Id": "viewer-1", "X-Astra-Role": "viewer"},
        )
        assert viewer_list.status_code == 200
        assert viewer_list.json()["total"] == 0

        forbidden = client.post(
            f"/api/library/knowledge/{knowledge_id}/publish",
            json={"space_id": space_id},
            headers={"X-Astra-User-Id": "viewer-1", "X-Astra-Role": "viewer"},
        )
        assert forbidden.status_code == 403

        published = client.post(
            f"/api/library/knowledge/{knowledge_id}/publish",
            json={"space_id": space_id, "verification_status": "verified"},
            headers={"X-Astra-User-Id": "reviewer-1", "X-Astra-Role": "reviewer"},
        )
        assert published.status_code == 200
        assert published.json()["status"] == "published"
        assert published.json()["published_by"] == "reviewer-1"

        helpful = client.post(
            f"/api/library/knowledge/{knowledge_id}/helpful",
            headers={"X-Astra-User-Id": "viewer-1", "X-Astra-Role": "viewer"},
        )
        repeated = client.post(
            f"/api/library/knowledge/{knowledge_id}/helpful",
            headers={"X-Astra-User-Id": "viewer-1", "X-Astra-Role": "viewer"},
        )
        assert helpful.json() == {"created": True, "helpful_count": 1}
        assert repeated.json() == {"created": False, "helpful_count": 1}

        stats = client.get("/api/library/stats").json()
        assert stats["knowledge_pages"] >= 1
        assert stats["published_experiences"] >= 1

        private_space = client.post(
            "/api/library/spaces",
            json={"name": unique("内部知识空间"), "access_scope": "admin"},
        ).json()
        private_knowledge = client.post(
            "/api/library/knowledge",
            json={
                "space_id": private_space["id"],
                "title": unique("不应对普通访问者可见"),
                "type": "document",
                "summary": "私有空间内容",
                "content_md": "# 私有内容",
            },
        ).json()
        client.post(
            f"/api/library/knowledge/{private_knowledge['id']}/publish",
            json={"space_id": private_space["id"]},
        )
        viewer_headers = {"X-Astra-User-Id": "viewer-1", "X-Astra-Role": "viewer"}
        private_list = client.get(
            "/api/library/knowledge",
            params={"keyword": private_knowledge["title"]},
            headers=viewer_headers,
        )
        private_detail = client.get(
            f"/api/library/knowledge/{private_knowledge['id']}",
            headers=viewer_headers,
        )
        visible_spaces = client.get("/api/library/spaces", headers=viewer_headers).json()
        viewer_stats = client.get("/api/library/stats", headers=viewer_headers).json()
        admin_stats = client.get("/api/library/stats").json()
        assert private_list.json()["total"] == 0
        assert private_detail.status_code == 403
        assert private_space["id"] not in {space["id"] for space in visible_spaces}
        assert admin_stats["knowledge_pages"] == viewer_stats["knowledge_pages"] + 1


def test_library_stats_visible_total_tracks_scope_statuses_and_unassigned_candidates():
    """左树根节点与右侧列表的数量必须共用同一套可见权限口径。"""

    with TestClient(app) as client:
        space_id = client.get("/api/library/spaces").json()[0]["id"]
        viewer_headers = {"X-Astra-User-Id": "viewer-scope", "X-Astra-Role": "viewer"}
        reviewer_headers = {"X-Astra-User-Id": "reviewer-scope", "X-Astra-Role": "reviewer"}

        admin_before = client.get("/api/library/stats").json()

        draft = client.post(
            "/api/library/knowledge",
            json={
                "space_id": space_id,
                "title": unique("可见总数统计草稿条目"),
                "type": "document",
                "summary": "统计口径测试",
                "content_md": "# 草稿",
            },
        ).json()
        assert draft["status"] == "draft"

        admin_after_draft = client.get("/api/library/stats").json()
        # 可见总数按 visible_statuses 统计，草稿也要计入
        assert admin_after_draft["visible_knowledge_total"] == admin_before["visible_knowledge_total"] + 1
        # knowledge_pages 语义保持“仅已发布”，不受草稿影响
        assert admin_after_draft["knowledge_pages"] == admin_before["knowledge_pages"]

        # 未分配空间的候选经验：只有审核者口径可见
        candidate_title = unique("未分配空间的候选经验")
        candidate_id = f"KB-UNASSIGNED-{uuid4().hex[:10].upper()}"

        async def seed_unassigned_candidate():
            async with SessionLocal() as session:
                session.add(
                    KnowledgeItem(
                        id=candidate_id,
                        space_id=None,
                        title=candidate_title,
                        type="experience",
                        status="candidate",
                        created_by="test",
                        updated_by="test",
                    )
                )
                await session.commit()

        asyncio.run(seed_unassigned_candidate())
        admin_after_candidate = client.get("/api/library/stats").json()
        assert (
            admin_after_candidate["visible_knowledge_total"]
            == admin_after_draft["visible_knowledge_total"] + 1
        )

        # 三种身份下，stats 的可见总数都必须等于同一未筛选列表的 total
        for headers in ({}, viewer_headers, reviewer_headers):
            stats = client.get("/api/library/stats", headers=headers).json()
            listing = client.get("/api/library/knowledge", headers=headers).json()
            assert stats["visible_knowledge_total"] == listing["total"]
            assert stats["visible_knowledge_total"] >= stats["knowledge_pages"]

        # viewer 只能看到已发布，可见总数与“仅已发布”口径一致
        viewer_stats = client.get("/api/library/stats", headers=viewer_headers).json()
        assert viewer_stats["knowledge_pages"] == viewer_stats["visible_knowledge_total"]

        reviewer_list = client.get(
            "/api/library/knowledge",
            params={"keyword": candidate_title},
            headers=reviewer_headers,
        ).json()
        assert reviewer_list["total"] == 1
        assert reviewer_list["items"][0]["id"] == candidate_id
        viewer_list = client.get(
            "/api/library/knowledge",
            params={"keyword": candidate_title},
            headers=viewer_headers,
        ).json()
        assert viewer_list["total"] == 0


def test_employee_mcp_exposes_read_and_feedback_without_experience_submission():
    tools = asyncio.run(mcp.list_tools())
    assert {tool.name for tool in tools} == {
        "library_search",
        "library_get",
        "library_related",
        "library_feedback",
    }
