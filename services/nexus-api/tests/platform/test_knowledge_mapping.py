"""已审核文档只给绑定了对应知识库的员工检索。个人记忆不入库。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.library_kb import KnowledgeMapError, map_published_item, retrieve_for_bindings
from app.library_schemas import KnowledgeCreate, KnowledgePatch, KnowledgePublishWrite
from app.library_service import KnowledgeService, LibraryPrincipal
from app.models import Base, KnowledgeItem, KnowledgeSpace


class RecordingApi:
    def __init__(self) -> None:
        self.created: list[str] = []
        self.documents: dict[str, str] = {}
        self.searches: list[str] = []
        self.deleted: list[tuple[str, str]] = []
        self.uploads = 0

    async def create_knowledge_base(self, name: str, description: str) -> str:
        del description
        kb_id = f"kb-{name}"
        self.created.append(kb_id)
        return kb_id

    async def upload_document(self, kb_id: str, filename: str, content: str) -> str:
        del filename
        self.uploads += 1
        self.documents[kb_id] = content
        suffix = "" if self.uploads == 1 else f"-{self.uploads}"
        return f"doc-{kb_id}{suffix}"

    async def delete_document(self, kb_id: str, document_id: str) -> None:
        self.deleted.append((kb_id, document_id))

    async def search(self, kb_id: str, query: str) -> list[dict]:
        self.searches.append(kb_id)
        text = self.documents.get(kb_id, "")
        if query not in text:
            return []
        return [{"document_id": f"doc-{kb_id}", "text": text, "score": 1}]


class FailingApi(RecordingApi):
    async def create_knowledge_base(self, name: str, description: str) -> str:
        del name, description
        raise KnowledgeMapError("知识库暂时不可用")


async def test_bound_employee_retrieves_the_approved_document_only(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'kb.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    api = RecordingApi()
    mapped: list[str] = []

    async def sync(item: KnowledgeItem) -> None:
        mapped.append(item.id)
        await map_published_item(item, api)

    monkeypatch.setattr("app.library_service.sync_published_item", sync)
    principal = LibraryPrincipal()
    async with sessions() as session:
        session.add(KnowledgeSpace(id="space-1", name="公共知识库-映射"))
        await session.commit()
        service = KnowledgeService(session)
        draft = await service.create_knowledge(
            KnowledgeCreate(space_id="space-1", title="未审核草稿", content_md="个人记忆：不要入库"),
            principal,
        )
        created = await service.create_knowledge(
            KnowledgeCreate(
                space_id="space-1",
                title="泄压步骤",
                summary="先泄压",
                content_md="阀门要先泄压，再拆开。",
            ),
            principal,
        )
        published = await service.publish_knowledge(
            created.id,
            KnowledgePublishWrite(space_id="space-1", verification_status="verified"),
            principal,
        )

        assert published.status == "published"
        assert published.agentscope_sync_status == "synced"
        assert published.agentscope_kb_id == "kb-泄压步骤"
        assert published.agentscope_document_id == "doc-kb-泄压步骤"
        assert draft.id not in mapped
        assert api.created == ["kb-泄压步骤"]

        bound = await retrieve_for_bindings(session, ["kb-泄压步骤"], "泄压", api)
        unbound = await retrieve_for_bindings(session, [], "泄压", api)
        other = await retrieve_for_bindings(session, ["kb-other"], "泄压", api)
        assert [hit["knowledge_id"] for hit in bound] == [created.id]
        assert bound[0]["title"] == "泄压步骤"
        assert "泄压" in bound[0]["text"]
        assert unbound == []
        assert other == []
        assert api.searches == ["kb-泄压步骤"]

        edited = await service.update_knowledge(
            created.id,
            KnowledgePatch(content_md="阀门要先泄压，再更换垫片。"),
            principal,
        )
        assert edited.agentscope_sync_status == "synced"
        assert edited.agentscope_document_id == "doc-kb-泄压步骤-2"
        assert api.deleted == [("kb-泄压步骤", "doc-kb-泄压步骤")]
        assert "垫片" in api.documents["kb-泄压步骤"]

        memory = "个人记忆：昨天的草稿不进图书馆"
        stored = (await session.scalars(select(KnowledgeItem))).all()
        assert all(memory not in (item.content_md or "") for item in stored)

        failed = KnowledgeItem(
            id="KNOW_fail",
            title="失败也不回滚",
            content_md="已经发布的正文",
            status="published",
            created_by="admin",
            updated_by="admin",
        )
        await map_published_item(failed, FailingApi())
        assert failed.status == "published"
        assert failed.agentscope_sync_status == "failed"
        assert not failed.agentscope_kb_id

    await engine.dispose()
