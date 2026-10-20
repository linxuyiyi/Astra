"""HTTP client for the original llm_wiki REST API; no local filesystem access.

Writes go through the Source API (``PUT/DELETE .../sources/file``); reads
(``search``/``read``) back the Agent-facing MCP tools. Astra never writes to the
llm_wiki project directory itself.
"""

import re
from pathlib import PurePosixPath
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from app.config.settings import Settings, get_settings
from app.library_schemas import LibraryResource, LibrarySearchResult

# The Source API is scoped to ``raw/sources/**``; callers pass the full
# project-relative path and the client strips this prefix for the API request.
SOURCE_ROOT = "raw/sources/"
# Stable identifier path: renaming the title keeps updating the same source.
KNOWLEDGE_SOURCE_ROOT = "raw/sources/astra/knowledge"
_KNOWLEDGE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


class LlmWikiError(Exception):
    pass


def knowledge_source_path(knowledge_id: str) -> str:
    """Full project-relative source path for one Astra knowledge record."""
    if not _KNOWLEDGE_ID.fullmatch(knowledge_id):
        raise LlmWikiError("知识同步失败：knowledge_id 不是安全的稳定文件名")
    return f"{KNOWLEDGE_SOURCE_ROOT}/{knowledge_id}.md"


def wiki_resource_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or len(value) > 512
        or "\\" in value
        or "\x00" in value
        or path.is_absolute()
        or ".." in path.parts
        or str(path) != value
        or len(path.parts) < 2
        or path.parts[0] != "wiki"
        or path.suffix != ".md"
    ):
        raise LlmWikiError("resource_id 必须是 wiki/ 下的 Markdown 相对路径")
    return value


class _SearchHit(BaseModel):
    model_config = ConfigDict(strict=True, allow_inf_nan=False)
    path: str
    title: str
    snippet: str
    score: float


class _SearchResponse(BaseModel):
    results: list[_SearchHit]


class _FileResponse(BaseModel):
    model_config = ConfigDict(strict=True)
    path: str
    content: str


class LlmWikiClient:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    async def _http(
        self, method: str, path: str, *, allow_not_found: bool = False, **kwargs
    ) -> dict | None:
        settings = self.settings
        headers = {"Accept": "application/json"}
        if settings.llm_wiki_api_token:
            headers["Authorization"] = f"Bearer {settings.llm_wiki_api_token}"
        try:
            async with httpx.AsyncClient(timeout=settings.llm_wiki_timeout, trust_env=False) as client:
                response = await client.request(
                    method, settings.llm_wiki_api_base_url + path, headers=headers, **kwargs
                )
        except httpx.TimeoutException:
            raise LlmWikiError("知识服务暂不可用：HTTP 请求超时") from None
        except httpx.RequestError:
            raise LlmWikiError("知识服务暂不可用：无法连接 llm_wiki") from None
        if response.status_code in {401, 403}:
            raise LlmWikiError("知识服务暂不可用：API Token 错误或权限不足")
        if response.status_code == 404:
            if allow_not_found:
                return None
            raise LlmWikiError("知识服务暂不可用：项目或资源不存在")
        if not response.is_success:
            raise LlmWikiError(f"知识服务暂不可用：HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError:
            raise LlmWikiError("知识服务暂不可用：响应不是 JSON") from None
        if not isinstance(data, dict) or data.get("ok") is False:
            raise LlmWikiError("知识服务暂不可用：API 返回失败结果")
        return data

    def _require_enabled(self) -> None:
        if not self.settings.llm_wiki_enabled:
            raise LlmWikiError("知识服务暂不可用：未启用 llm_wiki")

    async def request(self, method: str, path: str, **kwargs) -> dict | None:
        self._require_enabled()
        if not self.settings.llm_wiki_project_id:
            raise LlmWikiError("知识服务暂不可用：未配置 project_id")
        return await self._http(method, path, **kwargs)

    @property
    def project_route(self) -> str:
        return f"/projects/{quote(self.settings.llm_wiki_project_id, safe='')}"

    async def health(self) -> dict:
        """Confirm the HTTP API is reachable and running before touching sources."""
        self._require_enabled()
        data = await self._http("GET", "/health")
        if data is None or data.get("ok") is not True or data.get("status") != "running":
            raise LlmWikiError("知识服务暂不可用：健康检查失败")
        return data

    def _source_api_path(self, source_path: str) -> str:
        if (
            not source_path
            or not source_path.startswith(SOURCE_ROOT)
            or "\\" in source_path
            or "\x00" in source_path
            or ".." in PurePosixPath(source_path).parts
        ):
            raise LlmWikiError("知识同步失败：source_path 必须是 raw/sources/ 下的相对路径")
        return source_path[len(SOURCE_ROOT) :]

    @staticmethod
    def _validate_source_response(data: dict, source_path: str) -> None:
        if data.get("ok") is not True or data.get("path") != source_path:
            raise LlmWikiError("知识服务暂不可用：源文件操作响应不匹配")

    async def upsert_source(self, source_path: str, content: str) -> dict:
        """Create or update one source; llm_wiki triggers its own rescan."""
        api_path = self._source_api_path(source_path)
        data = await self.request(
            "PUT",
            self.project_route + "/sources/file",
            json={"path": api_path, "content": content},
        )
        assert data is not None
        self._validate_source_response(data, source_path)
        return data

    async def delete_source(self, source_path: str) -> bool:
        """Delete one source. A missing source already matches the target state."""
        api_path = self._source_api_path(source_path)
        data = await self.request(
            "DELETE",
            self.project_route + "/sources/file",
            params={"path": api_path},
            allow_not_found=True,
        )
        if data is None:
            return False
        self._validate_source_response(data, source_path)
        return True

    async def search(self, query: str, limit: int) -> list[LibrarySearchResult]:
        data = await self.request(
            "POST",
            self.project_route + "/search",
            json={
                "query": query,
                "topK": limit,
                "includeContent": False,
            },
        )
        assert data is not None
        try:
            hits = _SearchResponse.model_validate(data).results
        except ValidationError:
            raise LlmWikiError("知识服务暂不可用：搜索响应格式错误") from None
        return [
            LibrarySearchResult(
                resource_id=wiki_resource_path(hit.path),
                path=hit.path,
                title=hit.title,
                excerpt=hit.snippet,
                score=hit.score,
            )
            for hit in hits[:limit]
        ]

    async def read(self, resource_id: str) -> LibraryResource:
        path = wiki_resource_path(resource_id)
        data = await self.request("GET", self.project_route + "/files/content", params={"path": path})
        assert data is not None
        try:
            resource = _FileResponse.model_validate(data)
        except ValidationError:
            raise LlmWikiError("知识服务暂不可用：读取响应格式错误") from None
        if resource.path != path:
            raise LlmWikiError("知识服务暂不可用：返回资源路径不匹配")
        return LibraryResource(resource_id=path, path=path, content_md=resource.content)