"""telegram channel implementation."""

import asyncio
from dataclasses import dataclass
import logging
from pathlib import Path
from typing import Awaitable, Callable, Sequence

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from channel.base import Channel
from runtime.events import EventSource
from runtime.media import MessageAttachment
from utils.config import TelegramConfig

logger = logging.getLogger(__name__)


@dataclass
class TelegramEventSource(EventSource):
    """Source for Telegram-originated events."""

    _namespace = "platform-telegram"

    chat_id: str
    user_id: str | None = None
    thread_id: int | None = None

    def __str__(self) -> str:
        if self.thread_id is None:
            return f"{self._namespace}:{self.chat_id}"
        return f"{self._namespace}:{self.chat_id}/{self.thread_id}"

    @classmethod
    def from_string(cls, s: str) -> "TelegramEventSource":
        _, payload = s.split(":", 1)
        if "/" not in payload:
            return cls(chat_id=payload)

        chat_id, thread_id = payload.split("/", 1)
        return cls(chat_id=chat_id, thread_id=int(thread_id))

    @property
    def platform_name(self) -> str:
        return "telegram"


class TelegramChannel(Channel[TelegramEventSource]):
    """Telegram platform implementation using python-telegram-bot."""

    def __init__(self, config: TelegramConfig):
        self.config = config
        self._application: Application | None = None
        self._on_message: Callable[[str, TelegramEventSource], Awaitable[None]] | None = None
        self._stop_event: asyncio.Event | None = None
        self._shutdown_lock = asyncio.Lock()

    @property
    def platform_name(self) -> str:
        return "telegram"

    async def run(
        self,
        on_message: Callable[[str, TelegramEventSource], Awaitable[None]],
    ) -> None:
        """Run the Telegram long-polling loop until stop() is called."""
        self._on_message = on_message
        self._stop_event = asyncio.Event()
        self._application = Application.builder().token(self.config.bot_token).build()
        self._application.add_handler(MessageHandler(filters.TEXT, self._handle_text_message))

        await self._application.initialize()
        await self._application.start()

        if self._application.updater is None:
            raise RuntimeError("Telegram application was created without an updater")

        await self._application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        logger.info("Telegram channel started")

        try:
            await self._stop_event.wait()
        finally:
            await self._shutdown()

    async def reply(
        self,
        content: str,
        source: TelegramEventSource,
        attachments: Sequence[MessageAttachment] | None = None,
    ) -> None:
        """Send a reply to the Telegram chat represented by source."""
        if self._application is None:
            raise RuntimeError("Telegram channel is not running")

        kwargs = {}
        if source.thread_id is not None:
            kwargs["message_thread_id"] = source.thread_id

        for chunk in self._split_message(content) if content else []:
            await self._application.bot.send_message(
                chat_id=source.chat_id,
                text=chunk,
                **kwargs,
            )

        for attachment in attachments or ():
            await self._send_attachment(attachment, source, kwargs)

    async def _send_attachment(
        self,
        attachment: MessageAttachment,
        source: TelegramEventSource,
        kwargs: dict,
    ) -> None:
        if self._application is None:
            raise RuntimeError("Telegram channel is not running")

        path = Path(attachment.path)
        with path.open("rb") as f:
            if attachment.kind == "image":
                await self._application.bot.send_photo(
                    chat_id=source.chat_id,
                    photo=f,
                    filename=attachment.display_name,
                    **kwargs,
                )
            elif attachment.kind == "video":
                await self._application.bot.send_video(
                    chat_id=source.chat_id,
                    video=f,
                    filename=attachment.display_name,
                    supports_streaming=True,
                    **kwargs,
                )
            else:
                await self._application.bot.send_document(
                    chat_id=source.chat_id,
                    document=f,
                    filename=attachment.display_name,
                    **kwargs,
                )

    async def is_allowed(self, source: TelegramEventSource) -> bool:
        """Check whether a Telegram sender is allowed to use the bot."""
        if not self.config.allowed_user_ids:
            return True

        allowed_user_ids = {str(user_id) for user_id in self.config.allowed_user_ids}
        if source.user_id is not None and source.user_id in allowed_user_ids:
            return True

        return source.chat_id in allowed_user_ids

    async def stop(self) -> None:
        """Stop listening and cleanup resources."""
        if self._stop_event is not None:
            self._stop_event.set()
        else:
            await self._shutdown()

    async def _handle_text_message(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Convert Telegram text messages into channel messages."""
        del context

        message = update.effective_message
        chat = update.effective_chat
        user = update.effective_user
        if message is None or chat is None or message.text is None:
            return

        content = message.text.strip()
        if not content:
            return

        source = TelegramEventSource(
            chat_id=str(chat.id),
            user_id=str(user.id) if user is not None else None,
            thread_id=message.message_thread_id,
        )
        if not await self.is_allowed(source):
            logger.warning(
                "Rejected Telegram message from user %s in chat %s",
                source.user_id,
                source.chat_id,
            )
            return

        if self._on_message is None:
            logger.warning("Telegram message received before callback was registered")
            return

        try:
            await self._on_message(content, source)
        except Exception:
            logger.exception("Telegram message callback failed")
            await message.reply_text("Agent processing failed.")

    async def _shutdown(self) -> None:
        """Shutdown Telegram application resources."""
        async with self._shutdown_lock:
            application = self._application
            if application is None:
                return

            self._application = None
            try:
                if application.updater is not None and application.updater.running:
                    await application.updater.stop()
                if application.running:
                    await application.stop()
            finally:
                await application.shutdown()
                logger.info("Telegram channel stopped")

    @staticmethod
    def _split_message(content: str, limit: int = 4096) -> list[str]:
        """Split a Telegram message into API-sized chunks."""
        if not content:
            return [""]

        return [content[i : i + limit] for i in range(0, len(content), limit)]
