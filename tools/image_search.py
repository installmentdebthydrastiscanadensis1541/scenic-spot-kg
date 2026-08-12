"""景点图片搜索工具 — 从权威网站搜索景点相关图片

使用DuckDuckGo图片搜索，返回权威来源的景点图片链接。
支持搜索景点外观、内部、特定角度等图片。

【当前状态】
- 基于DuckDuckGo Images API搜索，返回图片URL和来源
- 优先返回权威来源（百度百科、维基百科、携程、马蜂窝等）
- 返回结果包含缩略图URL和来源页面URL

【未来改进方向】
- 集成专门的图片API（如Unsplash、百度图片API）获取更高质量图片
- 支持按图片类型筛选（外观/内部/航拍/夜景等）
- 缓存常用景点的图片搜索结果
- 支持图片相似度对比（找"像千与千寻"的景点图片）
"""
from tools.base import BaseTool, ToolParameter


class ImageSearchTool(BaseTool):
    name = "image_search"
    description = "搜索景点相关图片，输入景点名称和图片描述，返回权威来源的图片链接"
    parameters = [
        ToolParameter(name="query", description="景点名称+图片描述，如'广州塔内部照片'或'故宫太和殿'"),
    ]

    # 权威来源优先级（搜索结果中这些来源的图片优先展示）
    PREFERRED_SOURCES = [
        "baike.baidu.com",    # 百度百科
        "zh.wikipedia.org",   # 维基百科
        "mafengwo.cn",        # 马蜂窝
        "ctrip.com",          # 携程
        "dianping.com",       # 大众点评
        "you.ctrip.com",      # 携程游记
        "qunar.com",          # 去哪儿
    ]

    def __init__(self):
        self._available = False
        try:
            from ddgs import DDGS
            self._available = True
        except ImportError:
            print("[ImageSearch] ddgs未安装，图片搜索功能不可用")

    async def run(self, input_str: str) -> str:
        if not self._available:
            return "图片搜索功能不可用：ddgs未安装。请运行 pip install ddgs"

        try:
            import asyncio
            from ddgs import DDGS

            # 构造搜索词：追加"景点"确保相关性
            search_query = input_str.strip()
            if "照片" not in search_query and "图片" not in search_query and "图" not in search_query:
                search_query += " 景点照片"

            def _search():
                return DDGS().images(search_query, region="cn-zh", max_results=6)

            results = await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(None, _search),
                timeout=15
            )

            if not results:
                return f"未搜索到与 '{input_str}' 相关的图片。"

            # 格式化结果，优先展示权威来源
            parts = []
            preferred = []
            others = []

            for r in results:
                title = r.get("title", "")[:60]
                source = r.get("source", "") or r.get("hostname", "")
                image_url = r.get("image", "") or r.get("thumbnail", "")
                page_url = r.get("url", "") or r.get("source", "")
                width = r.get("width", "")
                height = r.get("height", "")

                if not image_url:
                    continue

                size_info = f" ({width}x{height})" if width and height else ""

                entry = {
                    "title": title,
                    "source": source,
                    "image_url": image_url,
                    "page_url": page_url,
                    "size_info": size_info,
                }

                # 判断是否来自权威来源
                is_preferred = any(ps in (source + page_url) for ps in self.PREFERRED_SOURCES)
                if is_preferred:
                    preferred.append(entry)
                else:
                    others.append(entry)

            # 优先展示权威来源，最多5张
            displayed = preferred[:3] + others[:2]
            displayed = displayed[:5]

            for i, entry in enumerate(displayed, 1):
                line = f"{i}. {entry['title']}"
                if entry['source']:
                    line += f" [来源: {entry['source']}]"
                line += f"{entry['size_info']}"
                line += f"\n   图片链接: {entry['image_url']}"
                if entry['page_url']:
                    line += f"\n   页面链接: {entry['page_url']}"
                parts.append(line)

            header = f"搜索 '{input_str}' 找到 {len(results)} 张图片，展示前 {len(displayed)} 张："
            return header + "\n\n" + "\n\n".join(parts)

        except asyncio.TimeoutError:
            return "图片搜索超时，请稍后重试。"
        except Exception as e:
            return f"图片搜索出错：{e}"
