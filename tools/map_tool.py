"""地图工具 — 高德地图API集成

功能：
- 景点搜索（POI搜索）
- 路线规划（步行/驾车/公交）
- 地理编码（地址→坐标）

高德地图API免费额度：日调用量5000次，无需付费。
申请地址：https://lbs.amap.com/

【当前状态】
- API接口已完整实现（POI搜索、驾车/步行/公交路线规划、地理编码）
- 需要配置 AMAP_API_KEY 才能使用，未配置时返回提示信息
- 未配置时路线规划依赖 route_plan 工具的预设数据

【未来改进方向】
- 集成实时路况信息，提供更精准的出行时间预估
- 增加周边搜索（餐饮/住宿/停车场），辅助游览规划
- 支持多点路线优化（TSP问题），为多景点一日游提供最优顺序
- 加入景区内部导航（部分景区有室内地图API）
- 缓存常用POI和路线数据，减少API调用次数
"""
import httpx
from tools.base import BaseTool, ToolParameter
from config.settings import settings


class MapTool(BaseTool):
    name = "map_tool"
    description = "查询景点地理位置、搜索附近景点、规划出行路线"
    parameters = [
        ToolParameter(name="query", description="查询内容，如景点名称或路线规划请求"),
    ]

    def __init__(self):
        self.api_key = getattr(settings, "AMAP_API_KEY", "")
        self._available = bool(self.api_key)

    async def run(self, input_str: str) -> str:
        if not self._available:
            return "地图功能不可用：未配置高德API Key。请在 .env 中设置 AMAP_API_KEY"

        # 判断查询类型
        if any(kw in input_str for kw in ["路线", "怎么去", "从", "到", "导航"]):
            return await self._route_plan(input_str)
        else:
            return await self._search_poi(input_str)

    async def _search_poi(self, keyword: str) -> str:
        """POI搜索景点"""
        url = "https://restapi.amap.com/v3/place/text"
        params = {
            "key": self.api_key,
            "keywords": keyword,
            "types": "110000",  # 风景名胜
            "offset": 5,
            "output": "json",
        }

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, params=params)
            data = resp.json()

        if data.get("status") != "1" or not data.get("pois"):
            return f"未找到景点 '{keyword}' 的地理信息。"

        results = []
        for poi in data["pois"][:5]:
            name = poi.get("name", "")
            address = poi.get("address", "") or poi.get("pname", "")
            location = poi.get("location", "")
            tel = poi.get("tel", "")
            rating = poi.get("biz_ext", {}).get("rating", "")

            info = f"- {name}"
            if address:
                info += f" | 地址: {address}"
            if rating:
                info += f" | 评分: {rating}"
            if tel:
                info += f" | 电话: {tel}"
            if location:
                lng, lat = location.split(",")
                info += f" | 坐标: ({lat}, {lng})"
            results.append(info)

        return "\n".join(results)

    async def _route_plan(self, query: str) -> str:
        """路线规划 — 提取起终点后同时调用步行/驾车/公交API"""
        import re
        match = re.search(r"从(.+?)[到去](.+?)(?:的|路线|怎么走|怎么去)?$", query)
        if not match:
            return "路线规划请使用格式：从XX到XX，例如'从凤凰新村到白云山风景区'"

        origin_name = match.group(1).strip()
        dest_name = match.group(2).strip()

        # 先查坐标
        origin_loc = await self._geocode(origin_name)
        dest_loc = await self._geocode(dest_name)

        if not origin_loc:
            return f"无法获取 '{origin_name}' 的位置信息。"
        if not dest_loc:
            return f"无法获取 '{dest_name}' 的位置信息。"

        # 从起终点推断城市（公交路线需要城市参数）
        city = self._guess_city(query) or self._guess_city(origin_name) or self._guess_city(dest_name)

        # 并发调用三种路线API
        import asyncio
        walking_task = self._route_walking(origin_loc, dest_loc)
        driving_task = self._route_driving(origin_loc, dest_loc)
        transit_task = self._route_transit(origin_loc, dest_loc, city)

        walking_result, driving_result, transit_result = await asyncio.gather(
            walking_task, driving_task, transit_task
        )

        # 拼接所有方案
        parts = [f"【{origin_name} → {dest_name} 路线规划】\n"]

        if driving_result:
            parts.append(f"🚗 方案一：驾车\n{driving_result}")
        if transit_result:
            parts.append(f"🚌 方案二：公共交通\n{transit_result}")
        if walking_result:
            parts.append(f"🚶 方案三：步行\n{walking_result}")

        if len(parts) == 1:
            return "路线规划失败，未找到可行路线。"

        return "\n\n".join(parts)

    async def _route_driving(self, origin: str, destination: str) -> str | None:
        """驾车路线规划"""
        url = "https://restapi.amap.com/v3/direction/driving"
        params = {
            "key": self.api_key,
            "origin": origin,
            "destination": destination,
            "output": "json",
        }

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url, params=params)
                data = resp.json()

            if data.get("status") != "1" or not data.get("route"):
                return None

            route = data["route"]
            paths = route.get("paths", [])
            if not paths:
                return None

            path = paths[0]
            distance = int(path.get("distance", 0))
            duration = int(path.get("duration", 0))
            taxi_cost = route.get("taxi_cost", "")

            result = f"距离：{distance/1000:.1f}公里 | 预计时间：{self._format_duration(duration)}"
            if taxi_cost:
                result += f" | 预估打车费：¥{float(taxi_cost):.0f}"

            # 关键步骤
            steps = []
            for step in path.get("steps", [])[:6]:
                instruction = step.get("instruction", "")
                if instruction:
                    steps.append(instruction)
            if steps:
                result += "\n途经：" + " → ".join(steps)

            return result
        except Exception:
            return None

    async def _route_walking(self, origin: str, destination: str) -> str | None:
        """步行路线规划"""
        url = "https://restapi.amap.com/v3/direction/walking"
        params = {
            "key": self.api_key,
            "origin": origin,
            "destination": destination,
            "output": "json",
        }

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url, params=params)
                data = resp.json()

            if data.get("status") != "1" or not data.get("route"):
                return None

            route = data["route"]
            paths = route.get("paths", [])
            if not paths:
                return None

            path = paths[0]
            distance = int(path.get("distance", 0))
            duration = int(path.get("duration", 0))

            # 步行超过10km不太现实，给出提示
            result = f"距离：{distance/1000:.1f}公里 | 预计时间：{self._format_duration(duration)}"
            if distance > 10000:
                result += "（距离较远，建议选择其他交通方式）"

            steps = []
            for step in path.get("steps", [])[:6]:
                instruction = step.get("instruction", "")
                if instruction:
                    steps.append(instruction)
            if steps:
                result += "\n途经：" + " → ".join(steps)

            return result
        except Exception:
            return None

    async def _route_transit(self, origin: str, destination: str, city: str) -> str | None:
        """公交/地铁路线规划"""
        if not city:
            city = "广州"  # 兜底默认

        url = "https://restapi.amap.com/v3/direction/transit/integrated"
        params = {
            "key": self.api_key,
            "origin": origin,
            "destination": destination,
            "city": city,
            "output": "json",
        }

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url, params=params)
                data = resp.json()

            if data.get("status") != "1" or not data.get("route"):
                return None

            route = data["route"]
            transits = route.get("transits", [])
            if not transits:
                return None

            # 取前2条公交方案
            results = []
            for i, transit in enumerate(transits[:2]):
                distance = int(transit.get("distance", 0))
                duration = int(transit.get("duration", 0))
                walking_dist = int(transit.get("walking_distance", 0))

                desc = f"方案{i+1}：{self._format_duration(duration)}，步行{walking_dist}米，总距离{distance/1000:.1f}公里"

                # 提取关键换乘信息
                segments = []
                for segment in transit.get("segments", []):
                    bus_info = segment.get("bus", {})
                    if bus_info:
                        bus_lines = bus_info.get("buslines", [])
                        if bus_lines:
                            line = bus_lines[0]
                            name = line.get("name", "")
                            departure = line.get("departure_stop", {}).get("name", "")
                            arrival = line.get("arrival_stop", {}).get("name", "")
                            via_num = line.get("via_num", "0")
                            segments.append(f"乘{name}（{departure}→{arrival}，{via_num}站）")

                if segments:
                    desc += "\n换乘：" + " → ".join(segments)

                results.append(desc)

            return "\n".join(results)
        except Exception:
            return None

    def _guess_city(self, text: str) -> str | None:
        """从文本中猜测城市名，找不到返回None"""
        # 先检查数据中的城市
        from data.scenic_data import SCENIC_SPOTS
        for spot in SCENIC_SPOTS:
            if spot["city"] in text:
                return spot["city"]

        # 常见城市关键词
        city_keywords = ["北京", "上海", "广州", "深圳", "杭州", "南京",
                         "成都", "重庆", "武汉", "西安", "苏州", "长沙",
                         "天津", "厦门", "青岛", "大连", "昆明", "哈尔滨",
                         "大理", "丽江", "三亚", "桂林", "敦煌", "拉萨"]
        for city in city_keywords:
            if city in text:
                return city

        return None

    @staticmethod
    def _format_duration(seconds: int) -> str:
        """将秒数格式化为可读时间"""
        if seconds < 60:
            return f"{seconds}秒"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes}分钟"
        hours = minutes // 60
        remain_min = minutes % 60
        if remain_min == 0:
            return f"{hours}小时"
        return f"{hours}小时{remain_min}分钟"

    async def _geocode(self, address: str) -> str | None:
        """地理编码：地址→坐标（经度,纬度）"""
        url = "https://restapi.amap.com/v3/geocode/geo"
        params = {
            "key": self.api_key,
            "address": address,
            "output": "json",
        }

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, params=params)
            data = resp.json()

        if data.get("status") == "1" and data.get("geocodes"):
            return data["geocodes"][0].get("location")
        return None
