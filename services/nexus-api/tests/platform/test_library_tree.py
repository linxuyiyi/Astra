"""Directory tree, multi-level directories, safe delete, knowledge move and usages."""

from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import app


def unique(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:10]}"


def _root_space(client: TestClient) -> dict:
    """Return a deterministic public top-level space (the seeded 公共知识库)."""
    spaces = client.get("/api/library/spaces").json()
    return next((space for space in spaces if space["name"] == "公共知识库"), spaces[0])


def test_directory_tree_and_enrichment(client_root: None = None):
    with TestClient(app) as client:
        root = _root_space(client)
        space_id = root["id"]
        assert "children" in root

        child = client.post(
            "/api/library/spaces",
            json={"name": unique("自动化工程"), "parent_id": space_id},
        )
        assert child.status_code == 201
        child_id = child.json()["id"]

        grand = client.post(
            "/api/library/spaces",
            json={"name": unique("问题定位"), "parent_id": child_id},
        )
        assert grand.status_code == 201
        grand_id = grand.json()["id"]

        great = client.post(
            "/api/library/spaces",
            json={"name": unique("SSH问题"), "parent_id": grand_id},
        )
        assert great.status_code == 201

        tree = client.get("/api/library/spaces").json()
        top = next(node for node in tree if node["id"] == space_id)
        assert any(c["id"] == child_id for c in top["children"])
        assert any(c["id"] == grand_id for c in top["children"][0]["children"])


def test_rename_directory():
    with TestClient(app) as client:
        root = _root_space(client)
        child = client.post(
            "/api/library/spaces",
            json={"name": unique("旧名称"), "parent_id": root["id"]},
        ).json()
        renamed = client.put(
            f"/api/library/spaces/{child['id']}",
            json={"name": unique("新名称")},
        )
        assert renamed.status_code == 200
        assert "新名称" in renamed.json()["name"] or renamed.json()["name"].startswith("新名称")


def test_move_directory_and_cycle_guards():
    with TestClient(app) as client:
        root = _root_space(client)
        folder_a = client.post(
            "/api/library/spaces",
            json={"name": unique("A"), "parent_id": root["id"]},
        ).json()
        folder_b = client.post(
            "/api/library/spaces",
            json={"name": unique("B"), "parent_id": root["id"]},
        ).json()
        a_child = client.post(
            "/api/library/spaces",
            json={"name": unique("A-子"), "parent_id": folder_a["id"]},
        ).json()

        # cross-parent move within same space (same root) is allowed
        moved = client.post(
            f"/api/library/spaces/{folder_b['id']}/move",
            json={"target_parent_id": folder_a["id"]},
        )
        assert moved.status_code == 200

        # cycle: move A under A-子 (its own descendant) -> rejected
        cycle = client.post(
            f"/api/library/spaces/{folder_a['id']}/move",
            json={"target_parent_id": a_child["id"]},
        )
        assert cycle.status_code in (409, 422)
        assert "子目录" in cycle.json()["detail"] or "自身" in cycle.json()["detail"]

        # move to self -> rejected
        self_move = client.post(
            f"/api/library/spaces/{folder_a['id']}/move",
            json={"target_parent_id": folder_a["id"]},
        )
        assert self_move.status_code in (409, 422)

        # moving a knowledge-space root is not allowed
        root_move = client.post(
            f"/api/library/spaces/{root['id']}/move",
            json={"target_parent_id": folder_a["id"]},
        )
        assert root_move.status_code in (409, 422)


def test_safe_delete_directory():
    with TestClient(app) as client:
        root = _root_space(client)
        folder = client.post(
            "/api/library/spaces",
            json={"name": unique("带子目录"), "parent_id": root["id"]},
        ).json()
        client.post(
            "/api/library/spaces",
            json={"name": unique("子"), "parent_id": folder["id"]},
        )

        # has children -> reject
        blocked = client.delete(f"/api/library/spaces/{folder['id']}")
        assert blocked.status_code == 409
        assert "子目录" in blocked.json()["detail"]

        # has knowledge -> reject
        with_knowledge = client.post(
            "/api/library/spaces",
            json={"name": unique("带知识"), "parent_id": root["id"]},
        ).json()
        client.post(
            "/api/library/knowledge",
            json={
                "space_id": with_knowledge["id"],
                "title": unique("测试知识"),
                "content_md": "## 内容",
            },
        )
        blocked_knowledge = client.delete(f"/api/library/spaces/{with_knowledge['id']}")
        assert blocked_knowledge.status_code == 409
        assert "知识" in blocked_knowledge.json()["detail"]

        # empty directory can be deleted
        empty = client.post(
            "/api/library/spaces",
            json={"name": unique("空白目录"), "parent_id": root["id"]},
        ).json()
        deleted = client.delete(f"/api/library/spaces/{empty['id']}")
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] == empty["id"]


def test_knowledge_move_and_descendant_listing():
    with TestClient(app) as client:
        root = _root_space(client)
        folder = client.post(
            "/api/library/spaces",
            json={"name": unique("目录A"), "parent_id": root["id"]},
        ).json()
        sub = client.post(
            "/api/library/spaces",
            json={"name": unique("目录B"), "parent_id": folder["id"]},
        ).json()
        created = client.post(
            "/api/library/knowledge",
            json={
                "space_id": folder["id"],
                "title": unique("可移动知识"),
                "content_md": "## 移动测试",
                "tags": ["移动"],
            },
        ).json()

        # list under root includes descendants
        listed = client.get("/api/library/knowledge", params={"space_id": root["id"]}).json()
        assert any(item["id"] == created["id"] for item in listed["items"])

        # move knowledge to another directory
        moved = client.post(
            f"/api/library/knowledge/{created['id']}/move",
            json={"space_id": sub["id"]},
        )
        assert moved.status_code == 200
        assert moved.json()["space_id"] == sub["id"]

        # descendant listing still finds it (root -> folder -> sub)
        listed_after = client.get("/api/library/knowledge", params={"space_id": root["id"]}).json()
        assert any(item["id"] == created["id"] for item in listed_after["items"])

        # an unrelated directory outside the subtree does not include it
        other = client.post(
            "/api/library/spaces",
            json={"name": unique("无关目录"), "parent_id": root["id"]},
        ).json()
        unrelated = client.get("/api/library/knowledge", params={"space_id": other["id"]}).json()
        assert not any(item["id"] == created["id"] for item in unrelated["items"])


def test_batch_archive_delete_and_publish():
    with TestClient(app) as client:
        root = _root_space(client)
        candidate = client.post(
            "/api/library/knowledge",
            json={
                "space_id": root["id"],
                "title": unique("候选经验"),
                "type": "experience",
                "content_md": "## 候选",
            },
        ).json()

        batch = client.post(
            "/api/library/knowledge/batch-publish",
            json={
                "knowledge_ids": [candidate["id"]],
                "space_id": root["id"],
                "verification_status": "verified",
            },
            headers={"X-Astra-User-Id": "reviewer-1", "X-Astra-Role": "reviewer"},
        )
        assert batch.status_code == 200
        assert batch.json()["published"] == 1

        archived = client.post(
            "/api/library/knowledge/batch-archive",
            json={"knowledge_ids": [candidate["id"]]},
        )
        assert archived.status_code == 200
        assert archived.json()["archived"] == 1

        # create a fresh candidate for delete
        todo = client.post(
            "/api/library/knowledge",
            json={"space_id": root["id"], "title": unique("待删知识"), "content_md": "## 删除"},
        ).json()
        deleted = client.delete(f"/api/library/knowledge/{todo['id']}")
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] == todo["id"]


def test_knowledge_usages_endpoint():
    with TestClient(app) as client:
        root = _root_space(client)
        created = client.post(
            "/api/library/knowledge",
            json={"space_id": root["id"], "title": unique("引用来源知识"), "content_md": "## 引用"},
        ).json()
        usages = client.get(f"/api/library/knowledge/{created['id']}/usages")
        assert usages.status_code == 200
        assert isinstance(usages.json(), list)


def test_private_space_scope_is_inherited_by_public_child_directory():
    with TestClient(app) as client:
        private_root = client.post(
            "/api/library/spaces",
            json={"name": unique("私有知识空间"), "access_scope": "admin"},
        ).json()
        child = client.post(
            "/api/library/spaces",
            json={"name": unique("默认公开子目录"), "parent_id": private_root["id"]},
        ).json()
        knowledge = client.post(
            "/api/library/knowledge",
            json={
                "space_id": child["id"],
                "title": unique("不可泄漏的子目录知识"),
                "content_md": "## 私有内容",
            },
        ).json()
        assert (
            client.post(
                f"/api/library/knowledge/{knowledge['id']}/publish",
                json={"space_id": child["id"], "verification_status": "verified"},
            ).status_code
            == 200
        )

        viewer_headers = {"X-Astra-User-Id": "viewer-scope", "X-Astra-Role": "viewer"}
        visible_spaces = client.get("/api/library/spaces", headers=viewer_headers).json()
        visible_ids = {node["id"] for root in visible_spaces for node in [root, *root.get("children", [])]}
        listed = client.get(
            "/api/library/knowledge",
            params={"keyword": knowledge["title"]},
            headers=viewer_headers,
        )
        detail = client.get(
            f"/api/library/knowledge/{knowledge['id']}",
            headers=viewer_headers,
        )

        assert private_root["id"] not in visible_ids
        assert child["id"] not in visible_ids
        assert listed.json()["total"] == 0
        assert detail.status_code == 403
