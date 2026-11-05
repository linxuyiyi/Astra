"""演示数据遵循正式 Task/Worker 模型；原生 Session 由运行时创建。"""

from app.bootstrap import bootstrap_categories, bootstrap_knowledge_spaces


async def seed_demo_data(session):
    await bootstrap_categories(session)
    await bootstrap_knowledge_spaces(session)
