#!/usr/bin/env python3
"""녹음 파일에서 **화자(목소리)별로 클립을 분리**한다.

배경 (2026-11-24)
  21분 녹음에서 뽑은 첫 클립이 사용자 본인 목소리였다("내 목소리가 추출이 됐네").
  같은 녹음 안에 대화 상대(여성)의 목소리도 있으므로, 21분 전체를 훑어
  **화자 A/B 로 나눠** 각각 레퍼런스 클립을 만든다. VoxCPM 클론은 **한 화자 클립**이어야
  음색이 섞이지 않는다.

방식 (외부 화자분리 모델 없이 — 이 venv 에 pyannote/speechbrain 이 없음)
  1) 30ms 프레임 RMS 로 발화 구간 검출 → 1~8초 구간으로 자름
  2) 각 구간의 **음향 특징**(MFCC 13 + 스펙트럼 중심/롤오프/대역에너지 + F0) 계산
  3) StandardScaler + KMeans(k=2) 로 군집 → 화자 A/B
  4) 각 화자에서 **무음/잡음이 섞이지 않은 가장 깨끗한 구간**을 골라 8~12초 클립 생성
     (연속 구간 우선, 부족하면 같은 화자 구간을 이어 붙임)

난이도/한계
  · 2화자 가정. 3명 이상이면 k 를 늘려야 하고 결과 해석이 어려워진다.
  · F0 중앙값으로 "a/b" 라벨만 붙인다(절대 성별 판정은 하지 않는다 — 억양/녹음 상태 영향).

실행
  .venv-voicemem/bin/python integration/split_speakers.py \
      --src "$HOME/Documents/새로운 녹음.mp3" \
      --out-dir maya/models/tts/reference --minutes 21
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import wave

import numpy as np


def decode(src: str, sr: int = 16000) -> np.ndarray:
    fd, tmp = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src,
                    "-ac", "1", "-ar", str(sr), "-af", "loudnorm=I=-18:TP=-2:LRA=11", tmp],
                   check=True)
    with wave.open(tmp, "rb") as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32)
    os.unlink(tmp)
    return pcm


def segments(pcm: np.ndarray, sr: int, min_len=1.0, max_len=8.0,
             gap=0.6) -> list[tuple[int, int]]:
    frame = int(sr * 0.03)
    n = len(pcm) // frame
    rms = np.sqrt(np.mean(pcm[: n * frame].reshape(n, frame) ** 2, axis=1))
    thr = max(float(np.percentile(rms, 30)) * 1.5, 180.0)
    act = rms > thr
    gap_f = int(gap / 0.03)
    runs, start = [], None
    for i, a in enumerate(act):
        if a and start is None:
            start = i
        elif not a and start is not None:
            runs.append([start, i])
            start = None
    if start is not None:
        runs.append([start, n])
    merged: list[list[int]] = []
    for r in runs:
        if merged and r[0] - merged[-1][1] < gap_f:
            merged[-1][1] = r[1]
        else:
            merged.append(r)
    out = []
    for a, b in merged:
        secs = (b - a) * 0.03
        if secs < min_len:
            continue
        if secs > max_len:                     # 너무 길면 앞부분만
            b = a + int(max_len / 0.03)
        out.append((a * frame, b * frame))
    return out


def feats(pcm: np.ndarray, sr: int, seg: tuple[int, int]) -> np.ndarray | None:
    """구간 하나의 음색 특징(MFCC 평균 + 스펙트럼 + F0)."""
    import librosa

    a, b = seg
    y = pcm[a:b].astype(np.float32) / 32768.0
    if len(y) < int(sr * 0.8):
        return None
    mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13)
    cent = librosa.feature.spectral_centroid(y=y, sr=sr)
    roll = librosa.feature.spectral_rolloff(y=y, sr=sr)
    zcr = librosa.feature.zero_crossing_rate(y)
    bands = []
    S = np.abs(librosa.stft(y))
    freqs = librosa.fft_frequencies(sr=sr)
    for lo, hi in ((80, 300), (300, 1000), (1000, 3000), (3000, 8000)):
        m = (freqs >= lo) & (freqs < hi)
        bands.append(float(S[m].mean()) if m.any() else 0.0)
    f0, voiced, _ = librosa.pyin(y, fmin=70, fmax=400, sr=sr, frame_length=1024)
    # pyin 은 무성 프레임을 NaN 으로 준다 → 유성 프레임만
    f0v = f0[voiced & np.isfinite(f0)] if (f0 is not None and voiced is not None) else np.array([])
    # 특징 벡터는 항상 같은 길이여야 한다(스칼라도 np.atleast_1d 로 감싸 shape 불일치 방지)
    v = np.concatenate([
        mfcc.mean(axis=1).ravel(),
        np.atleast_1d(cent.mean()),
        np.atleast_1d(roll.mean()),
        np.atleast_1d(zcr.mean()),
        np.log1p(np.array(bands, dtype=np.float32)).ravel(),
        np.array([float(np.median(f0v)) if f0v.size else 0.0], dtype=np.float32),
    ]).astype(np.float32)
    return v


def cluster(F: np.ndarray, k: int = 2) -> np.ndarray:
    from sklearn.cluster import KMeans
    from sklearn.preprocessing import StandardScaler

    X = StandardScaler().fit_transform(F)
    km = KMeans(n_clusters=k, n_init=10, random_state=0).fit(X)
    return km.labels_


def write_clip(pcm: np.ndarray, sr: int, segs: list[tuple[int, int]], path: str,
               target=10.0) -> float:
    parts = []
    total = 0.0
    for a, b in segs:
        d = (b - a) / sr
        if total + d > target + 2.0 and parts:
            break
        parts.append(pcm[a:b])
        parts.append(np.zeros(int(sr * 0.12), np.float32))
        total += d
        if total >= target:
            break
    sig = np.concatenate(parts) if parts else np.array([], np.float32)
    if len(sig) == 0:
        return 0.0
    peak = float(np.abs(sig).max()) or 1.0
    sig = np.clip(sig * (0.89 * 32767 / peak), -32768, 32767)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(sig.astype(np.int16).tobytes())
    return len(sig) / sr


def transcribe(path: str) -> tuple[str, float]:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
    import torch_compat  # noqa: F401
    from bench_stt_hard import SenseVoice

    with wave.open(path, "rb") as w:
        pcm = w.readframes(w.getnframes())
    return SenseVoice().transcribe(pcm)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--out-dir", default="maya/models/tts/reference")
    ap.add_argument("--minutes", type=float, default=21.0, help="분석할 앞부분 길이")
    ap.add_argument("--k", type=int, default=2, help="화자 수")
    ap.add_argument("--names", default="",
                    help="화자 라벨(쉼표). 비우면 speaker_a/b/c... — UI 페르소나와 맞출 때 쓴다")
    ap.add_argument("--transcribe", action="store_true", help="클립 전사문도 만들기")
    args = ap.parse_args()

    sr = 16000
    pcm = decode(args.src, sr)
    pcm = pcm[: int(args.minutes * 60 * sr)]
    segs = segments(pcm, sr)
    print(f"[seg] 발화 구간 {len(segs)}개 (앞 {args.minutes:.0f}분, {len(pcm)/sr/60:.1f}분)")

    rows, F = [], []
    for s in segs:
        f = feats(pcm, sr, s)
        if f is not None:
            rows.append(s)
            F.append(f)
    F = np.asarray(F, dtype=np.float32)
    if F.ndim != 2:
        print(f"[feat] 특징 행렬 형태 오류 {F.shape} → 중단")
        return 1
    print(f"[feat] 분석 가능 {len(F)}구간 × {F.shape[1]}차원")

    labels = cluster(F, k=args.k)
    os.makedirs(args.out_dir, exist_ok=True)
    report = {"src": os.path.basename(args.src), "segments": len(segs),
              "clustered": len(rows), "speakers": {}}

    labels = [s.strip() for s in args.names.split(",") if s.strip()]
    for c in range(args.k):
        idx = [i for i, lab in enumerate(labels) if lab == c]
        if not idx:
            continue
        # 화자별 F0 중앙값 (참고 지표)
        f0s = [F[i][-1] for i in idx if F[i][-1] > 0]
        f0 = float(np.median(f0s)) if f0s else 0.0
        # 깨끗한 순서: 길이 + 음량 균형(비정상적으로 큰 구간=웃음/기침 제외)
        def quality(i: int) -> float:
            a, b = rows[i]
            d = (b - a) / sr
            rms = float(np.sqrt(np.mean(pcm[a:b] ** 2)))
            return d - abs(rms - 3000) / 6000
        order = sorted(idx, key=quality, reverse=True)
        chosen = sorted(order[: 6], key=lambda i: rows[i][0])
        name = labels[c] if c < len(labels) else ("speaker_a" if c == 0 else f"speaker_{chr(97 + c)}")
        out = os.path.join(args.out_dir, f"clip_ref_{name}.wav")
        dur = write_clip(pcm, sr, [rows[i] for i in chosen], out)
        info = {"label": int(c), "segments": len(idx), "f0_median": round(f0),
                "clip": os.path.basename(out), "clip_seconds": round(dur, 2),
                "clip_start": round(rows[chosen[0]][0] / sr, 1)}
        if args.transcribe:
            text, ms = transcribe(out)
            info["prompt_text"] = text
            info["stt_ms"] = round(ms)
        report["speakers"][name] = info
        print(f"[{name}] {len(idx)}구간 F0~{f0:.0f}Hz → {out} ({dur:.2f}s) start="
              f"{info['clip_start']}s  {info.get('prompt_text','')}")

    out_json = os.path.join(args.out_dir, "speakers.json")
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"[done] {out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
