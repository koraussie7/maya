"""ElevenLabs TTS 합성 — IVC로 만든 voice_id 로 음성 생성.

사용:
  export ELEVENLABS_API_KEY="sk_..."
  export ELEVENLABS_VOICE_ID="<voice_id>"
  python integration/lyra_self_clone/ivc_tts.py \
      --text "안녕하세요, 오늘 하루 어땠어요?" \
      --out integration/lyra_self_clone/test_ko.wav \
      --lang ko

  python integration/lyra_self_clone/ivc_tts.py \
      --text "Hello, how was your day?" \
      --out integration/lyra_self_clone/test_en.wav \
      --lang en

출력:
  지정한 경로에 mp3 (ElevenLabs 응답은 mp3/mpeg-stream)
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from urllib import error, request

API_BASE = "https://api.elevenlabs.io/v1"

# 한국어/영어 권장 모델
MODEL_KO = "eleven_multilingual_v2"  # 한국어 권장
MODEL_EN = "eleven_multilingual_v2"  # 영어도 동일 모델 권장 (안정적)


def tts(api_key: str, voice_id: str, text: str, model: str, out: Path) -> int:
    url = f"{API_BASE}/text-to-speech/{voice_id}"
    payload = {
        "text": text,
        "model_id": model,
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75,
            "style": 0.0,
            "use_speaker_boost": True,
        },
    }
    req = request.Request(
        url,
        data=__import__("json").dumps(payload).encode(),
        method="POST",
        headers={
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        },
    )
    try:
        with request.urlopen(req, timeout=120) as resp:
            data = resp.read()
    except error.HTTPError as e:
        sys.stderr.write(f"[tts] HTTP {e.code}: {e.read().decode(errors='replace')}\n")
        return 1
    out.write_bytes(data)
    print(f"[tts] saved → {out} ({len(data)} bytes)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--voice", default=None, help="voice_id (default: $ELEVENLABS_VOICE_ID)")
    ap.add_argument("--model", default=None)
    ap.add_argument("--lang", default="ko", choices=["ko", "en"])
    args = ap.parse_args()

    api_key = os.environ.get("ELEVENLABS_API_KEY")
    voice_id = args.voice or os.environ.get("ELEVENLABS_VOICE_ID")
    if not api_key:
        sys.stderr.write("ELEVENLABS_API_KEY 비어있음\n")
        return 2
    if not voice_id:
        sys.stderr.write("voice_id 비어있음 (--voice 또는 $ELEVENLABS_VOICE_ID)\n")
        return 2

    model = args.model or (MODEL_KO if args.lang == "ko" else MODEL_EN)
    return tts(api_key, voice_id, args.text, model, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
