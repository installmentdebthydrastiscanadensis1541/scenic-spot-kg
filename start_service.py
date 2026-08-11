"""一键启动服务 — 自动完成全链路部署

用法:
  python start_service.py              # 完整启动（开机+部署+启动服务）
  python start_service.py --deploy     # 仅同步代码到AutoDL（不开机）
  python start_service.py --restart    # 强制重启vLLM+FastAPI（不重新部署）
  python start_service.py --status     # 查看远程服务状态
  python start_service.py --stop       # 停止远程服务

流程（完整启动）:
  1. 检查AutoDL实例状态，如关机则自动开机
  2. 等待实例就绪
  3. SSH连接，同步本地代码到远程
  4. 安装Python依赖
  5. 停止旧的vLLM和FastAPI
  6. 启动vLLM（--max-model-len 8192）
  7. 启动FastAPI（端口6006）
  8. 等待服务就绪
  9. 输出访问地址

测试者访问方式:
  - AutoDL公网地址（6006端口映射）: 直接访问
  - Cloudflare Worker: 自动开机+重定向
  - 本地SSH隧道: 仅开发者使用
"""
import asyncio
import os
import sys

try:
    import asyncssh
    import httpx
except ImportError:
    print("缺少依赖，请运行: pip install asyncssh httpx")
    sys.exit(1)

# ── 配置（从.env读取） ──
from dotenv import load_dotenv
load_dotenv()

AUTODL_API_TOKEN = os.getenv("AUTODL_API_TOKEN", "")
AUTODL_INSTANCE_ID = os.getenv("AUTODL_INSTANCE_ID", "")
AUTODL_SSH_HOST = os.getenv("AUTODL_SSH_HOST", "")
AUTODL_SSH_PORT = int(os.getenv("AUTODL_SSH_PORT", "0"))
AUTODL_SSH_PASSWORD = os.getenv("AUTODL_SSH_PASSWORD", "")
AUTODL_PUBLIC_URL = os.getenv("AUTODL_PUBLIC_URL", "")
ACCESS_KEY = os.getenv("ACCESS_KEY", "")

API_BASE = "https://www.autodl.com/api/v1"
REMOTE_DIR = "/root/autodl-tmp/scenic-agent"
LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))


# ── AutoDL API ──

async def api_post(path: str, body: dict) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{API_BASE}{path}",
            headers={
                "Authorization": AUTODL_API_TOKEN,
                "Content-Type": "application/json;charset=UTF-8",
            },
            json=body,
        )
        return resp.json()


async def check_balance() -> float:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{API_BASE}/user/balance",
                headers={"Authorization": AUTODL_API_TOKEN},
            )
            data = resp.json()
            if isinstance(data.get("data"), dict):
                return float(data["data"].get("balance", 0))
            return float(data.get("data", 0))
    except Exception:
        return -1


async def get_instance_status() -> str:
    """返回实例状态字符串"""
    try:
        result = await api_post("/instance", {
            "date_from": "", "date_to": "",
            "page_index": 1, "page_size": 100,
            "status": [], "charge_type": [],
        })
        if result.get("code") == "Success":
            for inst in result.get("data", {}).get("list", []):
                if inst.get("uuid") == AUTODL_INSTANCE_ID:
                    return inst.get("status", "unknown")
    except Exception as e:
        print(f"  查询失败: {e}")
    return "unknown"


async def power_on() -> str:
    """开机，返回 'ok'/'gpu_busy'/'fail'"""
    try:
        result = await api_post("/instance/power_on", {
            "instance_uuid": AUTODL_INSTANCE_ID,
        })
        if result.get("code") == "Success":
            return "ok"
        msg = str(result.get("msg", "") or result.get("message", ""))
        gpu_kws = ["GPU", "显卡", "资源", "空闲", "不足", "繁忙", "busy", "no available", "sold out"]
        if any(kw in msg for kw in gpu_kws):
            return "gpu_busy"
        print(f"  开机失败: {msg}")
    except Exception as e:
        print(f"  开机异常: {e}")
    return "fail"


# ── SSH操作 ──

async def ssh_connect(retries=6, delay=10) -> asyncssh.SSHClientConnection:
    """SSH连接（带重试）"""
    for attempt in range(retries):
        try:
            conn = await asyncssh.connect(
                host=AUTODL_SSH_HOST,
                port=AUTODL_SSH_PORT,
                username="root",
                password=AUTODL_SSH_PASSWORD,
                known_hosts=None,
            )
            return conn
        except Exception as e:
            if attempt < retries - 1:
                print(f"  SSH连接失败（第{attempt+1}次），{delay}秒后重试...")
                await asyncio.sleep(delay)
            else:
                raise


async def ssh_deploy(conn: asyncssh.SSHClientConnection):
    """同步代码到远程"""
    print("  同步代码到远程...")

    # 用rsync同步（排除大文件和不必要文件）
    # 如果rsync不可用，则用scp
    excludes = "--exclude='.git' --exclude='__pycache__' --exclude='.env' --exclude='*.pyc' --exclude='chroma_data' --exclude='node_modules' --exclude='.venv'"

    # 先确保远程目录存在
    await conn.run(f"mkdir -p {REMOTE_DIR}", check=False)

    # 用tar打包+SSH管道传输（比scp快，且不需要rsync）
    import subprocess
    tar_cmd = (
        f'tar cf - -C "{LOCAL_DIR}" '
        f'--exclude=".git" --exclude="__pycache__" --exclude=".env" '
        f'--exclude="*.pyc" --exclude="chroma_data" --exclude="node_modules" '
        f'--exclude=".venv" --exclude="*.egg-info" '
        f'. | ssh -p {AUTODL_SSH_PORT} -o StrictHostKeyChecking=no '
        f'root@{AUTODL_SSH_HOST} "tar xf - -C {REMOTE_DIR}"'
    )
    # Windows下tar可用，但ssh管道可能有问题，改用asyncssh直接传
    # 逐文件传输关键目录
    key_dirs = ["api", "config", "core", "data", "tools", "deploy", "static"]
    key_files = ["main.py", "requirements.txt", "start.py"]

    for d in key_dirs:
        local_path = os.path.join(LOCAL_DIR, d)
        if os.path.isdir(local_path):
            # 递归创建远程目录并传输
            await conn.run(f"mkdir -p {REMOTE_DIR}/{d}", check=False)
            # 用asyncssh的sftp
            async with conn.start_sftp_client() as sftp:
                await _upload_dir(sftp, local_path, f"{REMOTE_DIR}/{d}")

    for f in key_files:
        local_path = os.path.join(LOCAL_DIR, f)
        if os.path.isfile(local_path):
            async with conn.start_sftp_client() as sftp:
                await sftp.put(local_path, f"{REMOTE_DIR}/{f}")

    # 复制.env但不覆盖远程的（远程有AUTODL_MODE等特殊配置）
    # 远程使用自己的环境变量

    print("  代码同步完成")


async def _upload_dir(sftp, local_dir: str, remote_dir: str):
    """递归上传目录"""
    for entry in os.listdir(local_dir):
        local_path = os.path.join(local_dir, entry)
        remote_path = f"{remote_dir}/{entry}"
        if os.path.isdir(local_path):
            if entry in ("__pycache__", ".git", "node_modules", "chroma_data"):
                continue
            await sftp.mkdir(remote_path, exists_ok=True)
            await _upload_dir(sftp, local_path, remote_path)
        elif os.path.isfile(local_path):
            if entry.endswith((".pyc", ".egg-info")):
                continue
            await sftp.put(local_path, remote_path)


async def ssh_install_deps(conn: asyncssh.SSHClientConnection):
    """安装Python依赖"""
    print("  安装Python依赖...")
    result = await conn.run(
        f"cd {REMOTE_DIR} && /root/miniconda3/bin/pip install -r requirements.txt -q 2>&1 | tail -5",
        check=False, timeout=300
    )
    if "ERROR" in (result.stdout or "") or "ERROR" in (result.stderr or ""):
        print(f"  依赖安装可能有错误，请检查")
    else:
        print("  依赖安装完成")


async def ssh_stop_services(conn: asyncssh.SSHClientConnection):
    """停止旧的vLLM和FastAPI"""
    print("  停止旧服务...")
    await conn.run("pkill -f 'vllm.entrypoints' 2>/dev/null; echo done", check=False)
    await conn.run("pkill -f 'uvicorn.*api.main' 2>/dev/null; echo done", check=False)
    await asyncio.sleep(2)
    # 确认已停止
    result = await conn.run("pgrep -f 'vllm.entrypoints' || echo 'vLLM已停止'", check=False)
    print(f"  {result.stdout.strip()}")
    result = await conn.run("pgrep -f 'uvicorn.*api.main' || echo 'FastAPI已停止'", check=False)
    print(f"  {result.stdout.strip()}")


async def ssh_start_services(conn: asyncssh.SSHClientConnection):
    """启动vLLM和FastAPI"""
    # 启动vLLM
    print("  启动vLLM (max-model-len=8192)...")
    result = await conn.run(
        "nohup /root/miniconda3/bin/python -m vllm.entrypoints.openai.api_server "
        "--model /root/autodl-tmp/Qwen2.5-7B-Instruct "
        "--served-model-name Qwen2.5-7B-Instruct "
        "--host 0.0.0.0 --port 8000 --trust-remote-code "
        "--max-model-len 8192 --gpu-memory-utilization 0.9 "
        "> /root/vllm.log 2>&1 & echo $!",
        check=False
    )
    vllm_pid = result.stdout.strip()
    print(f"  vLLM PID: {vllm_pid}")

    # 等待vLLM就绪（通常需要1-2分钟）
    print("  等待vLLM就绪...")
    for i in range(36):  # 最多等3分钟
        await asyncio.sleep(5)
        result = await conn.run(
            "curl -sf http://localhost:8000/v1/models > /dev/null 2>&1 && echo ready || echo waiting",
            check=False
        )
        if result.stdout.strip() == "ready":
            print(f"  vLLM已就绪！（等待了{(i+1)*5}秒）")
            break
        if i == 35:
            print("  vLLM启动超时，请检查 /root/vllm.log")
            return False

    # 启动FastAPI
    print("  启动FastAPI...")
    result = await conn.run(
        f"cd {REMOTE_DIR} && "
        "export LLM_BASE_URL=http://localhost:8000/v1 && "
        "export LLM_API_KEY=empty && "
        "export LLM_MODEL=Qwen2.5-7B-Instruct && "
        "export DEV_MOCK_LLM=false && "
        "export AUTODL_MODE=true && "
        "nohup /root/miniconda3/bin/python -m uvicorn api.main:app "
        "--host 0.0.0.0 --port 6006 "
        "> /root/fastapi.log 2>&1 & echo $!",
        check=False
    )
    fastapi_pid = result.stdout.strip()
    print(f"  FastAPI PID: {fastapi_pid}")

    # 等待FastAPI就绪
    print("  等待FastAPI就绪...")
    for i in range(15):
        await asyncio.sleep(2)
        result = await conn.run(
            "curl -sf http://localhost:6006/health > /dev/null 2>&1 && echo ready || echo waiting",
            check=False
        )
        if result.stdout.strip() == "ready":
            print(f"  FastAPI已就绪！（等待了{(i+1)*2}秒）")
            return True
    print("  FastAPI启动超时，请检查 /root/fastapi.log")
    return False


async def ssh_check_status(conn: asyncssh.SSHClientConnection):
    """检查远程服务状态"""
    print("\n── 远程服务状态 ──")

    # vLLM
    result = await conn.run("pgrep -f 'vllm.entrypoints' > /dev/null && echo 'vLLM: 运行中' || echo 'vLLM: 未运行'", check=False)
    print(f"  {result.stdout.strip()}")

    result = await conn.run("curl -sf http://localhost:8000/v1/models > /dev/null 2>&1 && echo 'vLLM API: 可用' || echo 'vLLM API: 不可用'", check=False)
    print(f"  {result.stdout.strip()}")

    # FastAPI
    result = await conn.run("pgrep -f 'uvicorn.*api.main' > /dev/null && echo 'FastAPI: 运行中' || echo 'FastAPI: 未运行'", check=False)
    print(f"  {result.stdout.strip()}")

    result = await conn.run("curl -sf http://localhost:6006/health > /dev/null 2>&1 && echo 'FastAPI API: 可用' || echo 'FastAPI API: 不可用'", check=False)
    print(f"  {result.stdout.strip()}")

    # GPU
    result = await conn.run("nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv,noheader 2>/dev/null || echo 'GPU: 无'", check=False)
    print(f"  GPU: {result.stdout.strip()}")

    # 磁盘
    result = await conn.run("df -h /root/autodl-tmp | tail -1", check=False)
    print(f"  数据盘: {result.stdout.strip()}")


# ── 主流程 ──

async def full_start():
    """完整启动流程"""
    print("=" * 50)
    print("  小景 · 景点知识助手 — 一键启动")
    print("=" * 50)

    # 1. 检查余额
    print("\n[1/7] 检查AutoDL余额...")
    balance = await check_balance()
    if balance < 0:
        print("  无法查询余额（可能Token过期），继续执行")
    elif balance < 5:
        print(f"  余额不足: {balance:.2f}元，请充值后再试")
        return
    else:
        print(f"  余额: {balance:.2f}元")

    # 2. 检查实例状态，必要时开机
    print("\n[2/7] 检查AutoDL实例状态...")
    status = await get_instance_status()
    print(f"  当前状态: {status}")

    if status in ("running", "Running", "使用中"):
        print("  实例已开机")
    elif status in ("stopped", "closed", "已关机", "offline"):
        print("  实例关机，正在开机...")
        result = await power_on()
        if result == "ok":
            print("  开机指令已发送")
        elif result == "gpu_busy":
            print("  GPU繁忙，请稍后再试")
            return
        else:
            print("  开机失败")
            return

        # 等待实例就绪
        print("  等待实例就绪...")
        for i in range(18):  # 最多等3分钟
            await asyncio.sleep(10)
            status = await get_instance_status()
            if status in ("running", "Running", "使用中"):
                print(f"  实例已就绪！（等待了{(i+1)*10}秒）")
                break
            if i == 17:
                print("  实例启动超时")
                return
    elif status in ("starting", "开机中"):
        print("  实例开机中，等待就绪...")
        for i in range(18):
            await asyncio.sleep(10)
            status = await get_instance_status()
            if status in ("running", "Running", "使用中"):
                print("  实例已就绪！")
                break
    else:
        print(f"  未知状态: {status}，尝试继续...")

    # 3. SSH连接
    print("\n[3/7] SSH连接...")
    try:
        conn = await ssh_connect()
        print("  SSH连接成功")
    except Exception as e:
        print(f"  SSH连接失败: {e}")
        return

    try:
        # 4. 同步代码
        print("\n[4/7] 同步代码到远程...")
        await ssh_deploy(conn)

        # 5. 安装依赖
        print("\n[5/7] 安装Python依赖...")
        await ssh_install_deps(conn)

        # 6. 停止旧服务 + 启动新服务
        print("\n[6/7] 重启服务...")
        await ssh_stop_services(conn)
        ok = await ssh_start_services(conn)
        if not ok:
            print("  服务启动失败")
            return

        # 7. 输出访问信息
        print("\n[7/7] 服务启动完成！")
        print("\n" + "=" * 50)
        print("  访问地址：")
        if AUTODL_PUBLIC_URL:
            key_param = f"?key={ACCESS_KEY}" if ACCESS_KEY else ""
            print(f"  公网地址: {AUTODL_PUBLIC_URL}{key_param}")
        print(f"  Cloudflare Worker: 需配置（见下方说明）")
        print(f"  本地隧道: ssh -L 6006:localhost:6006 -p {AUTODL_SSH_PORT} root@{AUTODL_SSH_HOST}")
        print("=" * 50)
        print("\n  测试建议：")
        print("  1. 问\"广州塔有多高\" — 测试知识库检索")
        print("  2. 问\"推荐一条西安一日游路线\" — 测试路线规划")
        print("  3. 问\"从广州到杭州的行程和酒店\" — 测试多主题+web搜索")
        print("  4. 连续追问5-8轮 — 测试上下文上限提示")

    finally:
        conn.close()
        await conn.wait_closed()


async def deploy_only():
    """仅同步代码"""
    print("同步代码到AutoDL...")
    conn = await ssh_connect()
    try:
        await ssh_deploy(conn)
        await ssh_install_deps(conn)
        print("代码同步完成！如需重启服务，运行: python start_service.py --restart")
    finally:
        conn.close()
        await conn.wait_closed()


async def restart_only():
    """仅重启远程服务"""
    print("重启远程服务...")
    conn = await ssh_connect()
    try:
        await ssh_stop_services(conn)
        ok = await ssh_start_services(conn)
        if ok:
            print("服务重启成功！")
        else:
            print("服务重启失败")
    finally:
        conn.close()
        await conn.wait_closed()


async def status_check():
    """查看远程状态"""
    print("连接SSH...")
    conn = await ssh_connect()
    try:
        await ssh_check_status(conn)
    finally:
        conn.close()
        await conn.wait_closed()


async def stop_services():
    """停止远程服务"""
    print("停止远程服务...")
    conn = await ssh_connect()
    try:
        await ssh_stop_services(conn)
        print("远程服务已停止")
    finally:
        conn.close()
        await conn.wait_closed()


def main():
    if len(sys.argv) < 2:
        asyncio.run(full_start())
        return

    cmd = sys.argv[1]
    if cmd == "--deploy":
        asyncio.run(deploy_only())
    elif cmd == "--restart":
        asyncio.run(restart_only())
    elif cmd == "--status":
        asyncio.run(status_check())
    elif cmd == "--stop":
        asyncio.run(stop_services())
    else:
        print(f"未知命令: {cmd}")
        print("用法: python start_service.py [--deploy|--restart|--status|--stop]")


if __name__ == "__main__":
    main()
