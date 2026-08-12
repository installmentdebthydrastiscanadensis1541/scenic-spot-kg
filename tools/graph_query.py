"""图谱查询工具 — Neo4j知识图谱查询

支持：
- 自然语言转Cypher（由LLM完成）
- 直接执行Cypher查询
- 开发模式：内存模拟图谱（使用全国景点数据）

【当前状态】
- Neo4j连接可选：连接失败时自动降级为内存模拟模式，使用scenic_data.py中的RELATIONS数据
- 内存模式支持：同城景点、建于朝代、所属类别、等级等关系查询
- LLM转Cypher功能已实现，但在内存模式下仅做关键词匹配

【未来改进方向】
- 运行build_kg.py生成完整NER抽取结果后导入Neo4j，提升图谱覆盖度
- 支持多跳推理（如"和故宫同朝代建设的景点"需要2跳查询）
- 增加图谱可视化接口，前端展示实体关系网络图
- 集成图嵌入算法（如TransE），支持基于向量的关系推理
"""
from tools.base import BaseTool, ToolParameter
from config.settings import settings
from data.scenic_data import SCENIC_SPOTS, CITIES, DYNASTIES, RELATIONS


class GraphQueryTool(BaseTool):
    name = "graph_query"
    description = "查询知识图谱关系，输入实体名称或关系查询"
    parameters = [
        ToolParameter(name="query", description="实体名称、关系查询或Cypher语句"),
    ]

    def __init__(self, use_neo4j: bool = True):
        self.use_neo4j = False
        self.driver = None
        if use_neo4j:
            try:
                self._connect_neo4j()
                self.use_neo4j = True
            except Exception as e:
                print(f"[GraphQuery] Neo4j连接失败({e})，使用内存模拟模式")

    def _connect_neo4j(self):
        """连接Neo4j数据库"""
        from neo4j import GraphDatabase
        self.driver = GraphDatabase.driver(
            settings.NEO4J_URI,
            auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
        )

    async def run(self, input_str: str) -> str:
        """执行图谱查询（Neo4j模式用线程池避免阻塞事件循环）"""
        if self.use_neo4j and self.driver:
            import asyncio
            try:
                return await asyncio.wait_for(
                    asyncio.get_running_loop().run_in_executor(None, self._query_neo4j, input_str),
                    timeout=15
                )
            except asyncio.TimeoutError:
                return "图谱查询超时，请稍后重试。"
        return self._query_mock(input_str)

    def _query_mock(self, query: str) -> str:
        """内存模拟查询（使用全国景点数据，支持tags/适合人群/强度/核心特色）"""
        results = []

        # 查找包含查询实体的关系
        for rel in RELATIONS:
            if query in rel["src"] or query in rel["tgt"] or query in rel["rel"]:
                results.append(f"{rel['src']} -[{rel['rel']}]-> {rel['tgt']}")

        # 查找景点属性（增强：包含tags/适合人群/强度/核心特色）
        for spot in SCENIC_SPOTS:
            matched = False
            # 名称/城市/类别匹配
            if query in spot["name"] or query in spot.get("city", "") or query in spot.get("category", ""):
                matched = True
            # tags匹配
            if not matched and spot.get("tags"):
                for tag in spot["tags"]:
                    if query in tag or tag in query:
                        matched = True
                        break
            # suitable_for匹配
            if not matched and spot.get("suitable_for"):
                for sf in spot["suitable_for"]:
                    if query in sf or sf in query:
                        matched = True
                        break
            # core_feature匹配
            if not matched and spot.get("core_feature") and query in spot["core_feature"]:
                matched = True
            # intensity匹配
            if not matched and spot.get("intensity") and query in spot["intensity"]:
                matched = True
            # level匹配
            if not matched and spot.get("level") and query in spot["level"]:
                matched = True
            # dynasty匹配
            if not matched and spot.get("dynasty") and query in spot.get("dynasty", ""):
                matched = True

            if matched:
                info = f"景点: {spot['name']} | 城市: {spot['city']} | 类别: {spot['category']}"
                if spot.get("level"):
                    info += f" | 等级: {spot['level']}"
                if spot.get("dynasty"):
                    info += f" | 朝代: {spot['dynasty']}"
                if spot.get("duration"):
                    info += f" | 游览时长: {spot['duration']}"
                if spot.get("tags"):
                    info += f" | 标签: {', '.join(spot['tags'])}"
                if spot.get("suitable_for"):
                    info += f" | 适合: {', '.join(spot['suitable_for'])}"
                if spot.get("intensity"):
                    info += f" | 强度: {spot['intensity']}"
                if spot.get("core_feature"):
                    info += f" | 核心特色: {spot['core_feature']}"
                results.append(info)

        # 查找城市
        for city in CITIES:
            if query in city["name"]:
                spots = [s["name"] for s in SCENIC_SPOTS if s["city"] == city["name"]]
                results.append(f"城市: {city['name']} | {city['desc']} | 景点: {', '.join(spots)}")

        # 查找朝代
        for dynasty in DYNASTIES:
            if query in dynasty["name"]:
                spots = [s["name"] for s in SCENIC_SPOTS if s.get("dynasty") == dynasty["name"]]
                results.append(f"朝代: {dynasty['name']} ({dynasty['period']}) | {dynasty['desc']} | 相关景点: {', '.join(spots)}")

        return "\n".join(results) if results else f"未找到与 '{query}' 相关的图谱数据。"

    def _query_neo4j(self, query: str) -> str:
        """真实Neo4j查询"""
        if query.strip().upper().startswith(("MATCH", "RETURN", "WHERE")):
            cypher = query
        else:
            cypher = f"MATCH (n)-[r]->(m) WHERE n.name CONTAINS '{query}' OR m.name CONTAINS '{query}' RETURN n, r, m LIMIT 10"

        with self.driver.session() as session:
            result = session.run(cypher)
            records = [record.data() for record in result]

        return str(records) if records else "查询结果为空。"
