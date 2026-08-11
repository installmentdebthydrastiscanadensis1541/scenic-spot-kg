"""网页抓取工具 — 获取网页文本内容

当web_search返回链接后，可用此工具读取页面详细内容。
支持酒店/门票/美食等信息的抓取提取。
"""
import re
from tools.base import BaseTool, ToolParameter


class WebFetchTool(BaseTool):
    name = "web_fetch"
    description = "抓取网页内容，获取酒店、门票、美食等详细信息，输入URL网址"
    parameters = [
        ToolParameter(name="url", description="要抓取的网页URL地址"),
    ]

    def __init__(self):
        self._available = False
        try:
            import httpx
            import bs4
            self._available = True
        except ImportError:
            print("[WebFetch] httpx或beautifulsoup4未安装，网页抓取不可用")

    async def run(self, input_str: str) -> str:
        if not self._available:
            return "网页抓取不可用：请运行 pip install httpx beautifulsoup4"

        url = input_str.strip()
        if not url.startswith("http"):
            return "请输入有效的URL地址，以http://或https://开头"

        try:
            return await self._fetch_and_extract(url)
        except Exception as e:
            return f"网页抓取失败：{e}"

    async def _fetch_and_extract(self, url: str) -> str:
        """抓取网页并提取正文文本"""
        import httpx
        from bs4 import BeautifulSoup

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }

        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
        except httpx.TimeoutException:
            return f"网页抓取超时：{url} 响应时间过长"
        except httpx.HTTPStatusError as e:
            return f"网页返回错误：HTTP {e.response.status_code}"
        except httpx.ConnectError:
            return f"无法连接到 {url}，请检查网址是否正确"
        except httpx.TooManyRedirects:
            return f"网页重定向过多：{url}"

        # 自动检测编码
        content_type = resp.headers.get("content-type", "")
        if "charset" not in content_type.lower():
            resp.encoding = resp.charset_encoding or "utf-8"

        html = resp.text

        # 解析HTML
        soup = BeautifulSoup(html, "html.parser")

        # 移除无关标签
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "iframe"]):
            tag.decompose()

        # 提取标题
        title = ""
        if soup.title and soup.title.string:
            title = soup.title.string.strip()

        # 尝试提取主要内容区域
        main_content = None
        for selector in ["article", "main", ".content", ".article", ".detail",
                         "#content", "#article", ".post-content", ".entry-content"]:
            main_content = soup.select_one(selector)
            if main_content:
                break

        if not main_content:
            main_content = soup.body or soup

        # 提取文本
        text = main_content.get_text(separator="\n", strip=True)

        # 清理：去除过多空行和空白
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        text = "\n".join(lines)

        # 截断过长内容（保留前3000字符，避免token爆炸）
        if len(text) > 3000:
            text = text[:3000] + "\n...（内容过长已截断）"

        # 组装结果
        result = f"来源：{url}"
        if title:
            result += f"\n标题：{title}"
        result += f"\n\n{text}"

        return result
