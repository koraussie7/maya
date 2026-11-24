#!/usr/bin/env python3
"""HF Space(VoxCPM2)에서 B(여성) 목소리로 문장 하나를 만들어 **맥북 스피커로 재생**.

상황 (2026-11-24)
  · Space 는 지금 정상 응답한다(클론 출력 음색/발음 검증 완료: F0 192Hz, CER 0)
  · 그런데 `tts_local` 경유 경로는 **0.19초짜리 무음(전 샘플 0)** 을 돌려준다 —
    Space 가 WAV 경로가 아닌 다른 결과를 줄 때 생성기가 실패를 조용히 삼키기 때문.
  · 대화 파이프라인(백엔드)까지 가기 전에 "목소리만" 확실히 들려주는 것이 목적.

실행
  .venv-voicemem/bin/python integration/voxcpm_speak.py --text "안녕, 반가워" --voice b
  .venv-voicemem/bin/python integration/voxcpm_speak.py --text "테스트" --voice a --no-play

동작
  gradio_client 로 Space 를 직접 호출 → 반환값(경로/URL/dict/tuple)을 정규화 →
  ffmpeg 로 16kHz mono 로 정규화 → wav 저장 → afplay 로 재생.
  반환값이 오디오가 아니면 **원문을 그대로 출력**하고 실패로 끝난다(조용한 무음 금지).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VOICES = {
    "a": ("maya/models/tts/reference/clip_ref_speaker_a.wav",
          "그 리니트가 딱 나와있 있고 하루에 삼십분밖 통화도 못 된다고래고 오빠가 엄청"),
    "b": ("maya/models/tts/reference/clip_ref_speaker_b.wav",
          "그렇게 아끼는 사람이 새벽까지 내 목소리 하나 따겠다고"),
}


def normalize(result) -> str | None:
    """Space 반환값 → 로컬 오디오 경로(없으면 None)."""
    if isinstance(result, (tuple, list)):
        for x in result:
            p = normalize(x)
            if p:
                return p
        return None
    if isinstance(result, dict):
        p = result.get("path") or result.get("url") or result.get("name")
        return p if isinstance(p, str) else None
    if isinstance(result, str):
        low = result.lower()
        if low.startswith("http") or low.endswith((".wav", ".mp3", ".m4a", ".flac", ".ogg")):
            return result
    return None


def to_16k_pcm(src: str) -> bytes:
    tmp = tempfile.mktemp(suffix=".wav")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src,
                    "-ac", "1", "-ar", "16000", "-f", "wav", tmp], check=True)
    data = open(tmp, "rb").read()
    os.unlink(tmp)
    return data


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", default="안녕, 반가워")
    ap.add_argument("--voice", choices=list(VOICES), default="b")
    ap.add_argument("--space", default=os.environ.get(
        "VOXCPM_API_URL", "https://openbmb-voxcpm-tts.hf.space"))
    ap.add_argument("--save", default="/tmp/maya_live")
    ap.add_argument("--no-play", action="store_true")
    ap.add_argument("--prompt-text", default="", help="클립 전사문 덮어쓰기")
    args = ap.parse_args()

    ref_rel, prompt_text = VOICES[args.voice]
    ref = os.path.join(ROOT, ref_rel)
    if not os.path.exists(ref):
        print(f"[err] 레퍼런스 클립 없음: {ref}")
        return 1
    if args.prompt_text:
        prompt_text = args.prompt_text

    from gradio_client import Client, handle_file

    print(f"[space] {args.space}")
    print(f"[voice] {args.voice.upper()}  ref={os.path.basename(ref)}")
    print(f"[text ] {args.text}")
    t0 = time.monotonic()
    try:
        client = Client(args.space, verbose=False)
    except Exception as exc:  # noqa: BLE001
        print(f"[err] Space 접속 실패: {type(exc).__name__}: {exc}")
        print("      Space 가 잠들었을 수 있습니다 — 브라우저로 한 번 열어 깨우세요.")
        return 1

    try:
        result = client.predict(
            text_input=args.text,
            control_instruction="",
            reference_wav_path_input=handle_file(ref),
            use_prompt_text=True,
            prompt_text_input=prompt_text,
            cfg_value_input=2.0,
            do_normalize=False,
            denoise=False,
            api_name="/generate",
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[err] 생성 실패: {type(exc).__name__}: {exc}")
        return 1
    dt = time.monotonic() - t0
    print(f"[gen] {dt:.1f}s  raw={str(result)[:100]}")

    path = normalize(result)
    if not path:
        print("[err] 반환값이 오디오가 아님 — 위 raw 를 확인하세요")
        return 1
    if path.startswith("http"):
        tmp = tempfile.mktemp(suffix=".wav")
        subprocess.run(["curl", "-sL", "-o", tmp, path], check=True)
        path = tmp

    wav = to_16k_pcm(path)
    os.makedirs(args.save, exist_ok=True)
    out = os.path.join(args.save, f"voice_{args.voice}_{int(time.time())}.wav")
    with open(out, "wb") as f:
        f.write(wav)
    import wave as _w

    with _w.open(out, "rb") as w:
        secs = w.getnframes() / w.getframerate()
        frames = w.getnframes()
    print(f"[audio] {secs:.2f}s ({frames} frames) → {out}")
    if secs < 0.5:
        print("[err] 너무 짧음 — Space 가 빈 오디오를 돌려줬습니다")
        return 1
    if not args.no_play:
        print("[play] 🔊 맥북 스피커로 재생")
        subprocess.run(["afplay", out], check=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
