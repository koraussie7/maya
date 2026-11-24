"""EXAONE-4.0 Q8_0 GGUF 불량 감지 (다운로드본 무결성 확인).

증상(2026-11-24, 이 맥): `ollama create` 로 만든 exaone4:latest 가
  - 한국어 질문에 대해 **영어 사고 흐름만** 출력하고 JSON 은 한 번도 닫지 않는다
  - 아주 짧은 입력에서 20~40s (1.2B Q8, CPU 로는 비정상적으로 느림)
  - `max_tokens` 를 96/200 으로 바꿔도 `finish=length` 로 잘린다

`ollama create` 는 이름만 봐서는 텐서를 검증하지 않는다. 그래서 GGUF 의
**픽셀(가중치) 범위와 NaN/Inf** 를 직접 확인한다. 정상 Q8_0 가중치라면
  - 대부분의 값이 [-3, 3] 안에 있고(양자화 오차 포함),
  - NaN/Inf 비율이 사실상 0 에 가깝다.
이 범위를 크게 벗어나면(수십~수백 배, 또는 5~10% 가 NaN) **다운로드/양자화 손상**이다.

실행
  .venv-voicemem/bin/python integration/gguf_weight_check.py /tmp/exaone-4.0-1.2b-q8.gguf
"""
from __future__ import annotations

import sys

import numpy as np


def check(path: str, sample_tensors: int = 6) -> int:
    import gguf  # pip install gguf

    r = gguf.GGUFReader(path)
    names = [t.name for t in r.tensors]
    arch = r.fields["general.architecture"]
    print(f"{path}\n  tensors={len(names)}  architecture={arch.contents()}")

    # 큰 텐서 위주로 고른다(임베딩/출력/MLP) — 손상이 있으면 큰 텐서에서 드러난다.
    order = sorted(r.tensors, key=lambda t: -t.data.nbytes)[:sample_tensors]
    bad = 0
    for t in order:
        arr = t.data
        if t.tensor_type.name == "Q8_0":
            # Q8_0 블록: fp16 scale(2B) + int8 * 32. scale 만 읽어 분포를 본다.
            raw = np.frombuffer(arr, dtype=np.uint8)
            blocks = raw.size // 34
            scales = raw[: blocks * 34].reshape(blocks, 34)[:, :2].copy().view(np.float16)
            finite = np.isfinite(scales)
            nan_ratio = 1.0 - finite.mean()
            s = scales[finite].astype(np.float32)
            info = (f"scale[{s.min():.4g}, {s.max():.4g}] mean={s.mean():.4g} "
                    f"nan/inf={nan_ratio * 100:.2f}%")
            ok = nan_ratio < 0.005 and 1e-6 < float(np.abs(s).mean()) < 0.5
        else:  # F32/F16 등: 값을 직접 본다
            v = arr.astype(np.float32) if arr.dtype != np.float16 else arr.astype(np.float32)
            finite = np.isfinite(v)
            nan_ratio = 1.0 - finite.mean()
            f = v[finite]
            info = (f"min={f.min():.4g} max={f.max():.4g} std={f.std():.4g} "
                    f"nan/inf={nan_ratio * 100:.2f}%")
            ok = nan_ratio < 0.001 and float(np.abs(f).max()) < 50
        bad += int(not ok)
        print(f"  {'OK ' if ok else 'BAD'} {t.name:<40} {t.tensor_type.name:<6} "
              f"{arr.nbytes / 1e6:8.1f}MB  {info}")

    print(f"\n판정: {'불량 의심 없음' if bad == 0 else f'{bad}개 텐서에서 비정상 범위'}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(check(sys.argv[1] if len(sys.argv) > 1 else "/tmp/exaone-4.0-1.2b-q8.gguf"))
