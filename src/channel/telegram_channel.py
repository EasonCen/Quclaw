"""telegram channel implementation."""

import asyncio
from dataclasses import dataclass
import logging
from pathlib import Path
from typing import Awaitable, Callable, Sequence

from telegram import Message, Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from channel.base import Channel, ChannelMessage
from runtime.events import EventSource
from runtime.media import MediaLoadError, MediaStore, MessageAttachment
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

    def __init__(
        self,
        config: TelegramConfig,
        media_store: MediaStore,
    ):
        self.config = config
        self.media_store = media_store
        self._application: Application | None = None
        self._on_message: Callable[
            [ChannelMessage[TelegramEventSource]],
            Awaitable[None],
        ] | None = None
        self._stop_event: asyncio.Event | None = None
        self._shutdown_lock = asyncio.Lock()

    @property
    def platform_name(self) -> str:
        return "telegram"

    async def run(
        self,
        on_message: Callable[[ChannelMessage[TelegramEventSource]], Awaitable[None]],
    ) -> None:
        """Run the Telegram long-polling loop until stop() is called."""
        self._on_message = on_message
        self._stop_event = asyncio.Event()
        self._application = Application.builder().token(self.config.bot_token).build()
        self._application.add_handler(
            MessageHandler(self._message_filter(), self._handle_message)
        )

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

    async def _handle_message(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Convert Telegram messages into channel messages."""
        del context

        message = update.effective_message
        chat = update.effective_chat
        user = update.effective_user
        if message is None or chat is None:
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
            attachments = await self._collect_attachments(message)
        except MediaLoadError as exc:
            logger.warning("Rejected Telegram media from %s: %s", source, exc)
            await message.reply_text(str(exc))
            return
        except Exception:
            logger.exception("Telegram media download failed")
            await message.reply_text("Telegram file download failed.")
            return

        content = (message.text or message.caption or "").strip()
        if not content and not attachments:
            return

        try:
            await self._on_message(
                ChannelMessage(
                    content=content,
                    source=source,
                    attachments=attachments,
                )
            )
        except Exception:
            logger.exception("Telegram message callback failed")
            await message.reply_text("Agent processing failed.")

    async def _collect_attachments(
        self,
        message: Message,
    ) -> list[MessageAttachment]:
        """Download supported Telegram media and return attachment metadata."""
        if message.photo:
            photo = message.photo[-1]
            return [
                await self._download_telegram_file(
                    file_id=photo.file_id,
                    file_size=photo.file_size,
                    filename="telegram-photo.jpg",
                    media_type="image/jpeg",
                    kind="image",
                    label="telegram photo",
                )
            ]

        if message.document is not None:
            document = message.document
            return [
                await self._download_telegram_file(
                    file_id=document.file_id,
                    file_size=document.file_size,
                    filename=document.file_name,
                    media_type=document.mime_type,
                    kind=None,
                    label="telegram document",
                )
            ]

        if message.video is not None:
            video = message.video
            return [
                await self._download_telegram_file(
                    file_id=video.file_id,
                    file_size=video.file_size,
                    filename=getattr(video, "file_name", None) or "telegram-video.mp4",
                    media_type=video.mime_type,
                    kind="video",
                    label="telegram video",
                )
            ]

        if message.audio is not None:
            audio = message.audio
            return [
                await self._download_telegram_file(
                    file_id=audio.file_id,
                    file_size=audio.file_size,
                    filename=audio.file_name,
                    media_type=audio.mime_type,
                    kind="audio",
                    label="telegram audio",
                )
            ]

        if message.voice is not None:
            voice = message.voice
            return [
                await self._download_telegram_file(
                    file_id=voice.file_id,
                    file_size=voice.file_size,
                    filename="telegram-voice.ogg",
                    media_type=voice.mime_type,
                    kind="audio",
                    label="telegram voice",
                )
            ]

        return []

    async def _download_telegram_file(
        self,
        *,
        file_id: str,
        file_size: int | None,
        filename: str | None,
        media_type: str | None,
        kind: str | None,
        label: str,
    ) -> MessageAttachment:
        """Download one Telegram file into the media store."""
        if self._application is None:
            raise RuntimeError("Telegram channel is not running")

        if file_size is not None:
            self.media_store.ensure_size_allowed(file_size, label=label)

        telegram_file = await self._application.bot.get_file(file_id)
        data = await telegram_file.download_as_bytearray()
        return await self.media_store.save_bytes(
            data,
            filename=filename,
            media_type=media_type,
            kind=kind,
            namespace="telegram",
        )

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

    @staticmethod
    def _message_filter():
        return (
            filters.TEXT
            | filters.PHOTO
            | filters.Document.ALL
            | filters.VIDEO
            | filters.AUDIO
            | filters.VOICE
        )
