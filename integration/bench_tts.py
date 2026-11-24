#!/usr/bin/env python3
"""TTS 후보 측정 — 첫 소리까지 시간(latency-to-first-audio) 중심.

왜 이 지표인가
  §2-2 실측에서 `say` 는 문장 길이와 무관하게 **3.4초 고정비용**을 낸다.
  대화형 음성에서 사용자가 느끼는 것은 총 생성 시간이 아니라
  **"말이 끝난 뒤 첫 소리까지"** 다. 그래서 그것을 1순위로 잰다.

측정 항목
  · cold   : 프로세스 시작 후 첫 합성 (모델 로드 포함 아님 — 로드 제외)
  · warm   : 같은 엔진으로 연속 합성 (서버가 실제로 쓰는 조건)
  · first  : warm 상태에서 첫 PCM 청크가 나올 때까지
  · RTF    : 생성시간 / 오디오 길이 (< 1.0 이면 실시간보다 빠름)
  · CER    : 생성된 오디오를 SenseVoice 로 되받아 전사 → 원문과 비교
             (한국어 발음이 뭉개지면 CER 이 오른다)

실행:
  cd /Users/brianyeon/CodeGPT/maya
  .venv-stt/bin/python /Users/brianyeon/CodeGPT/maya-2/integration/bench_tts.py
"""
from __future__ import annotations

import os
import re
import statistics
import subprocess
import sys
import tempfile
import time

MODELS = "/Users/brianyeon/CodeGPT/maya/models/tts"
SENSEVOICE_DIR = "/Users/brianyeon/CodeGPT/maya/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"

SENTENCES = [
    "안녕하세요.",
    "오늘 날씨가 정말 좋네요.",
    "무엇을 도와드릴까요?",
    "예약이 완료되었습니다.",
    "잠시만 기다려 주세요.",
    "감사합니다. 좋은 하루 보내세요.",
]

PUNCT = re.compile(r"[^\w\s가-힣]")


def norm(s: str) -> str:
    """CER 비교용: 문장부호/공백/일본어 제거."""
    s = re.sub(r"[\u3040-\u30ff]", "", s)   # 히라가나/가타카나
    return PUNCT.sub("", s).replace(" ", "").strip()


def cer(ref: str, hyp: str) -> float:
    r, h = norm(ref), norm(hyp)
    if not r:
        return 0.0 if not h else 1.0
    dp = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, dp[0] = dp[0], i
        for j in range(1, len(h) + 1):
            cur = dp[j]
            dp[j] = min(dp[j] + 1, dp[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev = cur
    return dp[len(h)] / len(r)


def pcm_dur(pcm: bytes, rate: int = 16000) -> float:
    return len(pcm) / 2 / rate


# ── 엔진 래퍼 ────────────────────────────────────────────────
class SayTTS:
    name = "say (Yuna) — 현재"

    def __init__(self):
        self.engine = "say"
        self.sample_rate = 16000

    def synth(self, text: str) -> tuple[bytes, float]:
        fd, aiff = tempfile.mkstemp(suffix=".aiff")
        os.close(fd)
        try:
            t0 = time.perf_counter()
            subprocess.run(["say", "-v", "Yuna", "-o", aiff, text],
                           check=True, capture_output=True)
            t_say = time.perf_counter() - t0
            r = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", aiff,
                 "-ar", "16000", "-ac", "1", "-f", "s16le", "-"],
                capture_output=True, check=True)
            return r.stdout, (time.perf_counter() - t0) * 1000
        finally:
            try:
                os.unlink(aiff)
            except OSError:
                pass


class SherpaTTS:
    """sherpa-onnx VITS/Kokoro 계열 (ONNX, CPU)."""

    def __init__(self, name: str, model_dir: str, tokens: str, model_file: str,
                 kind: str = "vits", lang: str = "ko", speaker: int | None = None):
        import sherpa_onnx
        self.name = name
        self.speaker = speaker
        d = os.path.join(MODELS, model_dir)
        t_load = time.perf_counter()
        if kind == "kokoro":
            cfg = sherpa_onnx.OfflineTtsConfig(
                model=sherpa_onnx.OfflineTtsModelConfig(
                    kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                        model=os.path.join(d, model_file),
                        voices=os.path.join(d, "voices.bin"),
                        tokens=os.path.join(d, tokens),
                        data_dir=d,
                    ),
                    num_threads=4,
                ),
            )
        else:
            cfg = sherpa_onnx.OfflineTtsConfig(
                model=sherpa_onnx.OfflineTtsModelConfig(
                    vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                        model=os.path.join(d, model_file),
                        tokens=os.path.join(d, tokens),
                        data_dir=d,
                    ),
                    num_threads=4,
                ),
            )
        self.tts = sherpa_onnx.OfflineTts(cfg)
        self.load_s = time.perf_counter() - t_load
        self.sample_rate = self.tts.sample_rate

    def synth(self, text: str) -> tuple[bytes, float]:
        t0 = time.perf_counter()
        kw = {}
        if self.speaker is not None:
            kw["sid"] = self.speaker
        audio = self.tts.generate(text, **kw)
        ms = (time.perf_counter() - t0) * 1000
        samples = audio.samples
        # float32 [-1,1] → Int16 PCM
        import array
        a = array.array("h", (max(-32768, min(32767, int(s * 32767))) for s in samples))
        return a.tobytes(), ms


class PiperTTS:
    """piper-tts (ONNX, espeak 음소화 내장). 한국어 ko_KR-kss-medium."""

    def __init__(self, model_path: str, name: str = "piper ko_KR-kss-medium"):
        from piper import PiperVoice
        self.name = name
        t0 = time.perf_counter()
        self.voice = PiperVoice.load(model_path)
        self.load_s = time.perf_counter() - t0
        self.sample_rate = self.voice.config.sample_rate

    def synth(self, text: str) -> tuple[bytes, float]:
        t0 = time.perf_counter()
        chunks = [ch.audio_int16_bytes for ch in self.voice.synthesize(text)]
        ms = (time.perf_counter() - t0) * 1000
        return b"".join(chunks), ms


# ── 측정 ────────────────────────────────────────────────────
def bench(engine, asr, n_warm: int = 3) -> dict:
    # cold: 첫 호출
    pcm0, cold_ms = engine.synth(SENTENCES[0])
    # warm: 연속 호출 (서버가 실제로 처하는 조건)
    warm_ms, durs, cers = [], [], []
    for i in range(n_warm):
        text = SENTENCES[(i + 1) % len(SENTENCES)]
        pcm, ms = engine.synth(text)
        warm_ms.append(ms)
        durs.append(pcm_dur(pcm, engine.sample_rate))
        if asr is not None and pcm and pcm_dur(pcm, engine.sample_rate) > 0.1:
            hyp, _ = asr(pcm)
            cers.append(cer(text, hyp))
    return {
        "name": engine.name,
        "cold_ms": cold_ms,
        "warm_mean_ms": statistics.mean(warm_ms),
        "warm_p90_ms": sorted(warm_ms)[min(len(warm_ms) - 1, max(0, int(len(warm_ms) * 0.9) - 1))],
        "audio_s": statistics.mean(durs) if durs else 0.0,
        "rtf": ((statistics.mean(warm_ms) / 1000) / statistics.mean(durs)
                if durs and statistics.mean(durs) > 0 else float("nan")),
        "cer": statistics.mean(cers) if cers else None,
        "first_pcm_bytes": len(pcm0),
    }


def main() -> int:
    print("SenseVoice ASR 준비 (TTS 출력 검증용) …")
    import numpy as np
    import sherpa_onnx
    rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=os.path.join(SENSEVOICE_DIR, "model.int8.onnx"),
        tokens=os.path.join(SENSEVOICE_DIR, "tokens.txt"),
        num_threads=4, use_itn=True, language="ko", debug=False)

    def asr(pcm: bytes, rate: int = 16000):
        audio = np.frombuffer(pcm, dtype=np.int16).astype("float32") / 32768.0
        s = rec.create_stream()
        s.accept_waveform(rate, audio)
        rec.decode_stream(s)
        return s.result.text, 0.0

    engines = []

    # 1) baseline: say
    try:
        engines.append(SayTTS())
    except Exception as e:
        print(f"say 실패: {e}")

    # 2) sherpa vits-mimic3-ko
    try:
        engines.append(SherpaTTS("sherpa vits-mimic3-ko (KSS)",
                                 "vits-mimic3-ko_KO-kss_low",
                                 "tokens.txt", "ko_KO-kss_low.onnx"))
    except Exception as e:
        print(f"vits-mimic3-ko 실패: {type(e).__name__}: {e}")

    # 3) sherpa kokoro multi-lang (한국어 포함)
    try:
        engines.append(SherpaTTS("sherpa kokoro-int8-multi-lang",
                                 "kokoro-int8-multi-lang-v1_1",
                                 "tokens.txt", "model.int8.onnx",
                                 kind="kokoro", speaker=0))
    except Exception as e:
        print(f"kokoro 실패: {type(e).__name__}: {e}")

    # 4) piper (한국어 모델 없으면 자동 제외)
    piper_ko = os.path.join(MODELS, "piper-ko", "ko_KR-kss-medium.onnx")
    piper_ko = piper_ko if os.path.isfile(piper_ko) else None
    if piper_ko:
        try:
            engines.append(PiperTTS(piper_ko))
        except Exception as e:
            print(f"piper 실패: {type(e).__name__}: {e}")
    else:
        print("piper: 한국어 모델 없음 → 제외")

    print(f"\n측정 엔진 {len(engines)}개\n")
    results = []
    for eng in engines:
        try:
            r = bench(eng, asr)
            results.append(r)
            cer_s = f"{r['cer']*100:5.1f}%" if r["cer"] is not None else "  n/a"
            print(f"{r['name']:38s} cold {r['cold_ms']:7.0f}ms  "
                  f"warm {r['warm_mean_ms']:7.0f}ms  RTF {r['rtf']:.3f}  CER {cer_s}")
        except Exception as e:
            print(f"{eng.name:38s} 실패: {type(e).__name__}: {e}")

    if results:
        print("\n" + "=" * 88)
        base = next((r for r in results if r["name"].startswith("say")), None)
        print(f"{'엔진':38s} {'warm(첫소리)':>14s} {'RTF':>7s} {'CER':>7s} {'개선':>9s}")
        print("-" * 88)
        for r in results:
            cer_s = f"{r['cer']*100:5.1f}%" if r["cer"] is not None else "  n/a"
            gain = ""
            if base and r is not base:
                gain = f"{base['warm_mean_ms']/r['warm_mean_ms']:.1f}×"
            print(f"{r['name']:38s} {r['warm_mean_ms']:12.0f}ms {r['rtf']:7.3f} "
                  f"{cer_s:>7s} {gain:>9s}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
