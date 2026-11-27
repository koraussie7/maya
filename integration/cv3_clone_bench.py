#!/usr/bin/env python3
"""CosyVoice3 (audio.cpp GGUF, CPU) 로 한국어 크로스링구얼 클론을 합성·검증한다.

왜 (2026-11-24 실측)
  · VoxCPM2 는 이 Intel 맥에서 torch>=2.5 wheel 이 없어 못 돌린다(Space API 의존).
  · audio.cpp 의 cosyvoice3 GGUF 는 **CPU 만으로 돈다** → 5GB 모델 다운로드 없이 1.9GB q8 로 복제 가능.
  실측(이 맥, 8 threads, q8_0): 벽시계 52.6초 / 오디오 6.3초 → **RTF 8.34 (실시간 0.12배)**.

그래서 이 스크립트는 "실시간 경로"가 아니라 **오프라인 복제 검증기**다.
  · 맞는 것:  레퍼런스 클립의 음색을 유지하는가(F0), 발음이 정확한가(CER/SenseVoice 재전사)
  · 안 맞는 것: 대화 첫 소리 지연(52초는 대화 불가) → 실시간은 piper/say 유지

핵심 함정 (실측으로 확인)
  `--task clon` 을 쓰고 `--request-option template_name=cross_lingual` 을 줘야 한다.
  cross_lingual = "레퍼런스 목소리로, 대상 텍스트 언어로" 합성. 빼면 레퍼런스 언어를 따라간다.

실행
  .venv-voicemem/bin/python integration/cv3_clone_bench.py
  .venv-voicemem/bin/python integration/cv3_clone_bench.py --persona lyra_vex --text "테스트 문장"
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import subprocess
import sys
import time
import wave

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.environ.get("AUDIOCPP_CLI", "/tmp/audcpp/audiocpp_cli")
MODEL = os.environ.get("CV3_GGUF", "/tmp/audcpp/models/cv3/CosyVoice3-GGUF/cosyvoice3-q8_0.gguf")
REF_DIR = os.path.join(ROOT, "maya", "models", "tts", "reference")

DEFAULT_TEXT = "안녕하세요. 저는 라이라예요. 오늘 하루는 좀 어떠셨나요?"


# ── 오디오 분석 ───────────────────────────────────────────────────────
def read_wav(path: str) -> tuple[np.ndarray, int]:
    with wave.open(path, "rb") as w:
        sr, ch, n = w.getframerate(), w.getnchannels(), w.getnframes()
        x = np.frombuffer(w.readframes(n), np.int16).astype(np.float32)
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x, sr


def median_f0(x: np.ndarray, sr: int) -> float:
    """40ms 프레임 자기상관 F0 중앙값. 화자 음역이 유지됐는지 보는 용도."""
    f0: list[float] = []
    win = int(sr * 0.04)
    for i in range(0, max(0, len(x) - win), win):
        f = x[i:i + win]
        if np.sqrt((f ** 2).mean()) < 300:
            continue
        f = f - f.mean()
        c = np.correlate(f, f, "full")[len(f) - 1:]
        lo, hi = int(sr / 400), int(sr / 70)
        if hi >= len(c):
            continue
        k = lo + int(np.argmax(c[lo:hi]))
        if c[k] > 0.3 * c[0]:
            f0.append(sr / k)
    return float(np.median(f0)) if f0 else 0.0


def transcribe(path: str) -> str:
    """SenseVoice 재전사 — 로컬, 실제 발음이 어떻게 들리는지 측정."""
    try:
        sys.path.insert(0, os.path.join(ROOT, "integration"))
        import torch_compat  # noqa: F401
        from bench_stt_hard import SenseVoice
    except Exception as exc:  # noqa: BLE001
        return f"<stt 불가: {type(exc).__name__}>"
    with wave.open(path, "rb") as w:
        pcm = w.readframes(w.getnframes())
    text, _ = SenseVoice().transcribe(pcm)
    return text


# ── 합성 ──────────────────────────────────────────────────────────────
def synthesize(text: str, ref: str, out: str, threads: int) -> dict:
    cmd = [
        CLI, "--task", "clon", "--family", "cosyvoice3", "--model", MODEL,
        "--backend", "cpu", "--threads", str(threads), "--mode", "offline",
        "--text", text, "--voice-ref", ref,
        "--request-option", "template_name=cross_lingual",
        "--out", out, "--metrics",
    ]
    t0 = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    wall = time.monotonic() - t0
    if proc.returncode != 0 or not os.path.exists(out):
        raise RuntimeError(f"audiocpp_cli 실패 rc={proc.returncode}\n{proc.stderr[-800:]}")
    metrics = {}
    for line in proc.stdout.splitlines():
        if "=" in line and line.startswith("metrics."):
            k, _, v = line.partition("=")
            try:
                metrics[k] = float(v)
            except ValueError:
                metrics[k] = v
        elif "=" in line and line.split("=")[0] in ("sample_rate", "audio_out"):
            k, _, v = line.partition("=")
            metrics[k] = v
    metrics["wall_s_measured"] = round(wall, 2)
    metrics["audio_out_path"] = out
    return metrics


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--persona", default="lyra_vex_b")
    ap.add_argument("--ref", default="", help="레퍼런스 클립(기본: personas/<id>.yaml 의 ref_wav)")
    ap.add_argument("--text", default=DEFAULT_TEXT)
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--out", default="/tmp/cv3_bench.wav")
    ap.add_argument("--no-stt", action="store_true")
    args = ap.parse_args()

    ref = args.ref
    if not ref:
        ref = os.path.join(REF_DIR, f"clip_ref_{args.persona}.wav")
    if not os.path.exists(ref):
        print(f"[err] 레퍼런스 없음: {ref}")
        return 2
    if not os.path.exists(CLI) or not os.path.exists(MODEL):
        print(f"[err] audiocpp_cli/model 없음\n  cli={CLI}\n  model={MODEL}")
        return 2

    print(f"[ref] {os.path.basename(ref)}")
    rx, rsr = read_wav(ref)
    ref_f0 = median_f0(rx, rsr)
    print(f"      {len(rx) / rsr:.2f}s @ {rsr}Hz  F0 {ref_f0:.1f}Hz")

    print(f"[tts] cosyvoice3 cross_lingual (cpu x{args.threads}) …")
    m = synthesize(args.text, ref, args.out, args.threads)
    x, sr = read_wav(args.out)
    dur = len(x) / sr
    out_f0 = median_f0(x, sr)
    rtf = m["wall_s_measured"] / dur if dur else 0

    print(f"      {dur:.2f}s @ {sr}Hz  벽시계 {m['wall_s_measured']}s  RTF {rtf:.2f} "
          f"({1 / rtf:.2f}x 실시간)  RSS {m.get('metrics.memory.peak_rss_mb', 0):.0f}MB")
    print(f"      F0 {out_f0:.1f}Hz  (레퍼런스 대비 {out_f0 - ref_f0:+.1f}Hz)")

    stt = "" if args.no_stt else transcribe(args.out)
    sim = None
    if stt and not stt.startswith("<stt"):
        sim = difflib.SequenceMatcher(
            None, args.text.replace(" ", ""), stt.replace(" ", "")).ratio() * 100
        print(f"      STT: {stt}")
        print(f"      텍스트 유사도 {sim:.1f}%")

    report = {
        "engine": "cosyvoice3-gguf (audio.cpp)",
        "model": os.path.basename(MODEL),
        "backend": "cpu",
        "threads": args.threads,
        "text": args.text,
        "ref": ref,
        "ref_f0_hz": round(ref_f0, 1),
        "out_f0_hz": round(out_f0, 1),
        "audio_seconds": round(dur, 2),
        "wall_seconds": m["wall_s_measured"],
        "rtf": round(rtf, 2),
        "stt": stt,
        "text_similarity_pct": round(sim, 1) if sim is not None else None,
        "metrics": {k: v for k, v in m.items() if k.startswith("metrics.")},
    }
    out_json = os.path.splitext(args.out)[0] + ".json"
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"[report] {out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
