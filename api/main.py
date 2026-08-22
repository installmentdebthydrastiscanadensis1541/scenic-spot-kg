"""FastAPI后端入口 — 景点知识图谱应用API

启动方式：uvicorn api.main:app --reload
"""
import os
import pathlib
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from core.llm_client import LLMClient
from core.agent import ReActAgent
from core.autodl_manager import autodl_manager, AutoDLStatus
from core.chat_storage import list_conversations, create_conversation, get_messages, append_message, delete_conversation, rename_conversation
from tools.knowledge_search import KnowledgeSearchTool
from tools.graph_query import GraphQueryTool
from tools.route_plan import RoutePlanTool
from tools.web_search import WebSearchTool
from tools.web_fetch import WebFetchTool
from tools.map_tool import MapTool
from tools.image_search import ImageSearchTool
from config.settings import settings
from config.prompts import TOUR_GUIDE, GUIDE_STYLES


app = FastAPI(
    title="景点知识图谱API",
    description="基于大模型+智能体的景点深度知识图谱应用",
    version="0.2.0",
)

# 允许跨域（Cloudflare Worker健康检查页面需要从浏览器fetch /health）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# ── 简单访问认证（防止公网被滥用） ──
ACCESS_KEY = os.getenv("ACCESS_KEY", "")  # 留空则不启用认证


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """访问认证中间件：设置ACCESS_KEY后，所有请求需携带key"""
    # 未设置ACCESS_KEY则不认证
    if not ACCESS_KEY:
        return await call_next(request)
    # 从query参数或header获取key
    key = request.query_params.get("key") or request.headers.get("X-Access-Key", "")
    if key != ACCESS_KEY:
        return JSONResponse(status_code=403, content={"error": "访问被拒绝，请提供正确的access key"})
    return await call_next(request)

# ── 全局组件（启动时初始化） ──
llm: LLMClient = None
agent: ReActAgent = None
tools: dict = {}


@app.on_event("startup")
async def startup():
    """初始化组件"""
    global llm, agent, tools

    llm = LLMClient()

    tools = {
        "knowledge_search": KnowledgeSearchTool(),
        "graph_query": GraphQueryTool(),
        "route_plan": RoutePlanTool(),
        "web_search": WebSearchTool(),
        "web_fetch": WebFetchTool(),
        "map_tool": MapTool(),
        "image_search": ImageSearchTool(),
    }

    # 知识库索引延迟到首次使用时构建（避免启动时加载模型卡住）

    agent = ReActAgent(llm=llm, tools=tools)

    mode = "Mock" if llm.mock else f"真实({settings.LLM_BASE_URL})"
    vector_mode = tools["knowledge_search"]._vector_mode
    autodl_mode = "已配置" if autodl_manager.is_configured() else "未配置"
    print(f"启动完成 | LLM模式: {mode} | 向量检索: {'已启用' if vector_mode else '关键词模式'} | AutoDL管理: {autodl_mode} | 工具: {list(tools.keys())}")


# ── 请求/响应模型 ──
class QueryRequest(BaseModel):
    question: str
    mock: bool | None = None
    conversation_id: str | None = None


class ClearMemoryRequest(BaseModel):
    session_id: str | None = None


class QueryResponse(BaseModel):
    answer: str
    thoughts: list[str]
    actions: list[dict]
    iterations: int


class ToolListResponse(BaseModel):
    tools: list[dict]


class SaveMessageRequest(BaseModel):
    role: str
    content: str


class RenameRequest(BaseModel):
    title: str


# ── 会话管理 ──
from core.agent import ConversationMemory
import time as _time

sessions: dict[str, tuple[ConversationMemory, float]] = {}  # {id: (memory, last_access)}
_MAX_SESSIONS = 50  # 最多保留50个会话记忆，防止内存泄漏


def get_or_create_memory(session_id: str | None) -> ConversationMemory:
    """获取或创建会话记忆（带LRU淘汰）"""
    if session_id and session_id in sessions:
        memory, _ = sessions[session_id]
        sessions[session_id] = (memory, _time.time())
        return memory
    memory = ConversationMemory(max_rounds=10)
    if session_id:
        # 超过上限时淘汰最久未访问的会话
        if len(sessions) >= _MAX_SESSIONS:
            oldest_id = min(sessions, key=lambda k: sessions[k][1])
            del sessions[oldest_id]
        sessions[session_id] = (memory, _time.time())
    return memory


# ── 静态文件路径 ──
_STATIC_DIR = pathlib.Path(__file__).parent.parent / "static"


# ── API路由 ──
@app.get("/", response_class=HTMLResponse)
async def chat_page():
    """聊天界面"""
    resp = FileResponse(_STATIC_DIR / "index.html", media_type="text/html; charset=utf-8")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.get("/tools", response_model=ToolListResponse)
async def list_tools():
    """列出所有可用工具"""
    return {"tools": [t.to_mcp_spec() for t in tools.values()]}


@app.post("/query", response_model=QueryResponse)
async def query(req: QueryRequest):
    """智能问答 — Agent推理（JSON接口）"""
    if req.mock is not None:
        llm.mock = req.mock

    # 使用会话记忆
    original_memory = agent.memory
    agent.memory = get_or_create_memory(req.conversation_id)

    try:
        state = await agent.run(req.question, use_memory=True)
    except Exception as e:
        import traceback
        traceback.print_exc()
        state = AgentState(question=req.question)
        state.final_answer = f"抱歉，处理时出错：{e}"

    # 恢复默认记忆
    agent.memory = original_memory

    return QueryResponse(
        answer=state.final_answer or "无法回答",
        thoughts=state.thoughts,
        actions=state.actions,
        iterations=state.iteration,
    )


@app.post("/chat/stream")
async def chat_stream(req: QueryRequest):
    """流式聊天接口 — SSE逐字输出，支持AutoDL自动启动"""
    if req.mock is not None:
        llm.mock = req.mock

    # 如果LLM不可用且配置了AutoDL，自动触发启动（仅公网部署模式）
    # 本地SSH隧道模式下(AUTODL_MODE=true)跳过，避免httpx异步请求卡住
    if not llm.mock and autodl_manager.is_configured() and os.getenv("AUTODL_MODE") != "true":
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{settings.LLM_BASE_URL.replace('/v1', '')}/v1/models")
                if resp.status_code != 200:
                    raise Exception("LLM not ready")
        except Exception as e:
            print(f"[Chat] LLM不可达({e})，触发AutoDL启动...")
            status = await autodl_manager.ensure_running()
            if status == AutoDLStatus.LOW_BALANCE:
                return StreamingResponse(
                    iter(["⚠️ AutoDL余额不足，请充值后再试。"]),
                    media_type="text/plain; charset=utf-8",
                )
            if status == AutoDLStatus.GPU_BUSY:
                return StreamingResponse(
                    iter(["⚠️ GPU资源繁忙，暂时无法开机，请稍后再试。"]),
                    media_type="text/plain; charset=utf-8",
                )
            if status == AutoDLStatus.ERROR:
                return StreamingResponse(
                    iter([f"❌ AutoDL启动失败：{autodl_manager.get_status_info()['message']}"]),
                    media_type="text/plain; charset=utf-8",
                )
            if status == AutoDLStatus.STARTING:
                return StreamingResponse(
                    iter(["⏳ 正在启动AutoDL实例和vLLM服务，请稍后重试..."]),
                    media_type="text/plain; charset=utf-8",
                )
            new_llm = LLMClient()
            agent.llm = new_llm

    # 使用会话记忆（在生成器内切换，确保流式期间记忆不丢失）
    original_memory = agent.memory
    session_memory = get_or_create_memory(req.conversation_id)

    # 后端统一保存user消息（保证消息顺序：user先于assistant）
    # 避免前端并行fetch导致rowid颠倒（assistant先入库）
    if req.conversation_id:
        append_message(req.conversation_id, "user", req.question)

    async def generate():
        agent.memory = session_memory
        full_answer = []
        try:
            async for char in agent.run_stream(req.question, use_memory=True):
                full_answer.append(char)
                yield char
        except Exception as e:
            import traceback
            traceback.print_exc()
            yield f"抱歉，处理时出错：{e}"
        finally:
            agent.memory = original_memory
            # 流式结束后保存assistant消息（放在finally中，确保中断时也保存部分回答）
            # 顺序由后端代码执行顺序保证：user消息在generate()之前保存，一定在前
            if req.conversation_id and full_answer:
                append_message(req.conversation_id, "assistant", "".join(full_answer))

    return StreamingResponse(
        generate(),
        media_type="text/plain; charset=utf-8",
    )


@app.post("/chat/clear")
async def clear_memory(req: ClearMemoryRequest):
    """清除对话记忆"""
    if req.session_id and req.session_id in sessions:
        sessions[req.session_id][0].clear()
        del sessions[req.session_id]
    else:
        agent.memory.clear()
    return {"status": "ok", "message": "对话记忆已清除"}


# ── 对话管理API ──
@app.get("/api/conversations")
async def api_list_conversations(uid: str = ""):
    user_id = uid or None
    return {"conversations": list_conversations(user_id)}


@app.post("/api/conversations")
async def api_create_conversation(uid: str = ""):
    user_id = uid or None
    conv = create_conversation(user_id)
    return conv


@app.get("/api/conversations/{conv_id}/messages")
async def api_get_messages(conv_id: str):
    return {"messages": get_messages(conv_id)}


@app.post("/api/conversations/{conv_id}/messages")
async def api_save_message(conv_id: str, req: SaveMessageRequest):
    msg = append_message(conv_id, req.role, req.content)
    return msg


@app.delete("/api/conversations/{conv_id}")
async def api_delete_conversation(conv_id: str):
    delete_conversation(conv_id)
    return {"status": "ok"}


@app.patch("/api/conversations/{conv_id}")
async def api_rename_conversation(conv_id: str, req: RenameRequest):
    rename_conversation(conv_id, req.title)
    return {"status": "ok"}


@app.get("/guide/{spot_name}")
async def guide(spot_name: str, style: str = "vivid"):
    """讲解AI — 为指定景点生成讲解词

    基于知识库检索景点信息，再用TOUR_GUIDE提示词生成生动讲解。
    支持style参数切换讲解风格：vivid(活泼)/formal(正式)/humor(幽默)/poetic(诗意)/simple(简洁)
    """
    from data.scenic_data import SCENIC_SPOTS

    # 校验风格参数
    style_config = GUIDE_STYLES.get(style, GUIDE_STYLES["vivid"])

    # 查找景点
    spot = None
    for s in SCENIC_SPOTS:
        if spot_name in s["name"] or s["name"] in spot_name:
            spot = s
            break

    if not spot:
        return JSONResponse(status_code=404, content={"error": f"未找到景点: {spot_name}"})

    # 拼接景点知识
    knowledge_parts = []
    if spot.get("desc"):
        knowledge_parts.append(spot["desc"])
    if spot.get("detail"):
        knowledge_parts.append(spot["detail"])
    if spot.get("highlights"):
        knowledge_parts.append(f"主要看点：{spot['highlights']}")
    if spot.get("dynasty"):
        knowledge_parts.append(f"建造朝代：{spot['dynasty']}")
    if spot.get("duration"):
        knowledge_parts.append(f"建议游览时长：{spot['duration']}")
    knowledge = "\n".join(knowledge_parts)

    # 截断知识避免超出上下文
    if len(knowledge) > 600:
        knowledge = knowledge[:600] + "..."

    # 调用LLM生成讲解词（使用风格对应的system prompt和temperature）
    prompt = TOUR_GUIDE.format(spot_name=spot["name"], knowledge=knowledge, preference=style_config["label"])
    try:
        response = await llm.chat(
            messages=[
                {"role": "system", "content": style_config["system"]},
                {"role": "user", "content": prompt},
            ],
            temperature=style_config["temp"],
            max_tokens=512,
        )
        guide_text = response.strip()
    except Exception as e:
        guide_text = f"讲解生成失败：{e}"

    return {
        "spot_name": spot["name"],
        "city": spot.get("city", ""),
        "guide": guide_text,
        "style": style,
        "style_label": style_config["label"],
        "knowledge_source": "local_kb",
    }


@app.get("/guide")
async def guide_list():
    """列出可讲解的景点"""
    from data.scenic_data import SCENIC_SPOTS
    spots = [{"name": s["name"], "city": s["city"], "category": s["category"]} for s in SCENIC_SPOTS]
    return {"spots": spots, "total": len(spots)}


@app.get("/health")
async def health():
    """健康检查 — 验证vLLM是否真正就绪"""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{settings.LLM_BASE_URL}/models")
            if r.status_code == 200:
                return {"status": "ok", "llm_mode": "real", "model": settings.LLM_MODEL}
    except Exception:
        pass
    return {"status": "not_ready", "llm_mode": "real", "model": settings.LLM_MODEL}


@app.get("/autodl/status")
async def autodl_status():
    """AutoDL实例状态查询"""
    return autodl_manager.get_status_info()


@app.post("/autodl/start")
async def autodl_start():
    """手动触发AutoDL启动"""
    if not autodl_manager.is_configured():
        return JSONResponse(status_code=400, content={"error": "AutoDL未配置，请设置环境变量"})
    status = await autodl_manager.ensure_running()
    return autodl_manager.get_status_info()


@app.post("/autodl/stop")
async def autodl_stop():
    """手动关闭AutoDL实例"""
    await autodl_manager.shutdown()
    return autodl_manager.get_status_info()
