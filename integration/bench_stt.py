#!/usr/bin/env python3
"""SenseVoice(sherpa-onnx) 한국어 전사 실측 + faster-whisper 비교.

합성음(`say -v Yuna`)으로 한국어 오디오를 만들어 두 엔진에 넣는다.
⚠ 합성음이라 실제 마이크 대비 낙관적일 수 있다.

실행:
  cd /Users/brianyeon/CodeGPT/maya
  .venv-stt/bin/python /Users/brianyeon/CodeGPT/maya-2/integration/bench_stt.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time

MODEL_DIR = "/Users/brianyeon/CodeGPT/maya/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"

SENTENCES = [
    "안녕하세요.",
    "오늘 날씨가 정말 좋네요.",
    "내일 회의는 몇 시에 시작해요?",
    "배송이 너무 늦어서 화가 났어요.",
    "그거 어떻게 하는지 알려주실 수 있나요?",
    "예약을 취소하고 싶은데 가능할까요?",
    "결제가 두 번 됐어요. 확인해 주세요.",
    "감사합니다. 좋은 하루 보내세요.",
]


def synth_pcm(text: str) -> bytes:
    """say → AIFF → 16k mono s16le PCM."""
    fd, aiff = tempfile.mkstemp(suffix=".aiff")
    os.close(fd)
    try:
        subprocess.run(["say", "-v", "Yuna", "-o", aiff, text],
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


def bench_sensevoice(cases: list[tuple[str, bytes]]) -> list[tuple[str, float]]:
    import numpy as np
    import sherpa_onnx

    for variant in ("model.int8.onnx", "model.onnx"):
        path = os.path.join(MODEL_DIR, variant)
        if not os.path.isfile(path):
            continue
        dir_path = MODEL_DIR
        print(f"\n── SenseVoice [{variant}] ──")
        t0 = time.perf_counter()
        rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=path,
            tokens=os.path.join(dir_path, "tokens.txt"),
            num_threads=4,
            use_itn=True,
            language="ko",
            debug=False,
        )
        load = time.perf_counter() - t0
        print(f"로드 {load:.2f}s")

        out = []
        total_audio = 0.0
        for text, pcm in cases:
            audio = np.frombuffer(pcm, dtype=np.int16).astype("float32") / 32768.0
            total_audio += len(audio) / 16000
            t0 = time.perf_counter()
            s = rec.create_stream()
            s.accept_waveform(16000, audio)
            rec.decode_stream(s)
            ms = (time.perf_counter() - t0) * 1000
            got = s.result.text
            out.append((got, ms))
            # ⚠ 기존 마커는 '앞 6글자가 맞으면 O' 라서 문장 중간이 틀려도 통과했다.
            #   CER 로 직접 비교한다 (문장부호/공백 제외).
            mark = "O" if cer(text, got) <= 0.10 else "?"
            print(f"  {ms:6.0f}ms  [{mark}] {got}")
        total_ms = sum(m for _, m in out)
        print(f"  합계 {total_ms:.0f}ms / 오디오 {total_audio:.2f}s "
              f"(RTF {total_ms/1000/total_audio:.3f})")
        # ⚠ int8 만 측정하고 'break' 하면 full 모델은 영원히 안 돌아간다.
        #   둘 다 재고 비교하도록 break 를 제거했다 (full 은 시간이 더 걸린다).
        print(f"  [{variant}] CER {sum(cer(t, g) for (t, _), (g, _) in zip(cases, out))/len(out)*100:.1f}%")
    return out


def bench_whisper(cases: list[tuple[str, bytes]], model: str = "tiny"):
    import numpy as np
    from faster_whisper import WhisperModel

    print(f"\n── faster-whisper [{model}] ──")
    t0 = time.perf_counter()
    m = WhisperModel(model, device="cpu", compute_type="int8")
    print(f"로드 {time.perf_counter()-t0:.2f}s")
    out = []
    total_audio = 0.0
    for text, pcm in cases:
        audio = np.frombuffer(pcm, dtype=np.int16).astype("float32") / 32768.0
        total_audio += len(audio) / 16000
        t0 = time.perf_counter()
        segs, _ = m.transcribe(audio, language="ko", beam_size=1,
                               vad_filter=False, condition_on_previous_text=False)
        got = " ".join(s.text.strip() for s in segs).strip()
        ms = (time.perf_counter() - t0) * 1000
        out.append((got, ms))
        print(f"  {ms:6.0f}ms  {got}")
    print(f"  합계 {sum(m for _, m in out):.0f}ms / 오디오 {total_audio:.2f}s "
          f"(RTF {sum(m for _, m in out)/1000/total_audio:.3f})")
    return out


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


def main() -> int:
    print("오디오 생성 중 (say -v Yuna → 16k PCM) …")
    cases = [(t, synth_pcm(t)) for t in SENTENCES]
    dur = sum(len(p) / 2 / 16000 for _, p in cases)
    print(f"{len(cases)}문장 / 오디오 {dur:.2f}s")

    results = {}
    try:
        results["sensevoice"] = bench_sensevoice(cases)
    except Exception as e:
        print(f"SenseVoice 실패: {type(e).__name__}: {e}")

    try:
        results["whisper-tiny"] = bench_whisper(cases, "tiny")
    except Exception as e:
        print(f"faster-whisper 실패: {type(e).__name__}: {e}")

    if len(results) == 2:
        print("\n" + "=" * 60)
        print("정확도 비교 (CER, 낮을수록 좋음)")
        for name, out in results.items():
            c = [cer(SENTENCES[i], got) for i, (got, _) in enumerate(out)]
            avg = sum(c) / len(c)
            ms = sum(m for _, m in out)
            print(f"  {name:14s} CER {avg*100:5.1f}%  총 {ms:6.0f}ms  RTF {ms/1000/dur:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
