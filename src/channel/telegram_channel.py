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
from utils.config import TelegramBotConfig, TelegramConfig

logger = logging.getLogger(__name__)


def _looks_like_telegram_chat_id(value: str) -> bool:
    """Return whether a source path segment is an old-style Telegram chat id."""
    return value.lstrip("-").isdigit()


def _validate_telegram_source_bot_key(value: str) -> str:
    """Validate bot_key path segment from a Telegram source string."""
    bot_key = value.strip()
    if not bot_key:
        raise ValueError("Telegram bot_key must not be empty")
    if ":" in bot_key:
        raise ValueError("Telegram bot_key must not contain ':'")
    if bot_key.lstrip("-").isdigit():
        raise ValueError("Telegram bot_key must not be numeric")
    return bot_key


@dataclass
class TelegramEventSource(EventSource):
    """Source for Telegram-originated events."""

    _namespace = "platform-telegram"

    chat_id: str
    user_id: str | None = None
    thread_id: int | None = None
    bot_key: str | None = None

    def __str__(self) -> str:
        if self.bot_key is None:
            if self.thread_id is None:
                return f"{self._namespace}:{self.chat_id}"
            return f"{self._namespace}:{self.chat_id}/{self.thread_id}"

        if self.thread_id is None:
            return f"{self._namespace}:{self.bot_key}/{self.chat_id}"
        return f"{self._namespace}:{self.bot_key}/{self.chat_id}/{self.thread_id}"

    @classmethod
    def from_string(cls, s: str) -> "TelegramEventSource":
        _, payload = s.split(":", 1)
        parts = payload.split("/")
        if len(parts) == 1:
            return cls(chat_id=parts[0])

        if _looks_like_telegram_chat_id(parts[0]):
            if len(parts) != 2:
                raise ValueError(f"Invalid TelegramEventSource: {s}")
            return cls(chat_id=parts[0], thread_id=int(parts[1]))

        if len(parts) not in {2, 3}:
            raise ValueError(f"Invalid TelegramEventSource: {s}")

        bot_key = _validate_telegram_source_bot_key(parts[0])
        thread_id = int(parts[2]) if len(parts) == 3 else None
        return cls(chat_id=parts[1], thread_id=thread_id, bot_key=bot_key)

    @property
    def platform_name(self) -> str:
        return "telegram"


@dataclass
class _TelegramBotRuntime:
    """Runtime state for one configured Telegram bot."""

    bot_key: str
    config: TelegramBotConfig
    application: Application


class TelegramChannel(Channel[TelegramEventSource]):
    """Telegram platform implementation using python-telegram-bot."""

    def __init__(
        self,
        config: TelegramConfig,
        media_store: MediaStore,
    ):
        self.config = config
        self.media_store = media_store
        self._bot_configs = {
            bot_key: bot_config
            for bot_key, bot_config in config.normalized_bots.items()
            if bot_config.enabled
        }
        self._legacy_single_bot = not bool(config.bots)
        self._runtimes: dict[str, _TelegramBotRuntime] = {}
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
        """Run Telegram long-polling loops until stop() is called."""
        self._on_message = on_message
        self._stop_event = asyncio.Event()

        if not self._bot_configs:
            raise RuntimeError("Telegram channel has no enabled bot configs")

        try:
            for bot_key, bot_config in self._bot_configs.items():
                await self._start_bot(bot_key, bot_config)
            logger.info(
                "Telegram channel started with bot(s): %s",
                ", ".join(self._runtimes),
            )

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
        runtime = self._runtime_by_key(self._bot_key_for_source(source))

        kwargs = {}
        if source.thread_id is not None:
            kwargs["message_thread_id"] = source.thread_id

        for chunk in self._split_message(content) if content else []:
            await runtime.application.bot.send_message(
                chat_id=source.chat_id,
                text=chunk,
                **kwargs,
            )

        for attachment in attachments or ():
            await self._send_attachment(runtime.application, attachment, source, kwargs)

    async def _send_attachment(
        self,
        application: Application,
        attachment: MessageAttachment,
        source: TelegramEventSource,
        kwargs: dict,
    ) -> None:
        path = Path(attachment.path)
        with path.open("rb") as f:
            if attachment.kind == "image":
                await application.bot.send_photo(
                    chat_id=source.chat_id,
                    photo=f,
                    filename=attachment.display_name,
                    **kwargs,
                )
            elif attachment.kind == "video":
                await application.bot.send_video(
                    chat_id=source.chat_id,
                    video=f,
                    filename=attachment.display_name,
                    supports_streaming=True,
                    **kwargs,
                )
            else:
                await application.bot.send_document(
                    chat_id=source.chat_id,
                    document=f,
                    filename=attachment.display_name,
                    **kwargs,
                )

    async def is_allowed(self, source: TelegramEventSource) -> bool:
        """Check whether a Telegram sender is allowed to use the bot."""
        try:
            bot_config = self._bot_configs[self._bot_key_for_source(source)]
        except ValueError:
            return False
        except KeyError:
            return False

        if not bot_config.allowed_user_ids:
            return True

        allowed_user_ids = {str(user_id) for user_id in bot_config.allowed_user_ids}
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
        bot_key: str,
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
            bot_key=None if self._uses_default_source(bot_key) else bot_key,
        )
        if not await self.is_allowed(source):
            logger.warning(
                "Rejected Telegram message from user %s in chat %s for bot %s",
                source.user_id,
                source.chat_id,
                bot_key,
            )
            return

        if self._on_message is None:
            logger.warning("Telegram message received before callback was registered")
            return

        try:
            attachments = await self._collect_attachments(bot_key, message)
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
        bot_key: str,
        message: Message,
    ) -> list[MessageAttachment]:
        """Download supported Telegram media and return attachment metadata."""
        if message.photo:
            photo = message.photo[-1]
            return [
                await self._download_telegram_file(
                    bot_key=bot_key,
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
                    bot_key=bot_key,
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
                    bot_key=bot_key,
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
                    bot_key=bot_key,
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
                    bot_key=bot_key,
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
        bot_key: str,
        file_id: str,
        file_size: int | None,
        filename: str | None,
        media_type: str | None,
        kind: str | None,
        label: str,
    ) -> MessageAttachment:
        """Download one Telegram file into the media store."""
        runtime = self._runtime_by_key(bot_key)

        if file_size is not None:
            self.media_store.ensure_size_allowed(file_size, label=label)

        telegram_file = await runtime.application.bot.get_file(file_id)
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
            runtimes = list(self._runtimes.values())
            if not runtimes:
                return

            self._runtimes = {}
            for runtime in runtimes:
                try:
                    await self._shutdown_bot(runtime)
                except Exception:
                    logger.exception("Failed to stop Telegram bot %s", runtime.bot_key)
            logger.info("Telegram channel stopped")

    async def _start_bot(
        self,
        bot_key: str,
        bot_config: TelegramBotConfig,
    ) -> None:
        """Start polling for one configured Telegram bot."""
        application = Application.builder().token(bot_config.bot_token).build()

        async def handle_message(
            update: Update,
            context: ContextTypes.DEFAULT_TYPE,
            *,
            bot_key: str = bot_key,
        ) -> None:
            await self._handle_message(bot_key, update, context)

        application.add_handler(
            MessageHandler(self._message_filter(), handle_message)
        )

        initialized = False
        try:
            await application.initialize()
            initialized = True
            await application.start()

            if application.updater is None:
                raise RuntimeError(
                    "Telegram application was created without an updater"
                )

            await application.updater.start_polling(
                allowed_updates=Update.ALL_TYPES
            )
        except Exception:
            try:
                if application.updater is not None and application.updater.running:
                    await application.updater.stop()
                if application.running:
                    await application.stop()
            finally:
                if initialized:
                    await application.shutdown()
            raise

        self._runtimes[bot_key] = _TelegramBotRuntime(
            bot_key=bot_key,
            config=bot_config,
            application=application,
        )
        logger.info("Telegram bot %s started", bot_key)

    async def _shutdown_bot(self, runtime: _TelegramBotRuntime) -> None:
        """Shutdown one Telegram bot runtime."""
        application = runtime.application
        try:
            if application.updater is not None and application.updater.running:
                await application.updater.stop()
            if application.running:
                await application.stop()
        finally:
            await application.shutdown()
            logger.info("Telegram bot %s stopped", runtime.bot_key)

    def _runtime_by_key(self, bot_key: str) -> _TelegramBotRuntime:
        runtime = self._runtimes.get(bot_key)
        if runtime is None:
            raise RuntimeError(f"Telegram bot is not running: {bot_key}")
        return runtime

    def _bot_key_for_source(self, source: TelegramEventSource) -> str:
        """Resolve the configured bot key for a Telegram source."""
        if source.bot_key is not None:
            return source.bot_key
        if "default" in self._bot_configs:
            return "default"
        raise ValueError("Telegram source is missing bot_key")

    def _uses_default_source(self, bot_key: str) -> bool:
        """Return whether a bot should emit old-style Telegram source strings."""
        return bot_key == "default" or self._legacy_single_bot

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
