from typing import Any

from sqlalchemy import select

from app.models import KnowledgeItem


async def nexus_knowledge_search(params: dict[str, Any], user_id: str, db_session) -> dict[str, Any]:
    query = select(KnowledgeItem)
    if params.get("query"):
        query = query.where(KnowledgeItem.title.contains(params["query"]))

    result = await db_session.execute(query.limit(10))
    items = result.scalars().all()

    return {
        "items": [
            {"id": i.id, "title": i.title, "summary": i.summary[:100]}
            for i in items
        ],
        "total": len(items)
    }
