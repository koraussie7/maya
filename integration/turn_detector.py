#!/usr/bin/env python3
"""tools/turn_detector.py — 한국어 턴 종료 판단 (NAMO Turn Detector v1).

왜 필요한가
  pcm_turn_engine 의 턴 종료는 "발화 후 조용하면 확정" 이라는 시간 기반이다.
  그래서 다음 두 가지 오판이 생긴다(실측).
    · "네" 만 말했는데 오래 조용하면 턴이 닫힌다   → 백채널을 발화로 착각
    · 잠깐 쉬었을 뿐인데 턴이 닫혀 문장이 잘린다  → 단어 중간 끊김
  문장이 실제로 끝났는지는 텍스트를 봐야 알 수 있다.
  NAMO 는 전사 텍스트만으로 "말 끝" 여부를 15ms 만에 판정한다.

출처
  모델   : https://huggingface.co/videosdk-live/Namo-Turn-Detector-v1-Korean
  코드   : https://github.com/videosdk-live/NAMO-Turn-Detector-v1/inference.py
  라이선스: MIT (모델 카드 기준). 상업적 사용 전 원문 확인 권장.

설계
  · 의존성: onnxruntime + transformers 토크나이저 + huggingface_hub
  · 모델은 지연 로드(첫 predict 호출) — 서버 기동을 막지 않는다
  · 로드 실패 시 available=False 로 남고, 호출부는 시간 기반 판정으로 폴백
  · confidence 가 1.000 근처로 과신 경향 → 임계값 기본 0.9

주의
  · 실제 문장 부호가 없는 전사에도 동작하지만, "완결형 문장인가" 가 신호다.
    "네", "어", "그쵸" 같은 백채널은 NOT(비종료) 로 정확히 나온다.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

log = logging.getLogger("maya.turn_detector")

REPO = "videosdk-live/Namo-Turn-Detector-v1-Korean"
ONNX_FILE = "model.onnx"
DEFAULT_THRESHOLD = 0.9


class TurnDetector:
    """전사 텍스트가 '말 끝'인지 판정한다.

    predict(text) -> (is_end, confidence)

    모델을 못 쓰는 환경에서는 available=False 이고 항상 False 를 반환하므로,
    호출부는 '판단 불가' 로 보고 기존 시간 기반 로직을 유지하면 된다.
    """

    _session = None
    _tokenizer = None
    _load_error: Optional[str] = None
    _load_lock = threading.Lock()

    def __init__(self, threshold: float = DEFAULT_THRESHOLD):
        self.threshold = threshold
        self._ensure_loaded()
        self.available = TurnDetector._session is not None
        if not self.available:
            log.warning(
                "NAMO 턴 판정 비활성 (%s) → 시간 기반 판정으로 폴백",
                TurnDetector._load_error,
            )

    # ── 로드 ──────────────────────────────────────────────
    @classmethod
    def _ensure_loaded(cls) -> None:
        if cls._session is not None or cls._load_error is not None:
            return
        with cls._load_lock:
            if cls._session is not None or cls._load_error is not None:
                return
            try:
                import onnxruntime as ort  # type: ignore
                from transformers import AutoTokenizer  # type: ignore
                from huggingface_hub import hf_hub_download  # type: ignore

                path = hf_hub_download(repo_id=REPO, filename=ONNX_FILE)
                cls._session = ort.InferenceSession(
                    path, providers=["CPUExecutionProvider"]
                )
                cls._tokenizer = AutoTokenizer.from_pretrained(REPO)
                log.info("NAMO 턴 판정 모델 로드 완료: %s", path)
            except Exception as exc:  # noqa: BLE001
                cls._load_error = f"{type(exc).__name__}: {exc}"
                log.warning("NAMO 모델 로드 실패: %s", cls._load_error)

    # ── 추론 ──────────────────────────────────────────────
    def predict(self, text: str) -> tuple[bool, float]:
        """(is_end_of_turn, confidence) 를 반환한다."""
        if not self.available or not text or not text.strip():
            return False, 0.0
        try:
            import numpy as np  # type: ignore

            inp = self._tokenizer(
                text, return_tensors="np", truncation=True, max_length=128
            )
            logits = self._session.run(
                None,
                {
                    "input_ids": inp["input_ids"],
                    "attention_mask": inp["attention_mask"],
                },
            )[0][0]
            e = np.exp(logits - logits.max())
            p = e / e.sum()
            return bool(p.argmax() == 1), float(p.max())
        except Exception as exc:  # noqa: BLE001
            log.warning("NAMO 추론 실패: %r", exc)
            return False, 0.0

    def is_end(self, text: str) -> bool:
        """임계값을 적용한 편의 메서드."""
        end, conf = self.predict(text)
        return end and conf >= self.threshold


_shared: Optional[TurnDetector] = None
_shared_lock = threading.Lock()


def get_detector(threshold: float = DEFAULT_THRESHOLD) -> TurnDetector:
    """프로세스 전역 싱글턴. 모델을 한 번만 로드한다."""
    global _shared
    if _shared is None:
        with _shared_lock:
            if _shared is None:
                _shared = TurnDetector(threshold=threshold)
    return _shared


if __name__ == "__main__":
    # 자체 점검: python tools/turn_detector.py
    import sys
    import time

    logging.basicConfig(level=logging.INFO)
    d = get_detector()
    samples = [
        "안녕하세요.",
        "오늘 날씨가 정말 좋네요.",
        "그런데 말이죠, 제가 생각하기에",
        "음... 잠깐만요...",
        "네",
        "어",
        "그쵸",
        "그거 어떻게 하는지 알려주실 수 있나요?",
        "감사합니다.",
        "그러니까 그게",
    ]
    t0 = time.perf_counter()
    for s in samples:
        end, conf = d.predict(s)
        print(f"{'END' if end else 'NOT'} {conf:.3f} | {s}")
    elapsed = (time.perf_counter() - t0) * 1000
    print(f"— {len(samples)}문장 {elapsed:.0f}ms (평균 {elapsed/len(samples):.1f}ms)")
    sys.exit(0 if d.available else 1)
