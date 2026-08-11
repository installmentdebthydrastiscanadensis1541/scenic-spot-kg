"""验证 DuckDuckGo 搜索工具"""
import asyncio
from tools.web_search import WebSearchTool


async def test():
    tool = WebSearchTool()
    print(f"搜索可用: {tool._available}")

    if tool._available:
        result = await tool.run("黄山风景区 旅游攻略")
        print("\n=== 搜索结果 ===")
        print(result)
    else:
        print("搜索不可用，请检查 duckduckgo-search 是否安装")


asyncio.run(test())
