#!/bin/bash
# AutoDL实例一键启动脚本 — 同时启动vLLM + FastAPI
# 使用方法：在AutoDL终端中执行 bash /root/autodl-tmp/start_all.sh
# 或通过SSH远程执行
#
# 特性：
# - GPU就绪检测（开机后GPU驱动需要几秒加载）
# - HuggingFace国内镜像（避免连接超时）
# - 超时自动清除旧进程并重试（最多2次）
# - 进程重复检测（已在运行则跳过）

# ── 配置 ──
VLLM_PORT=8000
FASTAPI_PORT=6006          # AutoDL默认映射到公网的端口
MODEL_PATH=/root/autodl-tmp/Qwen2.5-7B-Instruct
PROJECT_DIR=/root/autodl-tmp/scenic-agent
VLLM_MAX_WAIT=90           # vLLM最大等待次数（每次5秒=7.5分钟）
FASTAPI_MAX_WAIT=30        # FastAPI最大等待次数（每次2秒=1分钟）
MAX_RETRY=2                # 启动失败最大重试次数

echo "========================================"
echo "  景点知识助手 — AutoDL一键启动"
echo "========================================"
echo "[$(date '+%H:%M:%S')] 开始启动流程..."

# ── 0. 等待GPU就绪 ──
echo "[$(date '+%H:%M:%S')] [GPU] 等待GPU就绪..."
for i in $(seq 1 30); do
    if nvidia-smi > /dev/null 2>&1; then
        echo "[$(date '+%H:%M:%S')] [GPU] 已就绪"
        break
    fi
    if [ $i -eq 30 ]; then
        echo "[$(date '+%H:%M:%S')] [GPU] 等待超时，GPU不可用"
        exit 1
    fi
    sleep 2
done

# ── 0.5 环境变量 ──
export HF_ENDPOINT=https://hf-mirror.com
export TRANSFORMERS_OFFLINE=1
export HF_HUB_OFFLINE=1

# ── 启动函数：带超时+重试 ──
start_vllm() {
    local attempt=1
    while [ $attempt -le $MAX_RETRY ]; do
        echo "[$(date '+%H:%M:%S')] [vLLM] 第 $attempt 次启动 (端口 $VLLM_PORT)..."

        # 清除旧进程
        pkill -f "vllm.entrypoints" 2>/dev/null || true
        sleep 2

        # 启动vLLM
        nohup python -m vllm.entrypoints.openai.api_server \
            --model $MODEL_PATH \
            --served-model-name Qwen2.5-7B-Instruct \
            --host 0.0.0.0 \
            --port $VLLM_PORT \
            --trust-remote-code \
            --max-model-len 8192 \
            --gpu-memory-utilization 0.9 \
            > /root/vllm.log 2>&1 &
        local vllm_pid=$!
        echo "[$(date '+%H:%M:%S')] [vLLM] PID=$vllm_pid，等待就绪..."

        # 等待就绪
        for i in $(seq 1 $VLLM_MAX_WAIT); do
            if curl -sf http://localhost:$VLLM_PORT/v1/models > /dev/null 2>&1; then
                echo "[$(date '+%H:%M:%S')] [vLLM] 已就绪！(耗时约 $((i*5)) 秒)"
                return 0
            fi
            # 检查进程是否还活着
            if ! kill -0 $vllm_pid 2>/dev/null; then
                echo "[$(date '+%H:%M:%S')] [vLLM] 进程已退出，查看日志："
                tail -10 /root/vllm.log
                break
            fi
            sleep 5
        done

        echo "[$(date '+%H:%M:%S')] [vLLM] 第 $attempt 次启动超时"
        attempt=$((attempt + 1))
    done

    echo "[$(date '+%H:%M:%S')] [vLLM] 重试 $MAX_RETRY 次后仍失败"
    return 1
}

start_fastapi() {
    local attempt=1
    while [ $attempt -le $MAX_RETRY ]; do
        echo "[$(date '+%H:%M:%S')] [FastAPI] 第 $attempt 次启动 (端口 $FASTAPI_PORT)..."

        # 清除旧进程
        pkill -f "uvicorn.*api.main" 2>/dev/null || true
        sleep 1

        # 设置环境变量
        export LLM_BASE_URL=http://localhost:$VLLM_PORT/v1
        export LLM_API_KEY=empty
        export LLM_MODEL=Qwen2.5-7B-Instruct
        export DEV_MOCK_LLM=false
        export AUTODL_MODE=true
        export CHROMA_HOST=localhost
        export CHROMA_PORT=8001

        cd $PROJECT_DIR
        nohup python -m uvicorn api.main:app \
            --host 0.0.0.0 \
            --port $FASTAPI_PORT \
            > /root/fastapi.log 2>&1 &
        local fastapi_pid=$!
        echo "[$(date '+%H:%M:%S')] [FastAPI] PID=$fastapi_pid，等待就绪..."

        # 等待就绪
        for i in $(seq 1 $FASTAPI_MAX_WAIT); do
            if curl -sf http://localhost:$FASTAPI_PORT/health > /dev/null 2>&1; then
                echo "[$(date '+%H:%M:%S')] [FastAPI] 已就绪！(耗时约 $((i*2)) 秒)"
                return 0
            fi
            if ! kill -0 $fastapi_pid 2>/dev/null; then
                echo "[$(date '+%H:%M:%S')] [FastAPI] 进程已退出，查看日志："
                tail -10 /root/fastapi.log
                break
            fi
            sleep 2
        done

        echo "[$(date '+%H:%M:%S')] [FastAPI] 第 $attempt 次启动超时"
        attempt=$((attempt + 1))
    done

    echo "[$(date '+%H:%M:%S')] [FastAPI] 重试 $MAX_RETRY 次后仍失败"
    return 1
}

# ── 1. 启动vLLM ──
if pgrep -f "vllm.entrypoints" > /dev/null && curl -sf http://localhost:$VLLM_PORT/v1/models > /dev/null 2>&1; then
    echo "[$(date '+%H:%M:%S')] [vLLM] 已在运行且就绪，跳过"
else
    start_vllm || exit 1
fi

# ── 2. 启动FastAPI ──
if pgrep -f "uvicorn.*api.main" > /dev/null && curl -sf http://localhost:$FASTAPI_PORT/health > /dev/null 2>&1; then
    echo "[$(date '+%H:%M:%S')] [FastAPI] 已在运行且就绪，跳过"
else
    start_fastapi || exit 1
fi

echo ""
echo "========================================"
echo "  全部服务已启动！"
echo "  vLLM:    http://localhost:$VLLM_PORT"
echo "  FastAPI: http://localhost:$FASTAPI_PORT"
echo "  公网访问: AutoDL自定义服务页面查看6006端口映射地址"
echo "========================================"
