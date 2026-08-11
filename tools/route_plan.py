"""路线规划工具 — 景点游览路线推荐

使用全国景点数据，支持同城多景点一日游行程规划。
"""
import re
from tools.base import BaseTool, ToolParameter
from data.scenic_data import SCENIC_SPOTS


class RoutePlanTool(BaseTool):
    name = "route_plan"
    description = "规划景点游览路线，输入景点名称或城市名"
    parameters = [
        ToolParameter(name="query", description="景点名称、城市名或路线请求"),
    ]

    # 预设景点内部路线
    SPOT_ROUTES = {
        "故宫": {
            "经典路线": "午门→太和殿→中和殿→保和殿→乾清宫→交泰殿→坤宁宫→御花园→神武门",
            "深度路线": "午门→文华殿→武英殿→太和门→太和殿→中和殿→保和殿→乾清宫→交泰殿→坤宁宫→东六宫→西六宫→御花园→神武门",
            "建议": "经典约2小时，深度约4小时",
        },
        "颐和园": {
            "经典路线": "东宫门→仁寿殿→德和园→玉澜堂→乐寿堂→长廊→万寿山→佛香阁→苏州街→北宫门",
            "建议": "约3小时",
        },
        "西湖": {
            "环湖路线": "断桥残雪→白堤→孤山→曲院风荷→苏堤→花港观鱼→雷峰塔→净慈寺→长桥公园→柳浪闻莺",
            "建议": "环湖步行约4小时，可乘船游览",
        },
        "白云山": {
            "经典路线": "南门→云台花园→鸣春谷→摩星岭→西门",
            "休闲路线": "南门→云台花园→明珠楼→桃花涧→西门",
            "建议": "经典约3小时，休闲约2.5小时，可乘索道",
        },
        "陈家祠": {
            "游览路线": "正门→前院→首进大厅→中进厅堂→后进正厅→侧院→出口",
            "建议": "约1.5小时，建议租讲解器",
        },
        "长隆旅游度假区": {
            "野生动物世界路线": "北门→乘车游览区→青龙馆→考拉馆→熊猫馆→白虎山→南门",
            "建议": "至少预留半天，乘小火车游览最佳",
        },
        "黄鹤楼": {
            "游览路线": "南门→诗碑廊→搁笔亭→崔颢题诗图→主楼→顶楼远眺→北门",
            "建议": "约1.5小时，建议傍晚登楼看长江日落",
        },
        "鼓浪屿": {
            "环岛路线": "三丘田码头→风琴博物馆→日光岩→菽庄花园→皓月园→内厝澳码头",
            "建议": "约4-5小时，穿舒适步行鞋",
        },
        "洱海": {
            "环湖路线": "大理古城→喜洲古镇→上关→双廊→挖色→小普陀→下关→大理古城",
            "建议": "环湖约130公里，建议电动车或包车一日游",
        },
        "苍山": {
            "推荐路线": "感通索道上山→清碧溪→玉带云游路→七龙女池→洗马潭索道下山",
            "建议": "约4小时，海拔高注意防寒",
        },
    }

    # 游览时段建议（按景点类别推荐上午/下午/晚上）
    TIME_PREFERENCES = {
        "宫殿": "morning",
        "园林": "morning",
        "寺庙": "morning",
        "陵墓": "morning",
        "山岳": "morning",
        "湖泊": "morning",
        "石窟": "morning",
        "城墙": "morning",
        "军事防御": "morning",
        "祭祀": "morning",
        "滨江景观": "evening",
        "海滩": "afternoon",
        "吊脚楼群": "evening",
        "古城": "evening",
        "体育场馆": "afternoon",
        "电视塔": "evening",
        "文化旅游": "morning",
        "动物园": "morning",
        "温泉宫殿": "afternoon",
        "祠堂": "morning",
        "水利工程": "morning",
        "石刻": "morning",
        "广场": "morning",
        "园林遗址": "morning",
        "古建筑群": "afternoon",
        "佛塔": "morning",
        "雪山": "morning",
        # 广州景点类别
        "公园": "morning",
        "历史街区": "afternoon",
        "主题乐园": "morning",
        "纪念建筑": "morning",
        # 新增城市景点类别
        "楼阁": "morning",
        "历史建筑": "morning",
        "江心洲": "afternoon",
        "博物馆": "morning",
        "岛屿": "morning",
        "喀斯特地貌": "morning",
        "文化创意": "afternoon",
        "古街": "afternoon",
        "教堂": "morning",
        "冰雪景观": "morning",
        "水系景观": "evening",
        "梯田": "morning",
        "近现代建筑": "morning",
        "古镇": "afternoon",
    }

    async def run(self, input_str: str) -> str:
        """规划游览路线"""
        # 1. 检查是否有景点内部路线
        for spot_name, routes in self.SPOT_ROUTES.items():
            if spot_name in input_str:
                parts = []
                for key, value in routes.items():
                    parts.append(f"{key}: {value}")
                return "\n".join(parts)

        # 2. 检查是否是城市级别路线（同城多景点一日游）
        city = self._detect_city(input_str)
        if city:
            city_spots = [s for s in SCENIC_SPOTS if s["city"] == city]
            if len(city_spots) >= 2:
                return self._plan_city_tour(city, city_spots)

        # 3. 检查是否是特定景点
        for spot in SCENIC_SPOTS:
            if spot["name"] in input_str:
                return self._plan_single_spot(spot)

        return f"暂无 '{input_str}' 的路线数据，可尝试输入城市名获取同城路线推荐。"

    def _detect_city(self, text: str) -> str | None:
        """从输入文本中检测城市名"""
        # 直接匹配数据中的城市
        cities = set(s["city"] for s in SCENIC_SPOTS)
        for city in cities:
            if city in text:
                return city
        # 常见别名
        aliases = {"羊城": "广州", "花城": "广州", "穗": "广州",
                   "蓉城": "成都", "锦城": "成都",
                   "春城": "昆明",
                   "山城": "重庆",
                   "鹏城": "深圳",
                   "星城": "长沙", "楚汉": "长沙",
                   "江城": "武汉",
                   "冰城": "哈尔滨",
                   "鹭岛": "厦门",
                   "津门": "天津",
                   }
        for alias, city in aliases.items():
            if alias in text:
                return city
        return None

    def _parse_duration_hours(self, duration_str: str) -> float:
        """将时长字符串解析为小时数"""
        if not duration_str:
            return 2.0
        duration_str = duration_str.strip()
        if "全天" in duration_str:
            return 6.0
        hours = 0.0
        m = re.search(r'(\d+)-?(\d+)?\s*小时', duration_str)
        if m:
            h1 = int(m.group(1))
            h2 = int(m.group(2)) if m.group(2) else None
            hours = (h1 + h2) / 2 if h2 else h1
        else:
            m = re.search(r'(\d+(?:\.\d+)?)\s*小时', duration_str)
            if m:
                hours = float(m.group(1))
        return hours if hours > 0 else 2.0

    def _plan_city_tour(self, city: str, spots: list) -> str:
        """规划城市一日游行程（带时间轴）"""
        # 按时段偏好排序：上午景点排前面，晚上景点排后面
        def sort_key(s):
            pref = self.TIME_PREFERENCES.get(s.get("category", ""), "morning")
            order = {"morning": 0, "afternoon": 1, "evening": 2}
            return order.get(pref, 0)

        sorted_spots = sorted(spots, key=sort_key)

        # 选取一天能逛完的景点（总时长控制在8小时内）
        selected = []
        total_hours = 0.0
        for spot in sorted_spots:
            dur = self._parse_duration_hours(spot.get("duration", ""))
            if total_hours + dur <= 8.0:
                selected.append((spot, dur))
                total_hours += dur
            if total_hours >= 7.0:
                break

        if not selected:
            return f"暂无 {city} 的路线数据。"

        # 生成时间轴
        current_hour = 9  # 早上9点出发
        schedule_lines = []
        schedule_lines.append(f"【{city}一日游推荐行程】")
        schedule_lines.append(f"游览景点：{' → '.join(s['name'] for s, _ in selected)}")
        schedule_lines.append(f"预计总时长：约{total_hours:.0f}小时\n")

        for i, (spot, dur) in enumerate(selected):
            end_hour = current_hour + dur
            time_range = f"{int(current_hour):02d}:{int((current_hour % 1) * 60):02d} - {int(end_hour):02d}:{int((end_hour % 1) * 60):02d}"

            schedule_lines.append(f"📍 {time_range}  {spot['name']}")
            schedule_lines.append(f"   看点：{spot.get('highlights', '—')}")
            schedule_lines.append(f"   建议游览：{spot.get('duration', '约2小时')}")

            # 景点间交通提示
            if i < len(selected) - 1:
                next_spot = selected[i + 1][0]
                transport = self._suggest_transport(spot, next_spot)
                commute_min = self._estimate_commute(spot, next_spot)
                schedule_lines.append(f"   ↓ 步行/乘车约{commute_min}分钟前往{next_spot['name']}（{transport}）\n")
                current_hour = end_hour + commute_min / 60
            else:
                schedule_lines.append("")
                current_hour = end_hour

        # 添加总结
        schedule_lines.append(f"📝 行程小结：")
        schedule_lines.append(f"   共{len(selected)}个景点，游览约{total_hours:.0f}小时")
        if current_hour > 17:
            schedule_lines.append(f"   预计{int(current_hour):02d}点左右结束，可安排晚餐")
        else:
            schedule_lines.append(f"   预计{int(current_hour):02d}点左右结束，还有时间可自由安排")

        return "\n".join(schedule_lines)

    def _suggest_transport(self, spot_a: dict, spot_b: dict) -> str:
        """根据两个景点信息建议交通方式"""
        cat_a = spot_a.get("category", "")
        cat_b = spot_b.get("category", "")

        # 同类/同城核心区景点通常距离较近
        close_categories = {"宫殿", "园林", "广场", "古建筑群", "祠堂", "寺庙",
                            "滨江景观", "吊脚楼群", "古城"}
        if cat_a in close_categories and cat_b in close_categories:
            return "建议步行或地铁"

        # 需要较长距离移动的
        far_categories = {"军事防御", "山岳", "雪山", "海滩", "水利工程"}
        if cat_a in far_categories or cat_b in far_categories:
            return "建议打车或公交"

        return "建议地铁或公交"

    def _estimate_commute(self, spot_a: dict, spot_b: dict) -> int:
        """估算两个景点间的通勤时间（分钟）"""
        cat_a = spot_a.get("category", "")
        cat_b = spot_b.get("category", "")

        # 同核心区景点
        close_categories = {"宫殿", "园林", "广场", "古建筑群", "祠堂", "寺庙",
                            "滨江景观", "吊脚楼群", "古城"}
        if cat_a in close_categories and cat_b in close_categories:
            return 20

        # 需远距离的
        far_categories = {"军事防御", "山岳", "雪山", "海滩", "水利工程"}
        if cat_a in far_categories or cat_b in far_categories:
            return 60

        return 35

    def _plan_single_spot(self, spot: dict) -> str:
        """单个景点的游览规划"""
        lines = [
            f"【{spot['name']}游览建议】",
            f"📍 {spot['name']} — {spot.get('desc', '')}",
            f"",
            f"📌 游览时长：{spot.get('duration', '约2小时')}",
            f"📌 主要看点：{spot.get('highlights', '—')}",
            f"📌 景区等级：{spot.get('level', '—')}",
        ]

        # 检查是否有预设内部路线
        if spot["name"] in self.SPOT_ROUTES:
            lines.append("")
            lines.append("🗺️ 内部路线：")
            for key, value in self.SPOT_ROUTES[spot["name"]].items():
                lines.append(f"   {key}: {value}")

        # 同城其他景点推荐
        city = spot.get("city", "")
        other_spots = [s for s in SCENIC_SPOTS if s["city"] == city and s["name"] != spot["name"]]
        if other_spots:
            lines.append("")
            lines.append(f"🗺️ 同城其他推荐：")
            for s in other_spots[:3]:
                lines.append(f"   {s['name']}（{s.get('duration', '约2小时')}）— {s.get('category', '')}")

        return "\n".join(lines)
