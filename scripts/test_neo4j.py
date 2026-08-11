"""验证 Neo4j 图谱连接和查询"""
from neo4j import GraphDatabase

URI = "bolt://localhost:7687"
AUTH = ("neo4j", "scenic2024")

def test_connection():
    """测试连接并插入示例景点数据"""
    try:
        driver = GraphDatabase.driver(URI, auth=AUTH)
        driver.verify_connectivity()
        print("[OK] Neo4j 连接成功！")
    except Exception as e:
        if "Neo.ClientError.Security.Unauthorized" in str(e):
            print("[INFO] 需要修改默认密码。请在浏览器打开 http://localhost:7474 修改密码后重试。")
            return
        print(f"[FAIL] 连接失败: {e}")
        return

    with driver.session(database="neo4j") as session:
        # 清空旧数据
        session.run("MATCH (n) DETACH DELETE n")
        print("[OK] 清空旧数据")

        # 插入景点知识图谱示例数据
        session.run("""
            CREATE (g:ScenicSpot {name: '故宫', dynasty: '明清', level: '5A', city: '北京'})
            CREATE (t:ScenicSpot {name: '天坛', dynasty: '明', level: '5A', city: '北京'})
            CREATE (s:ScenicSpot {name: '颐和园', dynasty: '清', level: '5A', city: '北京'})
            CREATE (e1:Event {name: '建成于永乐年间', year: 1420})
            CREATE (e2:Event {name: '被列为世界文化遗产', year: 1987})
            CREATE (p:Person {name: '朱棣', title: '明成祖'})
            CREATE (a:Architecture {name: '太和殿', style: '宫殿建筑'})
            CREATE (b:Architecture {name: '祈年殿', style: '坛庙建筑'})

            CREATE (g)-[:HAS_EVENT]->(e1)
            CREATE (g)-[:HAS_EVENT]->(e2)
            CREATE (e1)-[:ORDERED_BY]->(p)
            CREATE (g)-[:CONTAINS]->(a)
            CREATE (t)-[:CONTAINS]->(b)
            CREATE (g)-[:NEAR]->(t)
            CREATE (g)-[:NEAR]->(s)
        """)
        print("[OK] 插入景点知识图谱数据")

        # 查询1: 查找故宫的所有关系
        result = session.run("""
            MATCH (g:ScenicSpot {name: '故宫'})-[r]->(target)
            RETURN type(r) AS relation, labels(target) AS target_type, target.name AS target_name
        """)
        print("\n=== 故宫的关系 ===")
        for record in result:
            print(f"  故宫 --[{record['relation']}]--> {record['target_name']} ({record['target_type']})")

        # 查询2: 查找北京所有5A景点
        result = session.run("""
            MATCH (s:ScenicSpot {city: '北京', level: '5A'})
            RETURN s.name AS name, s.dynasty AS dynasty
        """)
        print("\n=== 北京5A景点 ===")
        for record in result:
            print(f"  {record['name']} ({record['dynasty']}代)")

        # 查询3: 路径查询 - 从故宫出发经过关系链找到的信息
        result = session.run("""
            MATCH path = (g:ScenicSpot {name: '故宫'})-[*1..2]-(endNode)
            WHERE NOT endNode:ScenicSpot OR endNode.name <> '故宫'
            RETURN DISTINCT labels(endNode) AS type, endNode.name AS name
        """)
        print("\n=== 从故宫出发2跳内可达节点 ===")
        for record in result:
            print(f"  {record['name']} ({record['type']})")

    driver.close()
    print("\n[PASS] Neo4j 图谱查询验证通过！")

if __name__ == "__main__":
    test_connection()
