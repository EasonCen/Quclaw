"""Base tool interface and decorator."""

import asyncio

from abc import ABC, abstractmethod
from typing import Any, TYPE_CHECKING
from collections.abc import Awaitable, Callable
from provider.llm.base import LLMToolCall

if TYPE_CHECKING:
    from core.agent import AgentSession

ToolFunc = Callable[..., str | Awaitable[str]]


def tool_call_to_message(tool_call: LLMToolCall) -> dict[str, Any]:
    """Convert a tool call into the message shape expected by the LLM API."""
    return {
        "id": tool_call.id,
        "type": "function",
        "function": {
            "name": tool_call.name,
            "arguments": tool_call.arguments,
        },
    }


class BaseTool(ABC):
    """Abstract base class for all tools."""

    name: str
    description: str
    parameters: dict[str, Any]

    @abstractmethod
    async def execute(self, session: "AgentSession", **kwargs: Any) -> str:
        """Execute the tool."""
    def get_tool_schema(self) -> dict[str, Any]:
        """Get the tool/function schema for LLM."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
    
def tool(name: str, description: str, parameters: dict[str, Any])-> Callable[[ToolFunc], "FunctionTool"]:
    """Decorator to register a function as a tool."""

    def decorator(func: Callable) -> "FunctionTool":
        return FunctionTool(name, description, parameters, func)
    
    return decorator
    

class FunctionTool(BaseTool):
    """A tool created from a function using the @tool decorator."""

    def __init__(
        self,
        name:str,
        description: str,
        parameters: dict[str, Any],
        func: ToolFunc,
    ):
        self.name = name
        self.description = description
        self.parameters = parameters
        self._func = func

    async def execute(self, session: "AgentSession", **kwargs: Any)->str:
        """Execute the underlying function."""
        result = self._func(session=session, **kwargs)
        if asyncio.iscoroutine(result):
            result = await result

        return str(result)
