"""景点图片搜索工具 — 从百度图片搜索景点相关图片

使用百度图片搜索接口，返回景点图片链接。
国内服务器可正常访问，不依赖DuckDuckGo。

【当前状态】
- 基于百度图片搜索接口（image.baidu.com），国内可用
- 返回结果包含图片URL和缩略图URL
- 支持搜索景点外观、内部、特定角度等图片

【未来改进方向】
- 支持按图片类型筛选（外观/内部/航拍/夜景等）
- 缓存常用景点的图片搜索结果
- 集成其他图片源（必应图片、搜狗图片）作为备选
"""
import asyncio
import re
import urllib.parse
import concurrent.futures
from tools.base import BaseTool, ToolParameter


class ImageSearchTool(BaseTool):
    name = "image_search"
    description = "搜索景点相关图片，输入景点名称和图片描述，返回图片链接"
    parameters = [
        ToolParameter(name="query", description="景点名称+图片描述，如'广州塔内部照片'或'故宫太和殿'"),
    ]

    # 独立线程池
    _executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="imgsearch")

    def __init__(self):
        self._available = False
        try:
            import httpx
            self._available = True
        except ImportError:
            print("[ImageSearch] httpx未安装，图片搜索功能不可用")

    async def run(self, input_str: str) -> str:
        if not self._available:
            return "图片搜索功能不可用：httpx未安装。请运行 pip install httpx"

        try:
            # 构造搜索词：追加"景点"确保相关性
            search_query = input_str.strip()
            if "照片" not in search_query and "图片" not in search_query and "图" not in search_query:
                search_query += " 景点照片"

            loop = asyncio.get_running_loop()
            future = loop.run_in_executor(
                self._executor,
                lambda: self._search_baidu_images(search_query)
            )
            results = await asyncio.wait_for(future, timeout=15)

            if not results:
                return f"未搜索到与 '{input_str}' 相关的图片。"

            # 格式化结果
            parts = []
            for i, r in enumerate(results[:5], 1):
                title = r.get("title", "")[:60]
                image_url = r.get("image_url", "")
                page_url = r.get("page_url", "")

                if not image_url:
                    continue

                line = f"{i}. {title}"
                line += f"\n   图片链接: {image_url}"
                if page_url:
                    line += f"\n   页面链接: {page_url}"
                parts.append(line)

            if not parts:
                return f"未搜索到与 '{input_str}' 相关的图片。"

            header = f"搜索 '{input_str}' 找到 {len(results)} 张图片，展示前 {len(parts)} 张："
            return header + "\n\n" + "\n\n".join(parts)

        except asyncio.TimeoutError:
            return "图片搜索超时，请稍后重试。"
        except Exception as e:
            return f"图片搜索出错：{e}"

    def _search_baidu_images(self, query: str) -> list[dict]:
        """通过百度图片搜索接口获取图片

        百度图片 acjson 接口返回JSON格式结果，国内可正常访问。
        """
        import httpx

        # 百度图片搜索接口
        encoded_query = urllib.parse.quote(query)
        url = f"https://image.baidu.com/search/acjson?tn=resultjson_com&word={encoded_query}&pn=0&rn=8&ie=utf-8"

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": "https://image.baidu.com/",
        }

        try:
            with httpx.Client(timeout=12, follow_redirects=True) as client:
                resp = client.get(url, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            print(f"[ImageSearch] 百度图片请求失败: {e}")
            return []

        # 解析百度图片返回的JSON
        results = []
        items = data.get("data", [])
        for item in items:
            if not isinstance(item, dict):
                continue

            # 百度图片返回字段：thumbURL（缩略图）、middleURL、hoverURL、objURL（原图）
            # 优先使用 thumbURL（稳定可访问），备选 middleURL
            image_url = item.get("thumbURL") or item.get("middleURL") or item.get("objURL")
            if not image_url:
                continue

            # 跳过非http链接（如 data:URI）
            if not image_url.startswith("http"):
                continue

            title = item.get("fromPageTitle") or item.get("title") or ""
            # 清理标题中的HTML标签
            title = re.sub(r"<[^>]+>", "", title).strip()

            page_url = item.get("fromURL") or item.get("flip_url") or ""

            results.append({
                "title": title,
                "image_url": image_url,
                "page_url": page_url,
            })

            if len(results) >= 8:
                break

        return results
