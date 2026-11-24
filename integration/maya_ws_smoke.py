#!/usr/bin/env python3
"""Maya 실제 연결 테스트 — 서버의 WebSocket 에 붙어 **맥북 스피커로 소리를 낸다**.

무엇을 증명하나
  "UI 는 뜨는데 대화가 되는가"를 사람 귀가 아니라 파이프라인으로 확인한다:
    텍스트 송신 → 서버(WS) → LLM → TTS(클론) → audio_out(b64 wav) → 이 프로세스가 **재생**
  즉 브라우저/마이크 없이도 서버의 대화 경로 전체와 **목소리**를 검증할 수 있다.

실행
  # 1) 서버(다른 터미널):  cd maya-ui/backend && MAYA_WS_PORT=8787 python -m app.server
  # 2) 테스트:
  .venv-voicemem/bin/python integration/maya_ws_smoke.py --url ws://127.0.0.1:8787/ws \
      --say "안녕, 지금 기분 어때?"

옵션
  --pcm      : audio_out 이 Int16 PCM(raw) 일 때 wav 로 감싸 재생 (서버 구현에 따라 다름)
  --save DIR : 받은 오디오를 파일로 남긴다(나중에 음색 비교용)
  --no-play  : 재생만 끈다(오디오 길이만 확인)
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import struct
import subprocess
import sys
import tempfile
import time
import wave

try:
    import websockets
except ImportError:  # pragma: no cover
    print("websockets 필요: .venv-voicemem/bin/pip install websockets")
    raise SystemExit(2)


def _to_wav(data: bytes, pcm: bool, rate: int = 16000) -> bytes:
    if data[:4] == b"RIFF":
        return data
    if not pcm:
        return data
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data)
    raw = open(path, "rb").read()
    os.unlink(path)
    return raw


def play(wav_bytes: bytes) -> None:
    """맥북 스피커로 재생. afplay 가 가장 정확하고, 없으면 ffplay."""
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    with open(path, "wb") as f:
        f.write(wav_bytes)
    try:
        if subprocess.run(["which", "afplay"], capture_output=True).returncode == 0:
            subprocess.run(["afplay", path], check=False)
        else:
            subprocess.run(
                ["ffplay", "-nodisp", "-autoexit", "-loglevel", "error", path], check=False
            )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


async def run(args) -> int:
    t0 = time.monotonic()
    # 엔진 워밍업(TTS 로딩)이 끝날 시간을 준다. WS 는 바로 받지만 첫 응답만 느리다.
    async with websockets.connect(args.url, ping_interval=20, max_size=None) as ws:
        print(f"[ws] connected {args.url}  ({time.monotonic() - t0:.2f}s)")
        payload = {"text": args.say}
        if args.persona:
            payload["persona"] = args.persona
        await ws.send(json.dumps({"type": "text", "payload": payload}))
        print(f"[user] {args.say}")

        buf: list[str] = []
        audio: bytes | None = None
        t_send = time.monotonic()
        t_first: float | None = None
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=args.timeout)
            except asyncio.TimeoutError:
                print(f"[ws] {args.timeout}s 응답 없음 — 타임아웃")
                return 1
            msg = json.loads(raw)
            typ = msg.get("type")
            # 서버는 type/payload 로 보내지만 일부 구현은 최상위에 payload 를 펼친다.
            payload = msg.get("payload") or {k: v for k, v in msg.items() if k != "type"}

            if typ == "assistant_delta":
                if t_first is None:
                    t_first = time.monotonic() - t_send
                    print(f"[llm] first token {t_first:.2f}s")
                buf.append(payload.get("text", ""))
            elif typ == "assistant_final":
                buf = [payload.get("text", "".join(buf))]
                print(f"[llm] reply ({time.monotonic() - t_send:.2f}s): {''.join(buf)[:200]}")
            elif typ == "audio_out":
                audio = base64.b64decode(payload.get("audio_b64", ""))
                print(f"[tts] audio {len(audio) / 1024:.1f}KB "
                      f"({time.monotonic() - t_send:.2f}s after send)")
            elif typ == "error":
                print(f"[error] {payload}")
            if audio is not None and buf:
                break

        if audio is None:
            print("[tts] 오디오 없음 — TTS 엔진/설정 확인 (TTS_ENGINE, VOXCPM_API_URL)")
            return 1

        wav = _to_wav(audio, args.pcm)
        if args.save:
            os.makedirs(args.save, exist_ok=True)
            out = os.path.join(args.save, f"reply_{int(time.time())}.wav")
            with open(out, "wb") as f:
                f.write(wav)
            print(f"[save] {out}")
        if wav[:4] == b"RIFF":
            with wave.open(__import__("io").BytesIO(wav), "rb") as w:
                secs = w.getnframes() / w.getframerate()
            print(f"[tts] {secs:.2f}s 오디오")
        if not args.no_play:
            print("[play] 스피커로 재생")
            play(wav)
    print(f"[done] 총 {time.monotonic() - t0:.2f}s")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://127.0.0.1:8787/ws")
    ap.add_argument("--say", default="안녕, 오늘 하루 어땠어?")
    ap.add_argument("--persona", default="", choices=["", "lyra_vex", "lyra_vex_b"],
                    help="서버에서 그 턴의 목소리를 바꾼다 (B=여성)")
    ap.add_argument("--timeout", type=float, default=90.0)
    ap.add_argument("--pcm", action="store_true", help="audio_out 이 raw PCM16 일 때")
    ap.add_argument("--save", default="")
    ap.add_argument("--no-play", action="store_true")
    args = ap.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
