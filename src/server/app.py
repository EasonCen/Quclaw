"""FastAPI application factory for the WebSocket gateway."""

from fastapi import FastAPI, WebSocket

from server.websocket_worker import WebSocketWorker


def create_app(websocket_worker: WebSocketWorker) -> FastAPI:
    """Create the HTTP/WebSocket application for a WebSocketWorker."""
    app = FastAPI(title="Quclaw Gateway")
    websocket_path = websocket_worker.context.config.websocket.path

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    async def websocket_endpoint(ws: WebSocket) -> None:
        await websocket_worker.handle_connection(ws)

    app.add_api_websocket_route(websocket_path, websocket_endpoint)
    return app
