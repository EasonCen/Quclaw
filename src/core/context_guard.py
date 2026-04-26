"""Context guard for proactive context window management."""
import json
from dataclasses import dataclass
from math import ceil
from typing import TYPE_CHECKING

from provider.llm.base import Message

from core.session_state import SessionState

if TYPE_CHECKING:
    from core.context import SharedContext
    from provider.llm.base import LLMProvider
    from core.session_state import SessionState


MAX_TOOL_RESULT_CHARS = 10000


def _stringify_token_input(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except TypeError:
        return str(value)


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return (
        0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
        or 0x20000 <= code <= 0x2A6DF
        or 0x2A700 <= code <= 0x2B73F
        or 0x2B740 <= code <= 0x2B81F
        or 0x2B820 <= code <= 0x2CEAF
    )


def _estimate_text_tokens(text: str) -> int:
    if not text:
        return 0

    cjk_chars = sum(1 for char in text if _is_cjk(char))
    non_cjk_chars = len(text) - cjk_chars
    return max(1, ceil(cjk_chars * 1.5 + non_cjk_chars / 4))


@dataclass
class ContextGuard:
    """Manages context window size with proactive compaction."""

    shared_context: "SharedContext"
    token_threshold: int
    max_tool_result_chars: int = MAX_TOOL_RESULT_CHARS

    def estimate_tokens(self, state: "SessionState") -> int:
        """Estimate token count for session state."""
        return sum(
            _estimate_text_tokens(_stringify_token_input(message))
            for message in state.build_messages()
        )

    async def check_and_compact(
        self,
        state: "SessionState",
        llm: "LLMProvider",
    ) -> "SessionState":
        """Check token count, compact if needed."""
        if self.estimate_tokens(state) <= self.token_threshold:
            return state

        original_messages = state.messages
        truncated_messages = self._truncate_large_tool_results(state.messages)
        if truncated_messages != state.messages:
            state.messages = truncated_messages

            if self.estimate_tokens(state) <= self.token_threshold:
                state.replace_messages(truncated_messages)
                return state

        try:
            return await self._compact_messages(state, llm)
        except Exception:
            state.messages = original_messages
            raise



    def _compress_message_count(self, state: "SessionState") -> int:
        """Calculate how many messages to compress."""
        messages = state.messages
        message_count = len(messages)
        min_recent_messages = 4
        if message_count <= min_recent_messages:
            return 0

        system_prompt = state.agent.agent_def.agent_md or ""
        system_tokens = _estimate_text_tokens(system_prompt)
        recent_token_budget = max(0, int(self.token_threshold * 0.6) - system_tokens)
        recent_tokens = 0
        keep_start = message_count - min_recent_messages

        for index in range(message_count - 1, -1, -1):
            token_count = _estimate_text_tokens(_stringify_token_input(messages[index]))
            kept_count = message_count - index
            if (
                kept_count >= min_recent_messages
                and recent_tokens + token_count > recent_token_budget
            ):
                keep_start = index + 1
                break
            recent_tokens += token_count
            keep_start = index

        keep_start = max(1, min(keep_start, message_count - min_recent_messages))
        while keep_start > 0 and messages[keep_start].get("role") == "tool":
            keep_start -= 1

        return keep_start


    def _truncate_large_tool_results(self, messages: list[Message]) -> list[Message]:
        """Truncate oversized tool results to reduce context size."""
        truncated_messages: list[Message] = []
        for message in messages:
            content = message.get("content")
            if (
                message.get("role") == "tool"
                and isinstance(content, str)
                and len(content) > self.max_tool_result_chars
            ):
                omitted_chars = len(content) - self.max_tool_result_chars
                truncated_message = dict(message)
                truncated_message["content"] = (
                    content[: self.max_tool_result_chars]
                    + f"\n\n[Tool result truncated: {omitted_chars} chars omitted]"
                )
                truncated_messages.append(truncated_message)
            else:
                truncated_messages.append(message)
        return truncated_messages

    def _serialize_messages_for_summary(self, messages: list[Message]) -> str:
        """Serialize messages to plain text for summarization."""
        chunks: list[str] = []
        for message in messages:
            role = str(message.get("role") or "unknown")
            content = str(message.get("content") or "")
            if tool_call_id := message.get("tool_call_id"):
                role = f"{role}({tool_call_id})"
            if tool_calls := message.get("tool_calls"):
                content = f"{content}\ntool_calls={_stringify_token_input(tool_calls)}"
            chunks.append(f"{role}: {content}".rstrip())
        return "\n\n".join(chunks)


    async def _compact_messages(
        self,
        state: "SessionState",
        llm: "LLMProvider",
    ) -> "SessionState":
        """Compact history by summarizing older messages."""
        compress_count = self._compress_message_count(state)
        if compress_count <= 0:
            state.replace_messages(self._truncate_large_tool_results(state.messages))
            return state

        messages_to_compress = state.messages[:compress_count]
        recent_messages = state.messages[compress_count:]
        serialized = self._serialize_messages_for_summary(messages_to_compress)
        max_summary_chars = max(
            120,
            min(self.max_tool_result_chars, self.token_threshold * 2),
        )
        if len(serialized) > max_summary_chars:
            head_chars = max_summary_chars // 2
            tail_chars = max_summary_chars - head_chars
            omitted_chars = len(serialized) - max_summary_chars
            serialized = (
                serialized[:head_chars]
                + f"\n\n[Compacted history truncated: {omitted_chars} chars omitted]\n\n"
                + serialized[-tail_chars:]
            )

        summary_prompt = (
            "Summarize the conversation so far. Keep it factual and concise. "
            "Focus on key decisions, facts, user preferences, open tasks, "
            "and tool results needed for future context.\n\n"
            f"{serialized}"
        )
        summary, _ = await llm.chat(
            [{"role": "user", "content": summary_prompt}],
            tools=None,
        )

        summary_messages: list[Message] = [
            {
                "role": "user",
                "content": f"[Previous conversation summary]\n{summary}",
            },
            {
                "role": "assistant",
                "content": "Understood, I have the context.",
            },
        ]
        state.replace_messages([*summary_messages, *recent_messages])
        return state
