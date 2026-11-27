"""Data models for the RAG knowledge system.

Chunks, entities, profiles, and search results used throughout the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np


@dataclass
class Chunk:
    """A text chunk with metadata."""

    chunk_id: str
    profile_id: str
    text: str
    embedding: np.ndarray | None = None
    source_file: str = ""
    page: int = 0
    entities: list[str] = field(default_factory=list)
    rank: float = 0.0


@dataclass
class Entity:
    """An entity extracted from a chunk."""

    entity_id: str
    label: str
    type: str  # e.g., "person", "organization", "concept"
    count: int = 1
    chunks: list[str] = field(default_factory=list)  # chunk_ids referencing this entity


@dataclass
class Profile:
    """A knowledge profile (collection of chunks/entities)."""

    profile_id: str
    name: str
    doc_count: int = 0
    created_at: str = ""
    chunks: list[Chunk] = field(default_factory=list)
    entities: list[Entity] = field(default_factory=list)


@dataclass
class SearchHit:
    """A single search result hit."""

    chunk_id: str
    text: str
    score: float
    source: str  # profile_id or doc_name
    doc_name: str = ""
    entity_ids: list[str] = field(default_factory=list)
    laya_score: float | None = None


@dataclass
class GraphResult:
    """Result for knowledge graph query."""

    nodes: list[Dict[str, Any]] = field(default_factory=list)
    edges: list[Dict[str, Any]] = field(default_factory=list)