"""实时搜索工具 — DuckDuckGo免费搜索

当知识库中无足够信息时，智能体调用此工具搜索实时信息，
避免LLM产生幻觉。搜索结果作为Observation反馈给Agent。

注意：DDGS是同步阻塞库，使用子进程方式调用以确保超时可控。
run_in_executor的线程无法被强制终止，会导致整个事件循环卡住。
"""
import concurrent.futures
from tools.base import BaseTool, ToolParameter


class WebSearchTool(BaseTool):
    name = "web_search"
    description = "搜索互联网获取景点实时信息，当知识库信息不足时使用"
    parameters = [
        ToolParameter(name="query", description="搜索查询文本"),
    ]

    # 独立线程池，避免占用主事件循环线程
    _executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="websearch")

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

            loop = asyncio.get_running_loop()

            # 用独立线程池提交，超时后放弃结果（线程会自行结束）
            future = loop.run_in_executor(
                self._executor,
                lambda: DDGS().text(input_str, region="cn-zh", max_results=5)
            )
            results = await asyncio.wait_for(future, timeout=12)

            if not results:
                return f"未搜索到与 '{input_str}' 相关的结果。"

            # 截断每条结果避免超长，最多3条
            parts = []
            for i, r in enumerate(results[:3], 1):
                title = r.get('title', '')[:50]
                body = r.get('body', '')[:100]
                if title or body:
                    parts.append(f"{i}. {title}\n   {body}")
            return "\n".join(parts) if parts else f"未搜索到与 '{input_str}' 相关的结果。"
        except asyncio.TimeoutError:
            return "搜索超时（12秒），请稍后重试。可尝试简化搜索词。"
        except Exception as e:
            err = str(e)[:100]
            return f"搜索出错：{err}"
