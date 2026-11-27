"""레퍼런스 음성 → Supertonic 보이스 스타일로 변환해 ``voices/`` 에 저장.

Supertonic 3 는 ``create_voice_style_from_audio`` 메서드를 제공하지 않는다.
대신, ``supertonic`` 패키지가 자체적으로 사전 계산된 ``.json`` 보이스 스타일
파일을 사용해 클로닝한다. 따라서 이 스크립트의 목적은:

1) 사용자가 직접 ``.json`` 보이스 스타일 파일을 가지고 있을 때 검증/로드
2) 향후 ``x-vector → json`` 변환 경로가 생기면 그 진입점 역할

사용법
------
    python -m integration.lyra.record_reference \\
        --name lyra --json-path ./lyra_style.json
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser(description="Lyra reference voice utility")
    p.add_argument("--name", required=True, help="저장할 보이스 이름 (예: lyra)")
    p.add_argument("--json-path", default="", help="기존 .json 보이스 스타일 경로")
    p.add_argument("--list-voices", action="store_true", help="사용 가능한 내장 보이스 출력")
    args = p.parse_args()

    try:
        from supertonic import TTS  # type: ignore
    except ImportError:
        print("supertonic 패키지가 설치된 인터프리터에서 실행해주세요.", flush=True)
        return 1

    tts = TTS(model="supertonic-3", auto_download=False, intra_op_num_threads=1)
    names = list(getattr(tts, "voice_style_names", []) or [])
    if args.list_voices:
        print("사용 가능한 내장 보이스:", names, flush=True)
        return 0

    if not args.json_path:
        print("--json-path 가 필요합니다. (--list-voices 만 정보 조회)", flush=True)
        return 2

    src = Path(args.json_path).expanduser().resolve()
    if not src.exists():
        print(f"파일 없음: {src}", flush=True)
        return 3

    voices_dir = Path(__file__).resolve().parent / "voices"
    voices_dir.mkdir(parents=True, exist_ok=True)
    dst = voices_dir / f"{args.name}.json"
    shutil.copy2(src, dst)

    # 검증: 실제 로드 가능한지
    style = tts.get_voice_style_from_path(str(dst))
    meta = {
        "ok": True,
        "name": args.name,
        "path": str(dst),
        "shape": list(style.shape) if hasattr(style, "shape") else None,
        "available_built_in": names,
    }
    print(json.dumps(meta, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
