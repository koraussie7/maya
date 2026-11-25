#!/usr/bin/env python3
"""STT 호출 횟수 A/B — VAD 가 STT 호출을 얼마나 줄이는지.

시나리오
  · 같은 8s 한국어 클립(또는 사용자 --src WAV)을
    ① baseline(8s 한번에 STT 1회) — STT 호출 1회
    ② RMS 기반 VAD (max_turn_ms=8s guard) — STT 호출 ~1회
    ③ FireRedVAD (silence endpoint) — STT 호출 1~2회
    로 흘리고 STT 호출 횟수·총 처리시간·CER 을 비교한다.

실행
  .venv-voicemem/bin/python integration/bench_stt_vad_split.py
  .venv-voicemem/bin/python integration/bench_stt_vad_split.py --src path/to/clip.wav
  .venv-voicemem/bin/python integration/bench_stt_vad_split.py --no-firered

목적
  "긴급/지능형/STT 호출 최소화" 정책의 정량적 근거 — FireRedVAD 가 RMS 대비
  STT 호출을 줄이는지, 아니라면 baseline(8s 한번)이 더 효율적인지 확인.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
import wave
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

ROOT = os.path.dirname(HERE)
DEFAULT_SRC = os.path.join(ROOT, "maya", "models", "tts", "reference", "clip_ref_speaker_b.wav")


def read_wav(path: str) -> tuple[bytes, int]:
    with wave.open(path, "rb") as w:
        return w.readframes(w.getnframes()), w.getframerate()


def synth_korean_sentence(text: str, sr: int = 16000) -> bytes:
    """`say -v Yuna` → 16k mono s16le PCM."""
    fd, aiff = tempfile.mkstemp(suffix=".aiff")
    os.close(fd)
    try:
        subprocess.run(["say", "-v", "Yuna", "-o", aiff, text],
                       check=True, capture_output=True)
        r = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", aiff,
             "-ar", str(sr), "-ac", "1", "-f", "s16le", "-"],
            capture_output=True, check=True,
        )
        return r.stdout
    finally:
        try:
            os.unlink(aiff)
        except OSError:
            pass


def cer(ref: str, hyp: str) -> float:
    """문자 오류율 (공백 제거 기준)."""
    r = ref.replace(" ", "")
    h = hyp.replace(" ", "")
    if not r:
        return 0.0 if not h else 1.0
    dp = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, dp[0] = dp[0], i
        for j in range(1, len(h) + 1):
            cur = dp[j]
            dp[j] = min(dp[j] + 1, dp[j - 1] + 1,
                        prev + (r[i - 1] != h[j - 1]))
            prev = cur
    return dp[len(h)] / len(r)


# ── baseline: 8s 한번 ──────────────────────────────────────────────────
def baseline_one_shot(pcm: bytes, transcriber, sr: int = 16000) -> dict:
    """오디오 전체를 1번에 STT 호출."""
    t0 = time.perf_counter()
    text = transcriber.transcribe(pcm) if transcriber else "(no model)"
    ms = (time.perf_counter() - t0) * 1000
    return {"calls": 1, "stt_ms": ms, "text": text}


# ── VAD 분할: RMS / FireRedVAD ────────────────────────────────────────
class AudioClock:
    """오디오 시간 == 벽시계."""
    _now = 0.0

    @classmethod
    def advance(cls, seconds: float) -> None:
        cls._now += seconds

    @classmethod
    def monotonic(cls) -> float:
        return cls._now


class _ClockShim:
    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    @staticmethod
    def monotonic() -> float:
        return AudioClock.monotonic()


def run_vad_split(pcm: bytes, sr: int, vad_engine: str,
                  transcriber, chunk_ms: int = 50,
                  silence_tail_s: float = 2.0) -> dict:
    """TurnEngine 으로 PCM 을 청크 스트림처럼 흘려 STT 호출 횟수 측정."""
    from pcm_turn_engine import TurnConfig, TurnEngine
    import pcm_turn_engine as pte

    real_time = pte.time
    pte.time = _ClockShim(real_time)

    try:
        cfg = TurnConfig(vad_engine=vad_engine, use_turn_detector=False)
        eng = TurnEngine(config=cfg)
        if vad_engine == "firered" and getattr(eng, "_vad", None) is None:
            eng.close()
            return {"calls": 0, "stt_ms": 0.0, "text": "",
                    "skipped": "firered 워커 없음"}

        step = int(sr * chunk_ms / 1000) * 2
        silence = b"\x00\x00" * int(sr * silence_tail_s)
        stream = pcm + silence

        calls = 0
        total_stt_ms = 0.0
        texts = []
        eng.begin_turn()
        for off in range(0, len(stream), step):
            chunk = stream[off:off + step]
            if not chunk:
                break
            AudioClock.advance(len(chunk) / 2 / sr)
            res = eng.push_result(chunk)
            if res is not None:
                calls += 1
                t0 = time.perf_counter()
                txt = transcriber.transcribe(res.pcm) if transcriber else ""
                total_stt_ms += (time.perf_counter() - t0) * 1000
                texts.append(txt)
                eng.begin_turn()
        eng.close()
        return {"calls": calls, "stt_ms": total_stt_ms,
                "text": " ".join(t for t in texts if t).strip()}
    finally:
        pte.time = real_time


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=None,
                    help="WAV 경로 (없으면 say -v Yuna 로 한국어 클립 생성)")
    ap.add_argument("--text", default="안녕하세요. 오늘 날씨가 정말 좋네요. 내일 회의는 몇 시에 시작해요?",
                    help="--src 없을 때 합성할 문장")
    ap.add_argument("--sr", type=int, default=16000)
    ap.add_argument("--chunk-ms", type=int, default=50)
    ap.add_argument("--silence-tail-s", type=float, default=2.0)
    ap.add_argument("--no-firered", action="store_true")
    ap.add_argument("--no-baseline", action="store_true",
                    help="baseline(1회 호출) 비교 생략")
    args = ap.parse_args()

    # ── 오디오 준비 ─────────────────────────────────────────────────
    if args.src:
        pcm, sr = read_wav(args.src)
        ref_text = args.text
        print(f"[src] {os.path.basename(args.src)}  sr={sr}  {len(pcm)/2/sr:.2f}s")
        if sr != args.sr:
            print(f"[skip] {sr}Hz — 16kHz WAV 가 필요합니다")
            return 2
    else:
        print(f"[synth] say -v Yuna: '{args.text}'")
        pcm = synth_korean_sentence(args.text, sr=args.sr)
        sr = args.sr
        ref_text = args.text

    audio_s = len(pcm) / 2 / sr
    print(f"[audio] {audio_s:.2f}s / {len(pcm)} bytes\n")

    # ── 전사기 로드 ─────────────────────────────────────────────────
    transcriber = None
    try:
        from faster_whisper import WhisperModel  # type: ignore
        import numpy as np  # noqa: F401

        print("[load] faster-whisper tiny …")
        t0 = time.perf_counter()
        _fw = WhisperModel("tiny", device="cpu", compute_type="int8")

        class _FW:
            def transcribe(self, pcm: bytes) -> str:
                if len(pcm) < 3200:
                    return ""
                audio = np.frombuffer(pcm, dtype=np.int16).astype("float32") / 32768.0
                segs, _ = _fw.transcribe(audio, language="ko", beam_size=1,
                                         vad_filter=False,
                                         condition_on_previous_text=False)
                return " ".join(s.text.strip() for s in segs).strip()

        transcriber = _FW()
        print(f"[load] {time.perf_counter()-t0:.2f}s\n")
    except Exception as e:
        print(f"[warn] faster-whisper 로드 실패: {type(e).__name__}: {e}")
        print("       모드 1,2,3 모두 텍스트 없이 호출 횟수만 비교합니다.\n")

    results: dict[str, dict] = {}

    # ── baseline: 8s 1회 ────────────────────────────────────────────
    if not args.no_baseline:
        print("=== baseline: 8s 한번 (STT 1회) ===")
        r = baseline_one_shot(pcm, transcriber, sr=sr)
        r["audio_s"] = audio_s
        results["baseline"] = r
        print(f"  calls={r['calls']}  stt={r['stt_ms']:.0f}ms")
        print(f"  text='{r['text'][:80]}'")

    # ── RMS 분할 ────────────────────────────────────────────────────
    print("\n=== RMS VAD 분할 ===")
    r = run_vad_split(pcm, sr, "rms", transcriber,
                      chunk_ms=args.chunk_ms,
                      silence_tail_s=args.silence_tail_s)
    r["audio_s"] = audio_s
    results["rms"] = r
    print(f"  calls={r['calls']}  stt={r['stt_ms']:.0f}ms")
    print(f"  text='{r['text'][:80]}'")

    # ── FireRedVAD 분할 ─────────────────────────────────────────────
    if not args.no_firered:
        print("\n=== FireRedVAD 분할 ===")
        r = run_vad_split(pcm, sr, "firered", transcriber,
                          chunk_ms=args.chunk_ms,
                          silence_tail_s=args.silence_tail_s)
        r["audio_s"] = audio_s
        results["firered"] = r
        if r.get("skipped"):
            print(f"  [skip] {r['skipped']}")
        else:
            print(f"  calls={r['calls']}  stt={r['stt_ms']:.0f}ms")
            print(f"  text='{r['text'][:80]}'")

    # ── 비교 요약 ───────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("STT 호출 비교 (긴급/지능형/STT 최소화 정책용)")
    print(f"  {'모드':<12} {'calls':>6} {'stt_ms':>8} {'CER':>7}  text")
    for name, r in results.items():
        c = cer(ref_text, r["text"]) if transcriber else -1.0
        cer_s = f"{c*100:5.1f}%" if c >= 0 else "  n/a"
        print(f"  {name:<12} {r['calls']:>6} {r['stt_ms']:>7.0f}ms {cer_s:>7}  "
              f"'{r['text'][:60]}'")

    if "baseline" in results and "rms" in results:
        b, m = results["baseline"], results["rms"]
        delta_calls = m["calls"] - b["calls"]
        delta_ms = m["stt_ms"] - b["stt_ms"]
        print(f"\n  RMS vs baseline:  calls {delta_calls:+d}  stt {delta_ms:+.0f}ms")

    if "firered" in results and not results["firered"].get("skipped") and "rms" in results:
        fr, m = results["firered"], results["rms"]
        delta_calls = fr["calls"] - m["calls"]
        delta_ms = fr["stt_ms"] - m["stt_ms"]
        print(f"  FireRed vs RMS:   calls {delta_calls:+d}  stt {delta_ms:+.0f}ms")

    return 0


if __name__ == "__main__":
    sys.exit(main())
