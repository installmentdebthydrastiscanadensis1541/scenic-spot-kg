"""ReAct Agent框架 — Thought→Action→Observation循环

核心流程：
1. LLM分析问题，输出Thought和Action
2. 执行Action对应的工具
3. 将Observation反馈给LLM
4. 重复直到LLM输出Final Answer或达到最大迭代次数
"""
import re
from collections import deque
from dataclasses import dataclass, field
from typing import AsyncGenerator

from core.llm_client import LLMClient, ContextLengthExceeded
from config.settings import settings
from config.prompts import REACT_SYSTEM

# 简单问候/闲聊关键词，直接回答不走ReAct
DIRECT_ANSWER_PATTERNS = [
    r"^你(好|是谁|叫什么|是什么)",
    r"^(嗨|hi|hello|hey)",
    r"^(谢谢|感谢|thanks)",
    r"^(再见|拜拜|bye)",
]

# 景点相关关键词 — 涉及这些词必须走知识检索，避免幻觉
SCENIC_KEYWORDS = [
    "景点", "景区", "公园", "博物馆", "寺", "庙", "宫", "殿", "塔", "楼",
    "园林", "古城", "遗址", "陵", "墓", "山", "湖", "河", "江", "湾",
    "海滩", "峡谷", "瀑布", "温泉", "长城", "广场", "纪念", "故居",
    "路线", "攻略", "门票", "怎么去", "怎么走", "游览", "参观", "一日游",
    "介绍", "历史", "朝代", "建于", "年", "公里", "米", "小时",
    "广州", "北京", "西安", "上海", "杭州", "成都", "桂林", "南京",
    "重庆", "苏州", "黄山", "张家界", "丽江", "三亚", "拉萨", "敦煌",
    "武汉", "长沙", "厦门", "昆明", "大理", "深圳", "天津", "哈尔滨",
]

DIRECT_SYSTEM = "你是一个景点知识图谱助手，名叫小景。用中文简洁回答用户问题。注意：不要编造任何景点信息，如果用户问了景点相关问题但你没有确切数据，请说'让我帮你查一下'。"


@dataclass
class AgentState:
    """Agent状态，跟踪整个推理过程"""
    question: str
    thoughts: list[str] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)
    final_answer: str | None = None
    iteration: int = 0


class ConversationMemory:
    """对话记忆 — 保存最近N轮对话，支持追问"""

    def __init__(self, max_rounds: int = 10):
        self.max_rounds = max_rounds
        self.history: list[dict] = []  # [{"role": "user"/"assistant", "content": "..."}]

    def add_user(self, text: str):
        self.history.append({"role": "user", "content": text})
        self._trim()

    def add_assistant(self, text: str):
        self.history.append({"role": "assistant", "content": text})
        self._trim()

    def _trim(self):
        """保留最近N轮对话（1轮 = 1条user + 1条assistant）"""
        while len(self.history) > self.max_rounds * 2:
            self.history.pop(0)

    def get_context(self, max_chars: int = 1500) -> str:
        """生成对话上下文摘要，供Agent理解追问"""
        if not self.history:
            return ""

        # 从最近的对话开始拼接
        parts = []
        total = 0
        for msg in reversed(self.history):
            prefix = "用户" if msg["role"] == "user" else "小景"
            line = f"{prefix}：{msg['content']}"
            if total + len(line) > max_chars:
                break
            parts.append(line)
            total += len(line)

        parts.reverse()
        return "\n".join(parts)

    def clear(self):
        self.history.clear()


class ReActAgent:
    """ReAct循环Agent（支持对话记忆）"""

    def __init__(self, llm: LLMClient, tools: dict):
        self.llm = llm
        self.tools = tools
        self.max_iterations = settings.MAX_ITERATIONS
        self.memory = ConversationMemory(max_rounds=10)
        self._image_urls: list[str] = []  # image_search返回的图片URL，用于最终回答保护

    def _is_simple_question(self, question: str) -> bool:
        """判断是否为简单问候/闲聊，无需工具调用。
        涉及景点关键词的问题必须走知识检索，防止LLM编造。
        """
        q = question.strip().lower()
        for pattern in DIRECT_ANSWER_PATTERNS:
            if re.search(pattern, q):
                return True
        # 涉及景点关键词，必须走检索，不能直接回答
        for kw in SCENIC_KEYWORDS:
            if kw in question:
                return False
        # "平替/类似/有没有像"类问题必须走web_search，不能直接回答
        if re.search(r"(类似|平替|有没有像|有没有.*一样|差不多|相似)", question):
            return False
        return len(q) < 6  # 太短的问题也直接回答

    def _needs_web_search_first(self, question: str) -> str | None:
        """检测是否为必须先web_search的问题类型，返回建议的搜索词；否则返回None。
        防止LLM凭记忆编造景点（如把公路隧道说成火车体验）。
        """
        # 平替/类似类问题：极易幻觉，必须先搜索
        if re.search(r"(类似|平替|有没有像|有没有.*一样|相似|差不多)", question):
            # 提取核心关键词作为搜索词
            # 去掉常见疑问词，保留核心名词
            search_term = re.sub(r"(国内|中国|有没有|类似|平替|相似|差不多|一样|像|的|吗|呢|吧|啊|有哪些|景点的|地方)", " ", question)
            search_term = re.sub(r"\s+", " ", search_term).strip()
            if len(search_term) < 3:
                search_term = question  # fallback用原问题
            return f"国内 {search_term} 景点"
        return None

    def _is_followup(self, question: str) -> bool:
        """判断是否为追问（依赖上下文才能理解）"""
        followup_patterns = [
            r"^(那|还有|另外|还有呢|它|他|她|这个|那个|上面|刚才|之前|那个)",
            r"(附近|周边|那里|那边|这里|这边|周围|旁边)",
            r"^(还有|也要|再|能不能|可以|帮我|给我)",
            r"^(怎么去|怎么走|多远|多长|多久|多少钱|几点|什么时候)",
            r"^(好|行|可以|推荐|介绍一下|说说|讲讲)",
            r"^(比|跟|与|和).*(比|比比|区别|不同|对比)",
        ]
        for pattern in followup_patterns:
            if re.search(pattern, question):
                return True
        return False

    def _resolve_context(self, question: str) -> str:
        """将追问与对话历史拼接，形成完整问题"""
        context = self.memory.get_context()
        if not context:
            return question

        # 追问时拼接历史上下文
        if self._is_followup(question):
            # 提取最近提到的景点/城市关键词
            recent_topics = self._extract_recent_topics()
            if recent_topics:
                return f"[上下文：用户之前问了{recent_topics}]\n{question}"
            return f"[上下文]\n{context}\n[当前问题]\n{question}"

        return question

    def _extract_recent_topics(self) -> str:
        """从最近对话中提取景点/城市关键词"""
        try:
            from data.scenic_data import SCENIC_SPOTS
        except ImportError:
            return ""
        spot_names = {s["name"] for s in SCENIC_SPOTS}
        city_names = {s["city"] for s in SCENIC_SPOTS}
        all_keywords = spot_names | city_names

        found = []
        context = self.memory.get_context(max_chars=500)
        for kw in all_keywords:
            if kw in context:
                found.append(kw)

        return "、".join(found[-3:]) if found else ""

    async def run(self, question: str, use_memory: bool = True) -> AgentState:
        """执行Agent推理循环"""
        # 清空上一轮的图片URL缓存
        self._image_urls = []
        # 解析追问上下文
        resolved_question = self._resolve_context(question) if use_memory else question
        state = AgentState(question=resolved_question)

        # 记录用户消息到记忆
        if use_memory:
            self.memory.add_user(question)

        # 简单问题直接回答，不走ReAct
        if self._is_simple_question(question):
            messages = [{"role": "system", "content": DIRECT_SYSTEM}]
            # 加入对话历史让直接回答也能感知上下文
            if use_memory and self.memory.history:
                for msg in self.memory.history[:-1]:  # 不包含刚加的user消息
                    messages.append(msg)
            messages.append({"role": "user", "content": question})

            try:
                response = await self.llm.chat(messages)
            except ContextLengthExceeded:
                state.final_answer = "当前对话上下文已达上限，请点击左侧「+ 开启新对话」继续。"
                return state
            state.final_answer = self._clean_answer(response)

            if use_memory:
                self.memory.add_assistant(state.final_answer)
            return state

        while state.iteration < self.max_iterations:
            state.iteration += 1

            prompt = self._build_prompt(state)
            messages = [
                {"role": "system", "content": REACT_SYSTEM},
                {"role": "user", "content": prompt},
            ]

            try:
                response = await self.llm.chat(messages)
            except ContextLengthExceeded:
                state.final_answer = "当前对话上下文已达上限，请点击左侧「+ 开启新对话」继续。"
                if use_memory:
                    self.memory.add_assistant(state.final_answer)
                return state
            state.thoughts.append(response)

            # 解析Action
            action = self._parse_action(response)
            if action is None:
                # 尝试解析Final Answer
                final = self._parse_final_answer(response)
                if final:
                    state.final_answer = self._clean_answer(final)
                    if use_memory:
                        self.memory.add_assistant(state.final_answer)
                    return state
                # LLM直接回答（没有遵循ReAct格式），直接作为最终答案
                if "Action:" not in response:
                    state.final_answer = self._clean_answer(response)
                    if use_memory:
                        self.memory.add_assistant(state.final_answer)
                    return state
                continue

            state.actions.append(action)
            observation = await self._execute_tool(action)
            state.observations.append(observation)

        # 达到最大迭代，强制生成最终回答
        if not state.final_answer:
            messages = [
                {"role": "system", "content": REACT_SYSTEM},
                {"role": "user", "content": self._build_prompt(state)},
                {"role": "user", "content": "请根据已有信息给出最终回答，不要包含思考过程。"},
            ]
            try:
                raw = await self.llm.chat(messages)
            except ContextLengthExceeded:
                state.final_answer = "当前对话上下文已达上限，请点击左侧「+ 开启新对话」继续。"
                if use_memory:
                    self.memory.add_assistant(state.final_answer)
                return state
            state.final_answer = self._clean_answer(raw)

        if use_memory:
            self.memory.add_assistant(state.final_answer)
        return state

    async def run_stream(self, question: str, use_memory: bool = True) -> AsyncGenerator[str, None]:
        """流式输出：实时推送思考进度，再输出最终答案"""
        # 清空上一轮的图片URL缓存
        self._image_urls = []
        resolved_question = self._resolve_context(question) if use_memory else question
        state = AgentState(question=resolved_question)

        if use_memory:
            self.memory.add_user(question)

        # 简单问题直接流式回答
        if self._is_simple_question(question):
            messages = [{"role": "system", "content": DIRECT_SYSTEM}]
            if use_memory and self.memory.history:
                for msg in self.memory.history[:-1]:
                    messages.append(msg)
            messages.append({"role": "user", "content": question})
            try:
                response = await self.llm.chat(messages)
            except ContextLengthExceeded:
                yield "[上下文超限] 当前对话上下文已达上限，请点击左侧「+ 开启新对话」继续。"
                return
            answer = self._clean_answer(response)
            if use_memory:
                self.memory.add_assistant(answer)
            for char in answer:
                yield char
            return

        # ReAct循环 — 实时推送进度
        while state.iteration < self.max_iterations:
            state.iteration += 1

            prompt = self._build_prompt(state)
            messages = [
                {"role": "system", "content": REACT_SYSTEM},
                {"role": "user", "content": prompt},
            ]

            try:
                response = await self.llm.chat(messages)
            except ContextLengthExceeded:
                yield "[上下文超限] 当前对话上下文已达上限，请点击左侧「+ 开启新对话」继续。"
                return
            state.thoughts.append(response)

            action = self._parse_action(response)
            if action is None:
                final = self._parse_final_answer(response)
                if final:
                    state.final_answer = self._clean_answer(final)
                    if use_memory:
                        self.memory.add_assistant(state.final_answer)
                    for char in state.final_answer:
                        yield char
                    return
                if "Action:" not in response:
                    state.final_answer = self._clean_answer(response)
                    if use_memory:
                        self.memory.add_assistant(state.final_answer)
                    for char in state.final_answer:
                        yield char
                    return
                continue

            state.actions.append(action)

            # 推送当前步骤进度
            tool_label = action["tool"]
            tool_labels = {
                "knowledge_search": "检索知识库",
                "graph_query": "查询知识图谱",
                "route_plan": "规划游览路线",
                "web_search": "搜索互联网",
                "web_fetch": "抓取网页详情",
                "map_tool": "查询地图路线",
                "image_search": "搜索景点图片",
            }
            label = tool_labels.get(tool_label, tool_label)
            yield f"[进度] {label}中...\n"

            observation = await self._execute_tool(action)
            state.observations.append(observation)

        # 达到最大迭代，强制生成最终回答
        if not state.final_answer:
            messages = [
                {"role": "system", "content": REACT_SYSTEM},
                {"role": "user", "content": self._build_prompt(state)},
                {"role": "user", "content": "请根据已有信息给出最终回答，不要包含思考过程。"},
            ]
            try:
                raw = await self.llm.chat(messages)
            except ContextLengthExceeded:
                yield "[上下文超限] 当前对话上下文已达上限，请点击左侧「+ 开启新对话」继续。"
                return
            state.final_answer = self._clean_answer(raw)

        if use_memory:
            self.memory.add_assistant(state.final_answer)
        for char in state.final_answer:
            yield char

    def _clean_answer(self, text: str) -> str:
        """清理最终回答中的ReAct格式残留、幻觉标记和重复内容"""
        # 去掉 "Final Answer:" 前缀
        text = re.sub(r"^Final Answer:\s*", "", text, flags=re.IGNORECASE)
        # 去掉残留的 Thought/Action/Observation 行
        text = re.sub(r"Thought:.*", "", text)
        text = re.sub(r"Action:\s*\w+.*", "", text)
        text = re.sub(r"Action Input:.*", "", text)
        text = re.sub(r"Observation:.*", "", text)
        # 去掉LLM可能输出的提示标记
        text = re.sub(r"【.*?】", "", text)
        # 去掉markdown超链接残留（[text](url) → text），但不处理图片语法 ![]
        text = re.sub(r"(?<!!)\[([^\]]+)\]\([^)]+\)", r"\1", text)
        # 删除LLM输出的markdown图片语法行（如 ![](url)、![alt](url)）
        # LLM可能用反引号包裹URL导致格式错误，或编造假URL（如example.com）
        # 真实图片URL由image_search工具返回的self._image_urls保证，会在末尾统一追加
        text = re.sub(r"^[ \t]*!\[.*$", "", text, flags=re.MULTILINE)
        # 清理删除图片语法后残留的孤立反引号行
        text = re.sub(r"^[ \t]*`+[ \t]*$", "", text, flags=re.MULTILINE)
        # 去掉LLM编造的无URL图片占位符（如"!黄鹤楼夜景"，不以![开头的情况）
        text = re.sub(r"^[ \t]*![^\[\(][^\n]*$", "", text, flags=re.MULTILINE)
        # 检测并清理循环重复内容（如路线中A→B→C→A→B→C模式）
        text = self._deduplicate_loops(text)
        # 去掉重复空行
        text = re.sub(r"\n{2,}", "\n", text)
        # 图片URL保护：防止LLM编造或残留图片链接
        if self._image_urls:
            # 调用了image_search，有真实URL
            if "图片链接:" not in text:
                # 情况1：LLM完全删除了图片链接行 → 追加真实URL
                img_lines = [f"图片链接: {url}" for url in self._image_urls[:5]]
                text = text.rstrip() + "\n" + "\n".join(img_lines)
            else:
                # 情况2：LLM输出了"图片链接:"但URL可能是编造的
                # 提取text中所有图片链接URL，检查是否与工具返回的真实URL匹配
                text_img_urls = re.findall(r"图片链接:\s*(https?://[^\s\n]+)", text)
                real_url_set = set(self._image_urls)
                # 如果text中的URL都不在真实URL列表中，说明全是编造的
                has_real_url = any(u in real_url_set for u in text_img_urls)
                if not has_real_url:
                    # 删除所有编造的图片链接行，替换为真实URL
                    text = re.sub(r'^[ \t]*图片链接:\s*https?://[^\s\n]+[ \t]*$', '', text, flags=re.MULTILINE)
                    img_lines = [f"图片链接: {url}" for url in self._image_urls[:5]]
                    text = text.rstrip() + "\n" + "\n".join(img_lines)
        else:
            # 未调用image_search，但LLM可能凭记忆输出图片链接行 → 全部删除
            if "图片链接:" in text:
                text = re.sub(r'^[ \t]*图片链接:\s*https?://[^\s\n]+[ \t]*$', '', text, flags=re.MULTILINE)
                # 也清理可能残留的"来源:"行和"页面链接:"行（通常跟在图片链接后面）
                text = re.sub(r'^[ \t]*(来源|页面链接):\s*[^\n]+[ \t]*$', '', text, flags=re.MULTILINE)
        return text.strip()

    def _deduplicate_loops(self, text: str) -> str:
        """检测并清理循环重复的模式，常见于LLM生成路线时的幻觉

        例如 "惠州 → 惠深高速 → 惠州机场高速 → 惠州机场 → 惠州环城高速"
        重复多次的情况，只保留第一次出现。
        """
        # 对每行单独检查→分隔的路线中是否有重复片段
        lines = text.split('\n')
        cleaned = []
        for line in lines:
            if '→' in line or ' -> ' in line:
                sep = '→' if '→' in line else ' -> '
                parts = [p.strip() for p in line.split(sep)]
                # 检测重复：如果连续出现相同的片段模式，截断
                seen_patterns = []
                deduped = []
                for p in parts:
                    # 短片段（<=8字）作为模式检测单元
                    pattern = p if len(p) <= 8 else p[:8]
                    if pattern in seen_patterns:
                        # 发现循环，停止添加
                        break
                    seen_patterns.append(pattern)
                    deduped.append(p)
                if len(deduped) < len(parts):
                    # 有截断，添加省略标记
                    line = sep.join(deduped) + ' 等'
            cleaned.append(line)
        return '\n'.join(cleaned)

    def _build_prompt(self, state: AgentState) -> str:
        """构建包含历史推理过程的prompt（截断超长内容，确保不超模型上下文）"""
        # 模型max_model_len=8192，系统提示词约1500字符≈500token
        # 上下文充裕，但仍需截断避免多轮ReAct后膨胀
        MAX_QUESTION_CHARS = 400
        MAX_OBS_CHARS = 400
        MAX_THOUGHT_CHARS = 500

        q = state.question[:MAX_QUESTION_CHARS] if len(state.question) > MAX_QUESTION_CHARS else state.question
        parts = [f"问题：{q}"]

        # 首轮推理时，如果是平替/类似类问题，强制提示先web_search
        if state.iteration == 1 and not state.observations:
            search_hint = self._needs_web_search_first(state.question)
            if search_hint:
                parts.append(f"\n⚠️ 注意：这是「平替/类似」类问题，极易产生幻觉。")
                parts.append(f"你必须第一步调用 web_search 搜索「{search_hint}」，")
                parts.append(f"只用搜索返回的真实景点作答，禁止凭记忆编造景点或错误关联属性。")

        for i, (thought, action, obs) in enumerate(
            zip(state.thoughts, state.actions, state.observations)
        ):
            parts.append(f"\n--- 第{i+1}轮 ---")
            parts.append(thought[:MAX_THOUGHT_CHARS])
            obs_text = obs[:MAX_OBS_CHARS] if len(obs) > MAX_OBS_CHARS else obs
            parts.append(f"Observation: {obs_text}")

        if len(state.thoughts) > len(state.observations):
            parts.append(f"\n{state.thoughts[-1][:MAX_THOUGHT_CHARS]}")

        return "\n".join(parts)

    def _parse_action(self, text: str) -> dict | None:
        """从LLM输出中解析Action和Action Input"""
        action_match = re.search(r"Action:\s*(\w+)", text)
        input_match = re.search(r"Action Input:\s*(.+?)(?:\n|$)", text)

        if action_match:
            tool_name = action_match.group(1)
            tool_input = input_match.group(1).strip() if input_match else ""
            return {"tool": tool_name, "input": tool_input}
        return None

    def _parse_final_answer(self, text: str) -> str | None:
        """从LLM输出中解析Final Answer"""
        match = re.search(r"Final Answer:\s*(.+)", text, re.DOTALL)
        if match:
            return match.group(1).strip()
        return None

    async def _execute_tool(self, action: dict) -> str:
        """执行工具调用（带超时保护）"""
        import asyncio

        tool_name = action["tool"]
        tool_input = action["input"]

        if tool_name not in self.tools:
            return f"错误：工具 '{tool_name}' 不存在。可用工具：{list(self.tools.keys())}"

        tool = self.tools[tool_name]
        try:
            result = await asyncio.wait_for(tool.run(tool_input), timeout=30)
            result_str = str(result)
            # 图片URL保护：image_search返回的URL缓存起来，供最终回答使用
            if tool_name == "image_search":
                urls = re.findall(r"图片链接:\s*(https?://[^\s\n]+)", result_str)
                self._image_urls.extend(urls[:5])
            return result_str
        except asyncio.TimeoutError:
            return f"工具 '{tool_name}' 执行超时（30秒），请简化问题后重试。"
        except Exception as e:
            return f"工具执行错误：{e}"
