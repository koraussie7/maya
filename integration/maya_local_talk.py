#!/usr/bin/env python3
"""마이크 없이 대화 경로 전체를 태우는 로컬 러너 (맥북 스피커 출력).

왜 따로 있나
  `maya-ui/backend` 는 fastapi/uvicorn 이 필요하고(이 맥의 .venv-voicemem 엔 없음),
  STT 는 faster-whisper/whisper 를 찾는다. 지금 확인하려는 것은 **대화 경로 + 클론 음성**이므로,
  텍스트를 넣고 TTS 를 스피커로 재생하는 최소 러너가 더 빠르고 실패 지점도 분명하다.

경로
  텍스트 → LLM(자체 게이트웨이 privseai.com/privseai/llm) → 페르소나 system → TTS(클론) → **afplay**

실행
  .venv-voicemem/bin/python integration/maya_local_talk.py --say "안녕, 오늘 하루 어땠어?"
  TTS_ENGINE=say .venv-voicemem/bin/python integration/maya_local_talk.py --say "테스트"

옵션
  --engine voxcpm-api|piper|say   (기본: 환경변수 TTS_ENGINE=auto)
  --save DIR                       오디오 파일 보관(음색 비교용)
  --no-tts                         LLM 응답만 확인
"""
from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def llm_reply(system: str, user: str, base: str, model: str, timeout: float,
              temperature: float) -> tuple[str, float]:
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "stream": False,
        "temperature": temperature,
        "max_tokens": 200,
    }).encode()
    url = f"{base.rstrip('/')}/chat/completions"
    req = urllib.request.Request(url, data=payload, headers={
        "Content-Type": "application/json",
        # 자체 게이트웨이는 키 없이 동작한다(실사 확인). 있으면 넣어 보낸다.
        **({"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}
           if os.environ.get("OPENAI_API_KEY") else {}),
    })
    t = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as exc:
        body = exc.read()[:300].decode("utf-8", "replace")
        raise SystemExit(f"LLM HTTP {exc.code}: {body}") from exc
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"LLM 실패: {type(exc).__name__}: {exc}") from exc
    return data["choices"][0]["message"]["content"].strip(), time.monotonic() - t


def to_wav(pcm: bytes, rate: int = 16000) -> bytes:
    if pcm[:4] == b"RIFF":
        return pcm
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def play(wav: bytes) -> float:
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    with open(path, "wb") as f:
        f.write(wav)
    try:
        with wave.open(path, "rb") as w:
            secs = w.getnframes() / w.getframerate()
        if shutil_which("afplay"):
            code = subprocess.run(["afplay", path], check=False).returncode
        else:
            code = subprocess.run(["ffplay", "-nodisp", "-autoexit", "-loglevel", "error", path],
                                  check=False).returncode
        if code != 0:
            print(f"[play] 재생 실패(exit {code})")
        return secs
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def shutil_which(bin_: str) -> bool:
    return subprocess.run(["which", bin_], capture_output=True).returncode == 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--say", default="안녕, 오늘 하루 어땠어?")
    ap.add_argument("--base", default=os.environ.get(
        "OPENAI_BASE", "https://privseai.com/privseai/llm/v1"))
    ap.add_argument("--model", default=os.environ.get("OPENAI_MODEL", "auto/best-fast"))
    ap.add_argument("--persona", default=os.environ.get("MAYA_PERSONA", "lyra_vex"))
    ap.add_argument("--engine", default=os.environ.get("TTS_ENGINE", "auto"))
    ap.add_argument("--timeout", type=float, default=40)
    ap.add_argument("--save", default="")
    ap.add_argument("--no-tts", action="store_true")
    args = ap.parse_args()

    sys.path.insert(0, os.path.join(ROOT, "maya-ui", "backend"))
    os.environ["MAYA_PERSONA"] = args.persona
    from app.persona import load_persona  # noqa: PLC0415

    p = load_persona()
    print(f"[persona] {p.id} ({p.name})  ref={os.path.basename(p.voice_ref_wav) or '-'}")
    print(f"[user] {args.say}")

    try:
        reply, llm_s = llm_reply(p.system_message, args.say, args.base, args.model,
                                 args.timeout, p.temperature)
    except SystemExit as exc:
        print(f"[llm] {exc}")
        print("[힌트] 게이트웨이 업스트림이 unavailable 이면 `--base https://openrouter.ai/api/v1` "
              "또는 로컬(`--base http://127.0.0.1:11434/v1 --model gemma3:4b`)로 대화만 확인 가능")
        return 1
    print(f"[llm] {reply[:300]}")
    print(f"[llm] {llm_s:.2f}s")
    if args.no_tts:
        return 0

    sys.path.insert(0, os.path.join(ROOT, "integration"))
    from tts_local import LocalTTS  # noqa: PLC0415

    tts = LocalTTS(engine=args.engine)
    print(f"[tts] engine={tts.engine}")
    t0 = time.monotonic()
    pcm = tts.synthesize(reply, ref_wav=p.voice_ref_wav, prompt_text=p.voice_prompt_text)
    tts_s = time.monotonic() - t0
    if not pcm:
        print("[tts] 오디오 없음")
        return 1
    wav = to_wav(pcm)
    if args.save:
        os.makedirs(args.save, exist_ok=True)
        out = os.path.join(args.save, f"reply_{int(time.time())}.wav")
        with open(out, "wb") as f:
            f.write(wav)
        print(f"[save] {out}")
    with wave.open(io.BytesIO(wav), "rb") as w:
        secs = w.getnframes() / w.getframerate()
    print(f"[tts] {tts_s:.2f}s 생성 / {secs:.2f}s 오디오 (RTF {tts_s / max(secs, 1e-6):.2f})")
    if tts.engine != "voxcpm-api" and p.voice_ref_wav:
        print("[경고] 클론이 적용되지 않았습니다(엔진 폴백) — 목소리가 페르소나와 다릅니다")
    print("[play] 스피커 출력")
    play(wav)
    print("[done]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
