"""Abstract base class for channel implementations."""

from abc import ABC, abstractmethod
from typing import Callable, Awaitable, Generic, TypeVar, Any, Sequence

from runtime.events import EventSource
from runtime.media import MessageAttachment
from utils.config import Config


T = TypeVar("T", bound=EventSource)


class Channel(ABC, Generic[T]):
    """Abstract base for messaging platforms with EventSource-based context."""

    @property
    @abstractmethod
    def platform_name(self) -> str:
        """Platform identifier."""
        pass

    @property
    def allow_normal_completion(self) -> bool:
        """Whether run() may return without crashing the channel worker."""
        return False

    @abstractmethod
    async def run(self, on_message: Callable[[str, T], Awaitable[None]]) -> None:
        """Run the channel. Block until stop() is called."""

    @abstractmethod
    async def reply(
        self,
        content: str,
        source: T,
        attachments: Sequence[MessageAttachment] | None = None,
    ) -> None:
        """Reply to incoming message."""
        pass

    @abstractmethod
    async def is_allowed(self, source: T) -> bool:
        """Check if sender is whitelisted."""
        pass

    @abstractmethod
    async def stop(self) -> None:
        """Stop listening and cleanup resources."""
        pass




    @staticmethod
    def from_config(config: Config) -> list["Channel[Any]"]:
        """Create channel instances from configuration."""
        # Inline imports to avoid circular dependency
        from channel.telegram_channel import TelegramChannel
        from channel.discord_channel import DiscordChannel
        from channel.feishu_channel import FeishuChannel

        channels: list["Channel[Any]"] = []
        channel_config = config.channels
        if not channel_config.enabled:
            return []

        if channel_config.telegram and channel_config.telegram.enabled:
            channels.append(TelegramChannel(channel_config.telegram))

        if channel_config.discord and channel_config.discord.enabled:
            channels.append(DiscordChannel(channel_config.discord))

        if channel_config.feishu and channel_config.feishu.enabled:
            channels.append(FeishuChannel(channel_config.feishu))

        return channels
