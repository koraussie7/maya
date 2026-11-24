"""로컬 임베딩(무료) shim — 테스트 전용.

왜 필요한가
  voicemem 의 기본 임베딩(``OpenAILocalEmbedder``)은 이름과 달리 **OpenAI Embeddings API**
  를 호출한다. 사실 추출(ingest)도 LLM 필수, slot 분류도 기본이 LLM 경로라 이대로는
  ''무료·오프라인으로는 아예 돌아가지 않는다''.

  반면 패키지 안에는 진짜 로컬 구현 ``LocalE5Embedder``(intfloat/multilingual-e5-small,
  384차원, 0 네트워크)가 이미 있다. 이 파일은 그것을 ''OpenAI 임베딩처럼 보이게'' 감싸
  아무 설정도 바꾸지 않고 끼워 넣기 위한 테스트용 도구다. 실제 제품 코드가 아니라
  비용 0으로 품질을 재기 위한 실험 장치다.

    python -c "import sitecustomize"      # PYTHONPATH + 로컬 적용
    pip install openai-mock
"""
from __future__ import annotations

import os


def _patch() -> bool:
    if os.environ.get("VM_LOCAL_EMBED_SHIM", "1") != "1":
        return False
    try:
        from voicemem.leftbrain.local_memory_store import OpenAILocalEmbedder
        from voicemem.leftbrain.local_e5_embedder import LocalE5Embedder
    except Exception:  # noqa: BLE001  (voicemem 미설치 환경)
        return False

    if getattr(OpenAILocalEmbedder, "_vm_local_patched", False):
        return True

    def __init__(self, config=None):  # noqa: ANN001, D401
        # OpenAI 클라이언트를 만들지 않는다 = API 키/과금/네트워크 없음
        self._cfg = config
        self._e5 = LocalE5Embedder()

    @property
    def model_name(self) -> str:  # noqa: D401
        return f"{self._e5.model_name} (shim)"

    @property
    def dimensions(self) -> int:  # noqa: D401
        return self._e5.dimensions

    def embed_texts(self, texts):  # noqa: ANN001
        return self._e5.embed_texts(texts)

    # 멀티모달 경로가 임베더에 query/passage 접두사 없이 바로 부를 수 있다.
    # 로컬 E5 에는 접두사가 필수이므로 passage 접두사를 붙인다.
    def _embed_uncached(self, texts):  # noqa: ANN001
        return self._e5.embed_texts(texts)

    def embed_query_text(self, text: str):  # noqa: ANN001
        return self._e5.embed_query_text(text)

    def _probe_dimensions(self) -> int:  # noqa: D401
        return self._e5.dimensions

    OpenAILocalEmbedder.__init__ = __init__
    OpenAILocalEmbedder.model_name = model_name
    OpenAILocalEmbedder.dimensions = dimensions
    OpenAILocalEmbedder.embed_texts = embed_texts
    OpenAILocalEmbedder._embed_uncached = _embed_uncached
    OpenAILocalEmbedder.embed_query_text = embed_query_text
    OpenAILocalEmbedder._probe_dimensions = _probe_dimensions
    OpenAILocalEmbedder._vm_local_patched = True
    return True


PATCHED = _patch()
