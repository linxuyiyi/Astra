from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

KnowledgeType = Literal["document", "rule", "guide", "experience", "faq", "tool", "example"]
KnowledgeStatus = Literal["draft", "candidate", "published", "archived", "rejected"]
VerificationStatus = Literal["unverified", "verified", "failed"]
FeedbackType = Literal["helpful", "incorrect", "outdated", "missing", "mismatch"]


class LibraryORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class KnowledgeSpaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=2000)
    parent_id: str | None = None
    sort_order: int = 0
    access_scope: str = Field(default="public", max_length=160)


class KnowledgeSpacePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=2000)
    parent_id: str | None = None
    sort_order: int | None = None
    status: Literal["active", "inactive"] | None = None
    access_scope: str | None = Field(default=None, max_length=160)


class KnowledgeSpaceRead(LibraryORMModel):
    id: str
    name: str
    description: str
    parent_id: str | None
    sort_order: int
    status: str
    access_scope: str
    knowledge_count: int = 0
    subtree_knowledge_count: int = 0
    children: list[KnowledgeSpaceRead] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class KnowledgeSourceRead(LibraryORMModel):
    id: int
    source_type: str
    source_id: str | None
    source_name: str | None
    source_uri: str | None
    version: str | None
    created_at: datetime


class ExperienceEvidenceWrite(BaseModel):
    evidence_type: str = Field(min_length=1, max_length=64)
    content: str | None = None
    uri: str | None = None
    result: str | None = Field(default=None, max_length=64)
    task_id: str | None = None
    message_id: str | None = Field(default=None, max_length=160)
    part_id: str | None = Field(default=None, max_length=160)
    file_path: str | None = None
    diff_hash: str | None = Field(default=None, max_length=64)
    source_system: str | None = None
    native_session_id: str | None = Field(default=None, max_length=160)
    source_digest: str | None = Field(default=None, max_length=64)
    review_job_id: str | None = Field(default=None, max_length=64)
    validation_status: str = "unverified"


class ExperienceEvidenceRead(LibraryORMModel):
    id: int
    task_id: str | None
    source_session_id: str | None
    message_id: str | None
    part_id: str | None
    file_path: str | None
    diff_hash: str | None
    evidence_type: str
    content: str | None
    uri: str | None
    result: str | None
    source_system: str | None = None
    native_session_id: str | None = None
    source_digest: str | None = None
    review_job_id: str | None = None
    validation_status: str = "unverified"
    created_at: datetime


class KnowledgeCreate(BaseModel):
    space_id: str | None = None
    title: str = Field(min_length=1, max_length=240)
    type: KnowledgeType = "document"
    summary: str = Field(default="", max_length=4000)
    content_md: str = Field(min_length=1)
    verification_status: VerificationStatus = "unverified"
    tags: list[str] = Field(default_factory=list, max_length=50)
    source_type: str | None = Field(default=None, max_length=64)
    source_ref: str | None = Field(default=None, max_length=240)
    source_task_id: str | None = None

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, tags: list[str]) -> list[str]:
        return list(dict.fromkeys(tag.strip() for tag in tags if tag.strip()))


class KnowledgePatch(BaseModel):
    space_id: str | None = None
    title: str | None = Field(default=None, min_length=1, max_length=240)
    type: KnowledgeType | None = None
    summary: str | None = Field(default=None, max_length=4000)
    content_md: str | None = Field(default=None, min_length=1)
    verification_status: VerificationStatus | None = None
    tags: list[str] | None = Field(default=None, max_length=50)
    source_type: str | None = Field(default=None, max_length=64)
    source_ref: str | None = Field(default=None, max_length=240)

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, tags: list[str] | None) -> list[str] | None:
        if tags is None:
            return None
        return list(dict.fromkeys(tag.strip() for tag in tags if tag.strip()))


class SpaceMoveWrite(BaseModel):
    target_parent_id: str | None = None


class KnowledgeMoveWrite(BaseModel):
    space_id: str


class KnowledgeBatchMoveWrite(BaseModel):
    knowledge_ids: list[str] = Field(min_length=1)
    space_id: str


class KnowledgeBatchIds(BaseModel):
    knowledge_ids: list[str] = Field(min_length=1)


class KnowledgeBatchPublishWrite(BaseModel):
    knowledge_ids: list[str] = Field(min_length=1)
    space_id: str
    verification_status: VerificationStatus = "verified"


class KnowledgeUsageRead(LibraryORMModel):
    id: int
    employee_id: str | None
    employee_name: str | None = None
    task_id: str | None
    task_title: str | None = None
    action: str
    used_at: datetime


class KnowledgePublishWrite(BaseModel):
    space_id: str
    title: str | None = Field(default=None, min_length=1, max_length=240)
    summary: str | None = Field(default=None, max_length=4000)
    content_md: str | None = Field(default=None, min_length=1)
    tags: list[str] | None = Field(default=None, max_length=50)
    verification_status: VerificationStatus | None = None

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, tags: list[str] | None) -> list[str] | None:
        if tags is None:
            return None
        return list(dict.fromkeys(tag.strip() for tag in tags if tag.strip()))


class KnowledgeReviewWrite(BaseModel):
    reason: str = Field(min_length=2, max_length=4000)


class KnowledgeListItem(LibraryORMModel):
    id: str
    space_id: str | None
    space_name: str | None = None
    title: str
    type: KnowledgeType
    summary: str
    status: KnowledgeStatus
    sync_status: str = "not_synced"
    verification_status: VerificationStatus
    tags: list[str]
    source_type: str | None = None
    source_task_id: str | None
    reference_count: int
    helpful_count: int
    updated_at: datetime
    published_at: datetime | None


class KnowledgeDetail(KnowledgeListItem):
    content_md: str
    source_type: str | None
    source_ref: str | None
    source_path: str | None = None
    sync_error: str | None = None
    synced_at: datetime | None = None
    source_employee_id: str | None
    source_employee_name: str | None = None
    source_task_title: str | None = None
    source_session_id: str | None = None
    experience_type: str | None = None
    verified_at: datetime | None = None
    verification_session_status: str | None = None
    experience_data: dict[str, Any]
    review_note: str | None
    created_by: str
    updated_by: str
    published_by: str | None
    agentscope_kb_id: str = ""
    agentscope_document_id: str = ""
    agentscope_sync_status: str = "not_synced"
    created_at: datetime
    sources: list[KnowledgeSourceRead] = Field(default_factory=list)
    evidence: list[ExperienceEvidenceRead] = Field(default_factory=list)
    is_favorite: bool = False
    user_helpful: bool = False


class KnowledgePage(BaseModel):
    items: list[KnowledgeListItem]
    total: int
    page: int
    page_size: int
    pages: int


class LibraryStats(BaseModel):
    knowledge_pages: int
    visible_knowledge_total: int
    knowledge_spaces: int
    weekly_references: int
    published_experiences: int
    candidate_experiences: int


class KnowledgeFeedbackWrite(BaseModel):
    feedback_type: FeedbackType
    comment: str | None = Field(default=None, max_length=4000)
    task_id: str | None = None


class KnowledgeFeedbackResult(BaseModel):
    created: bool
    helpful_count: int


class KnowledgeFavoriteResult(BaseModel):
    favorite: bool


class RelatedTaskRead(BaseModel):
    id: str
    title: str
    status: str
    relation: str
    last_used_at: datetime | None = None


class UsageTopKnowledge(BaseModel):
    resource_id: str
    title: str
    reference_count: int


class UsageTrendPoint(BaseModel):
    date: str
    reference_count: int


class LibraryUsageSummary(BaseModel):
    days: int
    total_references: int
    used_resources: int
    feedback_count: int
    top_knowledge: list[UsageTopKnowledge]
    trend: list[UsageTrendPoint]


class LibraryUsageRecent(BaseModel):
    id: int
    resource_id: str | None
    action: str
    query: str | None
    created_at: datetime


class LibrarySearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=10, ge=1, le=50)


class LibrarySearchResult(BaseModel):
    resource_id: str
    title: str
    path: str
    excerpt: str
    score: float


class LibraryResource(BaseModel):
    resource_id: str
    path: str
    content_md: str


class LibrarySubmitExperienceInput(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    summary: str = Field(default="", max_length=4000)
    content_md: str = Field(min_length=1)
    experience_type: str | None = Field(default=None, max_length=64)
    scope: dict[str, Any] = Field(default_factory=dict)
    source_employee_id: str | None = None
    source_task_id: str | None = None
    evidence: list[ExperienceEvidenceWrite] = Field(default_factory=list, max_length=100)
    tags: list[str] = Field(default_factory=list, max_length=50)
    experience_data: dict[str, Any] = Field(default_factory=dict)

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, tags: list[str]) -> list[str]:
        return list(dict.fromkeys(tag.strip() for tag in tags if tag.strip()))


class LibraryFeedbackInput(BaseModel):
    resource_id: str = Field(min_length=1, max_length=512)
    feedback_type: FeedbackType
    comment: str | None = Field(default=None, max_length=4000)


class LibraryResourceFeedbackResult(BaseModel):
    id: int
    resource_id: str
    feedback_type: FeedbackType


class LibraryServiceStatus(BaseModel):
    enabled: bool
    healthy: bool
    message: str
    last_synced_at: datetime | None
    pending_count: int
    failed_count: int


class ExperienceReviewSegmentRead(BaseModel):
    id: int
    segment_index: int
    status: str
    token_estimate: int
    attempts: int
    error_code: str | None = None


class ExperienceReviewJobRead(BaseModel):
    id: str
    task_id: str | None
    task_id_snapshot: str
    root_session_id: str
    status: str
    phase: str
    attempts: int
    source_message_count: int
    segment_count: int
    completed_segment_count: int
    candidate_ids: list[str] = Field(default_factory=list)
    zero_reason: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    confirmed_at: datetime
    created_at: datetime
    finished_at: datetime | None = None
    segments: list[ExperienceReviewSegmentRead] = Field(default_factory=list)
