"""通过SSH上传项目代码到AutoDL实例 — 使用tar打包方式"""
import asyncio
import asyncssh
import os
import tarfile
import io

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
REMOTE_DIR = "/root/autodl-tmp/scenic-agent"

# 需要上传的文件（排除__pycache__、.env、db等）
UPLOAD_FILES = [
    "api/__init__.py",
    "api/main.py",
    "config/__init__.py",
    "config/prompts.py",
    "config/settings.py",
    "core/__init__.py",
    "core/agent.py",
    "core/autodl_manager.py",
    "core/chat_storage.py",
    "core/llm_client.py",
    "data/__init__.py",
    "data/scenic_data.py",
    "tools/__init__.py",
    "tools/base.py",
    "tools/graph_query.py",
    "tools/image_search.py",
    "tools/knowledge_search.py",
    "tools/map_tool.py",
    "tools/route_plan.py",
    "tools/web_fetch.py",
    "tools/web_search.py",
    "static/index.html",
    "requirements.txt",
    "deploy/start_all.sh",
]

# AutoDL专用.env
ENV_CONTENT = """# LLM配置 (vLLM on AutoDL本地)
LLM_BASE_URL=http://localhost:8000/v1
LLM_API_KEY=empty
LLM_MODEL=Qwen2.5-7B-Instruct

# 知识图谱 - AutoDL上用内存模式
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=scenic2024

# 向量数据库 - AutoDL上用关键词模式
CHROMA_HOST=localhost
CHROMA_PORT=8001

# 高德地图API
AMAP_API_KEY=

# 服务端口
PORT=6006

# 开发模式
DEV_MOCK_LLM=false

# 访问认证
ACCESS_KEY=

# AutoDL模式标记
AUTODL_MODE=true
"""


def make_tar() -> bytes:
    """将项目文件打包为tar.gz内存字节流"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tar:
        for f in UPLOAD_FILES:
            local_path = os.path.join(PROJECT_DIR, f)
            if os.path.exists(local_path):
                tar.add(local_path, arcname=f"scenic-agent/{f}")
                print(f"  打包: {f}")
        # 加入.env
        env_bytes = ENV_CONTENT.encode()
        info = tarfile.TarInfo(name="scenic-agent/.env")
        info.size = len(env_bytes)
        tar.addfile(info, io.BytesIO(env_bytes))
        print("  打包: .env (AutoDL专用)")
    return buf.getvalue()


async def main():
    print("打包项目文件...")
    tar_data = make_tar()
    print(f"  包大小: {len(tar_data)/1024:.1f} KB")

    print("\n连接SSH...")
    conn = await asyncssh.connect(
        host="connect.bjb1.seetacloud.com",
        port=49122,
        username="root",
        password="8PUGAmYNn7Og",
        known_hosts=None,
    )

    # 通过SFTP写入tar包
    print("上传tar包...")
    async with conn.start_sftp_client() as sftp:
        async with sftp.open('/tmp/scenic-agent.tar.gz', 'wb') as f:
            await f.write(tar_data)
    print("  上传完成")

    # 解压到目标目录
    print("解压到远程目录...")
    result = await conn.run(
        f"mkdir -p {REMOTE_DIR} && "
        f"cd /root/autodl-tmp && "
        f"tar xzf /tmp/scenic-agent.tar.gz && "
        f"rm /tmp/scenic-agent.tar.gz",
        check=False
    )
    print(f"  解压: {'成功' if result.exit_status == 0 else '失败'}")
    if result.stderr:
        print(f"  stderr: {result.stderr[:200]}")

    # 复制start_all.sh到正确位置
    print("设置启动脚本...")
    await conn.run(f"cp {REMOTE_DIR}/deploy/start_all.sh /root/autodl-tmp/start_all.sh", check=False)
    await conn.run("chmod +x /root/autodl-tmp/start_all.sh", check=False)
    print("  start_all.sh 已就位")

    # 安装依赖
    print("\n安装Python依赖（可能需要1-2分钟）...")
    result = await conn.run(
        f"cd {REMOTE_DIR} && pip install -r requirements.txt -q 2>&1 | tail -5",
        check=False, timeout=180
    )
    print(result.stdout[-500:] if result.stdout else "安装完成")

    conn.close()
    await conn.wait_closed()
    print("\n上传和部署完成！")

asyncio.run(main())
