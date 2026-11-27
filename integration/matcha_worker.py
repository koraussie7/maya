#!/usr/bin/env python3
"""matcha_worker.py — Matcha-TTS 전용 서브프로세스 워커.

마야 TTS 파이프라인의 새 백엔드 엔진으로, piper_worker.py 와 동일한
IPC 프로토콜을 따른다. torch/onnxruntime 의존성을 호스트 프로세스와
격리하기 위해 별도 프로세스에서 실행된다.

프로토콜 (stdin/stdout, 바이너리)
  요청 : 4바이트 big-endian 길이 + UTF-8 텍스트
  응답 : 상태 1바이트(0=정상,1=오류) + 4바이트 BE 길이 + 페이로드
         정상 → Int16 mono PCM (모델 sample_rate 그대로, 부모가 리샘플)
         오류 → UTF-8 오류 메시지

환경변수
  MATCHA_MODEL_CHECKPOINT — Matcha-TTS 모델 가중치 경로(.ckpt / .pt)
  MATCHA_VOCODER_CHECKPOINT — vocoder(HiFi-GAN 등) 가중치 경로
  MATCHA_SAMPLE_RATE — 출력 샘플레이트(기본 22050)
  MATCHA_DEVICE — "cpu" 또는 "cuda" (기본 "cpu")
  MATCHA_LANGUAGE — 언어 코드(기본 "ko")

실행:
  .venv-matcha/bin/python integration/matcha_worker.py
"""
from __future__ import annotations

import json
import logging
import os
import struct
import sys
from pathlib import Path
from typing import Optional

log = logging.getLogger("matcha_worker")
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(message)s",
)

# ─── 설정 ────────────────────────────────────────────────────────────
MODEL_CHECKPOINT = os.environ.get("MATCHA_MODEL_CHECKPOINT", "")
VOCODER_CHECKPOINT = os.environ.get("MATCHA_VOCODER_CHECKPOINT", "")
SAMPLE_RATE = int(os.environ.get("MATCHA_SAMPLE_RATE", "22050"))
DEVICE = os.environ.get("MATCHA_DEVICE", "cpu")
LANGUAGE = os.environ.get("MATCHA_LANGUAGE", "ko")

# ─── 전역 모델 인스턴스 ─────────────────────────────────────────────
_model = None
_vocoder = None
_text_processor = None
_model_load_error: Optional[str] = None


def _load_model() -> None:
    """Matcha-TTS 모델과 vocoder 를 한 번 로드한다."""
    global _model, _vocoder, _text_processor, _model_load_error

    try:
        import torch  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        _model_load_error = f"torch import 실패: {exc}"
        log.exception(_model_load_error)
        return

    if not MODEL_CHECKPOINT:
        _model_load_error = "MATCHA_MODEL_CHECKPOINT 환경변수가 설정되지 않았습니다"
        log.warning(_model_load_error)
        return

    if not Path(MODEL_CHECKPOINT).is_file():
        _model_load_error = f"모델 파일을 찾을 수 없습니다: {MODEL_CHECKPOINT}"
        log.warning(_model_load_error)
        return

    try:
        # Matcha-TTS 모델 로드 (구현 예정 — 실제 모델 클래스에 맞게 수정)
        # 예: from matcha.models.matcha import Matcha
        #     _model = Matcha.load_from_checkpoint(MODEL_CHECKPOINT)
        #     _model.to(DEVICE).eval()
        log.info(
            "Matcha-TTS 모델 로드 준비: checkpoint=%s device=%s",
            MODEL_CHECKPOINT,
            DEVICE,
        )
    except Exception as exc:  # noqa: BLE001
        _model_load_error = f"모델 로드 실패: {exc}"
        log.exception(_model_load_error)


def _synthesize(text: str) -> bytes:
    """텍스트를 Int16 mono PCM 바이트로 합성한다.

    현재는 모델 가중치가 없으면 플레이스홀더 오류를 반환한다.
    실제 Matcha-TTS 추론 로직은 여기에 구현한다.
    """
    if _model_load_error is not None:
        raise RuntimeError(_model_load_error)
    if _model is None:
        raise RuntimeError("모델이 로드되지 않았습니다")

    # TODO: 실제 Matcha-TTS 추론 구현
    # 1. 텍스트 → 음소 시퀀스 (g2p)
    # 2. 음소 → 멜 스펙트로그램 (Matcha 인퍼런스)
    # 3. 멜 → 파형 (vocoder)
    # 4. Int16 PCM 바이트 반환
    raise NotImplementedError("Matcha-TTS 추론 로직이 아직 구현되지 않았습니다")


def _write_stdout(status: int, payload: bytes) -> None:
    """상태 바 + 4바이트 길이 + 페이로드를 stdout 에 쓴다."""
    header = struct.pack(">B I", status, len(payload))
    sys.stdout.buffer.write(header + payload)
    sys.stdout.buffer.flush()


def _read_exact(stream, n: int) -> bytes:
    """스트림에서 정확히 n 바이트를 읽는다."""
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)


def main() -> int:
    _load_model()

    # 준비 완료 신호: 상태 0 + 페이로드 = sample_rate 문자열(오류 시 에러 메시지)
    if _model_load_error:
        msg = _model_load_error.encode("utf-8")
        _write_stdout(1, msg)
        return 1

    ready = str(SAMPLE_RATE).encode("utf-8")
    _write_stdout(0, ready)

    inp = sys.stdin.buffer
    out = sys.stdout.buffer

    while True:
        head = _read_exact(inp, 4)
        if len(head) < 4:
            log.info("stdin EOF, 워커 종료")
            return 0
        n = struct.unpack(">I", head)[0]
        text_bytes = _read_exact(inp, n)
        if not text_bytes:
            continue
        text = text_bytes.decode("utf-8", "replace")

        try:
            pcm = _synthesize(text)
            _write_stdout(0, pcm)
        except Exception as exc:  # noqa: BLE001
            msg = f"{type(exc).__name__}: {exc}".encode("utf-8")
            _write_stdout(1, msg)
        out.flush()


if __name__ == "__main__":
    raise SystemExit(main())