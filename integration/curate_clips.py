#!/usr/bin/env python3
"""레퍼런스 클립 **사람 검수** 도구 — 클립이 진짜 그 화자 목소리인지 귀로 확인한다.

왜 (binxio/piper-voice-cloning 의 curate-audio-samples.py 에서 가져온 방식)
  자동 화자 분리는 확신도가 낮다. 우리 실측에서도 "여자 화자가 2명인가?" 의심이 나왔다.
  자동 추출 결과를 **사람이 듣고 perfect / no / maybe 로 분류**해야 클론 품질이 보장된다.
  VoxCPM 클론은 레퍼런스 클립에 섞인 다른 목소리를 그대로 학습한다(음색이 흐려짐).

무엇을 검수하나
  ① 기존 레퍼런스 클립(clip_ref_speaker_*.wav) — 지금 쓰는 정본
  ② 원본 녹음에서 뽑은 **후보 구간들** — 더 좋은 클립을 고르기 위해

분류 결과
  <out>/perfect/ , <out>/no/ , <out>/maybe/  에 wav + .txt(전사) 이동
  → perfect 중 가장 좋은 것을 `--install` 로 personas/*.yaml 에 반영

의존성
  재생: macOS `afplay` (없으면 ffplay)
  분류 입력: 터미널 키 입력 (Y/N/M/L/R/E/Q)

실행
  # ① 기존 클립만 듣고 판정
  .venv-voicemem/bin/python integration/curate_clips.py --clips

  # ② 원본 녹음에서 후보 12개를 뽑아 검수 (전사 자동)
  .venv-voicemem/bin/python integration/curate_clips.py \
      --src "$HOME/Documents/새로운 녹음.mp3" --candidates 12

  # ③ perfect 로 분류된 것 중 최적을 페르소나에 반영
  .venv-voicemem/bin/python integration/curate_clips.py --install lyra_vex_b
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REF_DIR = os.path.join(ROOT, "maya", "models", "tts", "reference")
DEFAULT_OUT = os.path.join(REF_DIR, "curated")

HELP = """
  [R] 다시 듣기   [Y] 좋음(perfect)   [M] 애매(maybe)
  [N] 나쁨(no)    [E] 전사 편집       [Q] 중단
"""


# ── 재생 ──────────────────────────────────────────────────────────────
def play(path: str) -> None:
    if subprocess.run(["which", "afplay"], capture_output=True).returncode == 0:
        subprocess.run(["afplay", path], check=False)
    else:
        subprocess.run(["ffplay", "-nodisp", "-autoexit", "-loglevel", "error", path],
                       check=False)


def play_repeat(path: str) -> None:
    """최소 2회 반복 재생 — 첫 재생은 시작이 잘려 들리는 경우가 많다."""
    play(path)
    play(path)


# ── 후보 추출 ─────────────────────────────────────────────────────────
def extract_candidates(src: str, count: int, seconds: float = 8.0) -> list[dict]:
    """원본에서 발화 구간을 골라 후보 클립을 만든다(전사 포함).

    `split_speakers.py` 의 검출/특징을 재사용하되, 여기서는 **고르게 퍼진 구간**을
    뽑는다(같은 5분에 몰리면 억양 편향이 생긴다).
    """
    sys.path.insert(0, os.path.join(ROOT, "integration"))
    import numpy as np

    import split_speakers as sp

    sr = 16000
    full = sp.decode(src, sr)
    segs = sp.segments(full, sr)
    if not segs:
        return []
    # 전체를 count 구간으로 나눠 각 구간에서 하나씩 고른다(시간 분산)
    total = len(full)
    out: list[dict] = []
    per = total // count if count else total
    tmpdir = tempfile.mkdtemp(prefix="curate_")
    for i in range(count):
        lo, hi = i * per, min((i + 1) * per, total)
        cands = [s for s in segs if lo <= s[0] < hi]
        if not cands:
            continue
        a, b = max(cands, key=lambda s: s[1] - s[0])
        b = min(b, a + int(seconds * sr))
        clip = full[a:b]
        path = os.path.join(tmpdir, f"cand_{i:02d}.wav")
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(clip.astype(np.int16).tobytes())
        out.append({"path": path, "start": round(a / sr, 1),
                    "seconds": round((b - a) / sr, 2), "text": ""})
    return out


def transcribe_all(items: list[dict]) -> None:
    """SenseVoice 로 전사(로컬, CER 0 실측). 실패해도 검수는 진행."""
    try:
        sys.path.insert(0, os.path.join(ROOT, "integration"))
        import torch_compat  # noqa: F401
        from bench_stt_hard import SenseVoice

        sv = SenseVoice()
    except Exception as exc:  # noqa: BLE001
        print(f"[stt] 사용 불가({type(exc).__name__}) → 전사 생략")
        return
    for it in items:
        try:
            with wave.open(it["path"], "rb") as w:
                pcm = w.readframes(w.getnframes())
            it["text"], _ = sv.transcribe(pcm)
            print(f"  - {os.path.basename(it['path'])}: {it['text'][:60]}")
        except Exception as exc:  # noqa: BLE001
            print(f"  - {os.path.basename(it['path'])} 전사 실패: {exc}")


# ── 분류 루프 ─────────────────────────────────────────────────────────
def curate(items: list[dict], out_dir: str) -> dict:
    dirs = {k: os.path.join(out_dir, k) for k in ("perfect", "maybe", "no")}
    for d in dirs.values():
        os.makedirs(d, exist_ok=True)
    result: dict[str, list[str]] = {k: [] for k in dirs}
    texts: dict[str, str] = {}

    print(HELP)
    idx = 0
    while idx < len(items):
        it = items[idx]
        name = os.path.basename(it["path"])
        print(f"\n[{idx + 1}/{len(items)}] {name}  ({it.get('seconds', '?')}s, "
              f"start {it.get('start', '?')}s)")
        print(f"  전사: {it.get('text', '(없음)') or '(없음)'}")
        play_repeat(it["path"])
        try:
            action = input("  판정 (R/Y/M/N/E/L=나중에/Q): ").strip().upper()[:1]
        except (EOFError, KeyboardInterrupt):
            print("\n중단")
            break
        if action == "R":
            play_repeat(it["path"])
            continue
        if action == "Q":
            break
        if action == "L":
            idx += 1
            continue
        if action == "E":
            new = input(f"  전사 수정 [{it.get('text', '')}]: ").strip()
            if new:
                it["text"] = new
            continue
        key = {"Y": "perfect", "M": "maybe", "N": "no"}.get(action)
        if not key:
            print("  알 수 없는 입력 — 다시")
            continue
        dst = os.path.join(dirs[key], name)
        shutil.copy2(it["path"], dst)
        if it.get("text"):
            with open(os.path.join(dirs[key], name.replace(".wav", ".txt")), "w",
                      encoding="utf-8") as f:
                f.write(it["text"])
            texts[name] = it["text"]
        result[key].append(name)
        idx += 1

    with open(os.path.join(out_dir, "curation.json"), "w", encoding="utf-8") as f:
        json.dump({"result": result, "texts": texts}, f, ensure_ascii=False, indent=2)
    return result


# ── 설치(페르소나 반영) ───────────────────────────────────────────────
def score_clip(path: str) -> float:
    """클론 레퍼런스로서의 적합도 점수(높을수록 좋다).

    VoxCPM 은 클립 앞부분을 주로 반영하므로:
      · 5~12초: 충분한 음색 정보 + 길이 폭주 방지
      · 무음 비율 낮음: 침묵을 배우면 억양이 늘어진다
      · 초반 0.3초에 소리: 첫 프레임이 삼켜지는 클립 회피
    """
    try:
        import numpy as np

        with wave.open(path, "rb") as w:
            sr = w.getframerate()
            pcm = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32)
    except Exception:  # noqa: BLE001
        return 0.0
    if len(pcm) == 0:
        return 0.0
    secs = len(pcm) / sr
    frame = int(sr * 0.03)
    n = max(1, len(pcm) // frame)
    rms = np.sqrt(np.mean(pcm[: n * frame].reshape(n, frame) ** 2, axis=1) + 1e-9)
    thr = max(float(np.median(rms)) * 0.25, 80.0)
    voiced = float((rms > thr).mean())
    head = pcm[: int(sr * 0.3)]
    head_ok = float(np.abs(head).max()) > 800 if len(head) else 0.0
    len_score = 1.0 if 5.0 <= secs <= 12.0 else max(0.0, 1.0 - abs(secs - 8.5) / 8.5)
    return 40 * len_score + 40 * voiced + 20 * head_ok
def install(persona_id: str, out_dir: str) -> int:
    """perfect 클립 중 최적을 페르소나 음색으로 반영한다.

    선택 기준: 길이만 보면 **주변 소음이 많은 긴 클립**이 뽑힌다(실측 실패).
    그래서 ① 길이 5~12초 선호 ② 무음 비율이 낮을 것 ③ 초반 0.3초가 비어있지 않을 것
    순으로 점수를 매긴다. 사람이 "다 좋다"고 판정한 뒤의 **자동 최적 선택**이다.
    """
    pdir = os.path.join(ROOT, "maya", "personas")
    path = None
    for cand in (f"{persona_id}.yaml", f"{persona_id}.yml"):
        p = os.path.join(pdir, cand)
        if os.path.isfile(p):
            path = p
            break
    if path is None:
        print(f"[install] 페르소나 파일 없음: {persona_id}")
        return 1
    perfect = os.path.join(out_dir, "perfect")
    wavs = [os.path.join(perfect, f) for f in os.listdir(perfect) if f.endswith(".wav")]
    if not wavs:
        print(f"[install] perfect 클립이 없습니다 ({perfect})")
        return 1
    scored = sorted(((score_clip(p), p) for p in wavs), key=lambda x: -x[0])
    best_score, best = scored[0]
    for sc, p in scored[:5]:
        print(f"  후보 {os.path.basename(p):16s} score={sc:6.2f}")
    txt = best.replace(".wav", ".txt")
    prompt_text = ""
    if os.path.isfile(txt):
        prompt_text = open(txt, encoding="utf-8").read().strip()
    # 레퍼런스 표준 위치로 복사 — 같은 파일명이면 shutil.copy2 가 **SameFileError** 를 낸다
    # (검수 대상이 이미 clip_ref_<persona>.wav 인 경우, 실측).
    dst = os.path.join(REF_DIR, f"clip_ref_{persona_id}.wav")
    if os.path.abspath(best) != os.path.abspath(dst):
        shutil.copy2(best, dst)
    rel = os.path.relpath(dst, ROOT)

    src = open(path, encoding="utf-8").read()
    import re

    src = re.sub(r"(\s+ref_wav:\s*).*", rf"\1{rel}", src, count=1)
    if prompt_text:
        src = re.sub(r"(\s+prompt_text:\s*).*", rf'\1"{prompt_text}"', src, count=1)
    open(path, "w", encoding="utf-8").write(src)
    print(f"[install] {persona_id} ← {rel}")
    print(f"          prompt_text = {prompt_text[:60]}")
    print("          캐시를 지우고 서버를 재시작해야 반영됩니다: rm -rf ~/.cache/maya-tts")
    return 0


# ── CLI ───────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clips", action="store_true", help="기존 레퍼런스 클립 검수")
    ap.add_argument("--src", default="", help="원본 녹음에서 후보 구간 추출")
    ap.add_argument("--candidates", type=int, default=10, help="추출할 후보 수")
    ap.add_argument("--seconds", type=float, default=8.0, help="후보 최대 길이")
    ap.add_argument("--out", default=DEFAULT_OUT, help="분류 결과 디렉터리")
    ap.add_argument("--install", default="", metavar="PERSONA_ID",
                    help="perfect 중 최적 클립을 페르소나에 반영")
    ap.add_argument("--no-transcribe", action="store_true")
    args = ap.parse_args()

    if args.install:
        return install(args.install, args.out)

    items: list[dict] = []
    if args.clips:
        for f in sorted(os.listdir(REF_DIR)):
            if f.startswith("clip_ref") and f.endswith(".wav"):
                p = os.path.join(REF_DIR, f)
                with wave.open(p, "rb") as w:
                    secs = w.getnframes() / w.getframerate()
                items.append({"path": p, "seconds": round(secs, 2), "start": "-", "text": ""})
    if args.src:
        print(f"[extract] {os.path.basename(args.src)} → 후보 {args.candidates}개")
        items += extract_candidates(args.src, args.candidates, args.seconds)

    if not items:
        ap.print_help()
        print("\n--clips 또는 --src 중 하나를 지정하세요.")
        return 2

    if not args.no_transcribe and args.src:
        print("[stt] 후보 전사 중 (SenseVoice, 수십 초 소요)")
        transcribe_all([i for i in items if not i.get("text")])

    res = curate(items, args.out)
    print("\n=== 결과 ===")
    for k, v in res.items():
        print(f"  {k:8s} {len(v)}개")
    print(f"  저장: {args.out}")
    print(f"\n다음: --install <persona_id> 로 perfect 중 최적을 반영")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
