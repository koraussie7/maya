#!/usr/bin/env python3
"""마이크 → WebSocket(서버) → 스피커. **실제 음성 대화**를 맥북에서 끝까지 태운다.

무엇을 하나
  1) 마이크 N초 녹음(sox 또는 ffmpeg) → PCM16 16kHz
  2) `audio.flush` + `chat.text`(전사문) 로 서버에 보냄 — STT 는 서버가 하고,
     브라우저와 같은 프로토콜을 그대로 쓴다
  3) 돌아오는 `stt.final` / `assistant.delta` / `audio.out` 을 받아 **스피커로 재생**

왜 이 방식인가
  · 서버 WS 주소가 맥북이면 TLS/도메인 문제가 없다(브라우저 대신 CLI 가 접속)
  · STT 를 서버에 맡기므로 이 스크립트에는 무거운 의존성이 없다

실행
  .venv-voicemem/bin/python integration/maya_voice_loop.py \
      --url ws://127.0.0.1:8788/ws --persona lyra_vex_b --seconds 5

  # 텍스트로(마이크 테스트 없이)
  .venv-voicemem/bin/python integration/maya_voice_loop.py --text "안녕?"
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import wave

import websockets


def record(seconds: float) -> bytes:
    tmp = tempfile.mktemp(suffix=".wav")
    if subprocess.run(["which", "sox"], capture_output=True).returncode == 0:
        subprocess.run(["sox", "-q", "-d", "-c", "1", "-r", "16000", "-b", "16", tmp,
                        "trim", "0", str(seconds)], check=True)
    else:
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "avfoundation", "-i", ":0", "-t", str(seconds),
                        "-ac", "1", "-ar", "16000", tmp], check=True)
    with wave.open(tmp, "rb") as w:
        pcm = w.readframes(w.getnframes())
    os.unlink(tmp)
    return pcm


def play(wav: bytes) -> None:
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    with open(path, "wb") as f:
        f.write(wav)
    try:
        subprocess.run(["afplay", path], check=False)
    finally:
        os.unlink(path)


def as_wav(data: bytes) -> bytes:
    if data[:4] == b"RIFF":
        return data
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(data)
    return buf.getvalue()


async def one_turn(args, ws) -> bool:
    payload: dict = {}
    if args.text:
        payload["text"] = args.text
    else:
        print(f"[mic] {args.seconds:.0f}초 녹음 — 지금 말하세요")
        pcm = record(args.seconds)
        print(f"[mic] {len(pcm) / 2 / 16000:.1f}s 수신 → 서버로 전송(audio.flush)")
        await ws.send(json.dumps({
            "type": "audio.flush",
            "payload": {"audio_b64": base64.b64encode(pcm).decode("ascii")},
        }))
        payload = {}
    if args.persona:
        payload["persona"] = args.persona
    if payload:
        await ws.send(json.dumps({"type": "chat.text", "payload": payload}))

    t0 = time.monotonic()
    audio: bytes | None = None
    reply = ""
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=args.timeout)
        msg = json.loads(raw)
        typ = msg.get("type")
        p = msg.get("payload") or {k: v for k, v in msg.items() if k != "type"}
        if typ == "stt.final":
            print(f"[stt] {time.monotonic() - t0:.2f}s  {p.get('text')!r}")
            if p.get("text"):
                # 음성 입력이면 서버가 전사 후 자동으로 대화를 진행한다.
                # 서버 구현이 전사만 하고 끝나는 경우를 대비해 텍스트로 한 번 더 보낸다.
                if not args.text and not args.no_resend:
                    await ws.send(json.dumps({
                        "type": "chat.text",
                        "payload": {"text": p["text"], **(
                            {"persona": args.persona} if args.persona else {})},
                    }))
        elif typ == "assistant.delta":
            reply += p.get("text", "")
        elif typ == "assistant.final":
            reply = p.get("text", reply)
            print(f"[llm] {time.monotonic() - t0:.2f}s  {reply[:200]}")
        elif typ == "persona.changed":
            print(f"[persona] {p.get('id')} (voice={p.get('voice')})")
        elif typ == "audio.out":
            audio = base64.b64decode(p.get("audio_b64", ""))
            print(f"[tts] {time.monotonic() - t0:.2f}s  {len(audio) / 1024:.1f}KB")
        elif typ == "error":
            print(f"[error] {p}")
        if audio is not None:
            break
        if typ == "phase" and p.get("phase") == "idle" and reply:
            break

    if audio is None:
        print("[tts] 오디오 없음 — 서버 TTS 설정 확인(TTS_ENGINE, VOXCPM_API_URL)")
        return False
    wav = as_wav(audio)
    if args.save:
        os.makedirs(args.save, exist_ok=True)
        out = os.path.join(args.save, f"turn_{int(time.time())}.wav")
        with open(out, "wb") as f:
            f.write(wav)
        print(f"[save] {out}")
    with wave.open(io.BytesIO(wav), "rb") as w:
        secs = w.getnframes() / w.getframerate()
    print(f"[tts] {secs:.2f}s 오디오  (전체 {time.monotonic() - t0:.2f}s)")
    print("[play] 🔊 스피커 출력")
    play(wav)
    return True


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://127.0.0.1:8788/ws")
    ap.add_argument("--persona", default="lyra_vex_b")
    ap.add_argument("--seconds", type=float, default=5.0)
    ap.add_argument("--text", default="")
    ap.add_argument("--timeout", type=float, default=240)
    ap.add_argument("--save", default="/tmp/maya_live")
    ap.add_argument("--no-resend", action="store_true",
                    help="서버가 stt.final 뒤 자동 진행할 때 이중 전송 방지")
    ap.add_argument("--turns", type=int, default=1)
    args = ap.parse_args()

    async with websockets.connect(args.url, ping_interval=20, max_size=None) as ws:
        print(f"[ws] {args.url} 연결 (persona={args.persona})")
        ok = True
        for i in range(args.turns):
            if args.turns > 1:
                print(f"\n--- 턴 {i + 1}/{args.turns} ---")
            ok = await one_turn(args, ws) and ok
            if not ok:
                break
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130)
