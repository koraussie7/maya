"""Knowledge router — maps /knowledge/* REST endpoints to RAG operations.

This is the router that Agent 1 creates, separate from the rag package.
It is imported by server.py via `include_router`.
"""

from __future__ import annotations

import os
from fastapi import APIRouter, Depends, HTTPException

from app.rag.pipeline import get_router as get_rag_router

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


def _get_rag() -> object:
    """Dependency that provides the global KnowledgeRouter instance."""
    from app.rag.pipeline import get_router
    return get_router()


@router.get("/health")
async def health(rag: object = Depends(_get_rag)) -> dict:
    """Health check for the knowledge service."""
    return {"status": "ok", "laya_loaded": True, "doc_count": 0, "graph_entity_count": 0}


@router.post("/ingest")
async def ingest(
    profile_id: str,
    files: list[dict],
    rag: object = Depends(_get_rag),
) -> dict:
    """Ingest files into a knowledge profile."""
    result = rag.ingest(profile_id, files)
    return result


@router.post("/search")
async def search(
    query: str,
    top_k: int = 5,
    profile_id: str | None = None,
    rag: object = Depends(_get_rag),
) -> dict:
    """Search the knowledge base."""
    return rag.search(query, top_k, profile_id)


@router.post("/delete")
async def delete(profile_id: str, rag: object = Depends(_get_rag)) -> dict:
    """Delete a knowledge profile."""
    # Currently a no-op; returns deleted count
    return {"deleted": 0}


@router.get("/profiles")
async def list_profiles(rag: object = Depends(_get_rag)) -> dict:
    """List knowledge profiles."""
    return rag.list_profiles()


@router.get("/graph")
async def graph(profile_id: str | None = None, rag: object = Depends(_get_rag)) -> dict:
    """Return knowledge graph nodes and edges."""
    return rag.get_graph(profile_id)


@router.get("/profile/{profile_id}")
async def profile(
    profile_id: str,
    rag: object = Depends(_get_rag),
) -> dict:
    """Return profile details including top entities."""
    return rag.list_profiles()  # placeholder — real impl would query specific profile