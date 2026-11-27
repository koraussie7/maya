"""Embedding wrapper for HF models (BAAI/bge-m3 etc.).

Mock mode when model not available — returns random unit vectors.
"""

from __future__ import annotations

import os
import random
from typing import List

import numpy as np


class Embedder:
    """Wrapper for HF embedding models.

    In mock/no‑install mode this returns random unit vectors so the
    rest of the pipeline never crashes when the model isn’t present.
    """

    def __init__(self, model_slug: str = "BAAI/bge-m3") -> None:
        self.model_slug = model_slug
        self._ready = False
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore

            self._model = SentenceTransformer(model_slug)
            self._ready = True
        except Exception as exc:  # noqa: BLE001
            logger = __import__("logging").getLogger(__name__)
            logger.warning(
                "Embedder: model %s not available (%s) → mock", model_slug, exc
            )

    def embed(self, texts: List[str]) -> np.ndarray:
        """Return embeddings as (N, D) float32 array.

        If the model isn’t available we return random unit vectors.
        """
        if self._ready and self._model is not None:
            return self._model.encode(texts, normalize_embeddings=True).astype(np.float32)
        # mock: random unit vectors, dim=768 (BGE-M3 default)
        dim = 768
        vectors: list[list[float]] = []
        for _ in texts:
            vec = [random.gauss(0, 1) for _ in range(dim)]
            norm = sum(x * x for x in vec) ** 0.5
            vec = [x / norm for x in vec]
            vectors.append(vec)
        return np.array(vectors, dtype=np.float32)