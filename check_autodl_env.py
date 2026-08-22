"""诊断AutoDL环境并启动FastAPI"""
import asyncio
import asyncssh

async def main():
    print("连接SSH...")
    conn = await asyncssh.connect(
        host="connect.bjb1.seetacloud.com",
        port=49122,
        username="root",
        password="8PUGAmYNn7Og",
        known_hosts=None,
    )

    # 检查环境
    print("== 环境检查 ==")
    for cmd in ["which python", "which python3", "which pip", "which pip3",
                "ls /root/miniconda3/bin/python* 2>/dev/null",
                "conda env list 2>/dev/null || echo 'no conda'",
                "ps aux | grep vllm | grep -v grep",
                "ls /root/autodl-tmp/scenic-agent/api/main.py"]:
        result = await conn.run(cmd, check=False)
        out = result.stdout.strip() if result.stdout else "(空)"
        print(f"  {cmd}: {out}")

    # 检查 start_all.sh 用什么命令启动
    print("\n== start_all.sh中的启动命令 ==")
    result = await conn.run("grep -n 'uvicorn\|python' /root/autodl-tmp/start_all.sh | head -10", check=False)
    print(result.stdout if result.stdout else "(无匹配)")

    conn.close()
    await conn.wait_closed()

asyncio.run(main())
