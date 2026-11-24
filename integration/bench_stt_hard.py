#!/usr/bin/env python3
"""bench_stt.py 의 결론을 의심해보는 벤치 (벤치마크의 벤치마크).

bench_stt.py 는 8문장·합성음·1회 측정으로 "SenseVoice 가 11× 빠르고 CER 3.4%"
라고 결론냈다. 이 스크립트는 그 결론이 다음 요인에 흔들리는지 본다.

  1) 표본 크기 — 8문장 → 40문장으로 늘리면 CER 이 유지되는가
  2) 오디오 길이 — 짧은 문장에서만 유리한 것 아닌가 (RTF vs 길이)
  3) 워밍업 — 첫 호출이 느린 것 아닌가 (engine 을 재사용하는 실제 서버와 다름)
  4) 노이즈 — 합성음은 깨끗하다. 실제 마이크는 배경음이 섞인다
  5) 목소리 — Yuna 한 명만 썼다. 다른 화자에서도 같은가
  6) 문장부호 — CER 계산에서 문장부호를 빼야 공정한가

실행:
  cd /Users/brianyeon/CodeGPT/maya
  .venv-stt/bin/python /Users/brianyeon/CodeGPT/maya-2/integration/bench_stt_hard.py
"""
from __future__ import annotations

import os
import re
import statistics
import subprocess
import sys
import tempfile
import time

MODEL_DIR = "/Users/brianyeon/CodeGPT/maya/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
INT8 = os.path.join(MODEL_DIR, "model.int8.onnx")
TOKENS = os.path.join(MODEL_DIR, "tokens.txt")

# 40문장 — 길이를 고르게 섞는다 (짧은 백채널 / 중간 문장 / 긴 문장)
SENTENCES = [
    # 짧음 (1~8자)
    "네.", "아니요.", "감사합니다.", "잠시만요.", "알겠습니다.",
    "다시 말씀해 주세요.", "지금 바로 갈게요.", "확인했습니다.",
    # 중간 (10~30자)
    "오늘 날씨가 정말 좋네요.",
    "내일 회의는 몇 시에 시작해요?",
    "배송이 너무 늦어서 화가 났어요.",
    "그거 어떻게 하는지 알려주실 수 있나요?",
    "예약을 취소하고 싶은데 가능할까요?",
    "결제가 두 번 됐어요. 확인해 주세요.",
    "이 상품 재고가 아직 있나요?",
    "배송지는 서울시 강남구로 해주세요.",
    "주문번호가 어떻게 되세요?",
    "카드 결제로 진행하겠습니다.",
    "환불은 언제쯤 처리되나요?",
    "포장은 선물용으로 부탁드려요.",
    "영업시간이 몇 시까지예요?",
    "주차장이 따로 있나요?",
    "할인 쿠폰 적용이 안 되는데요.",
    # 김 (30자 이상)
    "제가 어제 주문한 상품이 아직 도착하지 않아서 확인 부탁드립니다.",
    "혹시 이 제품을 다른 색상으로 교환할 수 있는지 궁금합니다.",
    "배송 조회를 해봤는데 송장번호가 조회가 되지 않습니다.",
    "회원 탈퇴를 하려고 하는데 진행 방법을 알려주시면 감사하겠습니다.",
    "지난번에 문의드렸던 건은 아직 답변을 받지 못했습니다.",
    "결제는 됐는데 주문 내역에 표시가 안 되어 있어서요.",
    "이번 주 금요일 오후 세 시쯤 방문할 예정인데 괜찮을까요?",
    "사이즈가 맞지 않아서 교환을 요청드리고 싶습니다.",
    "적립금이 얼마나 있는지 확인하고 싶어서 전화드렸습니다.",
    "제품 사용 중에 문제가 생겼는데 보상이 가능한가요?",
    "매장에서 직접 수령할 수 있는지 궁금합니다.",
    "다른 사람에게 선물로 보낼 건데 배송지 변경이 될까요?",
    "앱에서 로그인이 계속 안 되어서 비밀번호를 재설정했는데도 안 됩니다.",
    "할부 개월 수를 변경할 수 있는지도 함께 확인해 주세요.",
    "구매한 지 일주일이 지났는데 아직 설치가 안 되었습니다.",
    "반품 신청은 어디에서 해야 하는지 알려주세요.",
]

# 목소리 다양성 — 같은 문장을 여러 화자로
VOICES = ["Yuna", "Sora", "Eddy", "Flo"]

NOISE_LEVELS = [0.0, 0.005, 0.02]   # 백색잡음 크기 (0 = 깨끗함)

PUNCT = re.compile(r"[^\w\s가-힣]")


def norm(s: str) -> str:
    """CER 공정 비교용 정규화: 문장부호/공백 제거."""
    return PUNCT.sub("", s).replace(" ", "").strip()


def synth_pcm(text: str, voice: str = "Yuna") -> bytes:
    fd, aiff = tempfile.mkstemp(suffix=".aiff")
    os.close(fd)
    try:
        subprocess.run(["say", "-v", voice, "-o", aiff, text],
                       check=True, capture_output=True)
        r = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", aiff,
             "-ar", "16000", "-ac", "1", "-f", "s16le", "-"],
            capture_output=True, check=True,
        )
        return r.stdout
    finally:
        try:
            os.unlink(aiff)
        except OSError:
            pass


def add_noise(pcm: bytes, level: float) -> bytes:
    """Int16 PCM 에 백색잡음을 섞는다. level=0.02 는 아주 시끄럽다."""
    if level <= 0:
        return pcm
    import numpy as np
    a = np.frombuffer(pcm, dtype=np.int16).astype("float32")
    n = np.random.default_rng(42).normal(0, level * 32768, a.shape[0])
    return np.clip(a + n, -32768, 32767).astype(np.int16).tobytes()


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


# ── 엔진 ──────────────────────────────────────────────
class SenseVoice:
    def __init__(self, threads: int = 4):
        import sherpa_onnx
        self.rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=INT8, tokens=TOKENS, num_threads=threads,
            use_itn=True, language="ko", debug=False)

    def transcribe(self, pcm: bytes) -> tuple[str, float]:
        import numpy as np
        audio = np.frombuffer(pcm, dtype=np.int16).astype("float32") / 32768.0
        t0 = time.perf_counter()
        s = self.rec.create_stream()
        s.accept_waveform(16000, audio)
        self.rec.decode_stream(s)
        return s.result.text, (time.perf_counter() - t0) * 1000


class Whisper:
    def __init__(self, model: str = "tiny"):
        from faster_whisper import WhisperModel
        self.m = WhisperModel(model, device="cpu", compute_type="int8")

    def transcribe(self, pcm: bytes) -> tuple[str, float]:
        import numpy as np
        audio = np.frombuffer(pcm, dtype=np.int16).astype("float32") / 32768.0
        t0 = time.perf_counter()
        segs, _ = self.m.transcribe(audio, language="ko", beam_size=1,
                                    vad_filter=False,
                                    condition_on_previous_text=False)
        got = " ".join(s.text.strip() for s in segs).strip()
        return got, (time.perf_counter() - t0) * 1000


def run_suite(name, engine, cases) -> dict:
    """cases: list[(ref_text, pcm)]"""
    cers, times, durs = [], [], []
    errors = []
    for ref, pcm in cases:
        got, ms = engine.transcribe(pcm)
        c = cer(ref, got)
        cers.append(c)
        times.append(ms)
        durs.append(len(pcm) / 2 / 16000)
        if c > 0.15:
            errors.append((ref, got, c))
    total_audio = sum(durs)
    total_ms = sum(times)
    return {
        "name": name,
        "cer": sum(cers) / len(cers),
        "cer_median": statistics.median(cers),
        "p90": sorted(times)[int(len(times) * 0.9)],
        "mean_ms": statistics.mean(times),
        "total_ms": total_ms,
        "rtf": total_ms / 1000 / total_audio,
        "errors": errors,
    }


def show(r: dict) -> None:
    print(f"  {r['name']:22s} CER {r['cer']*100:5.1f}% "
          f"(중앙값 {r['cer_median']*100:4.1f}%)  "
          f"평균 {r['mean_ms']:6.0f}ms  p90 {r['p90']:6.0f}ms  RTF {r['rtf']:.3f}")
    for ref, got, c in r["errors"][:3]:
        print(f"      ✗ CER {c*100:.0f}%  기대 {ref!r}  실제 {got!r}")


def main() -> int:
    import numpy as np
    np.random.seed(42)

    print("오디오 생성 중 …")
    base = [(s, synth_pcm(s, "Yuna")) for s in SENTENCES]
    dur = sum(len(p) / 2 / 16000 for _, p in base)
    print(f"{len(base)}문장 / {dur:.1f}s ({dur/len(base):.2f}s 평균)\n")

    print("엔진 로드 …")
    t0 = time.perf_counter(); sv = SenseVoice(); t_sv = time.perf_counter() - t0
    t0 = time.perf_counter(); wh = Whisper("tiny"); t_wh = time.perf_counter() - t0
    print(f"  SenseVoice {t_sv:.2f}s / whisper-tiny {t_wh:.2f}s\n")

    print("=" * 74)
    print("① 표본 크기: 40문장 전체 (bench_stt 는 8문장)")
    print("=" * 74)
    a = run_suite("SenseVoice int8", sv, base); show(a)
    b = run_suite("faster-whisper tiny", wh, base); show(b)

    print("\n" + "=" * 74)
    print("② 워밍업 효과: 같은 40문장을 두 번째 통과 (엔진 재사용)")
    print("=" * 74)
    a2 = run_suite("SenseVoice int8 #2", sv, base); show(a2)
    b2 = run_suite("faster-whisper tiny #2", wh, base); show(b2)

    print("\n" + "=" * 74)
    print("③ 오디오 길이별 RTF (짧은 문장에서만 유리한가?)")
    print("=" * 74)
    buckets = {"짧음 <2s": [], "중간 2-4s": [], "김 >4s": []}
    for ref, pcm in base:
        d = len(pcm) / 2 / 16000
        k = "짧음 <2s" if d < 2 else ("중간 2-4s" if d < 4 else "김 >4s")
        buckets[k].append((ref, pcm))
    for k, items in buckets.items():
        if not items:
            continue
        ra = run_suite("SV " + k, sv, items); show(ra)
    print("  ---")
    for k, items in buckets.items():
        if not items:
            continue
        rb = run_suite("WH " + k, wh, items); show(rb)

    print("\n" + "=" * 74)
    print("④ 노이즈 내성 (실제 마이크 환경 모사)")
    print("=" * 74)
    for lvl in NOISE_LEVELS:
        noisy = [(ref, add_noise(pcm, lvl)) for ref, pcm in base]
        label = "깨끗함" if lvl == 0 else f"잡음 σ={lvl}"
        ra = run_suite(f"SV {label}", sv, noisy); show(ra)
        rb = run_suite(f"WH {label}", wh, noisy); show(rb)

    print("\n" + "=" * 74)
    print("⑤ 화자 다양성 (Yuna 외 3명)")
    print("=" * 74)
    for v in VOICES:
        if v == "Sora":
            print(f"  ({v}: macOS 미설치 — 생략)")
            continue
        try:
            cases = [(s, synth_pcm(s, v)) for s in SENTENCES[:12]]
        except subprocess.CalledProcessError:
            print(f"  ({v}: 합성 실패 — 생략)")
            continue
        ra = run_suite(f"SV voice={v}", sv, cases); show(ra)
        rb = run_suite(f"WH voice={v}", wh, cases); show(rb)

    print("\n" + "=" * 74)
    print("요약")
    print("=" * 74)
    print(f"{'':28s} {'CER':>7s} {'평균':>9s} {'p90':>9s} {'RTF':>7s}")
    for r in (a, a2, b, b2):
        print(f"{r['name']:28s} {r['cer']*100:6.1f}% {r['mean_ms']:8.0f}ms "
              f"{r['p90']:8.0f}ms {r['rtf']:7.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
