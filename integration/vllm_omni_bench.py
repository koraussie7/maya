#!/usr/bin/env python3
"""vLLM-Omni 벤치 — `/v1/audio/speech` 클라이언트에서 (text→waveform) + 청크 스트리밍.

목적 (둘 다 #2)
  · 우리 백엔드가 OpenAI 호환 TTS를 호출하는 흐름과 동일하게
    vLLM-Omni `/v1/audio/speech` 엔드포인트를 두 가지 방식으로 호출해 본다:
      (a) full : 한 번에 response.content 를 받아 .wav 로 저장
      (b) stream : 청크 스트리밍 모드로 수신 → chunk_size / 첫 chunk 지연 측정
  · VoxCPM2 대비 메트릭 (RTF, 첫 음성 청크 지연, 30초 입력의 wall-clock)을 표로 기록.

설치
  · 서버측: 별도 venv (Intel Mac은 GPU 가속이 없어서 **CPU 모드만 검증** 예정).
        uv venv .venv-vllm-omni && source .venv-vllm-omni/bin/activate
        uv pip install vllm-omni 'ray[default]' fastapi uvicorn websockets
        # 모델 다운로드
        hf download Qwen/Qwen3-Omni-30B-A3B-Instruct
  · 클라이언트측: 이 파일은 **클라이언트** 역할만 한다. 백엔드와의 충돌 없음.
        uv pip install httpx soundfile numpy

실행
  .venv-voicemem/bin/python integration/vllm_omni_bench.py \
      --base-url http://127.0.0.1:8090 --model Qwen/Qwen3-Omni-30B-A3B-Instruct \
      --text "안녕하세요. 반갑습니다." --voice echo \
      --mode stream --out /tmp/vllm_omni_stream.wav

주의
  · 이 스크립트는 vLLM-Omni 서버가 떠 있다고 가정한다.
    서버 기동은 `vllm-omni serve ... --port 8090` (별도 터미널).
  · 실패해도 우리 백엔드는 영향 없음. 음성합성 fallback이
    `say`, `piper`, `gtts` 로 3중화 되어 있기 때문.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
import wave
from pathlib import Path

import httpx
import numpy as np


def synth_full(base_url: str, model: str, text: str, voice: str) -> bytes:
    """`/v1/audio/speech` non-streaming 호출."""
    with httpx.Client(timeout=120.0) as c:
        r = c.post(
            f"{base_url}/v1/audio/speech",
            json={"model": model, "input": text, "voice": voice,
                  "response_format": "pcm", "stream": False},
        )
        r.raise_for_status()
        return r.content


def synth_stream(base_url: str, model: str, text: str, voice: str, out: Path):
    """`/v1/audio/speech` streaming 호출 → 청크 메트릭 수집."""
    chunks, sizes, times = [], [], []
    t0 = time.perf_counter()
    first_byte_ms = None
    with httpx.stream(
        "POST",
        f"{base_url}/v1/audio/speech",
        json={"model": model, "input": text, "voice": voice,
              "response_format": "pcm", "stream": True},
        timeout=120.0,
    ) as r:
        r.raise_for_status()
        for b in r.iter_bytes(chunk_size=4096):
            if not b:
                continue
            t = (time.perf_counter() - t0) * 1000
            if first_byte_ms is None:
                first_byte_ms = t
            chunks.append(b)
            sizes.append(len(b))
            times.append(t)
    pcm = b"".join(chunks)
    # 24k mono s16le 가정
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(pcm)
    return {
        "chunks": len(chunks),
        "total_bytes": len(pcm),
        "first_byte_ms": round(first_byte_ms or 0, 1),
        "wall_ms": round((time.perf_counter() - t0) * 1000, 1),
        "rtf_30s": round(((time.perf_counter() - t0) * 24000 * 2) / max(1, len(pcm)) / 30, 3),
        "out": str(out),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8090")
    ap.add_argument("--model", default="Qwen/Qwen3-Omni-30B-A3B-Instruct")
    ap.add_argument("--text", default="안녕하세요. 반갑습니다. 오늘 무엇을 도와드릴까요?")
    ap.add_argument("--voice", default="echo")
    ap.add_argument("--mode", choices=["full", "stream"], default="stream")
    ap.add_argument("--out", default="/tmp/vllm_omni.wav")
    args = ap.parse_args()

    print(f"[bench] base={args.base_url} model={args.model} mode={args.mode}")
    if args.mode == "full":
        t0 = time.perf_counter()
        pcm = synth_full(args.base_url, args.model, args.text, args.voice)
        dt_ms = (time.perf_counter() - t0) * 1000
        with open(args.out, "wb") as f:
            f.write(pcm)
        print(json.dumps({
            "mode": "full", "bytes": len(pcm),
            "wall_ms": round(dt_ms, 1),
            "out": args.out,
        }, ensure_ascii=False, indent=2))
    else:
        result = synth_stream(args.base_url, args.model, args.text, args.voice,
                              Path(args.out))
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
