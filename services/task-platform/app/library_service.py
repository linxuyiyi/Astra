from __future__ import annotations

import hashlib
import logging
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import yaml
from sqlalchemy import String, and_, cast, desc, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.bootstrap import CATEGORY_KNOWLEDGE_SPACE, DEFAULT_EXPERIENCE_SPACE
from app.config.settings import get_settings
from app.library_kb import sync_published_item
from app.library_schemas import (
    KnowledgeCreate,
    KnowledgeDetail,
    KnowledgeFavoriteResult,
    KnowledgeFeedbackResult,
    KnowledgeFeedbackWrite,
    KnowledgeListItem,
    KnowledgePage,
    KnowledgePatch,
    KnowledgePublishWrite,
    KnowledgeReviewWrite,
    KnowledgeSpaceCreate,
    KnowledgeSpacePatch,
    KnowledgeSpaceRead,
    KnowledgeUsageRead,
    LibraryFeedbackInput,
    LibraryResource,
    LibraryResourceFeedbackResult,
    LibrarySearchInput,
    LibrarySearchResult,
    LibraryServiceStatus,
    LibraryStats,
    LibrarySubmitExperienceInput,
    LibraryUsageRecent,
    LibraryUsageSummary,
    RelatedTaskRead,
    UsageTopKnowledge,
    UsageTrendPoint,
)
from app.llm_wiki import LlmWikiClient, LlmWikiError, knowledge_source_path, wiki_resource_path
from app.models import (
    AgentWorker,
    ExperienceEvidence,
    KnowledgeAudit,
    KnowledgeFavorite,
    KnowledgeFeedback,
    KnowledgeItem,
    KnowledgeSpace,
    KnowledgeUsage,
    RuntimeSession,
    Task,
    utcnow,
)
from app.runtime.runtime_context import RuntimeContext
from app.runtime.runtime_context import resolve_runtime_context as _resolve_runtime_context
from app.usage import day_bounds

KNOWLEDGE_TYPES = {"document", "rule", "guide", "experience", "faq", "tool", "example"}
logger = logging.getLogger(__name__)


def _source_markdown(item: KnowledgeItem) -> str:
    """Serialize one knowledge record into the llm_wiki source Markdown."""
    metadata = dict(
        astra_knowledge_id=item.id,
        title=item.title,
        type=item.type,
        space_id=item.space_id,
        tags=item.tags or [],
        updated_at=item.updated_at.isoformat(),
    )
    return (
        "---\n"
        + yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False)
        + "---\n\n"
        + f"# {item.title}\n\n{item.summary}\n\n{item.content_md}\n"
    )


def experience_fingerprint(content_md: str) -> str:
    # Case and indentation can change code semantics; leave semantic matching
    # to the reviewer rather than silently flattening distinct rules here.
    canonical = content_md.replace("\r\n", "\n").strip()
    return hashlib.sha256(canonical.encode()).hexdigest()


def invalidate_experience_verification(item: KnowledgeItem) -> None:
    item.verification_status = "unverified"
    item.verified_at = item.verification_message_id = item.verification_session_status = None
    item.session_last_message_id_at_verification = None


KNOWLEDGE_STATUSES = {"draft", "candidate", "published", "archived", "rejected"}
FEEDBACK_TYPES = {"helpful", "incorrect", "outdated", "missing", "mismatch"}
ROLE_ALIASES = {
    "viewer": "viewer",
    "browser": "viewer",
    "maintainer": "maintainer",
    "reviewer": "reviewer",
    "admin": "admin",
}


class LibraryError(Exception):
    pass


class LibraryNotFound(LibraryError):
    pass


class LibraryForbidden(LibraryError):
    pass


class LibraryConflict(LibraryError):
    pass


class LibraryValidationError(LibraryError):
    pass


@dataclass(frozen=True, slots=True)
class LibraryPrincipal:
    user_id: str = "admin"
    role: str = "admin"

    @property
    def normalized_role(self) -> str:
        return ROLE_ALIASES.get(self.role.lower(), "viewer")

    @property
    def can_manage(self) -> bool:
        return self.normalized_role in {"maintainer", "admin"}

    @property
    def can_review(self) -> bool:
        return self.normalized_role in {"reviewer", "admin"}

    @property
    def is_admin(self) -> bool:
        return self.normalized_role == "admin"

    @property
    def visible_statuses(self) -> set[str]:
        if self.is_admin:
            return KNOWLEDGE_STATUSES
        if self.normalized_role == "reviewer":
            return {"published", "candidate", "draft", "rejected"}
        if self.normalized_role == "maintainer":
            return {"published", "draft", "archived"}
        return {"published"}


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:20].upper()}"


def _require_manage(principal: LibraryPrincipal) -> None:
    if not principal.can_manage:
        raise LibraryForbidden("需要知识维护权限")


def _require_review(principal: LibraryPrincipal) -> None:
    if not principal.can_review:
        raise LibraryForbidden("需要知识审核权限")


def _scope_allowed(space: KnowledgeSpace | None, principal: LibraryPrincipal) -> bool:
    if space is None or principal.is_admin:
        return True
    scope = (space.access_scope or "public").strip().lower()
    if scope == "public":
        return True
    allowed = {item.strip() for item in scope.split(",") if item.strip()}
    return principal.normalized_role in allowed or f"user:{principal.user_id}" in allowed


def _tree_scope_allowed(
    space: KnowledgeSpace,
    spaces_by_id: dict[str, KnowledgeSpace],
    principal: LibraryPrincipal,
) -> bool:
    """A directory can never be more visible than its ancestor knowledge space."""

    current: KnowledgeSpace | None = space
    visited: set[str] = set()
    while current is not None:
        if current.id in visited or current.status != "active" or not _scope_allowed(current, principal):
            return False
        visited.add(current.id)
        if current.parent_id is None:
            return True
        current = spaces_by_id.get(current.parent_id)
    return False


def _item_summary(item: KnowledgeItem, space_name: str | None = None) -> KnowledgeListItem:
    return KnowledgeListItem(
        id=item.id,
        space_id=item.space_id,
        space_name=space_name if space_name is not None else item.space.name if item.space else None,
        title=item.title,
        type=item.type,
        summary=item.summary,
        status=item.status,
        sync_status=item.sync_status,
        verification_status=item.verification_status,
        tags=item.tags or [],
        source_type=item.source_type,
        source_task_id=item.source_task_id,
        reference_count=item.reference_count,
        helpful_count=item.helpful_count,
        updated_at=item.updated_at,
        published_at=item.published_at,
    )


async def _space_descendant_ids(session: AsyncSession, space_id: str) -> list[str]:
    ids = [space_id]
    queue = [space_id]
    visited = {space_id}
    while queue:
        current = queue.pop()
        rows = (
            await session.scalars(select(KnowledgeSpace.id).where(KnowledgeSpace.parent_id == current))
        ).all()
        for row in rows:
            if row in visited:
                continue
            visited.add(row)
            ids.append(row)
            queue.append(row)
    return ids


async def _space_root_id(session: AsyncSession, space_id: str) -> str:
    current = space_id
    visited: set[str] = set()
    while current and current not in visited:
        visited.add(current)
        space = await session.get(KnowledgeSpace, current)
        if not space or not space.parent_id:
            return current
        current = space.parent_id
    return current


def _build_space_tree(
    nodes: list[KnowledgeSpace],
    count_map: dict[str, int],
) -> list[KnowledgeSpaceRead]:
    by_parent: dict[str | None, list[KnowledgeSpace]] = {}
    for node in nodes:
        by_parent.setdefault(node.parent_id, []).append(node)
    memo: dict[str, int] = {}

    def subtree_count(node: KnowledgeSpace) -> int:
        if node.id in memo:
            return memo[node.id]
        total = count_map.get(node.id, 0) + sum(subtree_count(child) for child in by_parent.get(node.id, []))
        memo[node.id] = total
        return total

    def build(node: KnowledgeSpace) -> KnowledgeSpaceRead:
        return KnowledgeSpaceRead.model_validate(node).model_copy(
            update={
                "knowledge_count": count_map.get(node.id, 0),
                "subtree_knowledge_count": subtree_count(node),
                "children": [build(child) for child in by_parent.get(node.id, [])],
            }
        )

    return [build(node) for node in nodes if node.parent_id is None]


class KnowledgeService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def _audit(
        self,
        item: KnowledgeItem,
        action: str,
        principal: LibraryPrincipal,
        detail: dict | None = None,
    ) -> None:
        self.session.add(
            KnowledgeAudit(
                knowledge_id=item.id,
                action=action,
                actor_id=principal.user_id,
                actor_role=principal.normalized_role,
                detail=detail or {},
            )
        )

    async def _prepare_sync(self, item: KnowledgeItem) -> None:
        """Mark a business change as pending; the LLM Wiki attempt follows."""
        item.source_path = knowledge_source_path(item.id)
        item.sync_status = "pending"
        item.sync_error = None
        await self.session.commit()
        await self._perform_sync(item)

    async def _perform_sync(self, item: KnowledgeItem) -> None:
        """Push the current target state. Never touches ``status``."""
        source_path = item.source_path
        if source_path is None or item.status not in {"published", "archived", "rejected"}:
            item.sync_status = "not_synced"
            item.sync_error = None
            await self.session.commit()
            return
        client = LlmWikiClient()
        error: str | None = None
        try:
            # Knowledge a Library viewer may not read is removed from the wiki,
            # so tightening a space scope still withdraws the derived source.
            if item.status == "published" and await self._visible_to_viewer(item):
                await client.upsert_source(source_path, _source_markdown(item))
            else:
                await client.delete_source(source_path)
        except LlmWikiError as exc:
            error = str(exc)
        if error:
            item.sync_status = "failed"
            item.sync_error = error
        else:
            item.sync_status = "synced"
            item.sync_error = None
            item.synced_at = utcnow()
        await self.session.commit()

    async def _visible_to_viewer(self, item: KnowledgeItem) -> bool:
        if item.space_id is None:
            return True
        visible = await self._visible_space_ids(LibraryPrincipal(user_id="mcp", role="viewer"))
        return item.space_id in visible

    async def sync_knowledge(self, knowledge_id: str) -> KnowledgeItem:
        """Retry the LLM Wiki synchronization for one knowledge record."""
        item = await self.session.get(KnowledgeItem, knowledge_id)
        if item is None:
            raise LibraryNotFound("知识不存在")
        await self._prepare_sync(item)
        return item

    async def resync_knowledge(
        self, knowledge_id: str, principal: LibraryPrincipal
    ) -> KnowledgeDetail:
        _require_manage(principal)
        await self.sync_knowledge(knowledge_id)
        return await self.get_detail(knowledge_id, principal)

    async def _space(self, space_id: str | None, principal: LibraryPrincipal) -> KnowledgeSpace | None:
        if not space_id:
            return None
        space = await self.session.get(KnowledgeSpace, space_id)
        if not space or space.status != "active":
            raise LibraryNotFound("知识空间不存在或已停用")
        if not await self._space_scope_allowed(space, principal):
            raise LibraryForbidden("无权访问该知识空间")
        return space

    async def _space_scope_allowed(self, space: KnowledgeSpace, principal: LibraryPrincipal) -> bool:
        spaces_by_id = {space.id: space}
        current = space
        visited: set[str] = set()
        while current.parent_id is not None:
            if current.id in visited:
                return False
            visited.add(current.id)
            parent = await self.session.get(KnowledgeSpace, current.parent_id)
            if parent is None:
                return False
            spaces_by_id[parent.id] = parent
            current = parent
        return _tree_scope_allowed(space, spaces_by_id, principal)

    async def _visible_space_ids(self, principal: LibraryPrincipal) -> list[str]:
        spaces = (await self.session.scalars(select(KnowledgeSpace))).all()
        spaces_by_id = {space.id: space for space in spaces}
        return [space.id for space in spaces if _tree_scope_allowed(space, spaces_by_id, principal)]

    async def _item_scope_condition(self, principal: LibraryPrincipal):
        if principal.is_admin:
            return None
        visible_space_ids = await self._visible_space_ids(principal)
        return or_(
            KnowledgeItem.space_id.is_(None),
            KnowledgeItem.space_id.in_(visible_space_ids),
        )

    async def _item(
        self,
        knowledge_id: str,
        principal: LibraryPrincipal,
        *,
        load_detail: bool = False,
    ) -> KnowledgeItem:
        statement = select(KnowledgeItem).where(KnowledgeItem.id == knowledge_id)
        if load_detail:
            statement = statement.options(
                selectinload(KnowledgeItem.space),
                selectinload(KnowledgeItem.sources),
                selectinload(KnowledgeItem.evidence),
            )
        item = await self.session.scalar(statement)
        if not item or item.status not in principal.visible_statuses:
            raise LibraryNotFound("知识不存在或不可访问")
        if item.space_id:
            await self._space(item.space_id, principal)
        return item

    async def stats(self, principal: LibraryPrincipal) -> LibraryStats:
        scope_condition = await self._item_scope_condition(principal)
        published_conditions = [KnowledgeItem.status == "published"]
        if scope_condition is not None:
            published_conditions.append(scope_condition)
        published_count = await self.session.scalar(
            select(func.count(KnowledgeItem.id)).where(*published_conditions)
        )
        visible_conditions = [KnowledgeItem.status.in_(principal.visible_statuses)]
        if scope_condition is not None:
            visible_conditions.append(scope_condition)
        visible_count = await self.session.scalar(
            select(func.count(KnowledgeItem.id)).where(*visible_conditions)
        )
        if principal.is_admin:
            space_count = await self.session.scalar(
                select(func.count(KnowledgeSpace.id)).where(KnowledgeSpace.status == "active")
            )
        else:
            space_count = len(await self._visible_space_ids(principal))
        since = datetime.now(UTC) - timedelta(days=7)
        reference_conditions = [
            KnowledgeUsage.action == "read",
            KnowledgeUsage.created_at >= since,
        ]
        references = await self.session.scalar(
            select(func.count(KnowledgeUsage.id)).where(*reference_conditions)
        )
        experience_conditions = [
            KnowledgeItem.type == "experience",
            KnowledgeItem.status == "published",
        ]
        if scope_condition is not None:
            experience_conditions.append(scope_condition)
        published_experiences = await self.session.scalar(
            select(func.count(KnowledgeItem.id)).where(*experience_conditions)
        )
        candidates = 0
        if principal.can_review:
            candidate_conditions = [
                KnowledgeItem.type == "experience",
                KnowledgeItem.status == "candidate",
            ]
            if scope_condition is not None:
                candidate_conditions.append(scope_condition)
            candidates = (
                await self.session.scalar(select(func.count(KnowledgeItem.id)).where(*candidate_conditions))
                or 0
            )
        return LibraryStats(
            knowledge_pages=published_count or 0,
            visible_knowledge_total=visible_count or 0,
            knowledge_spaces=space_count or 0,
            weekly_references=references or 0,
            published_experiences=published_experiences or 0,
            candidate_experiences=candidates,
        )

    async def dashboard_summary(self, principal: LibraryPrincipal) -> dict[str, object]:
        """Return permission-aware counters for the lightweight dashboard card."""

        scope_condition = await self._item_scope_condition(principal)
        start, end = day_bounds()
        today_conditions = [
            KnowledgeItem.status.in_(principal.visible_statuses),
            KnowledgeItem.created_at >= start,
            KnowledgeItem.created_at < end,
        ]
        if scope_condition is not None:
            today_conditions.append(scope_condition)
        today_added = await self.session.scalar(select(func.count(KnowledgeItem.id)).where(*today_conditions))

        total_conditions = [KnowledgeItem.status.in_(principal.visible_statuses)]
        if scope_condition is not None:
            total_conditions.append(scope_condition)
        total = await self.session.scalar(select(func.count(KnowledgeItem.id)).where(*total_conditions))
        recent_items = (
            await self.session.scalars(
                select(KnowledgeItem)
                .where(*total_conditions)
                .order_by(KnowledgeItem.updated_at.desc())
                .limit(3)
            )
        ).all()

        candidates = 0
        if principal.can_review:
            candidate_conditions = [
                KnowledgeItem.type == "experience",
                KnowledgeItem.status == "candidate",
            ]
            if scope_condition is not None:
                candidate_conditions.append(scope_condition)
            candidates = (
                await self.session.scalar(select(func.count(KnowledgeItem.id)).where(*candidate_conditions))
                or 0
            )
        return {
            "today_added": today_added or 0,
            "candidate_experiences": candidates,
            "total": total or 0,
            "recent": [{"id": item.id, "title": item.title} for item in recent_items],
        }

    async def list_spaces(self, principal: LibraryPrincipal) -> list[KnowledgeSpaceRead]:
        count_statuses = principal.visible_statuses
        counts = (
            await self.session.execute(
                select(KnowledgeItem.space_id, func.count(KnowledgeItem.id))
                .where(KnowledgeItem.status.in_(count_statuses))
                .group_by(KnowledgeItem.space_id)
            )
        ).all()
        count_map = {space_id: count for space_id, count in counts}
        spaces = (
            await self.session.scalars(
                select(KnowledgeSpace).order_by(KnowledgeSpace.sort_order, KnowledgeSpace.name)
            )
        ).all()
        spaces_by_id = {space.id: space for space in spaces}
        visible = [space for space in spaces if _tree_scope_allowed(space, spaces_by_id, principal)]
        return _build_space_tree(visible, count_map)

    async def create_space(
        self, payload: KnowledgeSpaceCreate, principal: LibraryPrincipal
    ) -> KnowledgeSpaceRead:
        _require_manage(principal)
        if await self.session.scalar(select(KnowledgeSpace.id).where(KnowledgeSpace.name == payload.name)):
            raise LibraryConflict("知识空间名称已存在")
        if payload.parent_id:
            await self._space(payload.parent_id, principal)
        space = KnowledgeSpace(
            id=_new_id("SPACE"),
            created_by=principal.user_id,
            **payload.model_dump(),
        )
        self.session.add(space)
        await self.session.commit()
        await self.session.refresh(space)
        return KnowledgeSpaceRead.model_validate(space)

    async def update_space(
        self,
        space_id: str,
        payload: KnowledgeSpacePatch,
        principal: LibraryPrincipal,
    ) -> KnowledgeSpaceRead:
        _require_manage(principal)
        space = await self.session.get(KnowledgeSpace, space_id)
        if not space:
            raise LibraryNotFound("知识空间不存在")
        values = payload.model_dump(exclude_unset=True)
        if "parent_id" in values and values["parent_id"] != space.parent_id:
            await self._validate_space_move(space, values["parent_id"], principal)
        for key, value in values.items():
            setattr(space, key, value)
        await self.session.commit()
        if {"access_scope", "status", "parent_id"}.intersection(values):
            # Scope/placement changes alter which knowledge a viewer may read.
            descendant_ids = {space.id, *(await _space_descendant_ids(self.session, space.id))}
            items = (
                await self.session.scalars(
                    select(KnowledgeItem).where(
                        KnowledgeItem.space_id.in_(descendant_ids),
                        KnowledgeItem.status == "published",
                    )
                )
            ).all()
            for item in items:
                await self._prepare_sync(item)
        await self.session.refresh(space)
        return KnowledgeSpaceRead.model_validate(space)

    async def _validate_space_move(self, space: KnowledgeSpace, target_parent_id, principal) -> None:
        if target_parent_id == space.id:
            raise LibraryValidationError("目录不能移动到自身")
        if space.parent_id is None:
            if target_parent_id is not None:
                raise LibraryValidationError("知识空间不能移动，仅目录可以移动")
            return
        if target_parent_id is None:
            raise LibraryValidationError("目录必须归属于某个知识空间")
        await self._space(target_parent_id, principal)
        if await _space_root_id(self.session, space.id) != await _space_root_id(
            self.session, target_parent_id
        ):
            raise LibraryValidationError("目录不能移出当前知识空间")
        if target_parent_id in await _space_descendant_ids(self.session, space.id):
            raise LibraryValidationError("不能移动到自身或自己的子目录之下")

    async def move_space(
        self,
        space_id: str,
        target_parent_id: str | None,
        principal: LibraryPrincipal,
    ) -> KnowledgeSpaceRead:
        _require_manage(principal)
        await self._space(space_id, principal)
        return await self.update_space(space_id, KnowledgeSpacePatch(parent_id=target_parent_id), principal)

    async def delete_space(self, space_id: str, principal: LibraryPrincipal) -> dict[str, object]:
        _require_manage(principal)
        space = await self._space(space_id, principal)
        child_count = await self.session.scalar(
            select(func.count(KnowledgeSpace.id)).where(KnowledgeSpace.parent_id == space_id)
        )
        if child_count:
            raise LibraryConflict("该目录下存在子目录，请先移动或删除子目录。")
        knowledge_count = await self.session.scalar(
            select(func.count(KnowledgeItem.id)).where(KnowledgeItem.space_id == space_id)
        )
        if knowledge_count:
            raise LibraryConflict("该目录下存在知识，请先将知识移动到其他目录。")
        await self.session.delete(space)
        await self.session.commit()
        return {"deleted": space_id}

    async def list_knowledge(
        self,
        principal: LibraryPrincipal,
        *,
        space_id: str | None = None,
        type_: str | None = None,
        status: str | None = None,
        verification_status: str | None = None,
        source: str | None = None,
        tag: str | None = None,
        keyword: str | None = None,
        sort: str = "updated_desc",
        page: int = 1,
        page_size: int = 20,
    ) -> KnowledgePage:
        if status and status not in principal.visible_statuses:
            raise LibraryForbidden("无权查看该知识状态")
        if space_id:
            await self._space(space_id, principal)
        conditions = [KnowledgeItem.status.in_(principal.visible_statuses)]
        scope_condition = await self._item_scope_condition(principal)
        if scope_condition is not None:
            conditions.append(scope_condition)
        if space_id:
            conditions.append(KnowledgeItem.space_id.in_(await _space_descendant_ids(self.session, space_id)))
        if type_:
            if type_ not in KNOWLEDGE_TYPES:
                raise LibraryValidationError("未知知识类型")
            conditions.append(KnowledgeItem.type == type_)
        if status:
            conditions.append(KnowledgeItem.status == status)
        if verification_status:
            conditions.append(KnowledgeItem.verification_status == verification_status)
        if source == "manual":
            conditions.append(or_(KnowledgeItem.source_type == "manual", KnowledgeItem.source_type.is_(None)))
        elif source:
            conditions.append(KnowledgeItem.source_type == source)
        if tag and tag.strip():
            conditions.append(cast(KnowledgeItem.tags, String).ilike(f'%"{tag.strip()}"%'))
        if keyword and keyword.strip():
            pattern = f"%{keyword.strip()}%"
            conditions.append(
                or_(
                    KnowledgeItem.title.ilike(pattern),
                    KnowledgeItem.summary.ilike(pattern),
                    KnowledgeItem.content_md.ilike(pattern),
                    KnowledgeItem.source_ref.ilike(pattern),
                    KnowledgeItem.source_task_id.ilike(pattern),
                    cast(KnowledgeItem.tags, String).ilike(pattern),
                )
            )
        base = (
            select(KnowledgeItem, KnowledgeSpace.name)
            .outerjoin(KnowledgeSpace, KnowledgeItem.space_id == KnowledgeSpace.id)
            .where(*conditions)
        )
        order = {
            "updated_asc": KnowledgeItem.updated_at.asc(),
            "references_desc": desc(KnowledgeItem.reference_count),
            "title_asc": KnowledgeItem.title.asc(),
        }.get(sort, desc(KnowledgeItem.updated_at))
        total = await self.session.scalar(select(func.count(KnowledgeItem.id)).where(*conditions))
        rows = (
            await self.session.execute(
                base.order_by(order, KnowledgeItem.id).offset((page - 1) * page_size).limit(page_size)
            )
        ).all()
        return KnowledgePage(
            items=[_item_summary(item, space_name) for item, space_name in rows],
            total=total or 0,
            page=page,
            page_size=page_size,
            pages=max(1, math.ceil((total or 0) / page_size)),
        )

    async def get_detail(self, knowledge_id: str, principal: LibraryPrincipal) -> KnowledgeDetail:
        item = await self._item(knowledge_id, principal, load_detail=True)
        employee_name = None
        task_title = None
        if item.source_employee_id:
            employee_name = await self.session.scalar(
                select(AgentWorker.name).where(AgentWorker.id == item.source_employee_id)
            )
        if item.source_task_id:
            task_title = await self.session.scalar(select(Task.title).where(Task.id == item.source_task_id))
        favorite = await self.session.scalar(
            select(KnowledgeFavorite.id).where(
                KnowledgeFavorite.knowledge_id == item.id,
                KnowledgeFavorite.user_id == principal.user_id,
            )
        )
        helpful = await self.session.scalar(
            select(KnowledgeFeedback.id).where(
                KnowledgeFeedback.knowledge_id == item.id,
                KnowledgeFeedback.actor_key == f"user:{principal.user_id}",
                KnowledgeFeedback.feedback_type == "helpful",
            )
        )
        summary = _item_summary(item)
        return KnowledgeDetail(
            **summary.model_dump(),
            content_md=item.content_md,
            source_ref=item.source_ref,
            source_path=item.source_path,
            sync_error=item.sync_error,
            synced_at=item.synced_at,
            source_employee_id=item.source_employee_id,
            source_employee_name=employee_name,
            source_task_title=task_title,
            source_session_id=item.source_session_id,
            experience_type=item.experience_type,
            verified_at=item.verified_at,
            verification_session_status=item.verification_session_status,
            experience_data=item.experience_data or {},
            review_note=item.review_note,
            created_by=item.created_by,
            updated_by=item.updated_by,
            published_by=item.published_by,
            agentscope_kb_id=item.agentscope_kb_id,
            agentscope_document_id=item.agentscope_document_id,
            agentscope_sync_status=item.agentscope_sync_status,
            created_at=item.created_at,
            sources=item.sources,
            evidence=item.evidence,
            is_favorite=bool(favorite),
            user_helpful=bool(helpful),
        )

    async def create_knowledge(
        self, payload: KnowledgeCreate, principal: LibraryPrincipal
    ) -> KnowledgeDetail:
        _require_manage(principal)
        await self._space(payload.space_id, principal)
        item = KnowledgeItem(
            id=_new_id("KNOW"),
            status="draft",
            created_by=principal.user_id,
            updated_by=principal.user_id,
            **payload.model_dump(),
        )
        self.session.add(item)
        await self.session.flush()
        await self._audit(item, "created", principal, {"status": "draft"})
        await self.session.commit()
        return await self.get_detail(item.id, principal)

    async def update_knowledge(
        self,
        knowledge_id: str,
        payload: KnowledgePatch,
        principal: LibraryPrincipal,
    ) -> KnowledgeDetail:
        _require_manage(principal)
        item = await self._item(knowledge_id, principal)
        values = payload.model_dump(exclude_unset=True)
        if "space_id" in values:
            await self._space(values["space_id"], principal)
        if (item.type == "experience" and "content_md" in values
                and values["content_md"] != item.content_md):
            invalidate_experience_verification(item)
        for key, value in values.items():
            setattr(item, key, value)
        if item.type == "experience" and "content_md" in values:
            item.fingerprint = experience_fingerprint(item.content_md)
        item.updated_by = principal.user_id
        if item.status == "published":
            item.agentscope_sync_status = "pending"
        await self._audit(item, "updated", principal, {"fields": sorted(values)})
        await self.session.commit()
        if item.status == "published":
            # A published edit re-exports the same stable source path.
            await self._prepare_sync(item)
            await self._map_agentscope(item)
        return await self.get_detail(item.id, principal)

    async def move_knowledge(
        self, knowledge_id: str, space_id: str, principal: LibraryPrincipal
    ) -> KnowledgeDetail:
        _require_manage(principal)
        await self._space(space_id, principal)
        item = await self._item(knowledge_id, principal)
        previous = item.space_id
        item.space_id = space_id
        item.updated_by = principal.user_id
        await self._audit(item, "moved", principal, {"from_space_id": previous, "to_space_id": space_id})
        await self.session.commit()
        if item.status == "published" or item.source_path:
            await self._prepare_sync(item)
        return await self.get_detail(item.id, principal)

    async def batch_move(
        self, knowledge_ids: list[str], space_id: str, principal: LibraryPrincipal
    ) -> dict[str, int]:
        _require_manage(principal)
        await self._space(space_id, principal)
        moved = 0
        changed: list[KnowledgeItem] = []
        for knowledge_id in knowledge_ids:
            item = await self._item(knowledge_id, principal)
            previous = item.space_id
            item.space_id = space_id
            item.updated_by = principal.user_id
            await self._audit(item, "moved", principal, {"from_space_id": previous, "to_space_id": space_id})
            changed.append(item)
            moved += 1
        await self.session.commit()
        for item in changed:
            if item.status == "published" or item.source_path:
                await self._prepare_sync(item)
        return {"moved": moved}

    async def archive_knowledge(self, knowledge_id: str, principal: LibraryPrincipal) -> KnowledgeDetail:
        _require_manage(principal)
        item = await self._item(knowledge_id, principal)
        if item.status == "archived":
            raise LibraryConflict("知识已是归档状态")
        item.status = "archived"
        item.updated_by = principal.user_id
        await self._audit(item, "archived", principal)
        await self.session.commit()
        await self._prepare_sync(item)
        return await self.get_detail(item.id, principal)

    async def batch_archive(self, knowledge_ids: list[str], principal: LibraryPrincipal) -> dict[str, int]:
        _require_manage(principal)
        archived = 0
        changed: list[KnowledgeItem] = []
        for knowledge_id in knowledge_ids:
            item = await self._item(knowledge_id, principal)
            if item.status == "archived":
                continue
            item.status = "archived"
            item.updated_by = principal.user_id
            await self._audit(item, "archived", principal)
            changed.append(item)
            archived += 1
        await self.session.commit()
        for item in changed:
            await self._prepare_sync(item)
        return {"archived": archived}

    async def batch_publish(
        self,
        knowledge_ids: list[str],
        space_id: str,
        verification_status: str,
        principal: LibraryPrincipal,
    ) -> dict[str, int]:
        _require_review(principal)
        await self._space(space_id, principal)
        published = 0
        changed: list[KnowledgeItem] = []
        for knowledge_id in knowledge_ids:
            item = await self._item(knowledge_id, principal)
            if item.status not in {"candidate", "draft"}:
                raise LibraryConflict("只有候选或草稿知识可以发布")
            previous_status = item.status
            item.space_id = space_id
            item.status = "published"
            item.verification_status = verification_status
            item.published_by = principal.user_id
            item.published_at = utcnow()
            item.updated_by = principal.user_id
            await self._audit(item, "published", principal, {"from_status": previous_status})
            changed.append(item)
            published += 1
        await self.session.commit()
        for item in changed:
            await self._prepare_sync(item)
            await self._map_agentscope(item)
        return {"published": published}

    async def delete_knowledge(self, knowledge_id: str, principal: LibraryPrincipal) -> dict[str, str]:
        _require_manage(principal)
        item = await self._item(knowledge_id, principal)
        # The row disappears, so a failed cleanup cannot be recorded on it; log it.
        if item.source_path:
            try:
                await LlmWikiClient().delete_source(item.source_path)
            except LlmWikiError as exc:
                logger.warning("删除知识 %s 的 llm_wiki 源文件失败：%s", knowledge_id, exc)
        await self.session.execute(update(KnowledgeUsage).where(
            KnowledgeUsage.knowledge_id == knowledge_id).values(knowledge_id=None))
        await self.session.execute(update(KnowledgeFeedback).where(
            KnowledgeFeedback.knowledge_id == knowledge_id).values(knowledge_id=None))
        for model in (KnowledgeFavorite, KnowledgeAudit):
            rows = await self.session.scalars(select(model).where(model.knowledge_id == knowledge_id))
            for row in rows.all():
                await self.session.delete(row)
        await self.session.delete(item)
        await self.session.commit()
        return {"deleted": knowledge_id}

    async def knowledge_usages(
        self, knowledge_id: str, principal: LibraryPrincipal, limit: int = 20
    ) -> list[KnowledgeUsageRead]:
        await self._item(knowledge_id, principal)
        rows = (
            await self.session.execute(
                select(KnowledgeUsage, AgentWorker.name, Task.title)
                .outerjoin(AgentWorker, KnowledgeUsage.employee_id == AgentWorker.id)
                .outerjoin(Task, KnowledgeUsage.task_id == Task.id)
                .where(
                    KnowledgeUsage.knowledge_id == knowledge_id,
                    KnowledgeUsage.action == "read",
                )
                .order_by(desc(KnowledgeUsage.created_at))
                .limit(limit)
            )
        ).all()
        return [
            KnowledgeUsageRead(
                id=usage.id,
                employee_id=usage.employee_id,
                employee_name=employee_name,
                task_id=usage.task_id,
                task_title=task_title,
                action=usage.action,
                used_at=usage.created_at,
            )
            for usage, employee_name, task_title in rows
        ]

    async def publish_knowledge(
        self,
        knowledge_id: str,
        payload: KnowledgePublishWrite,
        principal: LibraryPrincipal,
    ) -> KnowledgeDetail:
        _require_review(principal)
        item = await self._item(knowledge_id, principal)
        if item.status not in {"candidate", "draft"}:
            raise LibraryConflict("只有候选或草稿知识可以发布")
        await self._space(payload.space_id, principal)
        previous_status = item.status
        values = payload.model_dump(exclude_unset=True)
        if (item.type == "experience" and "content_md" in values
                and values["content_md"] != item.content_md):
            invalidate_experience_verification(item)
        for key, value in values.items():
            setattr(item, key, value)
        if item.type == "experience":
            item.fingerprint = experience_fingerprint(item.content_md)
        item.status = "published"
        item.published_by = principal.user_id
        item.published_at = utcnow()
        item.updated_by = principal.user_id
        item.review_note = None
        await self._audit(
            item,
            "published",
            principal,
            {"from_status": previous_status, "space_id": payload.space_id},
        )
        await self.session.commit()
        await self._prepare_sync(item)
        await self._map_agentscope(item)
        return await self.get_detail(item.id, principal)

    async def _map_agentscope(self, item: KnowledgeItem) -> None:
        await sync_published_item(item)
        await self.session.commit()

    async def reject(
        self,
        knowledge_id: str,
        payload: KnowledgeReviewWrite,
        principal: LibraryPrincipal,
    ) -> KnowledgeDetail:
        _require_review(principal)
        item = await self._item(knowledge_id, principal)
        if item.status != "candidate":
            raise LibraryConflict("只有候选经验可以驳回")
        item.status = "rejected"
        item.review_note = payload.reason
        item.updated_by = principal.user_id
        await self._audit(item, "rejected", principal, {"reason": payload.reason})
        await self.session.commit()
        return await self.get_detail(item.id, principal)

    async def request_supplement(
        self,
        knowledge_id: str,
        payload: KnowledgeReviewWrite,
        principal: LibraryPrincipal,
    ) -> KnowledgeDetail:
        _require_review(principal)
        item = await self._item(knowledge_id, principal)
        if item.status != "candidate":
            raise LibraryConflict("只有候选经验可以要求补充")
        item.review_note = payload.reason
        item.updated_by = principal.user_id
        await self._audit(item, "supplement_requested", principal, {"reason": payload.reason})
        await self.session.commit()
        return await self.get_detail(item.id, principal)

    async def favorite(self, knowledge_id: str, principal: LibraryPrincipal) -> KnowledgeFavoriteResult:
        await self._item(knowledge_id, principal)
        favorite = await self.session.scalar(
            select(KnowledgeFavorite).where(
                KnowledgeFavorite.knowledge_id == knowledge_id,
                KnowledgeFavorite.user_id == principal.user_id,
            )
        )
        if favorite:
            await self.session.delete(favorite)
            value = False
        else:
            self.session.add(KnowledgeFavorite(knowledge_id=knowledge_id, user_id=principal.user_id))
            value = True
        await self.session.commit()
        return KnowledgeFavoriteResult(favorite=value)

    async def feedback_for_user(
        self,
        knowledge_id: str,
        payload: KnowledgeFeedbackWrite,
        principal: LibraryPrincipal,
    ) -> KnowledgeFeedbackResult:
        item = await self._item(knowledge_id, principal)
        return await self._feedback(
            item,
            feedback_type=payload.feedback_type,
            actor_key=f"user:{principal.user_id}",
            user_id=principal.user_id,
            employee_id=None,
            comment=payload.comment,
            task_id=payload.task_id,
        )

    async def _feedback(
        self,
        item: KnowledgeItem,
        *,
        feedback_type: str,
        actor_key: str,
        user_id: str | None,
        employee_id: str | None,
        comment: str | None,
        task_id: str | None,
    ) -> KnowledgeFeedbackResult:
        if feedback_type not in FEEDBACK_TYPES:
            raise LibraryValidationError("未知反馈类型")
        existing = await self.session.scalar(
            select(KnowledgeFeedback.id).where(
                KnowledgeFeedback.knowledge_id == item.id,
                KnowledgeFeedback.actor_key == actor_key,
                KnowledgeFeedback.feedback_type == feedback_type,
            )
        )
        created = existing is None
        if created:
            self.session.add(
                KnowledgeFeedback(
                    resource_id=f"astra:{item.id}",
                    knowledge_id=item.id,
                    actor_key=actor_key,
                    user_id=user_id,
                    employee_id=employee_id,
                    feedback_type=feedback_type,
                    comment=comment,
                    task_id=task_id,
                )
            )
            if feedback_type == "helpful":
                item.helpful_count += 1
        self.session.add(
            KnowledgeUsage(
                resource_id=f"astra:{item.id}",
                knowledge_id=item.id,
                employee_id=employee_id,
                task_id=task_id,
                tool_name="library_feedback",
                action="feedback",
                result={"feedback_type": feedback_type, "created": created},
            )
        )
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            current_count = await self.session.scalar(
                select(KnowledgeItem.helpful_count).where(KnowledgeItem.id == item.id)
            )
            return KnowledgeFeedbackResult(created=False, helpful_count=current_count or 0)
        return KnowledgeFeedbackResult(created=created, helpful_count=item.helpful_count)

    async def related_tasks(self, knowledge_id: str, principal: LibraryPrincipal) -> list[RelatedTaskRead]:
        item = await self._item(knowledge_id, principal)
        rows: dict[str, RelatedTaskRead] = {}
        if item.source_task_id:
            task = await self.session.get(Task, item.source_task_id)
            if task:
                rows[task.id] = RelatedTaskRead(
                    id=task.id,
                    title=task.title,
                    status=task.status,
                    relation="来源任务",
                )
        used = (
            await self.session.execute(
                select(
                    Task.id,
                    Task.title,
                    Task.status,
                    func.max(KnowledgeUsage.created_at),
                )
                .join(KnowledgeUsage, KnowledgeUsage.task_id == Task.id)
                .where(KnowledgeUsage.knowledge_id == knowledge_id)
                .group_by(Task.id, Task.title, Task.status)
                .order_by(desc(func.max(KnowledgeUsage.created_at)))
                .limit(50)
            )
        ).all()
        for task_id, title, status, last_used_at in used:
            if task_id in rows:
                rows[task_id].last_used_at = last_used_at
                continue
            rows[task_id] = RelatedTaskRead(
                id=task_id,
                title=title,
                status=status,
                relation="引用任务",
                last_used_at=last_used_at,
            )
        return list(rows.values())

    async def usage_summary(self, days: int = 7) -> LibraryUsageSummary:
        since = datetime.now(UTC) - timedelta(days=days - 1)
        conditions = [KnowledgeUsage.action == "read", KnowledgeUsage.created_at >= since]
        total = await self.session.scalar(select(func.count(KnowledgeUsage.id)).where(*conditions))
        used_resources = await self.session.scalar(
            select(func.count(func.distinct(KnowledgeUsage.resource_id))).where(*conditions)
        )
        feedback_count = await self.session.scalar(
            select(func.count(KnowledgeFeedback.id)).where(KnowledgeFeedback.created_at >= since)
        )
        top_rows = (
            await self.session.execute(
                select(
                    KnowledgeUsage.resource_id,
                    func.count(KnowledgeUsage.id).label("reference_count"),
                )
                .where(*conditions, KnowledgeUsage.resource_id.is_not(None))
                .group_by(KnowledgeUsage.resource_id)
                .order_by(desc("reference_count"))
                .limit(3)
            )
        ).all()
        trend_rows = (
            await self.session.execute(
                select(func.date(KnowledgeUsage.created_at), func.count(KnowledgeUsage.id))
                .where(*conditions)
                .group_by(func.date(KnowledgeUsage.created_at))
            )
        ).all()
        trend_map = {str(day): count for day, count in trend_rows}
        start = datetime.now(UTC).date() - timedelta(days=days - 1)
        trend = [
            UsageTrendPoint(
                date=(start + timedelta(days=offset)).isoformat(),
                reference_count=trend_map.get((start + timedelta(days=offset)).isoformat(), 0),
            )
            for offset in range(days)
        ]
        top_knowledge = []
        for resource_id, reference_count in top_rows:
            latest_hit = await self.session.scalar(
                select(KnowledgeUsage.result)
                .where(
                    KnowledgeUsage.resource_id == resource_id,
                    KnowledgeUsage.action == "search_hit",
                )
                .order_by(KnowledgeUsage.id.desc())
                .limit(1)
            )
            top_knowledge.append(
                UsageTopKnowledge(
                    resource_id=resource_id,
                    title=(latest_hit or {}).get("title") or resource_id,
                    reference_count=reference_count,
                )
            )
        return LibraryUsageSummary(
            days=days,
            total_references=total or 0,
            used_resources=used_resources or 0,
            feedback_count=feedback_count or 0,
            top_knowledge=top_knowledge,
            trend=trend,
        )

    async def recent_usage(self, limit: int = 20) -> list[LibraryUsageRecent]:
        rows = (
            await self.session.scalars(
                select(KnowledgeUsage).order_by(desc(KnowledgeUsage.created_at)).limit(limit)
            )
        ).all()
        return [
            LibraryUsageRecent(
                id=usage.id,
                resource_id=usage.resource_id,
                query=usage.query,
                action=usage.action,
                created_at=usage.created_at,
            )
            for usage in rows
        ]

    async def _validate_source_context(
        self,
        employee_id: str | None,
        task_id: str | None,
    ) -> tuple[AgentWorker | None, Task | None]:
        employee = await self.session.get(AgentWorker, employee_id) if employee_id else None
        task = await self.session.get(Task, task_id) if task_id else None
        if employee_id and not employee:
            raise LibraryValidationError("Agent Worker 不存在")
        if task_id and not task:
            raise LibraryValidationError("来源任务不存在")
        if task and employee and task.employee_id != employee.id:
            raise LibraryValidationError("Task 与 Employee 不匹配")
        return employee, task

    async def _experience_space_id(self, task: Task | None) -> str | None:
        """Map a task category onto an existing knowledge space."""

        if task is None:
            return None
        preferred = CATEGORY_KNOWLEDGE_SPACE.get(task.category_id or "", DEFAULT_EXPERIENCE_SPACE)
        for space_id in dict.fromkeys((preferred, DEFAULT_EXPERIENCE_SPACE)):
            space = await self.session.get(KnowledgeSpace, space_id)
            if space is not None and space.status == "active":
                return space.id
        return None

    async def mcp_search(
        self,
        payload: LibrarySearchInput,
        *,
        runtime_context: str,
    ) -> list[LibrarySearchResult]:
        context = await _resolve_runtime_context(self.session, runtime_context)
        results = await LlmWikiClient().search(payload.query, payload.limit)
        for result in results:
            self.session.add(
                KnowledgeUsage(
                    resource_id=result.resource_id,
                    employee_id=context.employee_id,
                    task_id=context.task_id,
                    session_id=context.session_id,
                    tool_name="library_search",
                    action="search_hit",
                    query=payload.query,
                    result={"title": result.title, "score": result.score},
                )
            )
        await self.session.commit()
        return results

    async def mcp_get(
        self,
        resource_id: str,
        *,
        runtime_context: str,
    ) -> LibraryResource:
        context = await _resolve_runtime_context(self.session, runtime_context)
        resource = await LlmWikiClient().read(resource_id)

        knowledge_id = None
        match = re.search(
            r"(?m)^astra_knowledge_id:\s*['\"]?([A-Za-z0-9_-]{1,64})['\"]?\s*$",
            resource.content_md,
        )
        if match:
            candidate = match.group(1)
            if await self.session.get(KnowledgeItem, candidate):
                knowledge_id = candidate

        self.session.add(
            KnowledgeUsage(
                resource_id=resource.resource_id,
                knowledge_id=knowledge_id,
                employee_id=context.employee_id,
                task_id=context.task_id,
                session_id=context.session_id,
                tool_name="library_get",
                action="read",
            )
        )
        await self.session.commit()
        return resource

    async def mcp_related(self, resource_id: str) -> dict:
        wiki_resource_path(resource_id)
        return {
            "status": "unsupported",
            "resource_id": resource_id,
            "results": [],
            "message": "当前 Graph API 不提供可靠的按页面查询邻居能力，相关知识暂不可用",
        }

    async def resolve_runtime_context(self, token: str | None) -> RuntimeContext:
        """Shared runtime MCP capability used by every runtime tool."""
        return await _resolve_runtime_context(self.session, token)

    async def mcp_submit_experience(
        self,
        payload: LibrarySubmitExperienceInput,
        *,
        review_session: RuntimeSession | None = None,
        review_key: str | None = None,
        merge_into_id: str | None = None,
        merge_reason: str = "",
        verified_message_id: str | None = None,
        commit: bool = True,
    ) -> KnowledgeDetail:
        """Internal candidate writer. Employee MCP deliberately has no submission tool.

        Native citations are checked by submit_review before entering this short
        transaction. Exact identity is global; semantic identity is selected by
        the review model after reading similar candidates and published items.
        """
        source_session_id = review_session.id if review_session else None
        if review_session:
            payload = payload.model_copy(update={
                "source_employee_id": review_session.employee_id,
                "source_task_id": review_session.task_id,
            })
        employee, task = await self._validate_source_context(
            payload.source_employee_id, payload.source_task_id,
        )
        fingerprint = experience_fingerprint(payload.content_md)
        actor = "assistant:nexus-task-experience-review" if review_session else "manual"
        exact = await self.session.scalar(
            select(KnowledgeItem).where(
                KnowledgeItem.type == "experience",
                KnowledgeItem.status.in_(("candidate", "published")),
                or_(KnowledgeItem.fingerprint == fingerprint,
                    KnowledgeItem.content_md == payload.content_md),
            ).order_by(KnowledgeItem.status.desc(), KnowledgeItem.created_at).limit(1)
            .with_for_update().execution_options(populate_existing=True)
        )
        item = exact
        if merge_into_id and item is None:
            if not review_session or not merge_reason.strip():
                raise LibraryConflict("语义合并必须说明适用条件与方法一致的依据")
            item = await self.session.scalar(select(KnowledgeItem).where(
                KnowledgeItem.id == merge_into_id,
            ).with_for_update().execution_options(populate_existing=True))
            if not item or item.type != "experience" or item.status not in {"candidate", "published"}:
                raise LibraryConflict("合并目标必须是候选或已发布经验")
        created = item is None
        if created:
            # 界面只认识 task / manual / import / system。复盘产物用 task，
            # 这样列表来源是「任务沉淀」并带上来源任务编号。
            item = KnowledgeItem(
                id=_new_id("KNOW"), title=payload.title, type="experience",
                summary=payload.summary, content_md=payload.content_md,
                status="candidate", verification_status="unverified", tags=payload.tags,
                space_id=await self._experience_space_id(task) if review_session else None,
                source_type="task" if review_session else "manual",
                source_ref=payload.source_task_id, source_employee_id=payload.source_employee_id,
                source_task_id=payload.source_task_id, source_session_id=source_session_id,
                origin="assistant" if review_session else "manual",
                dedupe_key="global-experience", fingerprint=fingerprint,
                experience_type=payload.experience_type or "other", scope=payload.scope,
                source_ref_snapshot={
                    "task_id": task.id if task else None, "task_title": task.title if task else None,
                    "employee_id": payload.source_employee_id, "session_id": source_session_id,
                    "turn_anchor_id": task.last_message_id if task else None, "review_id": review_key,
                },
                experience_data=payload.experience_data, created_by=actor, updated_by=actor,
            )
            try:
                # A unique global fingerprint closes concurrent exact inserts.
                async with self.session.begin_nested():
                    self.session.add(item)
                    await self.session.flush()
            except IntegrityError:
                item = await self.session.scalar(select(KnowledgeItem).where(
                    KnowledgeItem.dedupe_key == "global-experience",
                    KnowledgeItem.fingerprint == fingerprint,
                ).with_for_update())
                if item is None or item.status not in {"candidate", "published"}:
                    raise LibraryConflict("重复经验已被归档或拒绝，请人工审核") from None
                created = False
        elif item.status == "candidate" and merge_into_id and not exact:
            # The model submits a consolidated rule, retaining all prior evidence.
            item.title, item.summary, item.content_md = payload.title, payload.summary, payload.content_md
            item.tags = list(dict.fromkeys([*(item.tags or []), *payload.tags]))
            item.fingerprint = fingerprint
            invalidate_experience_verification(item)
            item.updated_by = actor
        # Published content and its approval are immutable to automatic reviews.
        existing_refs = set((await self.session.execute(select(
            ExperienceEvidence.source_session_id, ExperienceEvidence.message_id,
            ExperienceEvidence.part_id, ExperienceEvidence.evidence_type,
        ).where(ExperienceEvidence.knowledge_id == item.id))).all())
        for evidence in payload.evidence:
            ref = (source_session_id, evidence.message_id, evidence.part_id, evidence.evidence_type)
            if ref in existing_refs:
                continue
            existing_refs.add(ref)
            self.session.add(ExperienceEvidence(
                knowledge_id=item.id, task_id=payload.source_task_id,
                source_session_id=source_session_id, message_id=evidence.message_id,
                part_id=evidence.part_id, file_path=evidence.file_path, diff_hash=evidence.diff_hash,
                evidence_type=evidence.evidence_type, content=evidence.content,
                uri=evidence.uri, result=evidence.result, captured_at=utcnow(),
            ))
        if verified_message_id and item.status == "candidate":
            item.verification_status = "verified"
            item.verification_message_id = verified_message_id
            item.verification_session_status = review_session.status
            item.session_last_message_id_at_verification = review_session.last_message_id
            item.verified_at = utcnow()
        if created and review_session:
            item.session_status_at_submission = review_session.status
        await self._audit(
            item, "candidate_submitted" if created else "experience_reused",
            LibraryPrincipal(user_id=actor, role="viewer"),
            {"task_id": task.id if task else None, "session_id": source_session_id,
             "review_id": review_key, "merge_reason": merge_reason,
             "evidence_count": len(payload.evidence)},
        )
        self.session.add(KnowledgeUsage(
            resource_id=f"astra:{item.id}", knowledge_id=item.id,
            employee_id=employee.id if employee else None, task_id=task.id if task else None,
            session_id=source_session_id, tool_name="nexus_task_review_submit",
            action="experience_submitted" if created else "experience_reused",
            result={"status": item.status},
        ))
        if commit:
            await self.session.commit()
        else:
            await self.session.flush()
        return await self.get_detail(item.id, LibraryPrincipal(user_id="review", role="reviewer"))

    async def mcp_feedback(self, payload: LibraryFeedbackInput) -> LibraryResourceFeedbackResult:
        resource_id = wiki_resource_path(payload.resource_id)
        feedback = KnowledgeFeedback(
            resource_id=resource_id,
            actor_key="mcp",
            feedback_type=payload.feedback_type,
            comment=payload.comment,
        )
        self.session.add(feedback)
        self.session.add(
            KnowledgeUsage(
                resource_id=resource_id,
                tool_name="library_feedback",
                action="feedback",
                result={"feedback_type": payload.feedback_type},
            )
        )
        await self.session.commit()
        return LibraryResourceFeedbackResult(
            id=feedback.id,
            resource_id=resource_id,
            feedback_type=payload.feedback_type,
        )

    async def service_status(self) -> LibraryServiceStatus:
        settings = get_settings()
        counts = dict(
            (
                await self.session.execute(
                    select(KnowledgeItem.sync_status, func.count()).group_by(
                        KnowledgeItem.sync_status
                    )
                )
            ).all()
        )
        last_synced = await self.session.scalar(select(func.max(KnowledgeItem.synced_at)))
        healthy = False
        try:
            await LlmWikiClient(settings).health()
            healthy = True
            message = "源文件已同步；扫描接受不代表知识加工完成"
        except LlmWikiError as exc:
            message = str(exc)
        if counts.get("failed", 0):
            healthy = False
            error = await self.session.scalar(
                select(KnowledgeItem.sync_error)
                .where(KnowledgeItem.sync_status == "failed")
                .order_by(KnowledgeItem.updated_at.desc())
                .limit(1)
            )
            message = error or message
        return LibraryServiceStatus(
            enabled=settings.llm_wiki_enabled,
            healthy=healthy,
            message=message,
            last_synced_at=last_synced,
            pending_count=counts.get("pending", 0),
            failed_count=counts.get("failed", 0),
        )

    async def resync(self, principal: LibraryPrincipal) -> dict[str, int]:
        _require_manage(principal)
        # Re-push the current published set and retry failed/missing deletions.
        items = (
            await self.session.scalars(
                select(KnowledgeItem).where(
                    or_(
                        KnowledgeItem.status == "published",
                        and_(
                            KnowledgeItem.source_path.is_not(None),
                            KnowledgeItem.sync_status != "synced",
                        ),
                    )
                )
            )
        ).all()
        for item in items:
            await self._prepare_sync(item)
        return {"pending_count": len(items)}
