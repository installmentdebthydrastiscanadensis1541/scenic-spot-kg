"""AutoDL自动管理 — 开机/启动服务/关机/余额检查

架构：FastAPI和vLLM都跑在AutoDL实例上，通过AutoDL端口映射(6006)暴露到公网。
此管理器运行在"开机触发器"端（Cloudflare Worker或本地），负责：
  1. 调AutoDL API开机
  2. SSH连接执行 start_all.sh 启动vLLM+FastAPI
  3. 等待公网地址可用
  4. 空闲超时后调API关机
"""
import asyncio
import json
import os
import time
from enum import Enum
from typing import Optional

import httpx

# asyncssh为可选依赖
try:
    import asyncssh
    _HAS_ASYNCSSH = True
except ImportError:
    _HAS_ASYNCSSH = False


class AutoDLStatus(str, Enum):
    OFFLINE = "offline"           # 实例未开机
    STARTING = "starting"         # 正在开机/启动服务
    READY = "ready"              # 公网服务已就绪
    ERROR = "error"              # 出错
    LOW_BALANCE = "low_balance"  # 余额不足
    GPU_BUSY = "gpu_busy"        # GPU被占用，无法开机


class AutoDLManager:
    """AutoDL实例自动管理

    配置项（环境变量 / .env）：
        AUTODL_API_TOKEN         — AutoDL Authorization Token（浏览器localStorage获取）
        AUTODL_INSTANCE_ID       — 实例UUID
        AUTODL_SSH_HOST          — SSH主机，如 connect.bjb1.seetacloud.com
        AUTODL_SSH_PORT          — SSH端口，如 49122
        AUTODL_SSH_PASSWORD      — SSH密码
        AUTODL_PUBLIC_URL        — AutoDL端口映射的公网地址（6006端口对应的URL）
        AUTODL_BALANCE_THRESHOLD — 余额不足阈值（元），默认5
        AUTODL_IDLE_TIMEOUT      — 空闲自动关机秒数，默认600
    """

    API_BASE = "https://www.autodl.com/api/v1"

    def __init__(self):
        self.api_token = os.getenv("AUTODL_API_TOKEN", "")
        self.instance_id = os.getenv("AUTODL_INSTANCE_ID", "")
        self.ssh_host = os.getenv("AUTODL_SSH_HOST", "")
        self.ssh_port = int(os.getenv("AUTODL_SSH_PORT", "0"))
        self.ssh_password = os.getenv("AUTODL_SSH_PASSWORD", "")
        self.public_url = os.getenv("AUTODL_PUBLIC_URL", "")  # 如 https://xxxxx.autodl.pro
        self.balance_threshold = float(os.getenv("AUTODL_BALANCE_THRESHOLD", "5"))
        self.idle_timeout = int(os.getenv("AUTODL_IDLE_TIMEOUT", "600"))

        self._status = AutoDLStatus.OFFLINE
        self._ssh_conn: Optional[object] = None
        self._last_active = 0.0
        self._idle_task: Optional[asyncio.Task] = None
        self._starting_task: Optional[asyncio.Task] = None
        self._error_msg = ""

    # ── 公开接口 ──

    @property
    def status(self) -> AutoDLStatus:
        return self._status

    def get_status_info(self) -> dict:
        """获取当前状态信息"""
        messages = {
            AutoDLStatus.OFFLINE: "AutoDL实例未开机，发送消息将自动启动",
            AutoDLStatus.STARTING: "正在启动AutoDL实例，预计需要2-5分钟...",
            AutoDLStatus.READY: "服务已就绪",
            AutoDLStatus.ERROR: f"启动出错：{self._error_msg}",
            AutoDLStatus.LOW_BALANCE: "AutoDL余额不足，请充值后再试",
            AutoDLStatus.GPU_BUSY: "GPU资源繁忙，暂时无法开机，请稍后再试",
        }
        return {
            "status": self._status.value,
            "instance_id": self.instance_id,
            "public_url": self.public_url,
            "idle_seconds": int(time.time() - self._last_active) if self._status == AutoDLStatus.READY else 0,
            "message": messages.get(self._status, ""),
        }

    async def ensure_running(self) -> AutoDLStatus:
        """确保AutoDL实例和服务正在运行"""
        if self._status == AutoDLStatus.READY:
            self._last_active = time.time()
            return AutoDLStatus.READY

        if self._status == AutoDLStatus.STARTING:
            if self._starting_task:
                try:
                    await asyncio.wait_for(asyncio.shield(self._starting_task), timeout=300)
                except asyncio.TimeoutError:
                    pass
            return self._status

        self._starting_task = asyncio.create_task(self._full_startup())
        try:
            await asyncio.wait_for(asyncio.shield(self._starting_task), timeout=300)
        except asyncio.TimeoutError:
            pass
        return self._status

    def touch(self):
        """更新活跃时间"""
        self._last_active = time.time()

    async def shutdown(self):
        """关闭AutoDL实例"""
        print("[AutoDL] 正在关闭...")

        if self._ssh_conn:
            try:
                self._ssh_conn.close()
            except Exception:
                pass
            self._ssh_conn = None

        if self.api_token and self.instance_id:
            try:
                await self._api_post("/instance/power_off", {"instance_uuid": self.instance_id})
                print("[AutoDL] 实例关机指令已发送")
            except Exception as e:
                print(f"[AutoDL] 关机API调用失败: {e}")

        self._status = AutoDLStatus.OFFLINE

    def is_configured(self) -> bool:
        """检查是否配置了AutoDL管理所需参数"""
        return bool(self.api_token and self.instance_id)

    def needs_ssh(self) -> bool:
        """是否需要SSH来启动服务（本地模式）"""
        return bool(self.ssh_host and self.ssh_port)

    # ── 完整启动流程 ──

    async def _full_startup(self):
        """完整启动：查余额 → 开机 → 等就绪 → SSH启动服务 → 等公网可用"""
        self._status = AutoDLStatus.STARTING
        self._error_msg = ""

        try:
            # 1. 检查余额
            balance = await self._check_balance()
            if 0 <= balance < self.balance_threshold:
                self._status = AutoDLStatus.LOW_BALANCE
                print(f"[AutoDL] 余额不足: ¥{balance:.2f}（阈值 ¥{self.balance_threshold}）")
                return

            # 2. 开机
            print("[AutoDL] 正在开机...")
            ok = await self._power_on()
            if not ok:
                if self._status != AutoDLStatus.GPU_BUSY:
                    self._error_msg = "开机失败，请检查实例ID和Token"
                    self._status = AutoDLStatus.ERROR
                return

            # 3. 等待实例就绪
            print("[AutoDL] 等待实例启动...")
            ok = await self._wait_instance_ready(timeout=180)
            if not ok:
                self._error_msg = "实例启动超时（3分钟）"
                self._status = AutoDLStatus.ERROR
                return

            # 4. SSH连接并启动服务（vLLM + FastAPI）
            print("[AutoDL] SSH连接并启动服务...")
            ok = await self._ssh_start_services()
            if not ok:
                self._error_msg = "SSH启动服务失败"
                self._status = AutoDLStatus.ERROR
                return

            # 5. 等待公网地址可用
            if self.public_url:
                print(f"[AutoDL] 等待公网地址可用: {self.public_url}")
                ok = await self._wait_public_ready(timeout=120)
                if not ok:
                    self._error_msg = "公网地址超时未响应"
                    self._status = AutoDLStatus.ERROR
                    return

            # 6. 成功
            self._status = AutoDLStatus.READY
            self._last_active = time.time()
            print("[AutoDL] 服务已就绪！")

            # 启动空闲监控
            if self._idle_task and not self._idle_task.done():
                self._idle_task.cancel()
            self._idle_task = asyncio.create_task(self._idle_monitor())

        except Exception as e:
            self._error_msg = str(e)
            self._status = AutoDLStatus.ERROR
            print(f"[AutoDL] 启动异常: {e}")

    # ── AutoDL API ──

    async def _api_post(self, path: str, body: dict) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self.API_BASE}{path}",
                headers={
                    "Authorization": self.api_token,
                    "Content-Type": "application/json;charset=UTF-8",
                },
                data=json.dumps(body),
            )
            return resp.json()

    async def _check_balance(self) -> float:
        """查询余额，失败返回-1"""
        if not self.api_token:
            return 999
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{self.API_BASE}/user/balance",
                    headers={"Authorization": self.api_token},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data.get("data"), dict):
                        return float(data["data"].get("balance", 0))
                    elif isinstance(data.get("data"), (int, float, str)):
                        return float(data["data"])
                    return float(data.get("balance", 0))
        except Exception as e:
            print(f"[AutoDL] 余额查询失败: {e}")
        return -1

    async def _power_on(self) -> bool:
        """开机实例，检测余额不足和GPU繁忙"""
        try:
            result = await self._api_post("/instance/power_on", {
                "instance_uuid": self.instance_id,
            })
            code = result.get("code", "")
            if code == "Success":
                return True
            # 检测GPU繁忙（常见返回码/消息）
            msg = str(result.get("msg", "") or result.get("message", ""))
            if any(kw in msg for kw in ("GPU", "显卡", "资源", "空闲", "不足", "繁忙", "busy", "no available", "sold out")):
                self._status = AutoDLStatus.GPU_BUSY
                print(f"[AutoDL] GPU繁忙: {msg}")
            else:
                print(f"[AutoDL] 开机失败: code={code}, msg={msg}")
            return False
        except Exception as e:
            print(f"[AutoDL] 开机失败: {e}")
            return False

    async def _wait_instance_ready(self, timeout: int = 180) -> bool:
        start = time.time()
        while time.time() - start < timeout:
            try:
                result = await self._api_post("/instance", {
                    "date_from": "", "date_to": "",
                    "page_index": 1, "page_size": 100,
                    "status": [], "charge_type": [],
                })
                if result.get("code") == "Success":
                    for inst in result.get("data", {}).get("list", []):
                        if inst.get("uuid") == self.instance_id:
                            if inst.get("status") in ("running", "Running", "使用中"):
                                return True
            except Exception:
                pass
            await asyncio.sleep(10)
        return False

    # ── SSH操作 ──

    async def _ssh_start_services(self) -> bool:
        """SSH连接 → 执行start_all.sh → 启动vLLM+FastAPI"""
        if not _HAS_ASYNCSSH:
            print("[AutoDL] asyncssh未安装。pip install asyncssh")
            return False
        if not self.ssh_host or not self.ssh_port:
            print("[AutoDL] 未配置SSH，跳过服务启动（假设服务已自动启动）")
            return True

        # SSH连接（重试6次，每次等10秒）
        for attempt in range(6):
            try:
                self._ssh_conn = await asyncssh.connect(
                    host=self.ssh_host,
                    port=self.ssh_port,
                    username="root",
                    password=self.ssh_password,
                    known_hosts=None,
                )
                print("[AutoDL] SSH连接成功")
                break
            except Exception as e:
                if attempt < 5:
                    print(f"[AutoDL] SSH连接失败（第{attempt + 1}次），10秒后重试: {e}")
                    await asyncio.sleep(10)
                else:
                    print(f"[AutoDL] SSH连接最终失败: {e}")
                    return False

        try:
            # 执行一键启动脚本
            result = await self._ssh_conn.run(
                "bash /root/autodl-tmp/start_all.sh",
                check=False,
                timeout=600,  # 最多等10分钟
            )
            if result.exit_status == 0:
                print("[AutoDL] start_all.sh 执行成功")
            else:
                print(f"[AutoDL] start_all.sh 返回非零: {result.stderr[-500:] if result.stderr else 'unknown'}")
                # 不直接return False，继续尝试等待

            return True

        except Exception as e:
            print(f"[AutoDL] SSH执行失败: {e}")
            return False

    # ── 等待公网可用 ──

    async def _wait_public_ready(self, timeout: int = 120) -> bool:
        """等待公网地址的/health接口响应"""
        if not self.public_url:
            return True  # 没配置公网地址则跳过
        start = time.time()
        while time.time() - start < timeout:
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    resp = await client.get(f"{self.public_url}/health")
                    if resp.status_code == 200:
                        return True
            except Exception:
                pass
            await asyncio.sleep(5)
        return False

    # ── 空闲监控 ──

    async def _idle_monitor(self):
        """空闲超时自动关机"""
        while True:
            await asyncio.sleep(60)
            if self._status != AutoDLStatus.READY:
                return
            idle = time.time() - self._last_active
            if idle >= self.idle_timeout:
                print(f"[AutoDL] 空闲{int(idle)}秒，自动关机")
                await self.shutdown()
                return


# ── 全局单例 ──
autodl_manager = AutoDLManager()
