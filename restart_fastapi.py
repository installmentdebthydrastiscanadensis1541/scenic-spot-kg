"""通过SSH重启FastAPI - 用远程脚本方式"""
import asyncio
import asyncssh

PYTHON = "/root/miniconda3/bin/python"
PROJECT_DIR = "/root/autodl-tmp/scenic-agent"

async def main():
    print("连接SSH...")
    conn = await asyncssh.connect(
        host="connect.bjb1.seetacloud.com",
        port=49122,
        username="root",
        password="8PUGAmYNn7Og",
        known_hosts=None,
    )

    # 1. 写启动脚本到远程
    print("1. 写入启动脚本...")
    script = f"""#!/bin/bash
cd {PROJECT_DIR}
export PYTHONPATH={PROJECT_DIR}
kill $(pgrep -f 'uvicorn api.main') 2>/dev/null
sleep 1
nohup {PYTHON} -m uvicorn api.main:app --host 0.0.0.0 --port 6006 > /root/autodl-tmp/fastapi.log 2>&1 &
echo "started pid=$!"
"""
    async with conn.start_sftp_client() as sftp:
        async with sftp.open('/tmp/restart_fastapi.sh', 'w') as f:
            await f.write(script)
    await conn.run("chmod +x /tmp/restart_fastapi.sh", check=True)

    # 2. 用bash执行脚本（会立即返回因为nohup后台运行）
    print("2. 执行启动脚本...")
    result = await conn.run("bash /tmp/restart_fastapi.sh", check=False, timeout=10)
    print(f"   输出: {result.stdout.strip() if result.stdout else '(空)'}")

    # 3. 等待并检查
    print("3. 等待启动...")
    for i in range(10):
        await asyncio.sleep(3)
        result = await conn.run("curl -sf http://localhost:6006/health 2>&1", check=False, timeout=5)
        out = result.stdout.strip() if result.stdout else ""
        ps_result = await conn.run("pgrep -f 'uvicorn api.main'", check=False, timeout=5)
        ps_out = ps_result.stdout.strip() if ps_result.stdout else ""
        print(f"   第{i+1}次: health={out[:60] or '无'}, PID={ps_out or '无'}")
        if out and "ok" in out.lower():
            break

    # 4. 查看日志
    print("\n4. FastAPI日志:")
    result = await conn.run("tail -15 /root/autodl-tmp/fastapi.log", check=False, timeout=5)
    print(result.stdout if result.stdout else "(空)")

    conn.close()
    await conn.wait_closed()
    print("\n完成!")

asyncio.run(main())
