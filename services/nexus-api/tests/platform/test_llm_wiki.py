import pytest

from app.config.settings import Settings
from app.library_schemas import (
    KnowledgeCreate,
    KnowledgePublishWrite,
)
from app.library_service import KnowledgeService, LibraryPrincipal


def wiki_resource(full_path: str) -> str:
    return f"wiki/sources/{full_path.rsplit('/', 1)[-1]}"


async def publish(sessions, space_id, title="timeout", content="original body"):
    async with sessions() as session:
        service = KnowledgeService(session)
        item = await service.create_knowledge(
            KnowledgeCreate(space_id=space_id, title=title, content_md=content),
            LibraryPrincipal(),
        )
        await service.publish_knowledge(item.id, KnowledgePublishWrite(space_id=space_id), LibraryPrincipal())
        return item.id


def test_llm_wiki_url_allows_remote_and_https():
    from pydantic import ValidationError

    # Valid URLs: local, remote IP, domain, https, custom port
    for valid_url in (
        "http://127.0.0.1:19828/api/v1",
        "http://192.168.1.100:19828/api/v1",
        "https://wiki.example.com/api/v1",
        "https://wiki.company.internal:8443/api/v1",
        "http://remote-host:8000/api/v1/",
    ):
        settings = Settings(
            llm_wiki_api_base_url=valid_url,
            _env_file=None,
        )
        assert settings.llm_wiki_api_base_url == valid_url.rstrip("/")

    # Invalid URLs: invalid scheme, missing host, query params, fragments, auth, bad path
    for invalid_url in (
        "ftp://wiki.example.com/api/v1",
        "http:///api/v1",
        "https://wiki.example.com/api/v1?token=123",
        "https://wiki.example.com/api/v1#section",
        "https://user:pass@wiki.example.com/api/v1",
        "https://wiki.example.com/api/v2",
        "https://wiki.example.com/",
    ):
        with pytest.raises(ValidationError, match="llm_wiki_api_base_url"):
            Settings(
                llm_wiki_api_base_url=invalid_url,
                _env_file=None,
            )
