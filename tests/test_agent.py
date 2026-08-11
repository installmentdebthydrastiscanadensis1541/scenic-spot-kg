"""Agent框架测试 — Mock模式下测试ReAct循环"""
import pytest
from core.llm_client import LLMClient
from core.agent import ReActAgent
from tools.graph_query import GraphQueryTool
from tools.route_plan import RoutePlanTool


@pytest.fixture
def agent():
    llm = LLMClient(mock=True)
    tools = {
        "knowledge_search": None,  # 简化测试，不需要真实嵌入模型
        "graph_query": GraphQueryTool(use_neo4j=False),
        "route_plan": RoutePlanTool(),
    }
    return ReActAgent(llm=llm, tools=tools)


def test_agent_state_init():
    """测试AgentState初始化"""
    from core.agent import AgentState
    state = AgentState(question="故宫有什么？")
    assert state.question == "故宫有什么？"
    assert state.thoughts == []
    assert state.final_answer is None
    assert state.iteration == 0


def test_parse_action():
    """测试Action解析"""
    agent_obj = ReActAgent(llm=None, tools={})

    text = "Thought: 需要查询知识\nAction: knowledge_search\nAction Input: 故宫"
    result = agent_obj._parse_action(text)
    assert result == {"tool": "knowledge_search", "input": "故宫"}


def test_parse_final_answer():
    """测试Final Answer解析"""
    agent_obj = ReActAgent(llm=None, tools={})

    text = "Thought: 已有足够信息\nFinal Answer: 故宫是明清皇家宫殿"
    result = agent_obj._parse_final_answer(text)
    assert result == "故宫是明清皇家宫殿"


def test_parse_no_action():
    """测试无Action时的解析"""
    agent_obj = ReActAgent(llm=None, tools={})

    result = agent_obj._parse_action("这是一段普通文本")
    assert result is None
