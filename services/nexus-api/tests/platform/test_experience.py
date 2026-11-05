"""Experience identity is global; verification requires cited execution evidence."""

import asyncio

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.library_schemas import LibrarySubmitExperienceInput
from app.library_service import KnowledgeService
from app.models import Base, KnowledgeItem


def test_exact_fingerprint_preserves_code_case_and_indentation():
    from app.library_service import experience_fingerprint

    assert experience_fingerprint("assert Foo") != experience_fingerprint("assert foo")
    assert experience_fingerprint("if ok:\n    run()") != experience_fingerprint("if ok:\nrun()")
    assert experience_fingerprint("规则\r\n方法") == experience_fingerprint("规则\n方法")


async def test_concurrent_exact_submissions_share_one_record(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'concurrent.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def save(title):
        async with factory() as session:
            return (
                await KnowledgeService(session).write_experience_candidate(
                    LibrarySubmitExperienceInput(title=title, content_md="同一可复用判断与验证方法"),
                )
            ).id

    try:
        ids = await asyncio.gather(save("任务甲的提炼"), save("任务乙的提炼"))
        assert ids[0] == ids[1]
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(KnowledgeItem)) == 1
    finally:
        await engine.dispose()
