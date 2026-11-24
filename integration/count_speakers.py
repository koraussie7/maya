#!/usr/bin/env python3
"""화자 수 판정 — "몇 명이 말하는가"를 데이터로 확정한다.

배경 (2026-11-24 사용자 지적)
  앞서 `split_speakers.py --k 2` 로 **2명으로 강제**하고 결과만 보고 "남/여 2명"이라고 정리했다.
  실제로는 상대방 쪽에 **여성 화자가 여러 명**일 수 있다 — 출력 문장이 서로 다른 말투로
  계속 바뀌는 정황이 있었다("F0 170→227→157→122Hz"). k 를 고정하는 것은 **가정을 결과로
  착각하는 실수**이므로, k 를 바꿔 가며 **군집 품질**을 보고 적정 화자 수를 판단한다.

측정
  k=2..6 각각: KMeans 학습 → 실루엣 계수(높을수록 잘 분리) + 크기 분포
  + 각 군집 대표 구간 전사/중앙 F0
  판정 규칙: 실루엣이 k 를 늘려도 유의미하게 오르지 않으면(k+1 에서 0.02 미만 개선)
  그 k 를 적정값으로 본다. 단 1구간짜리 군집은 노이즈로 간주.

실행
  .venv-voicemem/bin/python integration/count_speakers.py \
      --src "$HOME/Documents/새로운 녹음.mp3" --minutes 21 --kmax 6
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import split_speakers as sp  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--minutes", type=float, default=21.0)
    ap.add_argument("--kmax", type=int, default=6)
    ap.add_argument("--transcribe", action="store_true", help="군집 대표 구간 전사")
    args = ap.parse_args()

    from sklearn.metrics import silhouette_score
    from sklearn.preprocessing import StandardScaler
    from sklearn.cluster import KMeans

    sr = 16000
    pcm = sp.decode(args.src, sr)[: int(args.minutes * 60 * sr)]
    segs = sp.segments(pcm, sr)
    rows, F = [], []
    for s in segs:
        f = sp.feats(pcm, sr, s)
        if f is not None:
            rows.append(s)
            F.append(f)
    F = np.asarray(F, dtype=np.float32)
    print(f"[data] 구간 {len(rows)}개 × {F.shape[1]}차원")
    X = StandardScaler().fit_transform(F)

    sv = None
    if args.transcribe:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import torch_compat  # noqa: F401
        from bench_stt_hard import SenseVoice

        sv = SenseVoice()

    result = {"segments": len(rows), "k": {}}
    prev = -1.0
    best = 2
    for k in range(2, args.kmax + 1):
        km = KMeans(n_clusters=k, n_init=20, random_state=0).fit(X)
        sil = float(silhouette_score(X, km.labels_)) if k < len(rows) else 0.0
        sizes = np.bincount(km.labels_, minlength=k)
        tiny = int((sizes < 3).sum())
        improv = sil - prev
        print(f"\n=== k={k}  silhouette={sil:.3f}  (개선 {improv:+.3f})  크기분포={sizes.tolist()}  "
              f"1~2구간 군집={tiny} ===")
        info = {"silhouette": round(sil, 4), "sizes": sizes.tolist(), "tiny_clusters": tiny,
                "clusters": []}
        for c in range(k):
            idx = np.where(km.labels_ == c)[0]
            if len(idx) == 0:
                continue
            f0s = [F[i][-1] for i in idx if F[i][-1] > 0]
            dur = sum((rows[i][1] - rows[i][0]) / sr for i in idx)
            rep = max(idx, key=lambda i: (rows[i][1] - rows[i][0]))
            item = {"size": int(len(idx)), "seconds": round(dur),
                    "f0_median": round(float(np.median(f0s))) if f0s else 0,
                    "rep_start_s": round(rows[rep][0] / sr, 1)}
            if sv is not None:
                import io
                import wave

                a, b = rows[rep]
                buf = io.BytesIO()
                with wave.open(buf, "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(sr)
                    w.writeframes(pcm[a:b].astype(np.int16).tobytes())
                item["rep_text"] = sv.transcribe(buf.getvalue())[0][:50]
            info["clusters"].append(item)
            print(f"   c{c}: {item['size']:3d}구간 {item['seconds']:5d}s F0~{item['f0_median']}Hz "
                  f"{item.get('rep_text', '')}")
        result["k"][str(k)] = info
        if tiny == 0 and improv > 0.02:
            best = k
        prev = max(prev, sil)

    gaps = sorted(((F[i][-1]) for i in range(len(F)) if F[i][-1] > 0))
    result["f0_distribution"] = {
        "p10": round(float(np.percentile(gaps, 10))),
        "p50": round(float(np.percentile(gaps, 50))),
        "p90": round(float(np.percentile(gaps, 90))),
    }
    result["suggested_k"] = best
    print(f"\n[suggest] k={best}  (실루엣 개선이 0.02 미만이거나 1~2구간 군집이 생기는 지점에서 멈춤)")
    print(f"[f0] p10={result['f0_distribution']['p10']} p50={result['f0_distribution']['p50']} "
          f"p90={result['f0_distribution']['p90']} Hz")

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "speaker_count.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"[done] {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
