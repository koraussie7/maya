"""Maya voice backend — FastAPI WebSocket + REST."""

from __future__ import annotations

import logging

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware

from .config import SETTINGS
from .session import handle_client

logging.basicConfig(
    level=SETTINGS.log_level,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)

app = FastAPI(title="Maya Voice Backend", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(SETTINGS.allow_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    await handle_client(websocket)


def main() -> None:
    """`python -m app.server` 진입점."""
    import uvicorn
    uvicorn.run(
        "app.server:app",
        host="0.0.0.0",
        port=SETTINGS.ws_port,
        log_level=SETTINGS.log_level.lower(),
    )


if __name__ == "__main__":
    main()
