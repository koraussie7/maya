#!/usr/bin/env python3
"""qpclone 노트북을 **우리 설정으로 미리 채운** ipynb 를 만든다 — 한 방에 업로드·실행용.

왜
  원본 노트북(`Qwen3_TTS_Voice_Cloning_to_PIPER.ipynb`)의 Cell 1 에는
  `PROJECT_NAME = "INSERT_PROJECTNAME_HERE"`, `LANGUAGE = "English"`, `REF_TEXT = ""` 가 들어 있다.
  매번 손으로 고치고 파일을 업로드하는 대신, **값을 미리 채운 사본**을 만들어
  Colab 에 그대로 열면 되게 한다.

무엇을 하나
  1. 원본 노트북을 GitHub 에서 받는다
  2. Cell 1 의 다음 값을 우리 것으로 바꾼다
       PROJECT_NAME = <프로젝트>        LANGUAGE = "Korean"
       REF_TEXT     = <페르소나 전사문>  (음절 단위 STT 출력은 자동 정리)
  3. Cell 2 에 **ref.wav 자동 주입 셀**을 추가한다
       (Colab 업로드 창을 띄우지 않고, 옆에 두는 .wav 를 바로 주입)
  4. `*_ready.ipynb` 로 저장 — Colab 에서 이 파일 하나만 열면 된다

실행
  .venv-voicemem/bin/python integration/make_qpclone_notebook.py --persona lyra_vex_b
  # → maya/models/tts/qpclone/lyra_vex_b/Qwen3_TTS_lyra_vex_b_ready.ipynb
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NB_URL = ("https://raw.githubusercontent.com/whit3rabbit/qpclone/main/"
          "Qwen3_TTS_Voice_Cloning_to_PIPER.ipynb")
OUT_ROOT = os.path.join(ROOT, "maya", "models", "tts", "qpclone")


def fetch_notebook() -> dict:
    with urllib.request.urlopen(NB_URL, timeout=60) as r:
        return json.loads(r.read())


def tidy_korean(text: str) -> str:
    """SenseVoice 출력의 음절 단위 띄어쓰기 정리.

    "찾 아 봤 는데 보통 채 지 피티나" → "찾아봤는데 보통 챗지피티나"
    완벽한 형태소 분석은 아니고, **한 글자 토큰을 붙이는** 규칙이다.
    Qwen3-TTS 는 띄어쓰기에 억양이 크게 좌우되므로 이 정도만 고쳐도 차이가 난다.
    """
    t = re.sub(r"\s+", " ", text or "").strip()
    # 음절 단위로 쪼개진 토큰을 복원한다.
    #   "찾 아 봤 는데 보통 채 지 피티나" → "찾아봤는데 보통 채지피티나"
    # 규칙: **한 글자 토큰은 앞 토큰에 붙인다**.
    #   · 첫 토큰은 띄어쓰기를 모르니 그대로 둔다(문장 시작은 보통 1~2음절 단어)
    #   · 이미 여러 글자인 토큰은 경계로 본다(원래 단어일 수 있으니 건드리지 않음)
    tokens = t.split(" ")
    out: list[str] = []
    for tok in tokens:
        bare = re.sub(r"[^\w가-힣]", "", tok)
        is_ko_single = len(bare) == 1 and re.fullmatch(r"[가-힣]", bare) is not None
        if out and is_ko_single:
            out[-1] = out[-1] + tok
        else:
            out.append(tok)
    t = " ".join(out)
    # 흔한 오인식 교정(우리 클립 전사에 실제로 나온 것)
    fixes = {
        "채 지 피티": "챗지피티", "채지피티": "챗지피티",
        "크로드 프로": "클로드 프로", "크 타임": "클로드", "크로드": "클로드",
        "20 불": "20불", "삼십분밖": "삼십 분밖에",
    }
    for a, b in fixes.items():
        t = t.replace(a, b)
    return re.sub(r"\s+", " ", t).strip()


def set_cell_vars(src: str, values: dict[str, str]) -> str:
    """`NAME = "...` 형태의 colab param 줄을 값으로 교체한다."""
    for name, val in values.items():
        esc = val.replace("\\", "\\\\").replace('"', '\\"')
        # 불리언은 Python 리터럴로 넣는다("true" 는 문자열이 된다)
        if val in ("true", "false"):
            literal = val.capitalize()
            repl = f"{name} = {literal}"
        else:
            repl = f'{name} = "{esc}"'
        pattern = re.compile(rf'^{name}\s*=\s*".*?"', re.MULTILINE)
        if pattern.search(src):
            src = pattern.sub(repl, src, count=1)
        else:
            pattern_any = re.compile(rf'^{name}\s*=\s*\S+', re.MULTILINE)
            if pattern_any.search(src):
                src = pattern_any.sub(repl, src, count=1)
            else:
                src += f"\n{repl}"
    return src


def inject_cell(project: str, ref_wav: str) -> str:
    """Cell 2 뒤에 넣을 'ref.wav 자동 주입' 셀."""
    return f'''# ── Maya: 레퍼런스 클립 자동 주입 (업로드 창 없이) ──────────────
# 준비 스크립트(prepare_qpclone_input.py)가 만든 ref.wav 를 Drive 프로젝트 폴더에 넣는다.
# 이후 Cell 6 의 find_existing_ref_audio() 가 이 파일을 자동으로 집어 쓴다.
import os, shutil
_REF_SRC = "/content/ref.wav"          # 업로드한 파일(같은 세션에서 업로드한 경우)
_proj = globals().get("PROJECT_DIR") or globals().get("project_dir")
try:
    if _proj and os.path.exists(_REF_SRC):
        os.makedirs(os.path.join(_proj, "ref"), exist_ok=True)
        dst = os.path.join(_proj, "ref", "ref.wav")
        shutil.copy2(_REF_SRC, dst)
        print("ref.wav →", dst)
    else:
        print("ref.wav 주입 건너뜀 — Cell 6 에서 업로드하세요 (PROJECT={project!r})")
except Exception as e:
    print("ref.wav 주입 실패:", e)
'''


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--persona", default="lyra_vex_b")
    ap.add_argument("--project", default="", help="PROJECT_NAME (기본: 페르소나 id)")
    ap.add_argument("--ref-text", default="", help="전사문 직접 지정(기본: 페르소나 값)")
    ap.add_argument("--no-tidy", action="store_true", help="전사문 정리 생략")
    ap.add_argument("--use-asr", action="store_true",
                    help="REF_TEXT 를 비우고 노트북의 Qwen3-ASR 로 전사하게 한다(권장)")
    ap.add_argument("--out", default=OUT_ROOT)
    args = ap.parse_args()

    project = args.project or args.persona
    sys.path.insert(0, os.path.join(ROOT, "maya-ui", "backend"))
    from app.persona import load_persona  # noqa: PLC0415

    p = load_persona(args.persona)
    text = args.ref_text or p.voice_prompt_text
    if args.use_asr:
        # ⚠ 자동 정리의 한계(실측): "찾 아 봤 는데 보통 채 지 피티나" →
        #    "찾아봤 는데 보통채지 피티나" 처럼 **어절 경계를 몰라 뭉갠다**.
        #    한국어는 띄어쓰기가 억양을 좌우하므로, 부정확한 전사보다
        #    Qwen3-ASR(노트북 내장)에 맡기는 편이 낫다.
        text = ""
    elif not args.no_tidy:
        text = tidy_korean(text)
    if not text:
        print("[info] REF_TEXT 비움 → 노트북이 Qwen3-ASR 로 전사합니다(USE_ASR)")

    nb = fetch_notebook()
    cells = nb["cells"]
    if len(cells) < 2 or cells[1].get("cell_type") != "code":
        print("[err] 노트북 구조가 예상과 다릅니다(Cell 1 이 설정 셀이 아님)")
        return 1

    src = "".join(cells[1]["source"])
    values = {
        "PROJECT_NAME": project,
        "LANGUAGE": "Korean",
        "REF_TEXT": text,
        "REF_AUDIO_MODE": "upload_if_missing",
    }
    if args.use_asr:
        values["USE_ASR"] = "true"
    src = set_cell_vars(src, values)
    cells[1]["source"] = src.splitlines(keepends=True)

    # ref 주입 셀 — Cell 2(설치) 뒤에 삽입
    new_cell = {
        "cell_type": "code",
        "metadata": {"id": "maya-ref-inject"},
        "execution_count": None,
        "outputs": [],
        "source": inject_cell(project, p.voice_ref_wav).splitlines(keepends=True),
    }
    cells.insert(2, new_cell)

    out_dir = os.path.join(args.out, project)
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, f"Qwen3_TTS_{project}_ready.ipynb")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(nb, f, ensure_ascii=False, indent=1)

    # 실행 안내
    guide = os.path.join(out_dir, "OPEN_IN_COLAB.txt")
    with open(guide, "w", encoding="utf-8") as f:
        f.write(
            f"1) https://colab.research.google.com/  → 파일 > 노트북 업로드\n"
            f"   → {os.path.basename(out)}\n"
            f"2) 런타임 > 런타임 유형 변경 > T4 GPU\n"
            f"3) 왼쪽 🔑 아이콘 > HF_TOKEN 시크릿 추가 (huggingface.co/settings/tokens)\n"
            f"4) 왼쪽 📁 에 ref.wav 업로드 (준비된 파일: {os.path.basename(p.voice_ref_wav)})\n"
            f"5) 런타임 > 모두 실행 (약 6시간, PIPER_RESUME=True 로 중단 후 재개 가능)\n"
            f"6) Drive/Piper_Qwen_Projects/{project}/ 에서 model.onnx 다운로드\n\n"
            f"설정: PROJECT_NAME={project}, LANGUAGE=Korean\n"
            f"REF_TEXT={text}\n"
        )

    print(f"[ok] {out}")
    print(f"     PROJECT_NAME = {project}")
    print(f"     LANGUAGE     = Korean")
    print(f"     REF_TEXT     = {text[:70]}")
    print(f"[guide] {guide}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
