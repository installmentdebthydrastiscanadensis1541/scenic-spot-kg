# 小景 - 基于大模型+智能体的景点深度知识图谱应用

基于大语言模型（Qwen2.5-7B-Instruct）与ReAct智能体框架，构建景点领域深度知识图谱，并开发讲解AI、智能问答等应用。

## 项目架构

```
├── api/                  # FastAPI后端
│   └── main.py           # API入口（聊天、讲解、对话管理）
├── config/               # 配置
│   ├── prompts.py         # 提示词模板（ReAct、NER抽取、讲解AI）
│   └── settings.py        # 全局配置（从.env读取）
├── core/                 # 核心模块
│   ├── agent.py           # ReAct智能体（流式推理+进度推送）
│   ├── llm_client.py      # LLM客户端（vLLM/Mock双模式）
│   ├── chat_storage.py    # 对话持久化（SQLite）
│   └── autodl_manager.py  # AutoDL GPU实例管理
├── data/
│   └── scenic_data.py     # 景点知识库（50+景点结构化数据）
├── tools/                # 工具集
│   ├── knowledge_search.py # 向量检索（ChromaDB+SentenceTransformer）
│   ├── graph_query.py      # 知识图谱查询（Neo4j/内存模拟）
│   ├── route_plan.py       # 游览路线规划
│   ├── web_search.py       # 互联网搜索（DuckDuckGo）
│   ├── web_fetch.py        # 网页内容抓取
│   └── map_tool.py         # 地理位置查询
├── scripts/
│   └── build_kg.py        # 知识图谱构建管线（NER+关系抽取）
├── static/
│   └── index.html         # 前端聊天界面
├── deploy/
│   ├── start_all.sh       # AutoDL远程启动脚本
│   └── worker.js          # Cloudflare Worker公网入口
├── start_service.py       # 一键部署启动脚本
├── docker-compose.yml     # Docker编排（vLLM+Neo4j+FastAPI）
└── Dockerfile             # 容器镜像
```

## 核心功能

| 功能 | 说明 |
|------|------|
| 智能问答 | ReAct框架多工具协同推理，支持知识库检索+互联网搜索 |
| 知识图谱 | NER实体抽取+关系三元组构建，支持Neo4j图查询 |
| 向量检索 | ChromaDB + SentenceTransformer语义匹配 |
| 讲解AI | 基于景点知识生成生动讲解词，前端面板展示 |
| 路线规划 | 景点内部路线+城市一日游推荐 |
| AutoDL管理 | 自动开机/关机/SSH部署，空闲超时关机 |
| 公网部署 | Cloudflare Worker入口，ACCESS_KEY认证 |

## 部署方式

### 方式一：Docker Compose（推荐，需GPU）

前置条件：NVIDIA GPU（显存 >= 8GB）、Docker、nvidia-container-toolkit

```bash
# 1. 克隆仓库
git clone https://github.com/installmentdebthydrastiscanadensis1541/scenic-spot-kg.git
cd scenic-spot-kg

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env，按需修改配置

# 3. 一键启动
docker-compose up -d

# 4. 访问 http://localhost:8088
```

### 方式二：AutoDL云GPU + 本地调试

前置条件：AutoDL账户（约2元/小时）、Python 3.10+

**步骤1：AutoDL启动vLLM**

在AutoDL JupyterLab终端执行：

```bash
bash /root/autodl-tmp/start_all.sh
```

等待vLLM加载完成（约2分钟）。

**步骤2：建立SSH隧道**

本地PowerShell执行：

```powershell
ssh -L 8000:localhost:8000 -p <端口> root@<SSH地址>
```

输入密码后保持窗口开着。

**步骤3：启动本地FastAPI**

另开PowerShell：

```powershell
cd <项目目录>
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env，确认 LLM_BASE_URL=http://localhost:8000/v1
python -m uvicorn api.main:app --reload --port 8088
```

**步骤4：访问 http://localhost:8088**

### 方式三：一键部署脚本

```powershell
# 自动完成：AutoDL开机 → SSH连接 → 部署代码 → 启动服务
python start_service.py --deploy

# 查看状态
python start_service.py --status

# 停止服务
python start_service.py --stop
```

## 环境变量说明

| 变量 | 默认值 | 说明 |
|------|--------|------|
| LLM_BASE_URL | http://localhost:8000/v1 | vLLM服务地址 |
| LLM_API_KEY | empty | API密钥（vLLM可留空） |
| LLM_MODEL | Qwen2.5-7B-Instruct | 模型名称 |
| NEO4J_URI | bolt://localhost:7687 | Neo4j连接地址 |
| NEO4J_PASSWORD | scenic2024 | Neo4j密码 |
| AMAP_API_KEY | | 高德地图API Key（可选） |
| ACCESS_KEY | | 访问认证密钥（公网部署时设置） |
| DEV_MOCK_LLM | false | Mock模式（开发调试用） |

完整配置见 `.env.example`。

## 技术栈

- **LLM**: Qwen2.5-7B-Instruct (vLLM部署)
- **Agent**: ReAct框架，6种工具协同
- **向量检索**: ChromaDB + SentenceTransformer (paraphrase-multilingual-MiniLM-L12-v2)
- **知识图谱**: Neo4j 5.26
- **后端**: FastAPI + Uvicorn
- **前端**: 原生HTML/CSS/JS
- **部署**: Docker Compose / AutoDL + SSH隧道 / Cloudflare Worker
