#!/usr/bin/env python3
"""전체 파이프라인 실측 — TTS 스트리밍까지 포함한 종단 지연 측정.

서버의 WS 에 한국어 음성 PCM 을 직접 넣어(마이크 없이) 다음을 잰다.

    발화 전송 종료 → final 전사 수신 → 첫 tts_audio 수신 → 마지막 tts_audio

⚠ 합성음(`say`)을 입력으로 쓰므로 실제 마이크보다 깨끗하다.
  여기서 보는 것은 STT 정확도가 아니라 **지연 구간별 분해**다.

실행:
  cd /Users/brianyeon/CodeGPT/maya
  .venv-stt/bin/python /Users/brianyeon/CodeGPT/maya-2/integration/verify_pipeline.py
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
    """입력 음성: say → 16k mono s16le PCM."""
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
    dur = len(pcm) / 2 / 16000
    print(f"입력: {SENTENCE!r}  {dur:.2f}s  {len(pcm)}B\n")

    t = {}
    async with websockets.connect(URL, open_timeout=10) as ws:
        await ws.send(json.dumps({"type": "start"}))
        # start 응답 소비
        try:
            for _ in range(3):
                raw = await asyncio.wait_for(ws.recv(), timeout=8)
                if isinstance(raw, str) and json.loads(raw).get("type") == "llm_info":
                    break
        except asyncio.TimeoutError:
            pass

        # 음성을 실시간 속도로 전송 (WebSocket 전송 속도 ≠ 실시간)
        CHUNK = 3200  # 0.1초 분량
        for i in range(0, len(pcm), CHUNK):
            await ws.send(pcm[i:i + CHUNK])
            await asyncio.sleep(0.1)
        t["speech_done"] = time.perf_counter()

        # 무음으로 턴 종료 유도
        # 발화 후 충분한 무음 (NAMO 재판정까지 고려해 2초 이상)
        silence = struct.pack("<1600h", *([0] * 1600))   # 0.1s
        for _ in range(30):
            await ws.send(silence)
            await asyncio.sleep(0.1)

        final_at = thinking_at = first_audio = last_audio = None
        replies = []
        n_audio = 0
        last_text = None

        deadline = time.perf_counter() + 90
        while time.perf_counter() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError:
                break
            now = time.perf_counter()
            if isinstance(raw, str):
                m = json.loads(raw)
                ty = m.get("type")
                if ty == "status" and m.get("status") == "thinking" and thinking_at is None:
                    thinking_at = now
                    print(f"  +{(now-t['speech_done'])*1000:6.0f}ms  thinking")
                elif ty == "final":
                    if final_at is None:
                        final_at = now
                        print(f"  +{(now-t['speech_done'])*1000:6.0f}ms  final: {m.get('text')!r}")
                        last_text = m.get("text")
                elif ty == "final_reply":
                    replies.append(m.get("text", ""))
                    print(f"  +{(now-t['speech_done'])*1000:6.0f}ms  final_reply: {(m.get('text') or '')[:60]!r}")
                elif ty == "tts_audio":
                    n_audio += 1
                    if first_audio is None:
                        first_audio = now
                        print(f"  +{(now-t['speech_done'])*1000:6.0f}ms  "
                              f"첫 tts_audio (seq={m.get('seq')}, "
                              f"{len(base64.b64decode(m['audio']))}B) "
                              f"{m.get('text')!r}")
                    else:
                        last_audio = now
                        if m.get("final"):
                            print(f"  +{(now-t['speech_done'])*1000:6.0f}ms  "
                                  f"마지막 tts_audio (final) {m.get('text')!r}")
                elif ty == "error":
                    print(f"  ⚠ error: {m.get('error')}")
            else:
                print(f"  +{(now-t['speech_done'])*1000:6.0f}ms  バイナリ {len(raw)}B")
            if n_audio and last_audio is None and first_audio is not None:
                # final 플래그가 없는 경우 대비: 2초 조용하면 끝으로 본다
                pass

        await ws.send(json.dumps({"type": "stop"}))

    print("\n" + "=" * 62)
    print("구간별 지연 (발화 종료 기준)")
    print("-" * 62)

    def row(label, ts):
        if ts is None:
            print(f"  {label:22s}  —")
        else:
            print(f"  {label:22s}  {(ts - t['speech_done'])*1000:7.0f}ms")

    row("턴 확정(thinking)", thinking_at)
    row("최종 전사(final)", final_at)
    row("첫 TTS 오디오", first_audio)
    row("마지막 TTS 오디오", last_audio or first_audio)
    print("-" * 62)
    print(f"  tts_audio 조각 수: {n_audio}")
    if first_audio:
        print(f"  ★ 말 끝 → 첫 소리: {(first_audio - t['speech_done'])*1000:.0f}ms")
    ok = first_audio is not None and n_audio >= 1
    print(f"\n{'✅ 스트리밍 TTS 동작' if ok else '❌ TTS 오디오 미수신'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
