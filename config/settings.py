"""全局配置 — 从环境变量读取，支持 .env 文件"""
import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    # ── LLM ──
    LLM_BASE_URL: str = os.getenv("LLM_BASE_URL", "http://localhost:8000/v1")
    LLM_API_KEY: str = os.getenv("LLM_API_KEY", "empty")
    LLM_MODEL: str = os.getenv("LLM_MODEL", "Qwen2.5-7B-Instruct")
    LLM_TEMPERATURE: float = float(os.getenv("LLM_TEMPERATURE", "0.1"))
    LLM_MAX_TOKENS: int = int(os.getenv("LLM_MAX_TOKENS", "512"))

    # ── 嵌入模型 ──
    EMBEDDING_MODEL: str = "paraphrase-multilingual-MiniLM-L12-v2"
    EMBEDDING_DIMENSION: int = 384

    # ── 向量数据库 ──
    CHROMA_HOST: str = os.getenv("CHROMA_HOST", "localhost")
    CHROMA_PORT: int = int(os.getenv("CHROMA_PORT", "8001"))
    CHROMA_COLLECTION: str = "scenic_spots"

    # ── 知识图谱 ──
    NEO4J_URI: str = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    NEO4J_USER: str = os.getenv("NEO4J_USER", "neo4j")
    NEO4J_PASSWORD: str = os.getenv("NEO4J_PASSWORD", "scenic2024")

    # ── 高德地图API ──
    AMAP_API_KEY: str = os.getenv("AMAP_API_KEY", "")

    # ── Agent ──
    MAX_ITERATIONS: int = int(os.getenv("MAX_ITERATIONS", "5"))
    TOOLS: list = ["knowledge_search", "graph_query", "route_plan", "web_search", "web_fetch", "map_tool", "image_search"]

    # ── 开发模式 ──
    DEV_MOCK_LLM: bool = os.getenv("DEV_MOCK_LLM", "false").lower() == "true"


settings = Settings()
