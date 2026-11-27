"""End-to-end Opus stream round-trip test using OpusStreamCodec.

What this exercises
-------------------
1. 700 Hz sine wave — pure tonal signal; lossy codecs should still score > 0.95.
2. Speech-like noise (white noise, RMS ~5000) — broadband stimulus; correlation
   > 0.7 means the codec preserves the waveform structure.

Both tests would fail if:
  * encode/decode state isn't configured correctly,
  * opuslib's ctypes bindings are broken (macOS Python 3.11 case),
  * the Opus pre-skip isn't accounted for (which we now trim in
    `stream_roundtrip`).

Run
---
    .venv-voicemem/bin/python -m pytest tests/test_opus_roundtrip.py -v
or
    .venv-voicemem/bin/python tests/test_opus_roundtrip.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maya.voicemem.codec.opus_stream import OpusConfig, OpusStreamCodec


SAMPLE_RATE = 16000
FRAME_MS = 20
FRAME = SAMPLE_RATE * FRAME_MS // 1000  # 320
N_FRAMES = 100
N_SAMPLES = N_FRAMES * FRAME


def _build_signal(kind: str, n: int = N_SAMPLES, seed: int = 42) -> np.ndarray:
    if kind == "sine":
        t = np.arange(n) / SAMPLE_RATE
        return (np.sin(2 * np.pi * 700 * t) * 32767 * 0.5).astype(np.int16)
    if kind == "noise":
        rng = np.random.default_rng(seed)
        return rng.normal(0, 5000, n).clip(-32768, 32767).astype(np.int16)
    raise ValueError(kind)


def _roundtrip(pcm: np.ndarray) -> np.ndarray:
    codec = OpusStreamCodec(
        OpusConfig(sample_rate=SAMPLE_RATE, channels=1, frame_size_ms=FRAME_MS)
    )
    try:
        return codec.stream_roundtrip(pcm)
    finally:
        codec.close()


def _aligned_corr(pcm: np.ndarray, recon: np.ndarray) -> float:
    """Correlation after the codec's pre-skip has been trimmed from the output.

    ``OpusStreamCodec.stream_roundtrip`` already strips the first
    ``pre_skip`` decoded samples (zero-padding) before returning. So
    ``recon[i]`` aligns with ``pcm[i]`` and we just compare equal-length
    slices from index 0 on both sides.
    """
    L = min(pcm.size, recon.size)
    if L <= 0:
        return float("nan")
    a = pcm[:L].astype(np.float32)
    b = recon[:L].astype(np.float32)
    return float(np.corrcoef(a, b)[0, 1])


def test_opus_pre_skip_104_samples() -> None:
    codec = OpusStreamCodec(
        OpusConfig(sample_rate=SAMPLE_RATE, channels=1, frame_size_ms=FRAME_MS)
    )
    try:
        assert codec.pre_skip == 104, (
            f"Opus pre-skip at 16 kHz should be 104 samples (CELT lookahead), "
            f"got {codec.pre_skip}"
        )
    finally:
        codec.close()


def test_opus_sine_roundtrip_high_correlation() -> None:
    pcm = _build_signal("sine")
    recon = _roundtrip(pcm)
    corr = _aligned_corr(pcm, recon)
    assert corr > 0.95, f"sine corr={corr:.4f}, expected > 0.95"


def test_opus_noise_roundtrip_preserves_structure() -> None:
    pcm = _build_signal("noise")
    recon = _roundtrip(pcm)
    corr = _aligned_corr(pcm, recon)
    assert corr > 0.7, f"noise corr={corr:.4f}, expected > 0.7"


def test_opus_rms_in_vs_out_close() -> None:
    """For a sustained sine, decoded RMS should be close to input RMS."""
    pcm = _build_signal("sine")
    recon = _roundtrip(pcm)
    L = min(pcm.size, recon.size)
    rms_in = float(np.sqrt(np.mean(pcm[:L].astype(np.float32) ** 2)))
    rms_out = float(np.sqrt(np.mean(recon.astype(np.float32) ** 2)))
    ratio = rms_out / rms_in
    assert 0.85 < ratio < 1.15, f"RMS ratio {ratio:.3f} out of range [0.85, 1.15]"


if __name__ == "__main__":
    # Manual run
    import json

    codec = OpusStreamCodec(
        OpusConfig(sample_rate=SAMPLE_RATE, channels=1, frame_size_ms=FRAME_MS)
    )
    print(f"Opus pre-skip = {codec.pre_skip}")

    for kind, min_corr in (("sine", 0.95), ("noise", 0.7)):
        pcm = _build_signal(kind)
        recon = _roundtrip(pcm)
        corr = _aligned_corr(pcm, recon)
        L = min(pcm.size, recon.size)
        rms_in = float(np.sqrt(np.mean(pcm[:L].astype(np.float32) ** 2)))
        rms_out = float(np.sqrt(np.mean(recon.astype(np.float32) ** 2)))
        status = "OK" if corr > min_corr else "FAIL"
        print(
            f"{kind:6s}  corr={corr:+.4f}  "
            f"RMS_in={rms_in:.1f}  RMS_out={rms_out:.1f}  [{status}]"
        )
    codec.close()
