#!/usr/bin/env python3
"""qpclone(Colab) 학습 입력 패키지 준비 — 레퍼런스 클립 + 전사문을 업로드용으로 정리.

왜 (whit3rabbit/qpclone)
  그 노트북은 **3~5초의 깨끗한 단일 화자 클립 + 전사문**을 입력으로 받는다.
  그걸로 Qwen3-TTS 가 ~1400개 합성 샘플을 만들고 Piper VITS 를 파인튜닝한다.
  → "실제 녹음 1~3시간" 없이 클론 piper 모델을 만드는 유일한 현실 경로(Colab 무료 T4).

이 스크립트가 하는 일
  1. 페르소나의 `voice.ref_wav`(+ `prompt_text`)를 읽는다
  2. **3~5초**로 잘라낸다(qpclone 권장 길이, 길면 앞부분 사용)
     · 무음으로 시작/끝나면 잘라낸 뒤 다시 트리밍
  3. peak 정규화(0.95) · 16-bit PCM · mono 22.05kHz (Piper 기대 포맷)
     · 원본이 16kHz 면 리샘플(ffmpeg)
  4. 전사문을 `ref_text.txt` 로 저장 — Qwen3-ASR 자동전사 대신 **정확한 전사** 제공
  5. `qpclone_input.zip` 으로 묶어 Colab 업로드를 한 번에

⚠️ 한국어 경고
  qpclone 은 한국어 프리트레인 체크포인트가 없어 **영어 체크포인트로 교차언어 파인튜닝**한다.
  발음이 어색해질 수 있으므로, 학습 후 `verify_piper_clone.py` 로 **CER 측정**을 반드시 한다.

실행
  .venv-voicemem/bin/python integration/prepare_qpclone_input.py --persona lyra_vex_b
  .venv-voicemem/bin/python integration/prepare_qpclone_input.py --wav my_clip.wav --text "전사문"
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_ROOT = os.path.join(ROOT, "maya", "models", "tts", "qpclone")
SR = 22050           # Piper 기대 샘플레이트
TARGET_SECS = 4.5    # 3~5초 중간값
MIN_SECS = 3.0


def read_wav(path: str) -> tuple[np.ndarray, int]:
    import wave

    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        pcm = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32)
    if ch > 1:
        pcm = pcm.reshape(-1, ch).mean(axis=1)
    return pcm, sr


def rms_envelope(pcm: np.ndarray, sr: int, frame_ms: float = 30.0) -> np.ndarray:
    n = int(sr * frame_ms / 1000)
    n_frames = max(1, len(pcm) // n)
    return np.sqrt(np.mean(pcm[: n_frames * n].reshape(n_frames, n) ** 2, axis=1) + 1e-9)


def trim_silence(pcm: np.ndarray, sr: int) -> np.ndarray:
    """앞뒤 무음 제거 — 클론은 첫 프레임이 조용하면 음색을 못 잡는다."""
    rms = rms_envelope(pcm, sr)
    thr = max(float(np.median(rms)) * 0.3, 100.0)
    voiced = np.where(rms > thr)[0]
    if len(voiced) == 0:
        return pcm
    n = int(sr * 0.03)
    a = max(0, voiced[0] * n - int(sr * 0.05))
    b = min(len(pcm), (voiced[-1] + 1) * n + int(sr * 0.08))
    return pcm[a:b]


def cut_window(pcm: np.ndarray, sr: int, secs: float = TARGET_SECS) -> tuple[np.ndarray, float]:
    """가장 음량이 높은 연속 구간을 secs 길이로 자른다(문장 중간이 잘려도 상관없음 — 음색이 목적)."""
    n = int(secs * sr)
    if len(pcm) <= n:
        return pcm, 0.0
    rms = rms_envelope(pcm, sr)
    win = max(1, int(secs * 1000 / 30))          # 프레임 수
    csum = np.concatenate([[0.0], np.cumsum(rms)])
    if len(csum) - 1 < win:
        return pcm[:n], 0.0
    sums = csum[win:] - csum[:-win]
    start = int(np.argmax(sums))
    a = int(start * sr * 0.03)
    return pcm[a:a + n], a / sr


def to_piper_wav(pcm: np.ndarray, sr: int, out: str) -> float:
    """peak 정규화 후 22050Hz 16-bit mono WAV 저장. 길이(초) 반환."""
    pcm = pcm.astype(np.float32)
    peak = float(np.abs(pcm).max()) or 1.0
    pcm = np.clip(pcm * (0.95 * 32767 / peak), -32768, 32767).astype(np.int16)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as t:
        tmp_in = t.name
    import wave

    with wave.open(tmp_in, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    try:
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-i", tmp_in, "-ac", "1", "-ar", str(SR), "-c:a", "pcm_s16le", out],
                       check=True)
    finally:
        os.unlink(tmp_in)
    dur = float(len(pcm)) / sr
    return dur


def persona_clip(persona_id: str) -> tuple[str, str]:
    sys.path.insert(0, os.path.join(ROOT, "maya-ui", "backend"))
    from app.persona import load_persona  # noqa: PLC0415

    p = load_persona(persona_id)
    return p.voice_ref_wav, p.voice_prompt_text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--persona", default="", help="페르소나 id (voice.ref_wav 자동 사용)")
    ap.add_argument("--wav", default="", help="직접 지정할 클립")
    ap.add_argument("--text", default="", help="클립 전사문(직접 지정)")
    ap.add_argument("--name", default="", help="프로젝트 이름(기본: 페르소나 id)")
    ap.add_argument("--out", default=OUT_ROOT)
    args = ap.parse_args()

    wav = args.wav
    text = args.text
    name = args.name or args.persona or "maya_voice"
    if args.persona:
        wav, pt = persona_clip(args.persona)
        text = text or pt
    if not wav or not os.path.isfile(wav):
        print(f"[err] 클립 없음: {wav!r}")
        return 1
    if not text.strip():
        print("[warn] 전사문이 비어 있습니다 — Colab 에서 Qwen3-ASR 자동전사로 대체됩니다")
        print("       정확한 전사를 넣는 편이 품질에 유리합니다 (--text)")

    pcm, sr = read_wav(wav)
    orig_secs = len(pcm) / sr
    pcm = trim_silence(pcm, sr)
    pcm, offset = cut_window(pcm, sr, TARGET_SECS)
    dur = len(pcm) / sr
    if dur < MIN_SECS:
        print(f"[warn] 잘라낸 클립이 {dur:.2f}s — qpclone 권장은 3~5초입니다")
    if dur < 1.0:
        print("[err] 클립이 너무 짧습니다(1초 미만)")
        return 1

    proj = os.path.join(args.out, name)
    os.makedirs(proj, exist_ok=True)
    ref_wav = os.path.join(proj, "ref.wav")
    to_piper_wav(pcm, sr, ref_wav)

    with open(os.path.join(proj, "ref_text.txt"), "w", encoding="utf-8") as f:
        f.write(text.strip())
    meta = {
        "project": name,
        "source_clip": os.path.abspath(wav),
        "source_secs": round(orig_secs, 2),
        "cut_offset_secs": round(offset, 2),
        "output": {"file": "ref.wav", "sr": SR, "channels": 1,
                   "sample_format": "s16le", "secs": round(dur, 2),
                   "peak_normalized": 0.95},
        "ref_text": text.strip(),
        "qpclone_settings": {
            "LANGUAGE": "Korean",   # ⚠️ ko 체크포인트 없음 → 영어 체크포인트 교차언어
            "QWEN_MODEL_ID": "Qwen3-TTS-12Hz-1.7B-Base",
            "NUM_SAMPLES": 1400,
            "OUTPUT_SAMPLE_RATE": SR,
            "MAX_TEXT_CHARS": 150,
            "MAX_DURATION_S": 10.0,
            "PIPER_PRETRAINED_CHECKPOINT": "en/en_US/lessac/medium  (Korean 없음)",
            "PIPER_MAX_EPOCHS": 4000,
            "PIPER_BATCH_SIZE": 8,
            "PIPER_PRECISION": 32,
        },
        "warning": (
            "한국어 프리트레인 체크포인트가 없어 영어 체크포인트로 교차언어 파인튜닝한다. "
            "학습 후 반드시 verify_piper_clone.py 로 CER 를 측정할 것."
        ),
    }
    with open(os.path.join(proj, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    # 체크리스트 — Colab 실행 순서
    with open(os.path.join(proj, "README.txt"), "w", encoding="utf-8") as f:
        f.write(
            f"qpclone 입력 — {name}\n"
            "=" * 40 + "\n\n"
            "1) https://github.com/whit3rabbit/qpclone → Open in Colab\n"
            "2) Runtime > Change runtime type > T4 GPU\n"
            "3) Cell 1 에서 PROJECT_NAME = \"" + name + "\", LANGUAGE = \"Korean\"\n"
            "4) 업로드 프롬프트에서 ref.wav 업로드\n"
            "5) REF_TEXT 칸에 ref_text.txt 내용을 붙여넣기\n"
            "6) 나머지 셀 순서대로 실행 (~6시간)\n"
            "7) Google Drive 에서 생성된 model.onnx 다운로드\n"
            "8) 이 저장소로 가져와 검증:\n"
            "   .venv-voicemem/bin/python integration/verify_piper_clone.py \\\n"
            f"       --onnx ~/Downloads/model.onnx --persona {args.persona or name}\n\n"
            + meta["warning"] + "\n"
        )

    zip_path = os.path.join(args.out, f"{name}_qpclone_input.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for fn in ("ref.wav", "ref_text.txt", "meta.json", "README.txt"):
            z.write(os.path.join(proj, fn), fn)

    print(f"[ok] {proj}")
    print(f"     ref.wav  {dur:.2f}s  {SR}Hz mono s16 (peak 0.95)")
    print(f"     원본 {orig_secs:.2f}s → 잘라낸 위치 {offset:.2f}s")
    print(f"     ref_text: {text.strip()[:60] or '(없음)'}")
    print(f"[zip] {zip_path}  ← Colab 에 업로드")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
