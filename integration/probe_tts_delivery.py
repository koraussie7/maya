#!/usr/bin/env python3
"""TTS 오디오 미수신 재현/추적 — 최소 시나리오로 서버와 대화한다.

verify_pipeline.py 와 같은 경로를 쓰되, 모든 WS 메시지를 (수신 시각, 종류, 크기)
그대로 찍는다. tts_audio 가 오지 않는 이유를 구간별로 좁히는 것이 목적이다.

실행:
  cd /Users/brianyeon/CodeGPT/maya
  .venv-stt/bin/python /Users/brianyeon/CodeGPT/maya-2/integration/probe_tts_delivery.py
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import struct
import subprocess
import sys
import tempfile
import time

import websockets

URL = "ws://127.0.0.1:8000/ws"
SENTENCE = "오늘 날씨가 정말 좋네요"


def synth_input(text: str) -> bytes:
    fd, aiff = tempfile.mkstemp(suffix=".aiff")
    os.close(fd)
    try:
        subprocess.run(["say", "-v", "Yuna", "-o", aiff, text],
                       check=True, capture_output=True)
        return subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", aiff,
             "-ar", "16000", "-ac", "1", "-f", "s16le", "-"],
            capture_output=True, check=True).stdout
    finally:
        os.unlink(aiff)


async def main() -> int:
    pcm = synth_input(SENTENCE)
    print(f"입력 {len(pcm) / 32000:.2f}s")

    async with websockets.connect(URL, open_timeout=10) as ws:
        await ws.send(json.dumps({"type": "start"}))
        t0 = time.perf_counter()

        CHUNK = 3200
        for i in range(0, len(pcm), CHUNK):
            await ws.send(pcm[i:i + CHUNK])
            await asyncio.sleep(0.1)
        silence = struct.pack("<1600h", *([0] * 1600))
        for _ in range(30):
            await ws.send(silence)
            await asyncio.sleep(0.1)
        t_speech_done = time.perf_counter()
        print(f"발화 종료 ({(t_speech_done - t0) * 1000:.0f}ms)\n")

        deadline = time.perf_counter() + 60
        counts: dict[str, int] = {}
        first_tts = None
        while time.perf_counter() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=8)
            except asyncio.TimeoutError:
                print(f"  [{(time.perf_counter() - t_speech_done) * 1000:7.0f}ms] 8s 침묵 → 종료")
                break
            now = (time.perf_counter() - t_speech_done) * 1000
            if isinstance(raw, bytes):
                counts["binary"] = counts.get("binary", 0) + 1
                print(f"  [{now:7.0f}ms] binary {len(raw)}B")
                continue
            m = json.loads(raw)
            ty = m.get("type", "?")
            counts[ty] = counts.get(ty, 0) + 1
            detail = ""
            if ty == "tts_audio":
                audio = base64.b64decode(m.get("audio", ""))
                if first_tts is None:
                    first_tts = now
                detail = (f"seq={m.get('seq')} final={m.get('final')} "
                          f"{len(audio)}B {m.get('text')!r}")
            elif "text" in m:
                detail = repr((m.get("text") or "")[:70])
            elif ty == "status":
                detail = m.get("status", "")
            print(f"  [{now:7.0f}ms] {ty:12s} {detail}")
        print("\n수신 요약:", counts)
        if first_tts is not None:
            print(f"★ 말 끝 → 첫 tts_audio: {first_tts:.0f}ms")
        await ws.send(json.dumps({"type": "stop"}))
    return 0 if first_tts is not None else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
