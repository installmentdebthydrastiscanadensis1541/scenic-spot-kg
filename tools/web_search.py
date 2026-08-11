"""实时搜索工具 — DuckDuckGo免费搜索

当知识库中无足够信息时，智能体调用此工具搜索实时信息，
避免LLM产生幻觉。搜索结果作为Observation反馈给Agent。
"""
from tools.base import BaseTool, ToolParameter


class WebSearchTool(BaseTool):
    name = "web_search"
    description = "搜索互联网获取景点实时信息，当知识库信息不足时使用"
    parameters = [
        ToolParameter(name="query", description="搜索查询文本"),
    ]

    def __init__(self):
        self._available = False
        try:
            from ddgs import DDGS
            self._available = True
        except ImportError:
            print("[WebSearch] ddgs未安装，搜索功能不可用")

    async def run(self, input_str: str) -> str:
        if not self._available:
            return "搜索功能不可用：ddgs未安装。请运行 pip install ddgs"

        try:
            import asyncio
            from ddgs import DDGS

            # DDGS是同步库，用线程池+超时避免卡住
            def _search():
                return DDGS().text(input_str, region="cn-zh", max_results=5)

            results = await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(None, _search),
                timeout=15
            )
            if not results:
                return f"未搜索到与 '{input_str}' 相关的结果。"

            # 截断每条结果避免超长，最多3条
            parts = []
            for i, r in enumerate(results[:3], 1):
                title = r['title'][:50]
                body = r['body'][:100]
                parts.append(f"{i}. {title}\n   {body}")
            return "\n".join(parts)
        except asyncio.TimeoutError:
            return "搜索超时，请稍后重试。"
        except Exception as e:
            return f"搜索出错：{e}"
