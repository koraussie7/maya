"""RAG pipeline — answer_with_rag hook for session.py.

This module is intentionally lightweight: when MAYA_RAG_ENABLED=0
(or MAYA_TTS_PROVIDER=piper), all functions are no-ops that return
neutral values so the existing conversation flow is unaffected.

The _maybe_rag() hook in session.py calls answer_with_rag() just
before finalizing assistant text — if RAG is disabled, it returns
empty context and the chat continues normally.

Design contract (Agent 1):
    - Import must NOT fail when MAYA_RAG_ENABLED=0 (default).
      Other RAG modules (embedder.py, vector_store.py, knowledge_models.py)
      are loaded lazily inside answer_with_rag() only when enabled.
    - This stub satisfies Agent 3's API contract (§0.3 of CODEPLAN.md):
        answer_with_rag(query: str) -> str
      and returns "" when disabled so the existing session loop is
      completely unchanged.
"""

from __future__ import annotations

import logging
import os
from typing import Any, List, Optional

# `from .config import Settings` 는 app.rag.config 가 존재하지 않아 실패한다.
# 백엔드 루트의 settings 가 단일 인스턴스(SETTINGS) 이므로 부모 패키지
# (app) 의 config 에서 직접 가져온다 — `..` 는 app.rag 기준 부모 = app.
from ..config import SETTINGS

log = logging.getLogger(__name__)

# RAG가 비활성일 때도 FastAPI import는 절대 깨지면 안 된다.
# 실제 REST 엔드포인트 묶음은 knowledge_router.py 가 담당하고,
# pipeline.py 는 검색·증강 인터페이스(answer_with_rag) 만 노출한다.
try:
    from fastapi import APIRouter  # type: ignore
except Exception:  # FastAPI 미설치 같은 극단 상황에서도 백엔드 부팅은 가능
    APIRouter = None  # type: ignore


def get_router() -> Optional["APIRouter"]:
    """Agent 3 (§0.3) 가 참조하는 RAG REST 라우터.

    Agent 1 stub 단계에서는 knowledge_router.py 가 이미 자체 라우터를
    보유하고 있으므로, 여기서는 None 을 돌려주고 호출측(knowledge_router.py)이
    자체 라우터를 사용하도록 한다. Agent 3 가 RAG-전용 엔드포인트를
    추가할 시 이 함수에서 APIRouter 를 빌드해 돌려주면 된다.
    """
    if APIRouter is None:
        return None
    return None


def answer_with_rag(query: str) -> str:
    """Return RAG-augmented context for `query`, or "" if RAG disabled.

    Agent 1 stub: returns empty string when MAYA_RAG_ENABLED is not "1".
    When enabled, the real pipeline would call embedder.py + vector_store.py
    + knowledge_models.py. For now we surface a structured warning in the
    logs so operators can see if RAG is "on" without code paths.

    RAG 활성 플래그는 두 가지 경로로 읽는다 (방어):
      1) SETTINGS.ma_rag_enabled (config.py 의 Settings 필드)
      2) MAYA_RAG_ENABLED 환경변수
    둘 중 하나라도 "1"/truthy 이면 활성으로 본다.

    The function is sync because session.py's hot path is already inside
    an asyncio task — moving to async would require a session.py refactor
    that is out of scope for Agent 1.
    """
    env_on = os.environ.get("MAYA_RAG_ENABLED", "0").strip() == "1"
    settings_on = bool(getattr(SETTINGS, "ma_rag_enabled", 0))
    enabled = env_on or settings_on
    if not enabled:
        return ""
    log.warning(
        "answer_with_rag() called with MAYA_RAG_ENABLED=1 but the "
        "real retrieval pipeline is not implemented yet (Agent 1 stub). "
        "Returning empty context."
    )
    return ""
