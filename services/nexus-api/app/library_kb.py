"""已发布知识项映射到 AgentScope knowledge base。个人记忆不写入图书馆。"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from app.config.settings import get_settings

_EMBEDDING_FIELDS = ("type", "credential_id", "model", "dimensions")


class KnowledgeMapError(Exception):
    pass


def embedding_config() -> dict[str, Any] | None:
    raw = os.environ.get("NEXUS_KB_EMBEDDING_JSON", "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise KnowledgeMapError("嵌入配置不是 JSON") from exc
    if not isinstance(data, dict) or any(key not in data for key in _EMBEDDING_FIELDS):
        raise KnowledgeMapError("嵌入配置缺少字段")
    return data


class AgentScopeKnowledgeApi:
    def __init__(
        self,
        base_url: str,
        user_id: str,
        embedding: dict[str, Any],
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.user_id = user_id
        self.embedding = embedding
        self._http = http
        self._owns_http = http is None

    async def aclose(self) -> None:
        if self._http is not None and self._owns_http:
            await self._http.aclose()

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(base_url=self.base_url, timeout=20)
        return self._http

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self._client().request(
                method,
                path,
                headers={"X-User-ID": self.user_id},
                **kwargs,
            )
        except httpx.HTTPError as exc:
            raise KnowledgeMapError("无法连接 AgentScope 知识库") from exc
        if response.status_code >= 400:
            detail = response.text.strip()[:300] or response.reason_phrase
            raise KnowledgeMapError(f"AgentScope 知识库返回 {response.status_code}：{detail}")
        if not response.content:
            return {}
        payload = response.json()
        if not isinstance(payload, dict):
            raise KnowledgeMapError("AgentScope 知识库返回的不是对象")
        return payload

    async def create_knowledge_base(self, name: str, description: str) -> str:
        payload = await self._request(
            "POST",
            "/knowledge_bases/",
            json={
                "name": name,
                "description": description,
                "embedding_model_config": self.embedding,
            },
        )
        kb_id = payload.get("knowledge_base_id")
        if not kb_id:
            raise KnowledgeMapError("AgentScope 没有返回 knowledge base id")
        return str(kb_id)

    async def upload_document(self, kb_id: str, filename: str, content: str) -> str:
        payload = await self._request(
            "POST",
            f"/knowledge_bases/{kb_id}/documents",
            files={"file": (filename, content.encode(), "text/markdown")},
        )
        document_id = payload.get("document_id")
        if not document_id:
            raise KnowledgeMapError("AgentScope 没有返回 document id")
        return str(document_id)

    async def delete_document(self, kb_id: str, document_id: str) -> None:
        await self._request("DELETE", f"/knowledge_bases/{kb_id}/documents/{document_id}")

    async def search(self, kb_id: str, query: str) -> list[dict[str, Any]]:
        payload = await self._request(
            "POST",
            f"/knowledge_bases/{kb_id}/search",
            json={"query": query, "top_k": 5},
        )
        rows = payload.get("results")
        if not isinstance(rows, list):
            raise KnowledgeMapError("AgentScope 知识库检索结果不是列表")
        hits: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            chunk = row.get("chunk") if isinstance(row.get("chunk"), dict) else {}
            content = chunk.get("content") if isinstance(chunk.get("content"), dict) else {}
            hits.append(
                {
                    "document_id": str(row.get("document_id") or ""),
                    "text": str(content.get("text") or ""),
                    "score": row.get("score"),
                }
            )
        return hits


async def map_published_item(item, api: AgentScopeKnowledgeApi) -> None:
    """已发布文档写入一个 knowledge base。失败只改映射状态，不改业务状态。"""

    if item.status != "published":
        return
    if item.agentscope_sync_status == "synced" and item.agentscope_kb_id and item.agentscope_document_id:
        return
    old_document = item.agentscope_document_id or ""
    try:
        kb_id = item.agentscope_kb_id or await api.create_knowledge_base(item.title, item.summary or "")
        item.agentscope_kb_id = kb_id
        if old_document:
            await api.delete_document(kb_id, old_document)
            item.agentscope_document_id = None
        document_id = await api.upload_document(kb_id, f"{item.id}.md", item.content_md or item.title)
    except KnowledgeMapError as exc:
        item.agentscope_sync_status = "failed"
        item.agentscope_sync_error = str(exc)
        return
    item.agentscope_document_id = document_id
    item.agentscope_sync_status = "synced"
    item.agentscope_sync_error = None


async def sync_published_item(item) -> None:
    if item.status != "published":
        return
    try:
        config = embedding_config()
    except KnowledgeMapError as exc:
        item.agentscope_sync_status = "failed"
        item.agentscope_sync_error = str(exc)
        return
    if config is None:
        return
    settings = get_settings()
    api = AgentScopeKnowledgeApi(settings.agentscope_base_url, settings.agentscope_user_id, config)
    try:
        await map_published_item(item, api)
    finally:
        await api.aclose()


async def retrieve_for_bindings(session, bound_kb_ids: list[str], query: str, api) -> list[dict[str, Any]]:
    """只检索这位员工绑定的知识库，命中带上图书馆来源。"""

    from sqlalchemy import select

    from app.models import KnowledgeItem

    ids = [kb_id for kb_id in bound_kb_ids if kb_id]
    if not ids or not query.strip():
        return []
    rows = (
        await session.scalars(
            select(KnowledgeItem).where(
                KnowledgeItem.status == "published",
                KnowledgeItem.agentscope_sync_status == "synced",
                KnowledgeItem.agentscope_kb_id.in_(ids),
            )
        )
    ).all()
    hits: list[dict[str, Any]] = []
    for item in rows:
        for found in await api.search(item.agentscope_kb_id, query):
            if found.get("document_id") != item.agentscope_document_id:
                continue
            hits.append(
                {
                    "knowledge_id": item.id,
                    "title": item.title,
                    "knowledge_base_id": item.agentscope_kb_id,
                    "document_id": item.agentscope_document_id,
                    "text": found.get("text") or "",
                    "score": found.get("score"),
                }
            )
    return hits
