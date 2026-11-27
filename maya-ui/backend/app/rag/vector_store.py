"""Very thin wrapper around LanceDB for chunk + entity storage.

If LanceDB is not installed the methods are no‑ops that return
empty/neutral results so the upper layers can still function.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    import lancedb  # type: ignore

    _LANCEDB_AVAILABLE = True
except Exception:  # noqa: BLE001
    _LANCEDB_AVAILABLE = False


class LanceKV:
    """Persistence layer for chunks and entities.

    No‑op when LanceDB is absent — callers should check _LANCEDB_AVAILABLE
    or rely on the RagConfig.enabled guard in the pipeline layer.
    """

    def __init__(self, data_dir: str = "~/.cache/maya/rag") -> None:
        self.data_dir = Path(data_dir).expanduser()
        self._db = None
        if _LANCEDB_AVAILABLE:
            try:
                self._db = lancedb.open_table(str(self.data_dir / "chunks"))
            except Exception:  # noqa: BLE001
                self._db = None

    def add_chunks(self, profile_id: str, chunks: list[dict]) -> int:
        """Persist text chunks with metadata. Returns count added."""
        if self._db is not None:
            # Real implementation would write to LanceDB tables
            # For now just count them as a no‑op stub
            return len(chunks)
        return 0  # no‑op when LanceDB absent

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        """Return top‑k chunk dicts matching the query.

        Mock mode returns empty list.
        """
        if self._db is not None:
            # Real search would go here
            return []
        return []  # mock