"""Lyra 의 합성 결과를 직렬화해서 stdout 으로 돌려주는 단일 목적 러너.

같은 인터프리터(Supertonic 이 설치된 venv) 에서 직접 ``TTS`` 를 호출해
``(wav, sr)`` 을 만들고, 헤더(JSON 1줄) + raw bytes 로 stdout 에 적는다.
LyraTTS._synthesize_subprocess() 가 이 출력을 다시 디코딩한다.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np


def main() -> int:
    p = argparse.ArgumentParser(description="Lyra runner (Supertonic 3)")
    p.add_argument("--text", required=True)
    p.add_argument("--base-voice", default="F1")
    p.add_argument("--speed", type=float, default=1.0)
    p.add_argument("--steps", type=int, default=8)
    p.add_argument("--model-dir", default="")
    p.add_argument("--lang", default="ko")
    p.add_argument("--voice-style-path", default="")
    args = p.parse_args()

    from supertonic import TTS  # type: ignore

    tts = TTS(
        model="supertonic-3",
        model_dir=args.model_dir or None,
        auto_download=False,
        intra_op_num_threads=1,
    )

    # 보이스 스타일
    if args.voice_style_path:
        style = tts.get_voice_style_from_path(args.voice_style_path)
    else:
        names = list(getattr(tts, "voice_style_names", []) or [])
        chosen = args.base_voice if args.base_voice in names else (
            names[0] if names else "F1"
        )
        style = tts.get_voice_style(chosen)

    wav, sr = tts.synthesize(
        args.text,
        voice_style=style,
        total_steps=args.steps,
        speed=args.speed,
        lang=args.lang or None,
        verbose=False,
    )
    wav = np.asarray(wav, dtype=np.float32)

    # stdout 으로 헤더 + 바이너리 송출
    sys.stdout.buffer.write(
        (json.dumps({"sr": int(sr), "n": int(wav.shape[0]), "dtype": "float32"}) + "\n").encode()
    )
    sys.stdout.buffer.write(wav.tobytes())
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
