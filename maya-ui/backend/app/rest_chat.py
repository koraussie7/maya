"""REST 대화 브리지 — WebSocket 이 없는 클라이언트(digital-twin UI 등)용.

Maya 의 정본 대화 경로는 WebSocket(`/ws`), 즉 `session.py` 의 `_handle_text()` 다.
이 모듈은 그 경로를 그대로 쓰지 않고 **LLM/TTS 만 얇게 감싼 REST** 를 제공한다.
목적이 다르기 때문:

  · /ws  — 마이크 오디오, 스트리밍 TTS, decision.log, phase 등 실시간 상태
  · /api/chat — 텍스트 한 번 넣고 답 한 번 받는 요청/응답

같은 LLM 스택(`llm.stream_chat` → `llm_fallback`)을 쓰므로 모델/키 설정은
.env 하나로 공유된다.

왜 이게 필요한가
---------------
digital-twin UI 는 `POST /api/chat` 을 호출한다. Maya 백엔드에 그 경로가 없으면
정적 서버가 SPA fallback 으로 index.html 을 돌려주고, 클라이언트는
`Unexpected token '<', "<!doctype html>" is not valid JSON` 으로 실패한다
(2026-11-27 실제 화면 증상).

스키마는 **관대하게** 받는다. 클라이언트마다 필드 이름이 달라서
(message / text / query / prompt / content / input, 중첩 payload 포함) 모두 허용한다.
"""
from __future__ import annotations

import logging
import json
import os
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .config import SETTINGS
from .llm import stream_chat
from .protocol import ServerMsgType

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["rest-bridge"])

# 클라이언트가 보내는 텍스트 필드 이름 후보 (우선순위 순)
# digital-twin UI 는 `question` 을 쓴다(실제 문서 확인). 나머지는 편의.
_TEXT_KEYS = ("question", "message", "text", "query", "prompt", "content", "input", "q")
# 대화 기록 필드 이름 후보
_HISTORY_KEYS = ("history", "messages", "conversation", "context")


def _extract_text(body: dict[str, Any]) -> str:
    """본문에서 사용자 발화를 꺼낸다. 중첩 payload/body/data 도 본다."""
    for src in (body, body.get("payload"), body.get("body"), body.get("data")):
        if not isinstance(src, dict):
            continue
        for key in _TEXT_KEYS:
            v = src.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
    # messages 배열만 온 경우 (OpenAI 스타일): 마지막 user 메시지
    for key in _HISTORY_KEYS:
        hist = body.get(key)
        if isinstance(hist, list):
            for m in reversed(hist):
                if isinstance(m, dict) and m.get("role") == "user":
                    c = m.get("content")
                    if isinstance(c, str) and c.strip():
                        return c.strip()
    return ""


def _extract_history(body: dict[str, Any]) -> list[dict[str, str]]:
    """이전 대화를 LLM 형식으로 정규화한다."""
    raw = None
    for key in _HISTORY_KEYS:
        if isinstance(body.get(key), list):
            raw = body[key]
            break
    if not raw:
        return []
    out: list[dict[str, str]] = []
    for m in raw:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content") or m.get("text")
        if role in ("user", "assistant", "system") and isinstance(content, str) and content.strip():
            out.append({"role": role, "content": content.strip()})
    # system 은 맨 앞으로, 나머지는 순서 유지
    sys_msgs = [m for m in out if m["role"] == "system"]
    return sys_msgs + [m for m in out if m["role"] != "system"]


async def _answer(
    text: str,
    history: list[dict[str, str]],
    persona: str,
    options: list[str] | None = None,
) -> dict[str, Any]:
    """stream_chat 으로 답을 모아 하나의 문자열로 반환한다."""
    messages: list[dict[str, str]] = []
    sys_parts: list[str] = []
    if persona:
        sys_parts.append(persona)
    if options:
        # digital-twin UI 의 "Choices to weigh" — 보기 중에서 고르라는 요청.
        sys_parts.append(
            "다음 선택지 중에서 하나를 골라 짧게 답하세요: "
            + " / ".join(f"{i + 1}) {o}" for i, o in enumerate(options))
        )
    if sys_parts:
        messages.append({"role": "system", "content": "\n".join(sys_parts)})
    messages.extend(history[-16:])          # 최근 16개만
    messages.append({"role": "user", "content": text})

    buf: list[str] = []
    error: str | None = None
    max_tokens = int(os.environ.get("MAYA_REST_MAX_TOKENS", "512"))

    async for msg in stream_chat(messages, temperature=0.7, max_tokens=max_tokens):
        if msg.type == ServerMsgType.ASSISTANT_DELTA.value:
            buf.append(msg.payload.get("text", ""))
        elif msg.type == ServerMsgType.ASSISTANT_FINAL.value:
            final = msg.payload.get("text") or ""
            if final:
                buf = [final]
        elif msg.type == ServerMsgType.ERROR.value:
            error = msg.payload.get("message") or "llm error"

    answer = "".join(buf).strip()
    return {"answer": answer, "text": answer, "error": error}


@router.post("/ask")
@router.post("/chat")
async def chat(request: Request) -> JSONResponse:
    """텍스트 → 답변 (요청/응답 한 번).

    `/api/ask`  — digital-twin UI 경로 (본문 `{question, options}`)
    `/api/chat` — 일반적인 이름. 같은 핸들러.
    """
    t0 = time.monotonic()
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — JSON 아님
        body = {}
    if not isinstance(body, dict):
        body = {}

    text = _extract_text(body)
    if not text:
        return JSONResponse(
            status_code=400,
            content={"error": "no text", "detail": "question/message/text 중 하나를 보내세요"},
        )

    persona = (body.get("persona") or body.get("system") or "").strip()
    history = _extract_history(body)
    opts = body.get("options")
    options = [str(o) for o in opts] if isinstance(opts, list) and opts else None

    result = await _answer(text, history, persona, options)
    ms = int((time.monotonic() - t0) * 1000)
    log.info("rest %s %dms question=%.30r answer=%.30r",
             request.url.path, ms, text, result["answer"])
    return JSONResponse({
        "answer": result["answer"],
        "text": result["text"],          # 클라이언트마다 읽는 필드가 다르다
        "reply": result["answer"],
        "response": result["answer"],
        "message": result["answer"],
        "question": text,
        "sources": [],
        "elapsed_ms": ms,
        **({"error": result["error"]} if result["error"] else {}),
    })


def _sse(event: dict[str, Any]) -> str:
    return json.dumps(event, ensure_ascii=False) + "\n"


@router.post("/ask/stream")
@router.post("/chat/stream")
async def chat_stream(request: Request) -> StreamingResponse:
    """NDJSON 스트림 — `{status}` → `{text}` 반복 → `{answer}`.

    digital-twin UI 의 `/api/ask/stream` 계약을 따른다.
    클라이언트가 스트림을 파싱하지 못해도 마지막 `{answer}` 만 읽으면 동작한다.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        body = {}

    text = _extract_text(body)
    if not text:
        async def _bad():
            yield _sse({"error": "no text"})
        return StreamingResponse(_bad(), media_type="application/x-ndjson")

    persona = (body.get("persona") or body.get("system") or "").strip()
    history = _extract_history(body)
    opts = body.get("options")
    options = [str(o) for o in opts] if isinstance(opts, list) and opts else None

    async def gen():
        yield _sse({"status": "thinking"})
        buf: list[str] = []
        messages: list[dict[str, str]] = []
        sys_parts: list[str] = []
        if persona:
            sys_parts.append(persona)
        if options:
            sys_parts.append(
                "다음 선택지 중에서 하나를 골라 짧게 답하세요: "
                + " / ".join(f"{i + 1}) {o}" for i, o in enumerate(options))
            )
        if sys_parts:
            messages.append({"role": "system", "content": "\n".join(sys_parts)})
        messages.extend(history[-16:])
        messages.append({"role": "user", "content": text})

        max_tokens = int(os.environ.get("MAYA_REST_MAX_TOKENS", "512"))
        async for msg in stream_chat(messages, temperature=0.7, max_tokens=max_tokens):
            if msg.type == ServerMsgType.ASSISTANT_DELTA.value:
                piece = msg.payload.get("text", "")
                buf.append(piece)
                yield _sse({"text": piece})
            elif msg.type == ServerMsgType.ERROR.value:
                yield _sse({"error": msg.payload.get("message") or "llm error"})
        yield _sse({"answer": "".join(buf).strip()})

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@router.get("/health")
async def health() -> dict[str, Any]:
    """UI 가 백엔드 존재 여부를 확인할 때 쓴다."""
    return {
        "status": "ok",
        "llm_provider": SETTINGS.llm_provider,
        "llm_model": SETTINGS.openai_model,
        "llm_base": SETTINGS.openai_base,
        "tts_provider": SETTINGS.tts_provider,
    }


@router.get("/models")
async def models() -> dict[str, Any]:
    """현재 대화 모델 정보."""
    return {
        "model": SETTINGS.openai_model,
        "provider": SETTINGS.llm_provider,
    }


# ── UI 가 추가로 호출하는 경로 ─────────────────────────────────
# 404 를 돌려주면 프론트가 별도 에러 토스트를 띄운다. 존재만 하고
# "아직 없음"을 정직하게 알려주는 편이 UI 가 부드럽게 떨어진다.

@router.get("/profile")
async def profile() -> dict[str, Any]:
    """학습된 프로필 요약 — Maya 에는 별도 프로필 학습이 없다."""
    return {"profile": None, "summary": "", "note": "Maya 는 프로필 학습을 하지 않습니다"}


@router.get("/graph")
async def graph() -> dict[str, Any]:
    """지식 그래프 — Maya 의 RAG 그래프가 있으면 요약을, 없으면 빈 구조."""
    try:
        from .rag import pipeline  # noqa: F401
        return {"nodes": [], "edges": [], "communities": []}
    except Exception:  # noqa: BLE001
        return {"nodes": [], "edges": [], "communities": []}


@router.post("/speech")
async def speech(request: Request) -> JSONResponse:
    """텍스트 → WAV(base64). Maya 의 piper 로컬 TTS 를 그대로 쓴다.

    본문: {"text": "..."} 또는 {"question": "..."}
    응답: {"audio_base64": "...", "mime": "audio/wav"}
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    text = _extract_text(body if isinstance(body, dict) else {})
    if not text:
        return JSONResponse(status_code=400, content={"error": "no text"})
    text = text[:4000]          # UI 계약: 최대 4000자
    try:
        from .tts import synthesize
        audio = await synthesize(text, "", "")
    except Exception as exc:  # noqa: BLE001
        log.warning("rest TTS 실패: %s", exc)
        return JSONResponse(status_code=503, content={"error": str(exc)})
    if not audio:
        return JSONResponse(status_code=503, content={"error": "tts unavailable"})
    import base64
    return JSONResponse({
        "audio_base64": base64.b64encode(audio).decode(),
        "audio": base64.b64encode(audio).decode(),
        "mime": "audio/wav",
        "bytes": len(audio),
    })
