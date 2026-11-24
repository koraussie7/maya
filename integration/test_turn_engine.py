#!/usr/bin/env python3
"""TurnEngine + NAMO 판정 통합 검증 (실제 모델 사용, 가짜 오디오).

실제 마이크 없이 '발화 3초 → 무음 1.5초' 패턴을 흘려보내
턴이 어떻게 확정되는지 본다. 전사기는 미리 정한 텍스트를 돌려준다.

실행:
  cd /Users/brianyeon/CodeGPT/maya
  PYTHONPATH=/Users/brianyeon/CodeGPT/maya-2/integration:. \
    .venv-stt/bin/python /Users/brianyeon/CodeGPT/maya-2/integration/test_turn_engine.py
"""
from __future__ import annotations

import logging
import struct
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(message)s")

import os  # noqa: E402

# 통합본(integration/)이 maya 원본보다 먼저 잡히도록 한다.
_INTEG = os.path.dirname(os.path.abspath(__file__))
if _INTEG in sys.path:
    sys.path.remove(_INTEG)
sys.path.insert(0, _INTEG)

import pcm_turn_engine  # noqa: E402

assert pcm_turn_engine.__file__.startswith(_INTEG), (
    f"잘못된 모듈 로드: {pcm_turn_engine.__file__}"
)
from pcm_turn_engine import TurnConfig, TurnEngine  # noqa: E402

CHUNK_MS = 128
CHUNK_SAMPLES = 16000 * CHUNK_MS // 1000


def make_chunk(loud: bool) -> bytes:
    """실제 Int16 PCM 청크. loud=True 이면 RMS 가 임계값을 넘는다."""
    amp = 6000 if loud else 30
    return struct.pack(f"<{CHUNK_SAMPLES}h", *([amp] * CHUNK_SAMPLES))


class FakeTranscriber:
    """n번째 전사 호출마다 미리 정한 텍스트를 돌려준다."""

    def __init__(self, script: list[str]):
        self.script = script
        self.calls = 0

    def transcribe(self, pcm: bytes) -> str:
        i = min(self.calls, len(self.script) - 1)
        self.calls += 1
        return self.script[i]


def run(name: str, script: list[str], speech_chunks: int, silence_chunks: int) -> None:
    print(f"\n=== {name} ===")
    eng = TurnEngine(
        config=TurnConfig(max_turn_ms=30_000, use_turn_detector=True),
        transcriber=FakeTranscriber(script),
    )
    print(f"검출기 사용: {eng._detector is not None}")
    eng.begin_turn()

    result = None
    t0 = time.monotonic()
    # 발화 구간
    for _ in range(speech_chunks):
        result = eng.push_result(make_chunk(True))
        time.sleep(CHUNK_MS / 1000)
        if result:
            break
    # 무음 구간
    if result is None:
        for _ in range(silence_chunks):
            result = eng.push_result(make_chunk(False))
            time.sleep(CHUNK_MS / 1000)
            if result:
                break

    elapsed = time.monotonic() - t0
    if result:
        print(f"→ 확정: reason={result.reason!r} text={result.text!r} "
              f"pcm={len(result.pcm)}B ({elapsed:.2f}s)")
    else:
        print(f"→ 미확정 (계속 듣는 중) ({elapsed:.2f}s)")


def main() -> int:
    # 1) 백채널 "네" → NAMO NOT → 계속 듣기
    run("백채널 '네' (계속 듣기 기대)", ["네"], speech_chunks=24, silence_chunks=20)

    # 2) 완결 문장 → NAMO END → 확정
    run("완결 문장 '안녕하세요.' (확정 기대)",
        ["안녕하세요."], speech_chunks=24, silence_chunks=20)

    # 3) 미완결 "그런데 말이죠" → NOT → 계속 듣기
    run("미완결 '그런데 말이죠' (계속 듣기 기대)",
        ["그런데 말이죠,"], speech_chunks=24, silence_chunks=20)

    # 4) 전사 공백 → 시간 기반 폴백 확정
    run("전사 공백 (폴백 확정 기대)", [""], speech_chunks=24, silence_chunks=20)
    return 0


if __name__ == "__main__":
    sys.exit(main())
