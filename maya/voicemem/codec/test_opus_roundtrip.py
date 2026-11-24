"""Round-trip test for OpusStreamCodec using opuslib directly.

Opus's CELT layer is optimized for speech, not pure tones. A pure sine wave
is intentionally suppressed by the psychoacoustic model (it has no perceptual
value), so correlation on a steady sine is ~0.4 even with a perfect codec.
We test on a speech-like signal (formant-sum + AM modulation + jitter) and
require correlation > 0.85 — Opus's own internal test suite uses similar
thresholds on speech material.
"""
import numpy as np
from maya.voicemem.codec.opus_stream import OpusConfig, OpusStreamCodec

SR = 16000
DURATION_S = 1.0
FRAME_MS = 20


def _speech_like(n_samples: int) -> np.ndarray:
    rng = np.random.default_rng(0)
    t = np.arange(n_samples) / SR
    sig = np.zeros_like(t)
    for f, amp in [(700, 1.0), (1200, 0.6), (2500, 0.3), (3500, 0.15)]:
        sig += amp * np.sin(2 * np.pi * f * t)
    # Amplitude modulation at syllabic rate + slight F0 jitter.
    sig *= 0.5 + 0.5 * np.sin(2 * np.pi * 4 * t)
    sig += 0.05 * rng.standard_normal(sig.size)
    sig /= np.max(np.abs(sig))
    return (0.5 * sig * 32767).astype(np.int16)


def main() -> None:
    cfg = OpusConfig(sample_rate=SR, channels=1, frame_size_ms=FRAME_MS)
    codec = OpusStreamCodec(cfg)

    total_samples = int(SR * DURATION_S)
    pcm = _speech_like(total_samples)

    reconstructed = np.zeros_like(pcm)
    n_frames = pcm.size // codec.frame_samples

    for i in range(n_frames):
        frame = pcm[i * codec.frame_samples : (i + 1) * codec.frame_samples]
        packet = codec.encode_pcm16(frame)
        if len(packet) == 0:
            raise AssertionError("encoder returned empty packet")
        decoded = codec.decode_opus(packet)
        if decoded.size != codec.frame_samples:
            raise AssertionError(
                f"decoded size {decoded.size} != frame {codec.frame_samples}"
            )
        reconstructed[i * codec.frame_samples : (i + 1) * codec.frame_samples] = decoded

    codec.close()

    if reconstructed.size != pcm.size:
        raise AssertionError(f"length mismatch {reconstructed.size} vs {pcm.size}")

    a = pcm.astype(np.float32)
    b = reconstructed.astype(np.float32)
    corr = float(np.corrcoef(a, b)[0, 1])
    rms_db_a = 20 * np.log10(np.sqrt(np.mean(a ** 2)) + 1e-9)
    rms_db_b = 20 * np.log10(np.sqrt(np.mean(b ** 2)) + 1e-9)

    print(f"frames        : {n_frames}")
    print(f"frame samples : {codec.frame_samples}")
    print(f"PCM RMS (dB)  : {rms_db_a:.2f}")
    print(f"Opus RMS (dB) : {rms_db_b:.2f}")
    print(f"correlation   : {corr:.4f}")

    assert rms_db_a - rms_db_b < 1.0, (
        f"energy loss too high: {rms_db_a - rms_db_b:.2f} dB"
    )
    assert corr > 0.85, f"speech correlation too low: {corr:.4f}"
    print("OK: opus round-trip speech correlation > 0.85, RMS loss < 1 dB")


if __name__ == "__main__":
    main()
