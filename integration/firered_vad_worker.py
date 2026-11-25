#!/usr/bin/env python3
"""firered_vad_worker.py — FireRedVAD 전용 서브프로세스 워커 (Phase B-1 PoC).

⚠ 왜 별도 프로세스인가 (piper_worker.py 와 같은 이유)
  이 맥에는 OpenMP 런타임(libiomp5)이 두 벌 링크된 라이브러리가 있다.
    · ctranslate2 (faster-whisper) → libiomp5
    · torch (FireRedVAD)           → libiomp5
  한 프로세스에서 둘을 같이 쓰면 `OMP: Error #15` → SIGABRT.
  그래서 VAD 를 **프로세스 분리**하고 부모(백엔드)는 PCM 만 넘긴다.

프로토콜 (stdin/stdout 바이너리) — piper_worker.py 와 동일 규격
  요청 : 4바이트 big-endian 길이 + Int16 mono 16kHz **PCM 조각**
         길이 0 → reset (다음 응답이 새 스트림의 첫 결과)
  응답 : 1바이트 상태(0=정상,1=오류) + 4바이트 BE 길이 + UTF-8 JSON
         0 → {"speech":bool,"end":bool,"starts":int,"frames":int,
              "prob":float,"raw_prob":float}
         1 → {"error":"..."}

  speech = 이번 조각까지 누적 스트림에 발화가 있었는가
  end    = 발화 종료 지점(is_speech_end)이 관측되었는가 → 턴 확정 후보

핵심: `detect_full()` 은 내부에서 reset() 을 호출하고 오디오를 파일/wav 경로로
  읽는다(스트리밍에 부적합). 그래서 `detect_chunk()` 로 **DFSMS 캐시를 유지하며**
  조각을 이어 붙인다. 이게 이 PoC 의 실제 측정 포인트다.

실행: .venv-voicemem/bin/python integration/firered_vad_worker.py [--model DIR]
"""
from __future__ import annotations

import argparse
import json
import os
import struct
import sys

SAMPLE_RATE = 16_000
DEFAULT_MODEL = os.environ.get("MAYA_FIRERED_MODEL") or next(
    (p for p in (
        os.path.expanduser("~/.cache/maya/models/FireRedVAD/Stream-VAD"),
        "/tmp/frvad_models/FireRedVAD/Stream-VAD",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "pretrained_models/FireRedVAD/Stream-VAD"),
    ) if os.path.isfile(os.path.join(p, "cmvn.ark"))),
    "",
)


def _read_exact(stream, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            return bytes(buf)
        buf.extend(chunk)
    return bytes(buf)


def _emit(stream, status: int, payload: dict) -> None:
    raw = json.dumps(payload, ensure_ascii=False).encode()
    stream.write(bytes([status]) + struct.pack(">I", len(raw)) + raw)
    stream.flush()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--threshold", type=float,
                    default=float(os.environ.get("MAYA_FIRERED_THRESHOLD", "0.5")))
    ap.add_argument("--min-silence-frame", type=int,
                    default=int(os.environ.get("MAYA_FIRERED_MIN_SILENCE", "20")))
    args = ap.parse_args()

    out, inp = sys.stdout.buffer, sys.stdin.buffer

    try:
        import numpy as np  # noqa: F401
        from fireredvad import FireRedStreamVad, FireRedStreamVadConfig  # type: ignore

        cfg = FireRedStreamVadConfig(
            use_gpu=False,
            speech_threshold=args.threshold,
            min_silence_frame=args.min_silence_frame,
        )
        vad = FireRedStreamVad.from_pretrained(args.model, cfg)
        vad.reset()
    except Exception as exc:  # noqa: BLE001
        _emit(out, 1, {"error": f"load failed: {type(exc).__name__}: {exc}"})
        return 1

    _emit(out, 0, {"ready": True, "model": args.model,
                   "threshold": args.threshold,
                   "min_silence_frame": args.min_silence_frame})

    speech = False
    starts = 0

    while True:
        head = _read_exact(inp, 4)
        if len(head) < 4:
            return 0                                   # 부모가 종료
        n = struct.unpack(">I", head)[0]

        if n == 0:                                     # reset
            vad.reset()
            speech, starts = False, 0
            _emit(out, 0, {"reset": True, "speech": False, "end": False})
            continue

        payload = _read_exact(inp, n)

        try:
            import numpy as np

            # AudioFeat 은 int16 ndarray 를 그대로 받는다(sf.read 와 동일 규격).
            pcm = np.frombuffer(payload[: len(payload) // 2 * 2], dtype=np.int16)
            results = vad.detect_chunk(pcm)
        except Exception as exc:  # noqa: BLE001
            _emit(out, 1, {"error": f"{type(exc).__name__}: {exc}"})
            continue

        end = False
        raw_prob = smoothed = 0.0
        for r in results or ():
            raw_prob, smoothed = float(r.raw_prob), float(r.smoothed_prob)
            if r.is_speech:
                speech = True
            if r.is_speech_start:
                starts += 1
            if r.is_speech_end:
                end = True

        _emit(out, 0, {"speech": speech, "end": end, "starts": starts,
                       "frames": len(results or ()),
                       "prob": round(smoothed, 3), "raw_prob": round(raw_prob, 3)})


if __name__ == "__main__":
    sys.exit(main())
