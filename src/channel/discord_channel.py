"""Discord channel implementation."""

import asyncio
from dataclasses import dataclass
import logging
from typing import Awaitable, Callable

import discord

from channel.base import Channel
from core.events import EventSource
from utils.config import DiscordConfig

logger = logging.getLogger(__name__)


@dataclass
class DiscordEventSource(EventSource):
    """Source for Discord-originated events."""

    _namespace = "platform-discord"

    channel_id: str
    user_id: str | None = None
    guild_id: str | None = None

    def __str__(self) -> str:
        if self.user_id is None:
            return f"{self._namespace}:{self.channel_id}"
        return f"{self._namespace}:{self.channel_id}/{self.user_id}"

    @classmethod
    def from_string(cls, s: str) -> "DiscordEventSource":
        _, payload = s.split(":", 1)
        if "/" not in payload:
            return cls(channel_id=payload)

        channel_id, user_id = payload.split("/", 1)
        return cls(channel_id=channel_id, user_id=user_id)

    @property
    def platform_name(self) -> str:
        return "discord"


class DiscordChannel(Channel[DiscordEventSource]):
    """Discord platform implementation using discord.py."""

    def __init__(self, config: DiscordConfig):
        self.config = config
        intents = discord.Intents.default()
        intents.messages = True
        intents.dm_messages = True
        intents.message_content = True

        self._client = discord.Client(intents=intents)
        self._on_message: Callable[[str, DiscordEventSource], Awaitable[None]] | None = None
        self._shutdown_lock = asyncio.Lock()
        self._configure_handlers()

    @property
    def platform_name(self) -> str:
        return "discord"

    async def run(
        self,
        on_message: Callable[[str, DiscordEventSource], Awaitable[None]],
    ) -> None:
        """Run the Discord client until stop() is called."""
        self._on_message = on_message
        logger.info("Starting Discord channel")
        await self._client.start(self.config.bot_token)

    async def reply(self, content: str, source: DiscordEventSource) -> None:
        """Send a reply to the Discord channel represented by source."""
        channel = self._client.get_channel(int(source.channel_id))
        if channel is None:
            channel = await self._client.fetch_channel(int(source.channel_id))

        send = getattr(channel, "send", None)
        if send is None:
            raise RuntimeError(f"Discord channel {source.channel_id} cannot receive messages")

        for chunk in self._split_message(content):
            await send(chunk)

    async def is_allowed(self, source: DiscordEventSource) -> bool:
        """Check whether a Discord sender is allowed to use the bot."""
        if self.config.channel_id is not None and source.channel_id != str(self.config.channel_id):
            return False

        if not self.config.allowed_user_ids:
            return True

        return source.user_id in {str(user_id) for user_id in self.config.allowed_user_ids}

    async def stop(self) -> None:
        """Stop listening and cleanup resources."""
        async with self._shutdown_lock:
            if self._client.is_closed():
                return
            await self._client.close()
            logger.info("Discord channel stopped")

    def _configure_handlers(self) -> None:
        @self._client.event
        async def on_ready() -> None:
            user = self._client.user
            logger.info("Discord channel logged in as %s", user)

        @self._client.event
        async def on_message(message: discord.Message) -> None:
            await self._handle_message(message)

    async def _handle_message(self, message: discord.Message) -> None:
        """Convert Discord messages into channel messages."""
        if self._client.user is not None and message.author.id == self._client.user.id:
            return

        if message.author.bot:
            return

        content = message.content.strip()
        if not content:
            return

        source = DiscordEventSource(
            channel_id=str(message.channel.id),
            user_id=str(message.author.id),
            guild_id=str(message.guild.id) if message.guild is not None else None,
        )
        if not await self.is_allowed(source):
            logger.warning(
                "Rejected Discord message from user %s in channel %s",
                source.user_id,
                source.channel_id,
            )
            return

        if self._on_message is None:
            logger.warning("Discord message received before callback was registered")
            return

        try:
            await self._on_message(content, source)
        except Exception:
            logger.exception("Discord message callback failed")
            await message.channel.send("Agent processing failed.")

    @staticmethod
    def _split_message(content: str, limit: int = 2000) -> list[str]:
        """Split a Discord message into API-sized chunks."""
        if not content:
            return [""]

        return [content[i : i + limit] for i in range(0, len(content), limit)]
