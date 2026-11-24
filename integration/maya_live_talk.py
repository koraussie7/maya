"""맥북 마이크 입력 + 스피커 출력으로 **실제 연결**을 검증하는 CLI.

왜 필요한가 (2026-11-24 실측)
  · 브라우저(privseai.com/maya/)는 서버 WS 가 필요하고→맥북 백엔드 의존 + TLS 인증서 문제
  · 서버 백엔드는 fastapi/uvicorn 이 이 맥에 없다(.venv-voicemem 엔 없음)
  · 로컬 LLM 은 대화에 못 쓴다(실측 안내 문서) / 자체 게이트웨이는 업스트림 unavailable
  그래서 **마이크 → STT → LLM → TTS(클론) → 스피커** 를 맥북에서 한 프로세스로 돌려
  파이프라인 자체를 사람 귀로 확인한다.

흐름
  sox/ffmpeg 로 N초 녹음 → SenseVoice 전사 → LLM(시스템=페르소나) → LocalTTS(클론) → afplay

실행
  # 기본: 6초 녹음, 페르소나 lyra_vex, 로컬 ollama 로 응답(게이트웨이 대신)
  .venv-voicemem/bin/python integration/maya_live_talk.py

  # 게이트웨이가 살아난 뒤
  .venv-voicemem/bin/python integration/maya_live_talk.py \
      --base https://privseai.com/privseai/llm/v1 --model auto/best-fast

  # 텍스트만으로(마이크 없이)
  .venv-voicemem/bin/python integration/maya_live_talk.py --text "안녕"

주의
  · 첫 TTS 호출은 VoxCPM Space 웜업으로 30~60s 걸린다(이후 8s/문장).
  · 소리는 afplay 로 난다 — 누르고 있던 음소거(Mute)를 해제해야 들린다.
"""
from __future__ import annotations

import argparse
import io
import os
import subprocess
import sys
import tempfile
import time
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def record(seconds: float, path: str) -> None:
    """마이크 녹음. sox 우선, 없으면 ffmpeg(avfoundation)."""
    if subprocess.run(["which", "sox"], capture_output=True).returncode == 0:
        subprocess.run(["sox", "-q", "-d", "-c", "1", "-r", "16000", "-b", "16", path,
                        "trim", "0", str(seconds)], check=True)
        return
    if subprocess.run(["which", "ffmpeg"], capture_output=True).returncode == 0:
        # ":0" = 기본 입력 장치
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "avfoundation", "-i", ":0", "-t", str(seconds),
                        "-ac", "1", "-ar", "16000", path], check=True)
        return
    raise SystemExit("sox/ffmpeg 둘 다 없음 — 마이크 녹음 불가")


def transcribe(path: str) -> str:
    sys.path.insert(0, os.path.join(ROOT, "integration"))
    import torch_compat  # noqa: F401  (torch 2.2.2 shim)
    from bench_stt_hard import SenseVoice  # noqa: PLC0415

    with wave.open(path, "rb") as w:
        pcm = w.readframes(w.getnframes())
    text, ms = SenseVoice().transcribe(pcm)
    print(f"[stt] {ms:.0f}ms  {text!r}")
    return text


def llm(system: str, user: str, base: str, model: str, temperature: float,
        timeout: float) -> tuple[str, float]:
    from openai import OpenAI

    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", "ollama"),
                    base_url=base, timeout=timeout, max_retries=0)
    t = time.monotonic()
    r = client.chat.completions.create(
        model=model, temperature=temperature, max_tokens=200,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    )
    return (r.choices[0].message.content or "").strip(), time.monotonic() - t


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


def play(wav: bytes) -> None:
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    with open(path, "wb") as f:
        f.write(wav)
    try:
        subprocess.run(["afplay", path], check=False)
    finally:
        os.unlink(path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=6.0)
    ap.add_argument("--text", default="", help="마이크 대신 이 문장을 입력으로")
    ap.add_argument("--base", default=os.environ.get("OPENAI_BASE", "http://127.0.0.1:11434/v1"))
    ap.add_argument("--model", default=os.environ.get("OPENAI_MODEL", "gemma3:4b"))
    # A = 본인 음성(117Hz), B = 여성 화자(201Hz). 같은 녹음에서 분리한 클립.
    ap.add_argument("--persona", default=os.environ.get("MAYA_PERSONA", "lyra_vex"),
                    choices=["lyra_vex", "lyra_vex_b"],
                    help="lyra_vex = A(본인 음성), lyra_vex_b = B(여성 음성)")
    ap.add_argument("--engine", default=os.environ.get("TTS_ENGINE", "auto"))
    ap.add_argument("--no-tts", action="store_true")
    ap.add_argument("--save", default="/tmp/maya_live")
    ap.add_argument("--llm-timeout", type=float, default=60)
    args = ap.parse_args()

    sys.path.insert(0, os.path.join(ROOT, "maya-ui", "backend"))
    os.environ["MAYA_PERSONA"] = args.persona
    from app.persona import load_persona  # noqa: PLC0415

    p = load_persona()
    print(f"[persona] {p.id} ({p.name})  ref={os.path.basename(p.voice_ref_wav) or '-'}")

    user = args.text
    if not user:
        tmp = tempfile.mktemp(suffix=".wav")
        print(f"[mic] {args.seconds:.0f}초 녹음 — 지금 말하세요")
        t = time.monotonic()
        record(args.seconds, tmp)
        print(f"[mic] 완료 {time.monotonic() - t:.1f}s → 전사")
        user = transcribe(tmp)
        os.unlink(tmp)

    if not user.strip():
        print("[stt] 빈 전사 — 다시 시도 (마이크 입력 장치/볼륨 확인)")
        return 1
    print(f"[user] {user}")

    try:
        reply, dt = llm(p.system_message, user, args.base, args.model, p.temperature,
                        args.llm_timeout)
    except Exception as exc:  # noqa: BLE001
        print(f"[llm] 실패: {type(exc).__name__}: {str(exc)[:160]}")
        print("[힌트] 로컬은 --base http://127.0.0.1:11434/v1 --model gemma3:4b, "
              "게이트웨이는 --base https://privseai.com/privseai/llm/v1")
        return 1
    print(f"[llm] {dt:.2f}s  {reply[:200]}")
    if args.no_tts:
        return 0

    sys.path.insert(0, os.path.join(ROOT, "integration"))
    from tts_local import LocalTTS  # noqa: PLC0415

    tts = LocalTTS(engine=args.engine)
    print(f"[tts] engine={tts.engine}")
    t0 = time.monotonic()
    pcm = tts.synthesize(reply, ref_wav=p.voice_ref_wav, prompt_text=p.voice_prompt_text)
    dt = time.monotonic() - t0
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
    print(f"[tts] {dt:.2f}s 생성 / {secs:.2f}s 오디오 (RTF {dt / max(secs, 1e-6):.2f})")
    if tts.engine != "voxcpm-api" and p.voice_ref_wav:
        print("[경고] 클론 미적용 — 목소리가 페르소나와 다릅니다")
    print("[play] 🔊 스피커 출력")
    play(wav)
    print("[done]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
