"""maya-ui UI/voice pipeline smoke + latency check.

- Connects to the actual FastAPI backend the browser would hit (ws://127.0.0.1:8001/ws)
- Uses the same JSON protocol the UI sends: session.start, audio.chunk, audio.flush,
  chat.text. Also tests a text-only turn (no mic) for the LLM/TTS path.
- Times each phase:
    T0 = flush / chat.text sent
    T1 = stt.final (if audio) | assistant.delta[0] | phase=thinking
    T2 = phase=speaking
    T3 = first audio.out arrival (when first byte reaches us)
- Saves the first TTS audio chunk to /tmp/maya_check_first_tts.wav.

Run:
    .venv-voicem/bin/python integration/maya_ui_pipeline_check.py
"""
from __future__ import annotations
import asyncio
import base64
import json
import os
import struct
import sys
import time
import wave
from pathlib import Path

import websockets  # type: ignore


WS_URL = os.environ.get("MAYA_WS_URL", "ws://127.0.0.1:8001/ws")


def synth_silence_pcm16(seconds: float = 1.5, rate: int = 16000) -> bytes:
    """모의 마이크: 무음 1.5초. STT는 무음을 무시할 가능성이 높아서
    텍스트 입력 경로도 같이 시험한다.
    """
    n = int(seconds * rate)
    return b"\x00\x00" * n


async def handshake(ws):
    """세션 시작 — session.start 를 보내고 session.ready 가 오는지 확인."""
    await ws.send(json.dumps({"type": "session.start", "refAudioId": "clip_ref.wav"}))
    ready_at = None
    try:
        for _ in range(20):
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
            msg = json.loads(raw)
            if msg.get("type") == "session.ready":
                ready_at = time.monotonic()
                print(f"[ready] in {(ready_at - T0)*1000:.0f}ms — payload={msg}")
                return ready_at
    except asyncio.TimeoutError:
        print("[ready] TIMEOUT", file=sys.stderr)
        return None


async def text_turn(ws, text: str, label: str):
    """텍스트 한 턴 — UI 의 'sendText' 와 동일."""
    print(f"\n===== {label}: text={text!r} =====")
    times = {"sent": time.monotonic()}
    last_phase = None
    first_delta = None
    first_speaking = None
    first_audio = None
    audio_count = 0
    audio_bytes_total = 0
    final_text = None
    await ws.send(json.dumps({"type": "chat.text", "text": text}))
    try:
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=120)
            msg = json.loads(raw)
            t = msg.get("type")
            if t == "phase":
                last_phase = msg.get("phase")
                if last_phase == "thinking" and "first_thinking" not in times:
                    times["first_thinking"] = time.monotonic()
                elif last_phase == "speaking" and first_speaking is None:
                    first_speaking = time.monotonic()
                    times["first_speaking"] = first_speaking
                elif last_phase == "idle":
                    times["idle"] = time.monotonic()
                    break
            elif t == "assistant.delta":
                if first_delta is None:
                    first_delta = time.monotonic()
                    times["first_delta"] = first_delta
                    print(f"[delta0] in {(first_delta - times['sent'])*1000:.0f}ms — {msg.get('text','')!r}")
            elif t == "assistant.final":
                final_text = msg.get("text", "")
                times["assistant_final"] = time.monotonic()
            elif t == "audio.out":
                audio_count += 1
                if first_audio is None:
                    first_audio = time.monotonic()
                    times["first_audio"] = first_audio
                    payload_bytes = len(base64.b64decode(msg.get("data") or msg.get("audio_b64") or ""))
                    print(f"[first audio.out] in {(first_audio - times['sent'])*1000:.0f}ms — "
                          f"{payload_bytes}B mime={msg.get('mime')} seq={msg.get('seq')} text={msg.get('text','')!r}")
                    # 첫 오디오 청크 저장 (디코드용)
                    Path("/tmp/maya_check_first_tts.wav").write_bytes(
                        base64.b64decode(msg.get("data") or msg.get("audio_b64") or "")
                    )
                audio_bytes_total += len(base64.b64decode(msg.get("data") or msg.get("audio_b64") or ""))
            elif t == "decision.log":
                print(f"[decision] route={msg.get('route_id')} source={msg.get('source')} "
                      f"score={msg.get('score')} abstained={msg.get('abstained')}")
            elif t == "route.match":
                print(f"[route.match] id={msg.get('id')} say={msg.get('say')!r}")
            elif t == "error":
                print(f"[ERROR] {msg}", file=sys.stderr)
                break
    except asyncio.TimeoutError:
        print("[idle] timeout — 서버 응답이 끊겼음", file=sys.stderr)

    return {
        "label": label,
        "audio_count": audio_count,
        "audio_bytes": audio_bytes_total,
        "final_text": final_text,
        "times": times,
        "last_phase": last_phase,
    }


async def audio_turn(ws, pcm: bytes, label: str):
    """오디오 한 턴 — UI 의 sendAudioChunk/flushAudio 와 동일."""
    print(f"\n===== {label}: pcm={len(pcm)}B =====")
    times = {"sent": time.monotonic()}
    await ws.send(json.dumps({"type": "session.start"}))
    seq = 0
    # 100ms 청크
    chunk = 16000 * 2 // 10  # 3200 bytes = 100ms of 16k mono PCM16
    for off in range(0, len(pcm), chunk):
        seq += 1
        b64 = base64.b64encode(pcm[off:off+chunk]).decode("ascii")
        await ws.send(json.dumps({"type": "audio.chunk", "seq": seq, "data": b64}))
    await ws.send(json.dumps({"type": "audio.flush", "seq": seq}))
    stt = None
    try:
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=120)
            msg = json.loads(raw)
            t = msg.get("type")
            if t == "stt.final":
                stt = msg.get("text", "")
                times["stt_final"] = time.monotonic()
                print(f"[stt.final] in {(times['stt_final']-times['sent'])*1000:.0f}ms — {stt!r}")
                # stt 가 빈 경우(무음) 라우터는 안 도는 게 정상
                if not stt:
                    print("[stt] empty → 서버가 idle 로 복귀하는지 확인")
            elif t == "phase":
                if msg.get("phase") == "idle":
                    times["idle"] = time.monotonic()
                    break
    except asyncio.TimeoutError:
        pass
    return {"label": label, "stt": stt, "times": times}


async def main():
    global T0
    T0 = time.monotonic()
    print(f"[connect] {WS_URL}")
    async with websockets.connect(WS_URL, max_size=None, open_timeout=30) as ws:
        await handshake(ws)
        # 1) 텍스트 자유 대화 — 가장 일반적 경로
        r1 = await text_turn(ws, "오늘 하루 어땠어?", "free-text-1")
        # 2) 짧은 응답 요청 — TTS 첫 청크 도달 시간 확인
        r2 = await text_turn(ws, "안녕", "free-text-2")
        # 3) 오디오 한 턴(무음) — STT/LLM/TTS 도달하는지
        r3 = await audio_turn(ws, synth_silence_pcm16(1.5), "audio-silence")
    # 결과 요약
    print("\n=========== SUMMARY ===========")
    for r in (r1, r2):
        t = r["times"]
        if "first_audio" in t and "sent" in t:
            print(f"{r['label']}: first_audio={(t['first_audio']-t['sent'])*1000:.0f}ms  "
                  f"chunks={r['audio_count']}  bytes={r['audio_bytes']}  "
                  f"final={(r['final_text'] or '')[:60]!r}")
        else:
            print(f"{r['label']}: no audio out — times={list(t.keys())}")
    print(f"\nfirst TTS audio saved at: /tmp/maya_check_first_tts.wav "
          f"({Path('/tmp/maya_check_first_tts.wav').stat().st_size if Path('/tmp/maya_check_first_tts.wav').exists() else 0}B)")


if __name__ == "__main__":
    asyncio.run(main())
