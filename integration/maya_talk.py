#!/usr/bin/env python3
"""마이크 → 서버 → 스피커 실대화 루프 (프론트엔드 프로토콜 준수판).

왜 다시 만들었나 (2026-11-24)
  `maya_voice_loop.py` 는 `{type, payload}` 로 보냈지만, **브라우저 클라이언트
  (`maya-ui/app/src/lib/mayaClient.js`)는 payload 를 쓰지 않고 필드를 최상위에 둔다**:
    요청: {type:'session.start'} → {type:'audio.chunk', data, seq} → {type:'audio.flush', seq}
          {type:'chat.text', text}
    응답: stt.final{text} / chat.partial{id,text} / chat.final{id,text}
          audio.out{id,mime,data} / session.state{phase} / error{code,message}
  즉 두 프로토콜이 달라서 서버가 요청을 못 알아듣고 응답을 오지 않았다(20~60초 대기만 남음).
  이 파일은 **프론트와 동일한 형식**을 쓴다 — 그리고 응답도 최상위 필드로 읽는다.

실행
  # 마이크 5초 → 대화 → 스피커
  .venv-voicemem/bin/python integration/maya_talk.py --url ws://127.0.0.1:8788/ws --persona lyra_vex_b

  # 마이크 없이 텍스트
  .venv-voicemem/bin/python integration/maya_talk.py --text "안녕?"
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import os
import subprocess
import tempfile
import time
import wave

import websockets

CHUNK_BYTES = 16000 * 2 // 5   # 200ms 분량


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
        try:
            os.unlink(path)
        except OSError:
            pass


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


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://127.0.0.1:8788/ws")
    ap.add_argument("--persona", default="lyra_vex_b")
    ap.add_argument("--seconds", type=float, default=5.0)
    ap.add_argument("--text", default="")
    ap.add_argument("--timeout", type=float, default=240)
    ap.add_argument("--save", default="/tmp/maya_live")
    ap.add_argument("--no-play", action="store_true")
    args = ap.parse_args()

    async with websockets.connect(args.url, ping_interval=20, max_size=None) as ws:
        print(f"[ws] {args.url} 연결")
        t0 = time.monotonic()

        # ① 세션 시작 (프론트와 동일)
        start = {"type": "session.start"}
        if args.persona:
            start["persona"] = args.persona
        await ws.send(json.dumps(start))

        # ② 입력: 마이크면 audio.chunk/flush, 아니면 chat.text
        if args.text:
            await ws.send(json.dumps({"type": "chat.text", "text": args.text,
                                      **({"persona": args.persona} if args.persona else {})}))
            print(f"[user] {args.text}")
        else:
            print(f"[mic] {args.seconds:.0f}초 녹음 — 지금 말하세요")
            pcm = record(args.seconds)
            n = 0
            for i in range(0, len(pcm), CHUNK_BYTES):
                chunk = pcm[i:i + CHUNK_BYTES]
                await ws.send(json.dumps({
                    "type": "audio.chunk", "data": base64.b64encode(chunk).decode(),
                    "seq": n,
                }))
                n += 1
            await ws.send(json.dumps({"type": "audio.flush", "seq": n}))
            print(f"[mic] {len(pcm) / 2 / 16000:.1f}s / {n}청크 전송 → 서버 STT 대기")

        audio: bytes | None = None
        reply = ""
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=args.timeout)
            msg = json.loads(raw)
            typ = msg.get("type")
            et = time.monotonic() - t0
            if typ == "stt.final":
                print(f"[stt] {et:.2f}s  {msg.get('text')!r}")
            elif typ == "chat.partial":
                reply += msg.get("text", "")
            elif typ == "chat.final":
                reply = msg.get("text", reply)
                print(f"[llm] {et:.2f}s  {reply[:200]}")
            elif typ == "audio.out":
                data = base64.b64decode(msg.get("data", ""))
                # 빈 오디오(44바이트 wav 헤더만)를 받아도 성공처럼 보인다 → 실제 길이로 판정
                if len(data) <= 64:
                    print(f"[tts] {et:.2f}s  빈 오디오({len(data)}B) — 서버 TTS 로그 확인")
                    continue
                audio = data
                print(f"[tts] {et:.2f}s  {len(audio) / 1024:.1f}KB  ({msg.get('mime')})")
            elif typ == "session.state":
                print(f"[state] {msg.get('phase')}")
            elif typ == "error":
                print(f"[error] {msg.get('code')}: {msg.get('message')}")
            if audio is not None:
                break

        if audio is None:
            print("[tts] 유음 오디오 없음 — 백엔드 로그에서 voxcpm/piper/say 를 확인")
            return 1
        wav = as_wav(audio)
        if args.save:
            os.makedirs(args.save, exist_ok=True)
            out = os.path.join(args.save, f"turn_{int(time.time())}.wav")
            with open(out, "wb") as f:
                f.write(wav)
            print(f"[save] {out}")
        with wave.open(io.BytesIO(wav), "rb") as w:
            print(f"[tts] {w.getnframes() / w.getframerate():.2f}s 오디오")
        if not args.no_play:
            print("[play] 🔊 스피커 출력")
            play(wav)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130)
