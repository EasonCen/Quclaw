"""Tests for WebSocket worker config hot reload behavior."""

import asyncio
import sys

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import server.server as server_module

from server.server import Server
from server.worker import Worker
from utils.config import WebSocketConfig


class FakeConfig:
    def __init__(self, websocket: WebSocketConfig) -> None:
        self.websocket = websocket


class FakeContext:
    def __init__(self, websocket: WebSocketConfig) -> None:
        self.config = FakeConfig(websocket)
        self.websocket_worker = None


class FakeWebSocketWorker(Worker):
    def __init__(self, context: FakeContext) -> None:
        super().__init__(context)
        self.stopped = False
        self.context.websocket_worker = self

    async def run(self) -> None:
        await asyncio.Future()

    async def stop(self) -> None:
        self.stopped = True
        if self.context.websocket_worker is self:
            self.context.websocket_worker = None
        await super().stop()


def test_reload_websocket_starts_when_enabled(monkeypatch) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(server_module, "WebSocketWorker", FakeWebSocketWorker)
        context = FakeContext(WebSocketConfig(enabled=True))
        server = Server(context)

        await server._reload_websocket(WebSocketConfig(enabled=False))

        assert len(server.workers) == 1
        worker = server.workers[0]
        assert isinstance(worker, FakeWebSocketWorker)
        assert worker.is_running()
        assert context.websocket_worker is worker

        await server._stop_all()

    asyncio.run(scenario())


def test_reload_websocket_stops_when_disabled(monkeypatch) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(server_module, "WebSocketWorker", FakeWebSocketWorker)
        context = FakeContext(WebSocketConfig(enabled=False))
        server = Server(context)
        worker = FakeWebSocketWorker(context)
        server.workers.append(worker)
        worker.start()

        await server._reload_websocket(WebSocketConfig(enabled=True))

        assert worker.stopped is True
        assert server.workers == []
        assert context.websocket_worker is None

    asyncio.run(scenario())


def test_reload_websocket_restarts_when_config_changes(monkeypatch) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(server_module, "WebSocketWorker", FakeWebSocketWorker)
        context = FakeContext(WebSocketConfig(enabled=True, port=7000))
        server = Server(context)
        old_worker = FakeWebSocketWorker(context)
        server.workers.append(old_worker)
        old_worker.start()

        await server._reload_websocket(WebSocketConfig(enabled=True, port=6948))

        assert old_worker.stopped is True
        assert len(server.workers) == 1
        new_worker = server.workers[0]
        assert isinstance(new_worker, FakeWebSocketWorker)
        assert new_worker is not old_worker
        assert new_worker.is_running()
        assert context.websocket_worker is new_worker

        await server._stop_all()

    asyncio.run(scenario())


def test_reload_websocket_keeps_worker_when_config_unchanged(monkeypatch) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(server_module, "WebSocketWorker", FakeWebSocketWorker)
        config = WebSocketConfig(enabled=True, port=6948)
        context = FakeContext(config)
        server = Server(context)
        worker = FakeWebSocketWorker(context)
        server.workers.append(worker)
        worker.start()

        await server._reload_websocket(config.model_copy(deep=True))

        assert worker.stopped is False
        assert server.workers == [worker]
        assert context.websocket_worker is worker

        await server._stop_all()

    asyncio.run(scenario())
