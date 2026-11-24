#!/usr/bin/env python3
"""SenseVoice STT 워커 — 메인 venv(무거운 의존성)를 백엔드 프로세스에서 분리한다.

왜 필요한가 (2026-11-24)
  대화 백엔드(`maya-ui/backend`)는 fastapi/uvicorn 만 있으면 뜨는 경량 프로세스인데,
  STT(funasr/torch)는 수 GB 의존성이고 메인 파이프라인 venv(.venv-voicemem)에만 있다.
  같은 프로세스에 넣으려고 하면 백엔드가 극도로 무거워지고, 반대로 백엔드 venv 에
  torch 를 설치하면 Intel Mac 의 torch 2.2.2 고정이 깨진다.
  → `stt.py` 가 이 워커를 서브프로세스로 띄워 **stdin/stdout 으로 WAV/PCM 을 주고받는다**.

프로토콜 (tts_local 의 piper_worker 와 같은 5바이트 헤더)
  요청 : 4바이트 big-endian 길이 + raw PCM16 mono 16kHz
  응답 : 1바이트 status + 4바이트 길이 + UTF-8 텍스트
         status 0 = 전사 성공, 1 = 오류(본문=메시지), 2 = 준비 완료(JSON)

실행 (백엔드가 자동으로 띄운다 — 직접 실행은 디버그용)
  .venv-voicemem/bin/python maya-ui/backend/app/stt_sensevoice_worker.py
"""
from __future__ import annotations

import io
import json
import os
import struct
import sys
import wave

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "integration")))


def read_exact(stream, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)


def write(status: int, payload: bytes) -> None:
    sys.stdout.buffer.write(bytes([status]) + struct.pack(">I", len(payload)) + payload)
    sys.stdout.buffer.flush()


def main() -> int:
    try:
        import torch_compat  # noqa: F401  (Intel Mac torch 2.2.2 shim)
        from bench_stt_hard import SenseVoice

        sv = SenseVoice()
    except Exception as exc:  # noqa: BLE001
        write(1, f"load_failed: {type(exc).__name__}: {exc}".encode())
        return 1

    write(2, json.dumps({"engine": "sensevoice", "sr": 16000}).encode())
    while True:
        head = read_exact(sys.stdin.buffer, 4)
        if len(head) < 4:
            break
        n = struct.unpack(">I", head)[0]
        pcm = read_exact(sys.stdin.buffer, n)
        if not pcm:
            write(0, b"")
            continue
        try:
            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(pcm)
            text, _ms = sv.transcribe(buf.getvalue())
            write(0, (text or "").encode())
        except Exception as exc:  # noqa: BLE001
            write(1, f"{type(exc).__name__}: {exc}".encode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
