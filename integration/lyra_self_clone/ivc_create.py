"""ElevenLabs IVC — 본인 목소리 즉시 클론 생성.

사용:
  export ELEVENLABS_API_KEY="sk_..."
  python integration/lyra_self_clone/ivc_create.py \
      --ref integration/lyra_self_clone/ref.wav \
      --name "Lyra Vex Self" \
      --description "Self-recorded Korean voice for Maya voice assistant"

출력:
  integration/lyra_self_clone/voice_id.txt (voice_id 저장)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib import error, parse, request

API_BASE = "https://api.elevenlabs.io/v1"
HERE = Path(__file__).resolve().parent


def create_ivc_voice(api_key: str, ref_path: Path, name: str, description: str) -> str:
    boundary = "----maya-lyra-boundary-7c9b1c"
    body = []
    # name
    body.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"name\"\r\n\r\n{name}\r\n".encode())
    # description
    body.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"description\"\r\n\r\n{description}\r\n".encode())
    # remove_background_noise
    body.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"remove_background_noise\"\r\n\r\ntrue\r\n".encode())
    # labels (JSON string)
    labels = json.dumps({"language": "ko", "accent": "korean", "gender": "unspecified"}, ensure_ascii=False)
    body.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"labels\"\r\n\r\n{labels}\r\n".encode())
    # file
    file_bytes = ref_path.read_bytes()
    body.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"{ref_path.name}\"\r\n"
        f"Content-Type: audio/wav\r\n\r\n".encode()
    )
    body.append(file_bytes)
    body.append(b"\r\n")
    body.append(f"--{boundary}--\r\n".encode())

    body_bytes = b"".join(body)

    req = request.Request(
        f"{API_BASE}/voices/add",
        data=body_bytes,
        method="POST",
        headers={
            "xi-api-key": api_key,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
        },
    )
    try:
        with request.urlopen(req, timeout=300) as resp:
            data = json.loads(resp.read().decode())
    except error.HTTPError as e:
        sys.stderr.write(f"[ivc] HTTP {e.code}: {e.read().decode(errors='replace')}\n")
        raise
    return data["voice_id"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", type=Path, required=True, help="reference WAV path (mono, 16kHz)")
    ap.add_argument("--name", default="Lyra Vex Self")
    ap.add_argument("--description", default="Self-recorded Korean voice for Maya voice assistant")
    args = ap.parse_args()

    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        sys.stderr.write("ELEVENLABS_API_KEY env가 비어있음\n")
        return 2
    if not args.ref.exists():
        sys.stderr.write(f"reference 파일 없음: {args.ref}\n")
        return 2

    voice_id = create_ivc_voice(api_key, args.ref, args.name, args.description)
    out = HERE / "voice_id.txt"
    out.write_text(voice_id + "\n")
    print(f"[ivc] voice_id={voice_id}")
    print(f"[ivc] saved → {out}")
    print()
    print("다음 단계:")
    print(f"  export ELEVENLABS_VOICE_ID={voice_id}")
    print("  python integration/lyra_self_clone/ivc_tts.py --text \"안녕하세요, 오늘 하루 어땠어요?\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
