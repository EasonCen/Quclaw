"""Base LLM provider abstraction."""
from dataclasses import dataclass
from types import SimpleNamespace
from openai import AsyncOpenAI


from typing import Any, Optional, TYPE_CHECKING, TypeAlias

if TYPE_CHECKING:
    from utils.config import LLMConfig

Message: TypeAlias = dict[str, Any]

@dataclass(slots=True)
class LLMToolCall:
    """A tool/function call from the LLM."""

    id: str
    name: str
    arguments: str #JSON string

class LLMProvider:
    """LLM provider using openai support"""

    def __init__(
            self,
            model: str,
            api_key: str,
            base_url: Optional[str] = None,
            temperature: float = 0.7,
            max_tokens: int = 2333,
            **kwargs: Any,
    ):
        """Initialize LLM provider"""
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url, **kwargs)

    @classmethod
    def from_config(cls, config: "LLMConfig") -> "LLMProvider":
        """Create provider from LLMConfig."""
        return cls(
            model=config.model,
            api_key=config.api_key,
            base_url=config.base_url,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )
    
    async def chat(
        self,
        messages: list[Message],
        tools: Optional[list[dict[str, Any]]] = None,
        **kwargs:Any
    ) -> tuple[str, list[LLMToolCall]]:
        """Default implementation. Subclasses can override."""
        params: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            **kwargs,
        }
        if tools:
            params["tools"] = tools

        completion = await self.client.chat.completions.create(**params)
        message = self._extract_message(completion)
        tool_calls = [
            LLMToolCall(
                id=tool_call.id,
                name=tool_call.function.name,
                arguments=tool_call.function.arguments,
            )
            for tool_call in (message.tool_calls or [])
        ]
        return message.content or "", tool_calls

    @staticmethod
    def _extract_message(completion: Any) -> Any:
        """Extract assistant message from OpenAI-compatible responses."""
        if isinstance(completion, str):
            raise ValueError(
                "LLM API returned plain text instead of an OpenAI chat completion. "
                "Check llm.model/base_url/api_key in config.user.yaml."
            )

        if isinstance(completion, dict):
            try:
                return LLMProvider._message_from_dict(completion["choices"][0]["message"])
            except (KeyError, IndexError, TypeError) as e:
                raise ValueError(
                    "LLM API response is not OpenAI chat-completions compatible."
                ) from e

        choices = getattr(completion, "choices", None)
        if not choices:
            raise ValueError("LLM API response does not contain choices.")

        return choices[0].message

    @staticmethod
    def _message_from_dict(message: dict[str, Any]) -> SimpleNamespace:
        """Convert dict message data into OpenAI SDK-like attributes."""
        tool_calls = [
            SimpleNamespace(
                id=tool_call.get("id", ""),
                function=SimpleNamespace(
                    name=tool_call.get("function", {}).get("name", ""),
                    arguments=tool_call.get("function", {}).get("arguments", "{}"),
                ),
            )
            for tool_call in (message.get("tool_calls") or [])
        ]
        return SimpleNamespace(
            content=message.get("content"),
            tool_calls=tool_calls,
        )
