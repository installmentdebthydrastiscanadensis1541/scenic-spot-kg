"""LLM客户端 — 统一接口，支持真实API / Mock模式

真实模式：连接 vLLM (AutoDL) / 本地vLLM
Mock模式：开发时不需要GPU，返回预设响应
"""
import re
import json
from typing import Any

from openai import AsyncOpenAI, BadRequestError
from config.settings import settings
from config.prompts import REACT_SYSTEM


class ContextLengthExceeded(Exception):
    """上下文长度超出模型限制"""
    pass


# ── Mock预设响应 ──
MOCK_RESPONSES = {
    "知识": "故宫又称紫禁城，位于北京中心，是明清两代的皇家宫殿，占地72万平方米，有大小宫殿七十多座。",
    "关系": "故宫-建于朝代->明朝, 故宫-位于->北京, 故宫-属于->世界文化遗产",
    "路线": "建议路线：午门→太和殿→中和殿→保和殿→乾清宫→御花园→神武门，全程约2小时。",
    "default": "这是一个关于中国景点的回答。故宫是中国最著名的景点之一。",
}


class LLMClient:
    """统一的LLM调用客户端"""

    # 模型最大上下文长度，需与vLLM启动参数max_model_len一致
    MAX_MODEL_LEN = 8192

    def __init__(self, mock: bool | None = None):
        self.mock = mock if mock is not None else settings.DEV_MOCK_LLM
        if not self.mock:
            self.client = AsyncOpenAI(
                base_url=settings.LLM_BASE_URL,
                api_key=settings.LLM_API_KEY,
                timeout=60.0,
            )

    async def chat(
        self,
        messages: list[dict],
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        guided_json: dict | None = None,
        guided_choice: list[str] | None = None,
        stream: bool = False,
    ) -> str | Any:
        """发送聊天请求

        Args:
            messages: OpenAI格式消息列表
            model: 模型名，默认用settings中的
            temperature: 温度
            max_tokens: 最大token数
            guided_json: vLLM结构化输出JSON Schema
            guided_choice: vLLM枚举选择约束
            stream: 是否流式输出

        Returns:
            模型生成的文本
        """
        if self.mock:
            return self._mock_response(messages)

        # 动态计算max_tokens，确保input+output不超模型上限
        estimated_input = self._estimate_tokens(messages)
        requested_max = max_tokens or settings.LLM_MAX_TOKENS
        # 预留256 token给输出，至少保证能生成Final Answer
        safe_max_tokens = max(self.MAX_MODEL_LEN - estimated_input - 10, 256)
        actual_max_tokens = min(requested_max, safe_max_tokens)

        kwargs: dict[str, Any] = {
            "model": model or settings.LLM_MODEL,
            "messages": messages,
            "temperature": temperature or settings.LLM_TEMPERATURE,
            "max_tokens": actual_max_tokens,
            "stream": stream,
        }

        # vLLM guided decoding参数
        extra_body = {}
        if guided_json:
            extra_body["guided_json"] = guided_json
        if guided_choice:
            extra_body["guided_choice"] = guided_choice
        if extra_body:
            kwargs["extra_body"] = extra_body

        if stream:
            return self._stream_response(kwargs)

        try:
            response = await self.client.chat.completions.create(**kwargs)
            return response.choices[0].message.content
        except BadRequestError as e:
            err_msg = str(e).lower()
            if "context length" in err_msg or "input_tokens" in err_msg or "maximum" in err_msg:
                raise ContextLengthExceeded(
                    f"当前上下文已超出模型限制（{self.MAX_MODEL_LEN} tokens），"
                    f"请开启新对话继续。"
                ) from e
            raise

    async def _stream_response(self, kwargs: dict) -> Any:
        """流式响应生成器"""
        stream = await self.client.chat.completions.create(**kwargs)
        async for chunk in stream:
            if chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    def _mock_response(self, messages: list[dict]) -> str:
        """Mock模式：根据输入关键词返回预设响应"""
        last_msg = messages[-1].get("content", "") if messages else ""

        for keyword, response in MOCK_RESPONSES.items():
            if keyword in last_msg:
                return response
        return MOCK_RESPONSES["default"]

    async def extract_json(self, text: str, schema: dict) -> dict:
        """从LLM输出中提取JSON，带兜底解析

        Args:
            text: LLM原始输出
            schema: 期望的JSON Schema

        Returns:
            解析后的字典
        """
        # 尝试直接解析
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 尝试提取```json ... ```代码块
        match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass

        # 尝试提取花括号内容
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass

        # 解析失败，返回空字典
        return {}

    def _estimate_tokens(self, messages: list[dict]) -> int:
        """估算消息的token数（中文约1.5字/token，英文约4字符/token）"""
        total_chars = 0
        for msg in messages:
            content = msg.get("content", "")
            # 粗略估算：中文字符数 + 英文单词数*1.3
            chinese_chars = sum(1 for c in content if '\u4e00' <= c <= '\u9fff')
            other_chars = len(content) - chinese_chars
            total_chars += chinese_chars * 1.5 + other_chars * 0.25
        return int(total_chars)
