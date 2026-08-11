"""修改 Neo4j 默认密码"""
from neo4j import GraphDatabase

driver = GraphDatabase.driver("bolt://localhost:7687", auth=("neo4j", "neo4j"))
with driver.session(database="system") as session:
    session.run("ALTER CURRENT USER SET PASSWORD FROM 'neo4j' TO 'scenic2024'")
print("Password changed to scenic2024")
driver.close()
