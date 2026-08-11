"""验证向量检索"""
import asyncio
from tools.knowledge_search import KnowledgeSearchTool

async def test():
    tool = KnowledgeSearchTool()
    result = await tool.run("故宫的历史")
    print("=== 向量检索结果 ===")
    print(result)
    print()
    print("向量模式:", tool._vector_mode)

asyncio.run(test())
