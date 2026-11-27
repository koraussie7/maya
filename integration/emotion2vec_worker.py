#!/usr/bin/env python3
"""integration/emotion2vec_worker.py — emotion2vec 임베딩 추출 서브프로세스 워커.

마야 TTS 워커(_PiperWorker/_VoxCpmWorker)와 동일한 IPC 프로토콜을 따른다.
별도 프로세스(권장 venv: .venv-emotion2vec 또는 .venv-voicemem)에서 실행돼
torch/transformers 의존성 충돌을 호스트 프로세스와 격리한다.

IPC 프로토콜 (stdin → stdout):
  쓰기(stdin):  4바이트 빅엔디안 길이 + JSON 페이로드 {"wav_path": "..."}
  읽기(stdout): 1바이트 상태(0=성공, 1=오류) + 4바이트 길이 + JSON 페이로드
                성공 시 페이로드: {"embedding": [768 floats], "duration_ms": ...}
                오류 시 페이로드: {"error": "..."}

환경변수:
  EMOTION2VEC_MODEL_ID  — HuggingFace 모델 ID (기본: iic/emotion2vec_base_plus)
  EMOTION2VEC_HUB       — "hf" 또는 "ms" (기본: "hf")
  EMOTION2VEC_DEVICE    — "cpu" 또는 "cuda" (기본: "cpu")
  EMOTION2VEC_GRANULARITY — "utterance" 또는 "frame" (기본: "utterance")

사전 요구사항 (워커 venv에 설치):
  pip install funasr torch torchaudio soundfile numpy
"""

from __future__ import annotations

import json
import logging
import os
import struct
import sys
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("emotion2vec_worker")
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(message)s",
)

# ─── 환경변수 설정 ──────────────────────────────────────────────────
# emotion2vec 모델 ID 후보 (FunASR hub 에 등록된 순서대로 시도)
_DEFAULT_MODEL_IDS = [
    "iic/emotion2vec_base_plus",
    "iic/emotion2vec_base",
    "ddlBoJack/emotion2vec_base_plus",
    "ddlBoJack/emotion2vec_base",
]
MODEL_ID = os.environ.get(
    "EMOTION2VEC_MODEL_ID", _DEFAULT_MODEL_IDS[0]
)
HUB = os.environ.get("EMOTION2VEC_HUB", "hf")  # "hf" 또는 "ms"
DEVICE = os.environ.get("EMOTION2VEC_DEVICE", "cpu")
GRANULARITY = os.environ.get("EMOTION2VEC_GRANULARITY", "utterance")

# ─── 전역 모델 인스턴스 (워커 수명 동안 한 번만 로드) ─────────────
_model = None
_model_load_error: Optional[str] = None
_loaded_model_id: Optional[str] = None


def _load_model() -> None:
    """emotion2vec 모델을 한 번 로드한다.

    기본 모델 ID 가 등록되지 않으면 후보 목록을 순차적으로 시도한다.
    실패 시 _model_load_error 에 기록.
    """
    global _model, _model_load_error, _loaded_model_id
    try:
        from funasr import AutoModel
    except Exception as exc:  # noqa: BLE001
        _model_load_error = f"funasr import 실패: {exc}"
        log.exception(_model_load_error)
        return

    model_ids = [MODEL_ID] + [
        m for m in _DEFAULT_MODEL_IDS if m != MODEL_ID
    ]
    hubs = [HUB] + (["ms"] if HUB == "hf" else ["hf"])

    last_err = None
    for mid in model_ids:
        for hb in hubs:
            try:
                _model = AutoModel(
                    model=mid,
                    hub=hb,
                    device=DEVICE,
                    disable_update=True,
                )
                _loaded_model_id = mid
                log.info(
                    "emotion2vec 모델 로드 완료: model=%s hub=%s device=%s",
                    mid,
                    hb,
                    DEVICE,
                )
                return
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                log.warning(
                    "모델 로드 실패(model=%s hub=%s): %s", mid, hb, exc
                )

    _model_load_error = f"모든 모델 후보 로드 실패 (마지막 오류): {last_err}"
    log.exception(_model_load_error)


def _extract_embedding(wav_path: str) -> dict:
    """WAV 파일에서 emotion2vec 임베딩을 추출한다."""
    if _model_load_error is not None:
        return {"error": _model_load_error}
    if _model is None:
        return {"error": "모델이 로드되지 않았습니다"}

    t0 = time.perf_counter()
    try:
        # FunASR generate: granularity="utterance" → {'feats': [*768]}
        result = _model.generate(
            wav_path,
            output_dir=None,
            granularity=GRANULARITY,
            emit_kwargs={},
        )
        dt_ms = (time.perf_counter() - t0) * 1000

        if not result or "feats" not in result[0]:
            return {"error": "모델 출력에 feats 가 없습니다"}

        feats = result[0]["feats"]
        # utterance-level: 1차원 배열(768,) 또는 2차원 (1, 768)
        if isinstance(feats, list):
            embedding = [float(v) for v in feats]
        else:
            import numpy as np

            arr = np.asarray(feats, dtype=np.float64)
            if arr.ndim == 2:
                arr = arr.mean(axis=0)  # frame-level → utterance 평균
            embedding = arr.tolist()

        return {
            "embedding": embedding,
            "dim": len(embedding),
            "duration_ms": round(dt_ms, 2),
            "granularity": GRANULARITY,
        }
    except Exception as exc:  # noqa: BLE001
        dt_ms = (time.perf_counter() - t0) * 1000
        log.error("임베딩 추출 실패(%s): %r", wav_path, exc)
        return {"error": f"추출 실패: {exc}", "duration_ms": round(dt_ms, 2)}


def _write_stdout(status: int, payload: dict) -> None:
    """상태 바 + 4바이트 길이 + JSON 페이로드를 stdout 에 쓴다."""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    header = struct.pack(">B I", status, len(data))
    sys.stdout.buffer.write(header + data)
    sys.stdout.buffer.flush()


def _read_stdin_length() -> Optional[int]:
    """stdin 에서 4바이트 빅엔디안 길이를 읽는다. EOF 시 None."""
    raw = sys.stdin.buffer.read(4)
    if not raw or len(raw) < 4:
        return None
    return struct.unpack(">I", raw)[0]


def _read_stdin_payload(n: int) -> Optional[dict]:
    """stdin 에서 n 바이트를 읽고 JSON 파싱한다."""
    raw = sys.stdin.buffer.read(n)
    if not raw or len(raw) < n:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        log.error("JSON 파싱 실패: %r", exc)
        return None


def main() -> int:
    """워커 메인 루프. stdin 에서 요청을 읽고 stdout 에 응답을 쓴다."""
    _load_model()

    while True:
        n = _read_stdin_length()
        if n is None:
            # EOF — 워커 종료
            log.info("stdin EOF, 워커 종료")
            return 0

        req = _read_stdin_payload(n)
        if req is None:
            _write_stdout(1, {"error": "잘못된 요청 페이로드"})
            continue

        wav_path = req.get("wav_path")
        if not wav_path:
            _write_stdout(1, {"error": "wav_path 가 필요합니다"})
            continue

        p = Path(wav_path)
        if not p.exists():
            _write_stdout(1, {"error": f"파일을 찾을 수 없습니다: {wav_path}"})
            continue

        result = _extract_embedding(str(p))
        if "error" in result:
            _write_stdout(1, result)
        else:
            _write_stdout(0, result)


if __name__ == "__main__":
    raise SystemExit(main())
