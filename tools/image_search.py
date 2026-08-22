"""景点图片搜索工具 — 从百度图片搜索景点相关图片

使用百度图片搜索接口，返回景点图片链接，标注来源和图片说明。

【当前状态】
- 基于百度图片搜索接口（image.baidu.com），国内可用
- 自动识别图片类型（夜景/外观/内部/航拍等）并生成说明
- 提取来源网站域名，标注权威来源
- 返回结果包含图片URL、来源说明、页面链接

【未来改进方向】
- 缓存常用景点的图片搜索结果
- 集成其他图片源（必应图片、搜狗图片）作为备选
"""
import asyncio
import re
import urllib.parse
from urllib.parse import urlparse
import concurrent.futures
from tools.base import BaseTool, ToolParameter


class ImageSearchTool(BaseTool):
    name = "image_search"
    description = "搜索景点相关图片，输入景点名称和图片描述，返回带来源说明的图片链接"
    parameters = [
        ToolParameter(name="query", description="景点名称+图片描述，如'广州塔内部照片'或'故宫太和殿'"),
    ]

    # 独立线程池
    _executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="imgsearch")

    # 权威景点网站白名单（优先展示这些来源的图片）
    _AUTHORITATIVE_DOMAINS = {
        "gov.cn": "政府官网",
        "edu.cn": "教育机构",
        "wikipedia.org": "维基百科",
        "baike.baidu.com": "百度百科",
        "mafengwo.cn": "马蜂窝",
        "ctrip.com": "携程",
        "qunar.com": "去哪儿",
        "tuniu.com": "途牛",
        "dianping.com": "大众点评",
        "youimg1.c-ctrip.com": "携程图库",
        "bhipics.cheaperonline.com": "图片库",
    }

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

            # 格式化结果：标注来源权威性+图片说明
            parts = []
            for i, r in enumerate(results[:5], 1):
                title = r.get("title", "")[:60]
                image_url = r.get("image_url", "")
                page_url = r.get("page_url", "")
                source_tag = r.get("source_tag", "")

                if not image_url:
                    continue

                # 生成图片说明（基于标题和搜索词）
                desc = self._generate_description(title, input_str)

                line = f"{i}. {desc}"
                line += f"\n   图片链接: {image_url}"
                if page_url:
                    line += f"\n   页面链接: {page_url}"
                if source_tag:
                    line += f"\n   来源: {source_tag}"
                parts.append(line)

            if not parts:
                return f"未搜索到与 '{input_str}' 相关的图片。"

            header = f"搜索 '{input_str}' 找到 {len(results)} 张图片，展示前 {len(parts)} 张："
            return header + "\n\n" + "\n\n".join(parts)

        except asyncio.TimeoutError:
            return "图片搜索超时，请稍后重试。"
        except Exception as e:
            return f"图片搜索出错：{e}"

    def _generate_description(self, title: str, query: str) -> str:
        """基于图片标题和搜索词生成简短说明"""
        # 如果原标题有意义，直接用
        if title and len(title) >= 4 and not title.isdigit():
            return title
        # 否则基于搜索词生成
        query_lower = query.lower()
        if "夜景" in query or "夜" in title:
            return f"{query.split()[0]}夜景照片"
        if "内部" in query or "内景" in title:
            return f"{query.split()[0]}内部景观"
        if "航拍" in query or "俯瞰" in title:
            return f"{query.split()[0]}航拍视角"
        if "全景" in query or "全貌" in title:
            return f"{query.split()[0]}全景"
        return f"{query.split()[0]}相关图片"

    def _get_source_tag(self, url: str) -> str:
        """根据URL域名判断来源权威性"""
        if not url or not url.startswith("http"):
            return ""
        try:
            domain = urlparse(url).netloc.lower()
            # 去掉www前缀
            if domain.startswith("www."):
                domain = domain[4:]

            # 检查是否权威来源
            for auth_domain, auth_name in self._AUTHORITATIVE_DOMAINS.items():
                if auth_domain in domain:
                    return f"权威来源({auth_name})"

            # 其他来源只显示域名（不加"来源:"前缀，外层已加）
            return domain
        except Exception:
            return ""

    def _search_baidu_images(self, query: str) -> list[dict]:
        """通过百度图片搜索接口获取图片"""
        import httpx

        encoded_query = urllib.parse.quote(query)
        url = f"https://image.baidu.com/search/acjson?tn=resultjson_com&word={encoded_query}&pn=0&rn=10&ie=utf-8"

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

        results = []
        items = data.get("data", [])
        for item in items:
            if not isinstance(item, dict):
                continue

            image_url = item.get("thumbURL") or item.get("middleURL") or item.get("objURL")
            if not image_url:
                continue
            if not image_url.startswith("http"):
                continue

            title = item.get("fromPageTitle") or item.get("title") or ""
            title = re.sub(r"<[^>]+>", "", title).strip()

            # fromURL常为百度加密格式(如ippr_z2C$q...)，不是有效URL，需过滤
            raw_page_url = item.get("fromURL") or ""
            page_url = raw_page_url if raw_page_url.startswith(("http://", "https://")) else ""

            # 标注来源权威性
            source_tag = self._get_source_tag(page_url) or self._get_source_tag(image_url)

            results.append({
                "title": title,
                "image_url": image_url,
                "page_url": page_url,
                "source_tag": source_tag,
            })

            if len(results) >= 10:
                break

        # 优先排序：权威来源排前面
        results.sort(key=lambda x: 0 if "权威来源" in x.get("source_tag", "") else 1)

        return results
