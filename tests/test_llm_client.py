"""LLM客户端测试 — Mock模式下无需GPU"""
import pytest
from core.llm_client import LLMClient


@pytest.fixture
def mock_llm():
    return LLMClient(mock=True)


@pytest.mark.asyncio
async def test_mock_chat(mock_llm):
    """测试Mock模式基本调用"""
    messages = [{"role": "user", "content": "故宫的知识"}]
    result = await mock_llm.chat(messages)
    assert isinstance(result, str)
    assert len(result) > 0


@pytest.mark.asyncio
async def test_mock_knowledge_keyword(mock_llm):
    """测试Mock模式关键词匹配"""
    messages = [{"role": "user", "content": "查询故宫知识"}]
    result = await mock_llm.chat(messages)
    assert "故宫" in result


@pytest.mark.asyncio
async def test_mock_relation_keyword(mock_llm):
    """测试Mock模式关系关键词"""
    messages = [{"role": "user", "content": "查询关系"}]
    result = await mock_llm.chat(messages)
    assert "建于朝代" in result


@pytest.mark.asyncio
async def test_extract_json(mock_llm):
    """测试JSON提取"""
    # 直接JSON
    result = await mock_llm.extract_json('{"name": "故宫", "category": "宫殿"}', {})
    assert result == {"name": "故宫", "category": "宫殿"}

    # 代码块包裹
    result = await mock_llm.extract_json('```json\n{"name": "故宫"}\n```', {})
    assert result == {"name": "故宫"}

    # 非法JSON
    result = await mock_llm.extract_json("这不是JSON", {})
    assert result == {}
