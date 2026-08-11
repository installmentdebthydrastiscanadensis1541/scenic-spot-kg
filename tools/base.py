"""工具基类 — MCP协议规范

所有工具继承BaseTool，实现run方法。
工具元数据（name/description/parameters）遵循MCP工具描述规范。
"""
from abc import ABC, abstractmethod
from pydantic import BaseModel, Field


class ToolParameter(BaseModel):
    """工具参数描述"""
    name: str
    type: str = "string"
    description: str = ""
    required: bool = True


class BaseTool(ABC):
    """工具基类，所有MCP工具必须继承"""

    name: str = ""
    description: str = ""
    parameters: list[ToolParameter] = []

    @abstractmethod
    async def run(self, input_str: str) -> str:
        """执行工具

        Args:
            input_str: 工具输入（字符串形式）

        Returns:
            工具执行结果（字符串形式）
        """
        ...

    def to_mcp_spec(self) -> dict:
        """转换为MCP工具描述规范"""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": {
                "type": "object",
                "properties": {
                    p.name: {"type": p.type, "description": p.description}
                    for p in self.parameters
                },
                "required": [p.name for p in self.parameters if p.required],
            },
        }
