#!/usr/bin/env python3
"""emotion2vec_worker IPC 계약 테스트.

워커 서브프로세스를 띄우고 WAV 경로를 보내 임베딩을 받아온다.
`integration/emotion2vec_worker.py` 의 wire format 을 고정 검증한다:

  요청: 4B big-endian 길이 + JSON {"wav_path": "..."}
  응답: 1B 상태(0=ok) + 4B big-endian 길이 + JSON {"embedding": [...], ...}

사전 조건:
  .venv-voicemem 에 funasr, torch, torchaudio, soundfile, numpy
  EMOTION2VEC_MODEL_ID / EMOTION2VEC_HUB 로 모델 지정
  (기본 iic/emotion2vec_base_plus 는 HF/MS 에 없을 수 있다)

실행:
  EMOTION2VEC_DEVICE=cpu .venv-voicemem/bin/python integration/emotion2vec_ipc_test.py
  # 모델이 없으면 워커가 status!=0 으로 응답하고 이 스크립트도 1 을 반환한다.
"""
from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import threading
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
WORKER = str(_HERE / "emotion2vec_worker.py")
DEFAULT_WAV = _REPO / "maya" / "models" / "tts" / "reference" / "clip_ref_speaker_a.wav"


def _read_exact(stream, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)


def main() -> int:
    wav = Path(os.environ.get("EMOTION2VEC_TEST_WAV", DEFAULT_WAV))
    if not wav.exists():
        print(f"WAV 파일 없음: {wav}", file=sys.stderr)
        print("EMOTION2VEC_TEST_WAV=<경로> 로 지정하세요", file=sys.stderr)
        return 1

    print(f"워커 시작: {WORKER}")
    proc = subprocess.Popen(
        [sys.executable, WORKER],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, bufsize=0,
    )
    # 모델을 못 받으면 워커가 오래 매달린다. 무한 대기를 막기 위해 감시 타이머를 둔다.
    timeout_s = float(os.environ.get("EMOTION2VEC_TEST_TIMEOUT", "120"))
    timer = threading.Timer(timeout_s, proc.kill)
    timer.start()

    try:
        req = json.dumps({"wav_path": str(wav)}, ensure_ascii=False).encode("utf-8")
        assert proc.stdin is not None
        proc.stdin.write(struct.pack(">I", len(req)) + req)
        proc.stdin.flush()

        assert proc.stdout is not None
        status_raw = _read_exact(proc.stdout, 1)
        if len(status_raw) < 1:
            print("응답 상태 바이트 누락", file=sys.stderr)
            return 1
        status = status_raw[0]

        len_raw = _read_exact(proc.stdout, 4)
        if len(len_raw) < 4:
            print("응답 길이 바이트 누락", file=sys.stderr)
            return 1
        payload_len = struct.unpack(">I", len_raw)[0]

        payload_raw = _read_exact(proc.stdout, payload_len)
        if len(payload_raw) < payload_len:
            print("응답 페이로드 누락", file=sys.stderr)
            return 1

        payload = json.loads(payload_raw.decode("utf-8"))

        proc.stdin.close()
        proc.wait(timeout=5)

        if status != 0:
            print(f"워커 오류: {payload.get('error')}", file=sys.stderr)
            return 1

        embedding = payload.get("embedding")
        if not embedding:
            print("응답에 embedding 이 없습니다", file=sys.stderr)
            return 1

        print("=== 감정 임베딩 추출 성공 ===")
        print(f"dim: {len(embedding)}")
        print(f"duration_ms: {payload.get('duration_ms')}")
        print(f"granularity: {payload.get('granularity')}")
        print(f"첫 5개 값: {embedding[:5]}")
        print(f"최소/최대: {min(embedding):.4f} / {max(embedding):.4f}")
        return 0

    except Exception as exc:  # noqa: BLE001
        print(f"통신 실패: {exc!r}", file=sys.stderr)
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        return 1
    finally:
        timer.cancel()


if __name__ == "__main__":
    raise SystemExit(main())
