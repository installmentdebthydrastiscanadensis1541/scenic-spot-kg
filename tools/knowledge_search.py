"""知识搜索工具 — 向量检索景点知识

连接Chroma向量数据库，用sentence-transformers做语义检索。
开发阶段：若无依赖则退化为关键词匹配模式。
"""
from tools.base import BaseTool, ToolParameter
from config.settings import settings
from data.scenic_data import KNOWLEDGE_TEXTS


class KnowledgeSearchTool(BaseTool):
    name = "knowledge_search"
    description = "检索景点知识库，输入搜索查询，返回相关景点信息"
    parameters = [
        ToolParameter(name="query", description="搜索查询文本"),
    ]

    def __init__(self):
        self._vector_mode = False
        self._vector_initialized = False
        self.model = None
        self.collection = None
        self.client = None
        self.knowledge = KNOWLEDGE_TEXTS  # 使用全国景点数据

        # 只检测包是否安装，不立即加载模型（避免启动时下载模型卡住）
        try:
            import chromadb
            import sentence_transformers
            self._vector_mode = True
            print("[KnowledgeSearch] 向量检索模式（模型将在首次使用时加载）")
        except ImportError:
            print("[KnowledgeSearch] chromadb/sentence-transformers未安装，使用关键词匹配模式")

    def _ensure_vector_init(self):
        """延迟初始化：首次使用时才加载模型和数据"""
        if self._vector_initialized:
            return True
        try:
            # 设置HuggingFace国内镜像，避免连接超时
            import os
            hf_mirror = os.getenv("HF_ENDPOINT", "")
            if hf_mirror and not os.environ.get("HF_ENDPOINT"):
                os.environ["HF_ENDPOINT"] = hf_mirror

            from sentence_transformers import SentenceTransformer
            import chromadb

            print("[KnowledgeSearch] 正在加载向量模型...")
            self.model = SentenceTransformer(settings.EMBEDDING_MODEL)
            self.client = chromadb.Client()
            self.collection = self.client.get_or_create_collection(
                name=settings.CHROMA_COLLECTION,
                metadata={"hnsw:space": "cosine"},
            )
            self._load_data()
            self._vector_initialized = True
            print("[KnowledgeSearch] 向量模型加载完成")
            return True
        except Exception as e:
            print(f"[KnowledgeSearch] 向量模型加载失败，降级为关键词模式: {e}")
            self._vector_mode = False
            return False

    def _load_data(self, force: bool = False):
        """加载景点数据到向量库

        Args:
            force: 强制重建索引（数据更新后设为True）
        """
        if not force and self.collection.count() > 0:
            return
        # 强制重建：先清空旧数据
        if self.collection.count() > 0:
            try:
                self.client.delete_collection(name=settings.CHROMA_COLLECTION)
            except Exception:
                pass
            self.collection = self.client.get_or_create_collection(
                name=settings.CHROMA_COLLECTION,
                metadata={"hnsw:space": "cosine"},
            )
        embeddings = self.model.encode(self.knowledge).tolist()
        self.collection.add(
            documents=self.knowledge,
            embeddings=embeddings,
            ids=[f"doc_{i}" for i in range(len(self.knowledge))],
        )
        print(f"[KnowledgeSearch] 已加载 {len(self.knowledge)} 条知识到向量库")

    def rebuild_index(self):
        """强制重建向量索引（在知识库更新后调用）"""
        if not self._vector_mode:
            print("[KnowledgeSearch] 非向量模式，无需重建索引")
            return
        if not self._vector_initialized:
            self._ensure_vector_init()
            return
        self._load_data(force=True)

    async def run(self, input_str: str) -> str:
        """执行检索（向量模式用线程池避免阻塞事件循环）"""
        import asyncio

        if self._vector_mode:
            if not self._vector_initialized:
                try:
                    await asyncio.wait_for(
                        asyncio.get_running_loop().run_in_executor(None, self._ensure_vector_init),
                        timeout=120  # 模型加载最多2分钟
                    )
                except asyncio.TimeoutError:
                    return "知识库加载超时，请稍后重试。"
                except Exception as e:
                    return f"知识库加载失败：{e}"
            if self._vector_mode:
                try:
                    return await asyncio.wait_for(
                        asyncio.get_running_loop().run_in_executor(None, self._vector_search, input_str),
                        timeout=15
                    )
                except asyncio.TimeoutError:
                    return "知识检索超时，请稍后重试。"
        return self._keyword_search(input_str)

    def _vector_search(self, query: str) -> str:
        """向量语义检索"""
        query_embedding = self.model.encode([query]).tolist()
        results = self.collection.query(
            query_embeddings=query_embedding,
            n_results=3,
        )
        if not results["documents"][0]:
            return "未找到相关知识。"
        docs = results["documents"][0]
        distances = results["distances"][0]
        formatted = []
        for doc, dist in zip(docs, distances):
            formatted.append(f"[相关度: {1-dist:.2f}] {doc}")
        return "\n".join(formatted)

    def _keyword_search(self, query: str) -> str:
        """关键词匹配（无向量依赖时的降级方案）"""
        results = []
        query_chars = set(query)
        for doc in self.knowledge:
            overlap = sum(1 for c in query_chars if c in doc)
            # 至少40%字符匹配（提高精度，减少无关结果）
            if overlap > len(query_chars) * 0.4:
                results.append((overlap, doc))
        results.sort(key=lambda x: x[0], reverse=True)

        if not results:
            return "未找到相关知识。"
        return "\n".join(f"[匹配] {doc}" for _, doc in results[:3])
