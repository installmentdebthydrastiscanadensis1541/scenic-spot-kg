"""图谱查询工具 — Neo4j知识图谱查询

支持：
- 自然语言转Cypher（由LLM完成）
- 直接执行Cypher查询
- 开发模式：内存模拟图谱（使用全国景点数据）
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
        """内存模拟查询（使用全国景点数据）"""
        results = []

        # 查找包含查询实体的关系
        for rel in RELATIONS:
            if query in rel["src"] or query in rel["tgt"] or query in rel["rel"]:
                results.append(f"{rel['src']} -[{rel['rel']}]-> {rel['tgt']}")

        # 查找景点属性
        for spot in SCENIC_SPOTS:
            if query in spot["name"] or query in spot.get("city", "") or query in spot.get("category", ""):
                info = f"景点: {spot['name']} | 城市: {spot['city']} | 类别: {spot['category']}"
                if spot.get("level"):
                    info += f" | 等级: {spot['level']}"
                if spot.get("dynasty"):
                    info += f" | 朝代: {spot['dynasty']}"
                if spot.get("duration"):
                    info += f" | 游览时长: {spot['duration']}"
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
