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
        self._tools_used: list[str] = []  # 本轮调用的工具列表，用于可信度标签

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

    def _detect_time_sensitive_question(self, question: str) -> str | None:
        """检测是否为时效性问题（含"明天/今天/下周/本周末"等），返回提示；否则返回None。
        防止用四季通用模板回答明确的时效性问题（如泰山日出时间随季节变化）。
        """
        time_patterns = [
            r"明天", r"今天", r"后天", r"下周", r"本周末", r"这周末",
            r"现在", r"当前", r"本月", r"这个月", r"近期", r"马上",
        ]
        for pattern in time_patterns:
            if re.search(pattern, question):
                return (
                    "\n⏰ 【时效性问题提示 — 严禁用四季通用模板】"
                    "\n用户问题包含明确时间词，你必须调用 web_search 获取当前时效信息："
                    "\n1. 搜索当前季节的天气/温度/管控/限流信息"
                    "\n2. 搜索当前月份的日出时间/开放时间/特有坑点"
                    "\n3. 给出'倒推时间表'而非笼统建议（如几点出发→几点到中转点→几点到终点）"
                    "\n4. 引用景区官方建议时间"
                    "\n5. 补充当前季节特有的坑点（如雷阵雨/限流/温差）"
                    "\n❌ 严禁用'凌晨1-2点出发'这种四季通用的笼统建议回答明确的时效性问题"
                )
        return None

    def _detect_decision_question(self, question: str) -> str | None:
        """检测是否为决策类问题（A还是B选择），返回提示；否则返回None。
        防止LLM给出"两边都行"的废话式回答。
        """
        # A还是B / A和B选哪个 / A或B
        if re.search(r"(还是|或者|对比|选哪个|哪个好|哪个适合|哪个更)", question):
            # 进一步检测是否包含用户约束（恐高/老人/孩子/时间/预算）
            has_constraint = bool(re.search(r"(恐高|老人|孩子|小孩|孕妇|时间紧|预算|便宜|省钱|半天|一天)", question))
            constraint_hint = ""
            if has_constraint:
                constraint_hint = (
                    "\n⚠️ 检测到用户有明确约束条件——这是决策的唯一依据，必须基于此直接拍板"
                )
            return (
                f"\n🎯 【决策类问题提示 — 必须直接拍板，严禁和稀泥】"
                f"{constraint_hint}"
                "\n1. 直接拍板选一个，❌ 禁止'如果你能克服就去A，否则去B'这种重言式废话"
                "\n2. 点出被排除项的具体劣势（体验层面，不是笼统数据）"
                "\n3. 点出推荐项的精准卖点（独家功能，不是'稳固安全'这种废话）"
                "\n4. 理解用户约束的本质（恐高是生理本能，不是胆量小，不能用'克服'解决）"
                "\n⚠️ 用户要的是斩钉截铁的推荐，不是把选择题踢回给用户"
            )
        return None

    def _detect_emotional_boundary(self, question: str) -> str | None:
        """检测用户是否表达情感倾诉（孤独/悲伤/压力），返回人设边界提示；否则返回None。
        防止LLM越界进入"陪伴者/心理咨询师"角色，引发产品伦理风险。
        """
        emotional_patterns = [
            r"孤独", r"寂寞", r"难过", r"伤心", r"不开心", r"郁闷",
            r"陪我说", r"陪我聊", r"说说话", r"聊聊", r"压力大",
            r"心情不好", r"烦躁", r"焦虑", r"崩溃", r"想哭",
        ]
        for pattern in emotional_patterns:
            if re.search(pattern, question):
                return (
                    "\n🚧 【人设边界提示 — 严禁越界陪聊】"
                    "\n检测到用户表达情感倾诉，你必须守住'景点助手'边界："
                    "\n1. 温柔倾听，但不深入情感话题，❌ 严禁进入'心理咨询师'模式"
                    "\n2. 明确身份边界：'小景很愿意倾听，但我是您的景点助手'"
                    "\n3. 拉回景点功能：推荐适合散心的目的地（如洱海/成都大熊猫基地）"
                    "\n4. 用引导语让用户做选择，而非被动陪聊"
                    "\n⚠️ 严禁从'工具'变成'陪伴者'——这会引发用户情感依赖，有产品伦理风险"
                )
        return None

    def _detect_insufficient_info_for_planning(self, question: str) -> str | None:
        """检测行程规划请求是否缺少关键约束信息，返回反问提示；否则返回None。
        防止LLM在信息不足时自作主张直接给行程（如用户说"帮我安排云南"直接给2天行程）。
        """
        # 检测是否为行程规划请求
        planning_patterns = [
            r"帮我安排", r"帮我规划", r"安排一下", r"规划一下",
            r"行程", r"旅游攻略", r"怎么玩", r"游玩路线",
        ]
        is_planning = False
        for pattern in planning_patterns:
            if re.search(pattern, question):
                is_planning = True
                break
        if not is_planning:
            return None

        # 检测是否包含关键约束信息（天数/偏好/同行人/预算）
        has_days = bool(re.search(r"(\d+天|\d+日|几天|半天|一天|两天|三天|一周|几日)", question))
        has_preference = bool(re.search(r"(自然|人文|古镇|雪山|海|山|古城|美食|购物|休闲)", question))
        has_companion = bool(re.search(r"(老人|小孩|孩子|亲子|情侣|一个人|独自|单身|全家|家庭)", question))

        # 如果缺少天数信息，必须反问
        if not has_days:
            return (
                "\n❓ 【信息不足提示 — 必须先反问，严禁硬答】"
                "\n检测到行程规划请求但缺少关键约束（天数/偏好/同行人），你必须："
                "\n1. 先反问澄清：几天时间？自然还是人文？同行人有老人小孩吗？"
                "\n2. 同时给一个'参考路线'让用户有预期（如'先给您一个5日经典路线供参考'）"
                "\n3. 检查行程可行性：车程/抢票/体力消耗是否合理"
                "\n❌ 严禁自作主张直接给行程——用户没说几天你就给2天，这是'自作主张'不是'智能规划'"
                "\n❌ 严禁忽略抢票限制（如玉龙雪山需提前3天抢票，临时行程进不去）"
            )
        return None

    def _check_high_confidence_fact(self, question: str) -> str | None:
        """检测问题是否命中高置信度事实库，返回防护提示；否则返回None。
        防止LLM被低质信源污染，把真实存在的权威记载现象说成"文人想象"（信息反转）。
        """
        try:
            from data.scenic_data import HIGH_CONFIDENCE_FACTS
        except ImportError:
            return None

        for key, info in HIGH_CONFIDENCE_FACTS.items():
            # 关键词匹配：优先使用keywords字段，回退到key本身
            keywords = info.get("keywords", [key])
            matched = False
            for kw in keywords:
                if kw and kw in question:
                    matched = True
                    break

            if not matched:
                continue

            # 命中高置信度事实，构建防护提示
            confidence = info.get("confidence", 8)
            if confidence < 8:
                continue  # 仅对高置信度（8+）事实注入防护

            hint_parts = [
                f"\n🛡️ 【高置信度事实防护 — 命中知识库权威条目】",
                f"知识库条目：{key}（置信度{confidence}/10，来源：{info.get('source', '权威信源')}）",
                f"权威结论：{info.get('fact', '')}",
            ]
            # 补充详细属性
            for field in ("principle", "history", "status"):
                val = info.get(field)
                if val:
                    label = {"principle": "科学原理", "history": "历史记载", "status": "现状"}[field]
                    hint_parts.append(f"{label}：{val}")
            # 警告字段
            warning = info.get("warning")
            if warning:
                hint_parts.append(f"⚠️ {warning}")

            hint_parts.append(
                "👉 你的Final Answer必须以上述权威结论为准，"
                "严禁采纳web_search中与此冲突的低质信源（如自媒体否定说法）。"
            )
            return "\n".join(hint_parts)

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
        # 清空上一轮的图片URL缓存和工具调用记录
        self._image_urls = []
        self._tools_used = []
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
        # 检测并修复被截断的回答（max_tokens不足导致生成不完整）
        text = self._fix_truncated_answer(text)
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

        # 可信度标签注入：根据工具调用情况自动标注信源等级
        text = self._inject_credibility_tag(text)
        return text.strip()

    def _inject_credibility_tag(self, text: str) -> str:
        """根据本轮工具调用情况，在回答末尾注入可信度标签。
        知识库优先 > 网络搜索 > 无工具调用。
        """
        # 如果LLM已经手动标注了可信度标签，不重复添加
        if re.search(r'\[官方确认\]|\[多方参考\]|\[存在争议\]|\[暂无可靠信息\]', text):
            return text

        # 如果是上下文超限提示或简单问候，不加标签
        if text.startswith('[上下文超限]') or len(text) < 20:
            return text

        used = set(self._tools_used)
        has_kb = bool(used & {"knowledge_search", "graph_query"})  # 知识库工具
        has_web = bool(used & {"web_search", "web_fetch"})  # 网络工具

        if has_kb and not has_web:
            tag = "\n\n🟢 [官方确认] 本回答基于小景知识库（景区官方/文物局文献，经人工审核）"
        elif has_kb and has_web:
            tag = "\n\n🟡 [多方参考] 本回答综合知识库+网络信息，已交叉验证"
        elif has_web and not has_kb:
            tag = "\n\n🟡 [多方参考] 本回答信息来自网络搜索，建议出发前通过景区官方确认"
        else:
            # 无工具调用（简单问题直接回答），不加标签
            tag = ""

        if tag:
            text = text.rstrip() + tag
        return text

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

    def _fix_truncated_answer(self, text: str) -> str:
        """检测并修复被截断的回答（max_tokens不足导致生成不完整）。

        常见截断特征：
        - 句子以逗号/顿号/连接词结尾（如"游览约"、"都能让您"、"含"）
        - 行程小结不完整（"共X个景点，游览约"后面没了）
        - 列表项被截断（数字编号后无内容）
        - 标点符号不匹配（括号/引号未闭合）
        """
        if not text or len(text) < 20:
            return text

        text = text.rstrip()
        # 检测截断特征：以不完整的标点或连接词结尾
        truncation_patterns = [
            r'[，,、；;：:]$',          # 以逗号/顿号/分号/冒号结尾
            r'[约含共经由和与]$',         # 以连接词/介词结尾
            r'[的在为了]$',              # 以虚词结尾
            r'\d+$',                    # 以纯数字结尾（如"游览约8"）
            r'[（(]\d+$',              # 括号内数字结尾（如"游览约（8"）
            r'[一二三四五六七八九十]$',   # 以中文数字结尾
            r'[\(（][^）)]*$',          # 未闭合的括号
            r'["“][^"”]*$',            # 未闭合的引号
        ]

        is_truncated = False
        for pattern in truncation_patterns:
            if re.search(pattern, text):
                is_truncated = True
                break

        # 另一种截断：最后一句没有句号/感叹号/问号/emoji收尾
        last_char = text[-1] if text else ''
        # 句末标点：句号、感叹号、问号、换行
        is_ending_punct = last_char in '。！？!?\n'
        # emoji范围判断（避免构建超长字符串影响性能）
        is_emoji = last_char and (
            0x1F300 <= ord(last_char) <= 0x1FAFF
            or 0x2600 <= ord(last_char) <= 0x27BF  # 杂项符号（☀☁等）
        )
        if last_char and not is_ending_punct and not is_emoji and not last_char.isdigit():
            # 最后一个字符既不是句末标点，也不是emoji，可能是截断
            # 但要排除正常的引导语结尾（如"吗？""路线吗？"）
            if not re.search(r'[？?]\s*$', text):
                is_truncated = True

        if is_truncated:
            # 截断到最后一个完整句子（以句号/换行为分隔）
            # 找最后一个句号、换行或emoji的位置
            last_complete = max(
                text.rfind('。'),
                text.rfind('\n'),
                text.rfind('！'),
                text.rfind('？'),
            )
            if last_complete > len(text) * 0.5:  # 至少保留一半内容
                text = text[:last_complete + 1]
            # 添加截断提示
            text += '\n\n⚠️ （回答因长度限制被截断，如需完整信息请告诉我，我会分段为您解答）'

        return text

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

        # 首轮推理时，如果命中高置信度事实，注入防护提示（防信息反转）
        if state.iteration == 1 and not state.observations:
            hcf_hint = self._check_high_confidence_fact(state.question)
            if hcf_hint:
                parts.append(hcf_hint)

        # 首轮推理时，如果是时效性问题，注入时效提示（防四季通用模板）
        if state.iteration == 1 and not state.observations:
            time_hint = self._detect_time_sensitive_question(state.question)
            if time_hint:
                parts.append(time_hint)

        # 首轮推理时，如果是决策类问题，注入拍板提示（防和稀泥）
        if state.iteration == 1 and not state.observations:
            decision_hint = self._detect_decision_question(state.question)
            if decision_hint:
                parts.append(decision_hint)

        # 首轮推理时，如果检测到情感倾诉，注入人设边界提示（防越界陪聊）
        if state.iteration == 1 and not state.observations:
            emotional_hint = self._detect_emotional_boundary(state.question)
            if emotional_hint:
                parts.append(emotional_hint)

        # 首轮推理时，如果行程规划信息不足，注入反问提示（防自作主张硬答）
        if state.iteration == 1 and not state.observations:
            planning_hint = self._detect_insufficient_info_for_planning(state.question)
            if planning_hint:
                parts.append(planning_hint)

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
            # 记录工具调用（用于可信度标签）
            if tool_name not in self._tools_used:
                self._tools_used.append(tool_name)
            # 图片URL保护：image_search返回的URL缓存起来，供最终回答使用
            if tool_name == "image_search":
                urls = re.findall(r"图片链接:\s*(https?://[^\s\n]+)", result_str)
                self._image_urls.extend(urls[:5])
            return result_str
        except asyncio.TimeoutError:
            return f"工具 '{tool_name}' 执行超时（30秒），请简化问题后重试。"
        except Exception as e:
            return f"工具执行错误：{e}"
