#!/bin/bash
# AutoDL实例一键启动脚本 — 同时启动vLLM + FastAPI
# 使用方法：在AutoDL终端中执行 bash /root/autodl-tmp/start_all.sh
# 或通过SSH远程执行

set -e

# ── 配置 ──
VLLM_PORT=8000
FASTAPI_PORT=6006          # AutoDL默认映射到公网的端口
MODEL_PATH=/root/autodl-tmp/Qwen2.5-7B-Instruct
PROJECT_DIR=/root/autodl-tmp/scenic-agent

echo "========================================"
echo "  景点知识助手 — AutoDL一键启动"
echo "========================================"

# ── 1. 启动vLLM ──
if pgrep -f "vllm.entrypoints" > /dev/null; then
    echo "[vLLM] 已在运行，跳过"
else
    echo "[vLLM] 正在启动 (端口 $VLLM_PORT)..."
    nohup python -m vllm.entrypoints.openai.api_server \
        --model $MODEL_PATH \
        --served-model-name Qwen2.5-7B-Instruct \
        --host 0.0.0.0 \
        --port $VLLM_PORT \
        --trust-remote-code \
        --max-model-len 8192 \
        --gpu-memory-utilization 0.9 \
        > /root/vllm.log 2>&1 &
    echo "[vLLM] 等待就绪..."
    for i in $(seq 1 60); do
        if curl -sf http://localhost:$VLLM_PORT/v1/models > /dev/null 2>&1; then
            echo "[vLLM] 已就绪！"
            break
        fi
        if [ $i -eq 60 ]; then
            echo "[vLLM] 启动超时，请检查 /root/vllm.log"
            exit 1
        fi
        sleep 5
    done
fi

# ── 2. 启动FastAPI ──
if pgrep -f "uvicorn.*api.main" > /dev/null; then
    echo "[FastAPI] 已在运行，跳过"
else
    echo "[FastAPI] 正在启动 (端口 $FASTAPI_PORT)..."
    # 设置环境变量
    export LLM_BASE_URL=http://localhost:$VLLM_PORT/v1
    export LLM_API_KEY=empty
    export LLM_MODEL=Qwen2.5-7B-Instruct
    export DEV_MOCK_LLM=false
    export AUTODL_MODE=true    # 标记运行在AutoDL上
    # 向量数据库和Neo4j在AutoDL上用内存模式
    export CHROMA_HOST=localhost
    export CHROMA_PORT=8001

    cd $PROJECT_DIR
    nohup python -m uvicorn api.main:app \
        --host 0.0.0.0 \
        --port $FASTAPI_PORT \
        > /root/fastapi.log 2>&1 &
    echo "[FastAPI] 等待就绪..."
    for i in $(seq 1 30); do
        if curl -sf http://localhost:$FASTAPI_PORT/health > /dev/null 2>&1; then
            echo "[FastAPI] 已就绪！"
            break
        fi
        if [ $i -eq 30 ]; then
            echo "[FastAPI] 启动超时，请检查 /root/fastapi.log"
            exit 1
        fi
        sleep 2
    done
fi

echo ""
echo "========================================"
echo "  全部服务已启动！"
echo "  vLLM:    http://localhost:$VLLM_PORT"
echo "  FastAPI: http://localhost:$FASTAPI_PORT"
echo "  公网访问: AutoDL自定义服务页面查看6006端口映射地址"
echo "========================================"
