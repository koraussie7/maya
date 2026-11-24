#!/usr/bin/env python3
"""piper_worker.py — Piper TTS 전용 서브프로세스 워커.

⚠ 왜 별도 프로세스인가
  이 환경에는 OpenMP 런타임(libiomp5.dylib)이 두 벌 링크된 라이브러리가 있다.
    · ctranslate2 4.9.0 (faster-whisper)  → libiomp5
    · onnxruntime 1.23.2 (piper)          → libiomp5
  한 프로세스에서 둘을 같이 쓰면 다음이 발생한다(실측):
    OMP: Error #15: Initializing libiomp5.dylib, but found libiomp5.dylib already initialized.
    → SIGABRT (KMP_DUPLICATE_LIB_OK=TRUE 없이는 크래시)
    → TRUE 여도 piper 합성이 RuntimeError 로 조용히 실패
  그래서 piper 를 **프로세스 분리**하면 onnxruntime 만 로드되므로 충돌이 사라진다.

프로토콜 (stdin/stdout, 바이너리)
  요청 : 4바이트 big-endian 길이 + UTF-8 텍스트
  응답 : 상태 1바이트(0=정상,1=오류) + 4바이트 BE 길이 + 페이로드
         정상 → Int16 mono PCM (모델 sample_rate 그대로, 리샘플은 부모가)
         오류 → UTF-8 오류 메시지

실행: .venv-stt/bin/python tools/piper_worker.py <model.onnx>
"""
from __future__ import annotations

import struct
import sys


def _read_exact(stream, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            return bytes(buf)
        buf.extend(chunk)
    return bytes(buf)


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: piper_worker.py <model.onnx>", file=sys.stderr)
        return 2
    model_path = sys.argv[1]

    try:
        from piper import PiperVoice  # type: ignore
        voice = PiperVoice.load(model_path)
    except Exception as exc:  # noqa: BLE001
        # 로드 실패는 조용히 죽지 않고 알린다
        msg = f"piper load failed: {type(exc).__name__}: {exc}".encode()
        sys.stdout.buffer.write(b"\x01" + struct.pack(">I", len(msg)) + msg)
        sys.stdout.buffer.flush()
        return 1

    out = sys.stdout.buffer
    inp = sys.stdin.buffer

    # 준비 완료 신호: 상태 0 + 페이로드 = sample_rate 문자열
    ready = str(voice.config.sample_rate).encode()
    out.write(b"\x00" + struct.pack(">I", len(ready)) + ready)
    out.flush()

    while True:
        head = _read_exact(inp, 4)
        if len(head) < 4:
            return 0                       # 부모가 종료함
        n = struct.unpack(">I", head)[0]
        text = _read_exact(inp, n).decode("utf-8", "replace")
        if not text:
            continue

        try:
            pcm = b"".join(ch.audio_int16_bytes for ch in voice.synthesize(text))
            out.write(b"\x00" + struct.pack(">I", len(pcm)) + pcm)
        except Exception as exc:  # noqa: BLE001
            msg = f"{type(exc).__name__}: {exc}".encode()
            out.write(b"\x01" + struct.pack(">I", len(msg)) + msg)
        out.flush()


if __name__ == "__main__":
    sys.exit(main())
