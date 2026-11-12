"""旧附件复用、校验和安全归档的迁移回归。"""

import hashlib
import json

import httpx
from sqlalchemy import inspect, text

from nexus_api.agentscope import AgentScopeClient
from nexus_api.db import connect
from scripts.migrate_workspace_attachments import migrate


async def test_migration_imports_reused_original_and_rewrites_both_groups(tmp_path):
    original = b"legacy-shared-file"
    key = "att_123456abcdef"
    root = tmp_path / "files"
    root.mkdir()
    (root / key).write_bytes(original)
    targets = ("ch_111111111111", "ch_222222222222")
    remote = {}

    def handler(request):
        path = request.url.path
        if "/groups/" in path:
            return httpx.Response(200, json={
                "agent_id": "nexus-group-resources", "session_id": f"resources-{path.rsplit('/', 1)[-1]}",
            })
        location = request.url.params["session_id"], path.rsplit("/", 1)[-1]
        if request.method == "PUT":
            previous = remote.setdefault(location, request.content)
            assert previous == request.content
            return httpx.Response(200, json={
                "type": "application/octet-stream",
                "size": len(previous), "sha256": hashlib.sha256(previous).hexdigest(),
            })
        return httpx.Response(200, content=remote[location])

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://agentscope.test",
    ) as http:
        client = AgentScopeClient("http://agentscope.test", "nexus", http)
        with connect() as db:
            db.execute(text(
                "CREATE TABLE t_nexus_attachments (id TEXT PRIMARY KEY, owner_uid TEXT, name TEXT, "
                "media_type TEXT, size INTEGER, path TEXT, created_at TEXT)"
            ))
            db.execute(text(
                "INSERT INTO t_nexus_attachments VALUES (:id,'owner','旧文件.bin','binary',:size,:path,'old')"
            ), {"id": key, "size": len(original), "path": str(root / key)})
            for index, channel_id in enumerate(targets):
                db.execute(text(
                    "INSERT INTO t_nexus_messages "
                    "(id,channel_id,sender,text,attachments_json,native_json,created_at) "
                    "VALUES (:id,:channel,'user','history',:attachments,:native,'old')"
                ), {
                    "id": f"old-{index}", "channel": channel_id, "attachments": json.dumps([key]),
                    "native": json.dumps({"content": [{"source": {"url": f"/api/v1/attachments/{key}"}}]}),
                })
            db.commit()
            report = await migrate(db, client, root)
            assert report["scoped_copies"] == 2 and report["imported"] == 0
            assert not remote

            report = await migrate(db, client, root, apply=True)
            assert report["imported"] == 2
            assert len(remote) == 2 and all(raw == original for raw in remote.values())
            rows = db.execute(text(
                "SELECT attachments_json,native_json FROM t_nexus_messages ORDER BY id"
            )).mappings().all()
            references = [json.loads(row["attachments_json"])[0] for row in rows]
            assert references[0] == key and references[1] != key
            for row, reference in zip(rows, references, strict=True):
                assert f"/api/v1/attachments/{reference}" in row["native_json"]

            archive = tmp_path / "backup"
            result = await migrate(db, client, root, apply=True, archive=archive)
            assert result["archived"] == 1 and len(remote) == 2
            assert "t_nexus_attachments" not in inspect(db).get_table_names()
            assert (archive / key).read_bytes() == original
            assert not (root / key).exists()
