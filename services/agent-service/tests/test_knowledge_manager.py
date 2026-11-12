"""启动时挂上 knowledge_base_manager。个人记忆不写图书馆。"""

from pathlib import Path


def test_launcher_passes_a_knowledge_base_manager_and_keeps_memory_local():
    source = Path(__file__).resolve().parents[1].joinpath("agent_service.py").read_text()
    before_main = source.split("def main", 1)[0]
    main = source.split("def main", 1)[1]
    assert "import agentscope" not in before_main
    assert "knowledge_base_manager=knowledge_base_manager" in main
    assert "CollectionPerKbManager" in main
    memory = source.split("def attach_memory", 1)[1].split("async def memory_middlewares", 1)[0]
    middlewares = source.split("async def memory_middlewares", 1)[1]
    middlewares = middlewares.split("def main(", 1)[0]
    block = memory + middlewares
    assert "KnowledgeItem" not in block
    assert "knowledge_items" not in block
    assert "library_service" not in block
