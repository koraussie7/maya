"""Maya voice backend — FastAPI WebSocket + REST."""

from __future__ import annotations

import logging

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware

from .config import SETTINGS
from .knowledge_router import router as knowledge_router  # Agent 1: include_router 1줄
from .rest_chat import router as rest_chat_router  # REST 대화 브리지 (/api/chat)
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

# Agent 1: include_router 1줄 — /knowledge/* 엔드포인트 등록
app.include_router(knowledge_router)

# REST 대화 브리지 — WebSocket 이 없는 UI(digital-twin 등)가 /api/chat 으로
# 대화할 수 있게 한다. 이 경로가 없으면 정적 서버 fallback 이 index.html 을
# 돌려주고 클라이언트는 "Unexpected token '<'" 로 실패한다.
app.include_router(rest_chat_router)


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
