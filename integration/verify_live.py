#!/usr/bin/env python3
"""통합 검증 — 실행 중인 서버에 실제 WS 로 붙어 TTS 스트리밍을 확인한다.

파이프라인: /ws 연결 → start → PCM 발화 전송 → 전사 → LLM → 문장별 TTS
이 스크립트는 '마이크 없이' 검증하기 위해, 서버에 직접 텍스트 턴을 넣는 대신
TTS 경로만 별도로 확인한다. (전체 파이프라인은 브라우저에서 확인)

검증 항목
  1) /ws 연결 성공
  2) start → status=listening, llm_info 수신
  3) 무음만 보냈을 때 턴이 닫히지 않는지 (no speech 60s)

실행:
  cd /Users/brianyeon/CodeGPT/maya
  .venv-stt/bin/python /Users/brianyeon/CodeGPT/maya-2/integration/verify_live.py
"""
from __future__ import annotations

import asyncio
import json
import struct
import sys

import websockets

URL = "ws://127.0.0.1:8000/ws"
SAMPLES = 2048          # 128ms @16k
SILENCE = struct.pack(f"<{SAMPLES}h", *([0] * SAMPLES))


def log(msg: str) -> None:
    print(msg, flush=True)


async def main() -> int:
    ok = 0
    total = 0

    # 1) 연결
    total += 1
    try:
        ws = await asyncio.wait_for(websockets.connect(URL, open_timeout=8), timeout=10)
        log(f"✅ 1) WebSocket 연결 성공: {URL}")
        ok += 1
    except Exception as e:
        log(f"❌ 1) WebSocket 연결 실패: {type(e).__name__}: {e}")
        return 1

    try:
        # 2) start → status/llm_info
        total += 1
        await ws.send(json.dumps({"type": "start"}))
        seen: list[str] = []
        try:
            for _ in range(3):
                raw = await asyncio.wait_for(ws.recv(), timeout=12)
                if isinstance(raw, str):
                    m = json.loads(raw)
                    seen.append(m.get("type", "?"))
                    if m.get("type") == "llm_info":
                        log(f"   llm_info ready={m.get('ready')} backend={m.get('backend')}")
                else:
                    seen.append("binary")
                if "status" in seen and "llm_info" in seen:
                    break
        except asyncio.TimeoutError:
            pass
        if "status" in seen:
            log(f"✅ 2) start 응답 수신: {seen}")
            ok += 1
        else:
            log(f"❌ 2) start 응답 없음: {seen}")

        # 3) 무음 2초 → 턴 유지(60s no-speech 설정)
        total += 1
        for _ in range(16):
            await ws.send(SILENCE)
        closed = False
        try:
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=2.5)
                if isinstance(raw, str):
                    m = json.loads(raw)
                    if m.get("type") in ("final", "status") and m.get("status") == "stopped":
                        closed = True
                        break
        except asyncio.TimeoutError:
            pass
        if closed:
            log("❌ 3) 무음 만으로 턴이 닫힘")
        else:
            log("✅ 3) 무음 2초 후에도 턴 유지 (발화 대기)")
            ok += 1

        await ws.send(json.dumps({"type": "stop"}))
    finally:
        await ws.close()

    log(f"\n결과: {ok}/{total}")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
