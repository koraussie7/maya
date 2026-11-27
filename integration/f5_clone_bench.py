"""
F5-TTS Lyra Vex zero-shot clone PoC.

Reference: clip_ref_lyra_vex_b.wav (Lyra's actual voice)
Output: /tmp/maya_lyra_f5.wav

Usage:
    source .venv-f5tts/bin/activate
    python integration/f5_clone_bench.py [--text 안녕하세요 저는 마야입니다 음성 확인입니다]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path("/Users/brianyeon/CodeGPT/maya-2")
REF = REPO / "maya/models/tts/reference/clip_ref_lyra_vex_b.wav"
OUT = Path("/tmp/maya_lyra_f5.wav")
TEXT_DEFAULT = "안녕하세요, 저는 마야입니다. 음성 확인을 위해 짧은 문장을 읽어 드립니다."


def ffmpeg_to_wav16k(src: Path, dst: Path) -> None:
    """F5-TTS requires 24kHz mono wav ref. We make ref 24k mono PCM16."""
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-ac", "1", "-ar", "24000", "-sample_fmt", "s16",
        str(dst),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def wav_duration_sec(path: Path) -> float:
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ]).decode().strip()
    return float(out)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--text", default=TEXT_DEFAULT)
    parser.add_argument("--ref", default=str(REF))
    parser.add_argument("--out", default=str(OUT))
    parser.add_argument("--steps", type=int, default=8, help="NFE steps (8=fast, 32=quality)")
    parser.add_argument("--cfg", type=float, default=2.0)
    parser.add_argument("--speed", type=float, default=1.0)
    args = parser.parse_args()

    ref_src = Path(args.ref)
    if not ref_src.exists():
        print(f"REF missing: {ref_src}", file=sys.stderr); sys.exit(1)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # 1) ref → 24k mono wav (F5-TTS expects this)
    ref_24k = Path("/tmp/f5_ref_24k.wav")
    print(f"[1/4] ffmpeg → 24k mono wav: {ref_24k}")
    ffmpeg_to_wav16k(ref_src, ref_24k)
    ref_dur = wav_duration_sec(ref_24k)
    print(f"      ref duration = {ref_dur:.2f}s")

    # 2) write transcribe.txt next to ref (F5-TTS API uses ref_text to guide)
    ref_text_path = ref_24k.with_suffix(".txt")
    # Note: we don't have ground truth ref text. F5-TTS supports empty ref_text
    # (model will use ASR internally), but this requires faster-whisper installed.
    # To avoid that dep, we provide a placeholder. Voice identity may be slightly off.
    ref_text_path.write_text("", encoding="utf-8")
    print(f"[2/4] wrote empty ref_text at {ref_text_path} (F5-TTS will use internal ASR)")

    # 3) F5-TTS inference
    from f5_tts.infer.utils_infer import (
        load_model, load_vocoder, infer_process, preprocess_ref_audio_text,
    )
    print("[3/4] loading F5-TTS model + vocoder (CPU)…")
    t_load = time.time()
    model = load_model("F5TTS_v1_Base", hf_cache_dir=os.environ.get("HF_HOME"))
    vocoder = load_vocoder("vocos")
    print(f"      load took {time.time()-t_load:.1f}s")

    # preprocess ref (will run ASR if ref_text empty — requires faster-whisper)
    try:
        ref_audio_proc, ref_text_proc = preprocess_ref_audio_text(
            str(ref_24k), str(ref_text_path),
        )
    except Exception as e:
        print(f"      preprocess_ref_audio_text failed: {e}", file=sys.stderr)
        print("      If faster-whisper missing, install it OR provide ref_text manually.", file=sys.stderr)
        sys.exit(2)
    print(f"      ASR ref_text → {ref_text_proc!r}")

    t0 = time.time()
    audio, sr = infer_process(
        ref_audio_proc, ref_text_proc,
        args.text, model, vocoder,
        cross_fade_duration=0.15, nfe_step=args.steps, cfg_strength=args.cfg,
        speed=args.speed, device="cpu",
    )
    infer_dt = time.time() - t0

    # 4) save wav
    import soundfile as sf
    sf.write(str(out_path), audio, sr, subtype="PCM_16")
    out_dur = wav_duration_sec(out_path)

    print(f"[4/4] wrote {out_path}  sr={sr}  dur={out_dur:.2f}s  infer={infer_dt:.1f}s  "
          f"RTF={infer_dt/out_dur:.2f}x")

    summary = {
        "engine": "F5-TTS",
        "ref": str(ref_24k),
        "ref_dur_s": ref_dur,
        "asr_ref_text": ref_text_proc,
        "text": args.text,
        "out": str(out_path),
        "out_dur_s": out_dur,
        "infer_s": round(infer_dt, 2),
        "rtf": round(infer_dt / out_dur, 2),
        "nfe_step": args.steps,
        "cfg": args.cfg,
        "speed": args.speed,
    }
    json_path = Path("/tmp/maya_lyra_f5.json")
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"summary → {json_path}")


if __name__ == "__main__":
    main()
