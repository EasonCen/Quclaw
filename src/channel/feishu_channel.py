"""Feishu channel implementation."""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Sequence

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from channel.base import Channel
from runtime.events import EventSource
from runtime.media import MessageAttachment
from utils.config import FeishuConfig

logger = logging.getLogger(__name__)


@dataclass
class FeishuEventSource(EventSource):
    """Source for Feishu-originated events."""

    _namespace = "platform-feishu"

    chat_id: str
    user_id: str | None = None
    message_id: str | None = None

    def __str__(self) -> str:
        if self.user_id is None:
            return f"{self._namespace}:{self.chat_id}"
        return f"{self._namespace}:{self.chat_id}/{self.user_id}"

    @classmethod
    def from_string(cls, s: str) -> "FeishuEventSource":
        _, payload = s.split(":", 1)
        if "/" not in payload:
            return cls(chat_id=payload)

        chat_id, user_id = payload.split("/", 1)
        return cls(chat_id=chat_id, user_id=user_id)

    @property
    def platform_name(self) -> str:
        return "feishu"


class FeishuChannel(Channel[FeishuEventSource]):
    """Feishu implementation using event callbacks and OpenAPI messages."""

    def __init__(self, config: FeishuConfig):
        self.config = config
        self._on_message: Callable[[str, FeishuEventSource], Awaitable[None]] | None = None
        self._stop_event: asyncio.Event | None = None
        self._server: uvicorn.Server | None = None
        self._http: httpx.AsyncClient | None = None
        self._tenant_access_token: str | None = None
        self._tenant_access_token_expires_at = 0.0
        self._shutdown_lock = asyncio.Lock()

    @property
    def platform_name(self) -> str:
        return "feishu"

    async def run(
        self,
        on_message: Callable[[str, FeishuEventSource], Awaitable[None]],
    ) -> None:
        """Run the Feishu event callback HTTP endpoint until stop() is called."""
        self._on_message = on_message
        self._stop_event = asyncio.Event()
        self._http = httpx.AsyncClient(base_url=self.config.domain, timeout=10.0)

        app = self._create_app()
        uvicorn_config = uvicorn.Config(
            app,
            host=self.config.host,
            port=self.config.port,
            log_config=None,
            lifespan="off",
        )
        self._server = uvicorn.Server(uvicorn_config)
        server_task = asyncio.create_task(self._server.serve(), name="feishu-http")
        stop_task = asyncio.create_task(self._stop_event.wait(), name="feishu-stop")

        logger.info(
            "Feishu channel listening on http://%s:%s%s",
            self.config.host,
            self.config.port,
            self.config.path,
        )

        try:
            done, _ = await asyncio.wait(
                {server_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if server_task in done and not stop_task.done():
                exc = server_task.exception()
                if exc is not None:
                    raise exc
                raise RuntimeError("Feishu HTTP server stopped unexpectedly")
        finally:
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)
            await self._shutdown_server(server_task)

    async def reply(
        self,
        content: str,
        source: FeishuEventSource,
        attachments: Sequence[MessageAttachment] | None = None,
    ) -> None:
        """Send a message to the Feishu chat represented by source."""
        if self._http is None:
            raise RuntimeError("Feishu channel is not running")

        for chunk in self._split_message(content) if content else []:
            token = await self._get_tenant_access_token()
            await self._send_message_payload(
                source.chat_id,
                "text",
                {"text": chunk},
                token,
            )

        for attachment in attachments or ():
            await self._send_attachment(attachment, source)

    async def is_allowed(self, source: FeishuEventSource) -> bool:
        """Check whether a Feishu sender is allowed to use the bot."""
        allowed_chat_ids = {str(chat_id) for chat_id in self.config.allowed_chat_ids}
        if self.config.chat_id is not None:
            allowed_chat_ids.add(str(self.config.chat_id))

        if allowed_chat_ids and source.chat_id not in allowed_chat_ids:
            return False

        if not self.config.allowed_user_ids:
            return True

        return source.user_id in {str(user_id) for user_id in self.config.allowed_user_ids}

    async def stop(self) -> None:
        """Stop listening and cleanup resources."""
        if self._stop_event is not None:
            self._stop_event.set()
        elif self._server is not None:
            self._server.should_exit = True

    def _create_app(self) -> FastAPI:
        app = FastAPI(title="Quclaw Feishu Channel")

        @app.get("/health")
        async def health() -> dict[str, bool]:
            return {"ok": True}

        @app.post(self.config.path)
        async def event_callback(request: Request) -> JSONResponse:
            return await self._handle_event_callback(request)

        return app

    async def _handle_event_callback(self, request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid_json"}, status_code=400)

        if not isinstance(payload, dict):
            return JSONResponse({"error": "invalid_payload"}, status_code=400)

        if payload.get("type") == "url_verification":
            if not self._verify_event_token(payload):
                return JSONResponse({"error": "invalid_token"}, status_code=403)

            challenge = self._handle_url_verification(payload)
            if challenge is None:
                return JSONResponse({"error": "invalid_challenge"}, status_code=400)
            return JSONResponse(challenge)

        if "encrypt" in payload:
            return JSONResponse(
                {"error": "encrypted_events_are_not_supported"},
                status_code=400,
            )

        if not self._verify_event_token(payload):
            logger.warning("Rejected Feishu event with invalid verification token")
            return JSONResponse({"error": "invalid_token"}, status_code=403)

        parsed = self._parse_text_message_event(payload)
        if parsed is None:
            return JSONResponse({"ok": True})

        content, source = parsed
        if not await self.is_allowed(source):
            logger.warning(
                "Rejected Feishu message from user %s in chat %s",
                source.user_id,
                source.chat_id,
            )
            return JSONResponse({"ok": True})

        if self._on_message is None:
            logger.warning("Feishu message received before callback was registered")
            return JSONResponse({"ok": True})

        try:
            await self._on_message(content, source)
        except Exception:
            logger.exception("Feishu message callback failed")
            return JSONResponse({"error": "callback_failed"}, status_code=500)

        return JSONResponse({"ok": True})

    def _handle_url_verification(self, payload: dict) -> dict[str, str] | None:
        if payload.get("type") != "url_verification":
            return None

        challenge = payload.get("challenge")
        if not isinstance(challenge, str):
            return None
        return {"challenge": challenge}

    def _verify_event_token(self, payload: dict) -> bool:
        if self.config.verification_token is None:
            return True

        token = payload.get("token")
        header = payload.get("header")
        if isinstance(header, dict):
            token = header.get("token", token)
        return token == self.config.verification_token

    def _parse_text_message_event(
        self,
        payload: dict,
    ) -> tuple[str, FeishuEventSource] | None:
        header = payload.get("header")
        if not isinstance(header, dict):
            return None

        if header.get("event_type") != "im.message.receive_v1":
            return None

        event = payload.get("event")
        if not isinstance(event, dict):
            return None

        message = event.get("message")
        if not isinstance(message, dict):
            return None

        if message.get("message_type") != "text":
            return None

        chat_id = message.get("chat_id")
        if not isinstance(chat_id, str) or not chat_id:
            return None

        text = self._extract_text(message.get("content"))
        if not text:
            return None

        sender = event.get("sender")
        user_id = self._extract_sender_id(sender if isinstance(sender, dict) else {})
        message_id = message.get("message_id")
        source = FeishuEventSource(
            chat_id=chat_id,
            user_id=user_id,
            message_id=message_id if isinstance(message_id, str) else None,
        )
        return text, source

    @staticmethod
    def _extract_text(raw_content: object) -> str:
        if not isinstance(raw_content, str):
            return ""

        try:
            content = json.loads(raw_content)
        except json.JSONDecodeError:
            return ""

        text = content.get("text") if isinstance(content, dict) else None
        if not isinstance(text, str):
            return ""

        text = re.sub(r"<at\b[^>]*>.*?</at>", "", text, flags=re.DOTALL)
        return text.strip()

    @staticmethod
    def _extract_sender_id(sender: dict) -> str | None:
        sender_id = sender.get("sender_id")
        if not isinstance(sender_id, dict):
            return None

        for key in ("open_id", "user_id", "union_id"):
            value = sender_id.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    async def _get_tenant_access_token(self) -> str:
        now = time.monotonic()
        if (
            self._tenant_access_token is not None
            and now < self._tenant_access_token_expires_at
        ):
            return self._tenant_access_token

        if self._http is None:
            raise RuntimeError("Feishu channel is not running")

        response = await self._http.post(
            "/open-apis/auth/v3/tenant_access_token/internal",
            json={
                "app_id": self.config.app_id,
                "app_secret": self.config.app_secret,
            },
        )
        data = self._raise_for_feishu_response(
            response,
            "get Feishu tenant access token",
        )
        token = data.get("tenant_access_token")
        if not isinstance(token, str) or not token:
            raise RuntimeError("Feishu tenant access token response missing token")

        expire = data.get("expire", 7200)
        ttl_seconds = int(expire) if isinstance(expire, int | str) else 7200
        self._tenant_access_token = token
        self._tenant_access_token_expires_at = now + max(60, ttl_seconds - 60)
        return token

    async def _send_attachment(
        self,
        attachment: MessageAttachment,
        source: FeishuEventSource,
    ) -> None:
        token = await self._get_tenant_access_token()
        if attachment.kind == "image":
            image_key = await self._upload_image(attachment, token)
            await self._send_message_payload(
                source.chat_id,
                "image",
                {"image_key": image_key},
                token,
            )
            return

        file_key = await self._upload_file(attachment, token)
        await self._send_message_payload(
            source.chat_id,
            "file",
            {"file_key": file_key},
            token,
        )

    async def _send_message_payload(
        self,
        chat_id: str,
        msg_type: str,
        content: dict[str, str],
        token: str,
    ) -> None:
        if self._http is None:
            raise RuntimeError("Feishu channel is not running")

        response = await self._http.post(
            "/open-apis/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            headers={"Authorization": f"Bearer {token}"},
            json={
                "receive_id": chat_id,
                "msg_type": msg_type,
                "content": json.dumps(content, ensure_ascii=False),
            },
        )
        self._raise_for_feishu_response(response, "send Feishu message")

    async def _upload_image(
        self,
        attachment: MessageAttachment,
        token: str,
    ) -> str:
        if self._http is None:
            raise RuntimeError("Feishu channel is not running")

        path = Path(attachment.path)
        media_type = attachment.media_type or "application/octet-stream"
        with path.open("rb") as f:
            response = await self._http.post(
                "/open-apis/im/v1/images",
                headers={"Authorization": f"Bearer {token}"},
                data={"image_type": "message"},
                files={"image": (attachment.display_name, f, media_type)},
            )

        data = self._raise_for_feishu_response(response, "upload Feishu image")
        payload = data.get("data")
        image_key = payload.get("image_key") if isinstance(payload, dict) else None
        if not isinstance(image_key, str) or not image_key:
            raise RuntimeError("Feishu image upload response missing image_key")
        return image_key

    async def _upload_file(
        self,
        attachment: MessageAttachment,
        token: str,
    ) -> str:
        if self._http is None:
            raise RuntimeError("Feishu channel is not running")

        path = Path(attachment.path)
        media_type = attachment.media_type or "application/octet-stream"
        with path.open("rb") as f:
            response = await self._http.post(
                "/open-apis/im/v1/files",
                headers={"Authorization": f"Bearer {token}"},
                data={
                    "file_type": self._file_type_for_feishu(attachment),
                    "file_name": attachment.display_name,
                },
                files={"file": (attachment.display_name, f, media_type)},
            )

        data = self._raise_for_feishu_response(response, "upload Feishu file")
        payload = data.get("data")
        file_key = payload.get("file_key") if isinstance(payload, dict) else None
        if not isinstance(file_key, str) or not file_key:
            raise RuntimeError("Feishu file upload response missing file_key")
        return file_key

    @staticmethod
    def _file_type_for_feishu(attachment: MessageAttachment) -> str:
        suffix = Path(attachment.display_name).suffix.lower()
        if suffix == ".pdf":
            return "pdf"
        if suffix in {".doc", ".docx"}:
            return "doc"
        if suffix in {".xls", ".xlsx"}:
            return "xls"
        if suffix in {".ppt", ".pptx"}:
            return "ppt"
        if suffix in {".txt", ".md", ".csv", ".json"}:
            return "txt"
        if suffix in {".mp4", ".mov", ".m4v"}:
            return "mp4"
        if suffix in {".mp3", ".wav", ".m4a", ".ogg"}:
            return "opus"
        return "stream"

    @staticmethod
    def _raise_for_feishu_response(response: httpx.Response, action: str) -> dict:
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError(f"Feishu {action} response is not an object")

        code = data.get("code", 0)
        if code != 0:
            message = data.get("msg") or data.get("message") or "unknown error"
            raise RuntimeError(f"Feishu failed to {action}: {code} {message}")
        return data

    async def _shutdown_server(self, server_task: asyncio.Task[None]) -> None:
        async with self._shutdown_lock:
            if self._server is not None:
                self._server.should_exit = True

            if not server_task.done():
                await asyncio.gather(server_task, return_exceptions=True)

            if self._http is not None:
                await self._http.aclose()
                self._http = None

            self._server = None
            self._stop_event = None
            logger.info("Feishu channel stopped")

    @staticmethod
    def _split_message(content: str, limit: int = 30000) -> list[str]:
        if not content:
            return [""]

        return [content[i : i + limit] for i in range(0, len(content), limit)]
