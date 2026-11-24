#!/usr/bin/env python3
"""LLM 후보 측정 — 대화형 한국어 응답의 '첫 문장까지' 지연.

왜 '첫 문장'인가
  TTS 는 문장 단위로 스트리밍된다(§13). 사용자가 첫 소리를 듣는 시점은
  **응답 전체가 아니라 첫 문장이 완성되는 순간**이다.
  따라서 전체 생성 시간보다 첫 문장 지연이 체감과 직결된다.

측정 항목
  · ttft  : 요청 → 첫 토큰 (time to first token)
  · first : 요청 → 첫 문장 종결(.!? 이후) 까지
  · total : 요청 → 응답 완료
  · hangul: 응답의 한글 비율 (한국어로 답하는지)
  · cold/warm 구분 (모델 로드 여부)

실행:
  .venv-stt/bin/python /Users/brianyeon/CodeGPT/maya-2/integration/bench_llm.py [모델...]
"""
from __future__ import annotations

import json
import re
import statistics
import sys
import time
import urllib.request

OLLAMA = "http://127.0.0.1:11434/api/chat"

SYSTEM = (
    "당신은 마야입니다. 한국어로 짧고 자연스럽게 대화합니다. "
    "답변은 두 문장을 넘기지 말고, 먼저 핵심을 한 문장으로 말하세요."
)

PROMPTS = [
    "오늘 날씨가 정말 좋네요",
    "내일 회의는 몇 시에 시작해요?",
    "배송이 너무 늦어서 화가 났어요",
    "예약을 취소하고 싶은데 가능할까요?",
]

DEFAULT_MODELS = [
    "qwen2.5-coder:1.5b",   # 현재
    "gemma3:4b",
    "llama3.1:8b",
]


def hangul_ratio(s: str) -> float:
    letters = [c for c in s if not c.isspace() and c.isalnum()]
    if not letters:
        return 0.0
    ko = sum(1 for c in letters if "\uac00" <= c <= "\ud7a3")
    return ko / len(letters)


def first_sentence_time(stream_times: list[tuple[float, str]]) -> float | None:
    """토큰 누적 중 처음으로 문장 종결부호가 나온 시점."""
    buf = ""
    for t, piece in stream_times:
        buf += piece
        if re.search(r"[.!?。？！]\s*$", buf.strip()) or len(buf.strip()) >= 40:
            return t
    return None


def run_model(model: str, warm: bool = False) -> dict | None:
    ttfts, firsts, totals, ratios, texts = [], [], [], [], []
    t_load = None

    for i, prompt in enumerate(PROMPTS):
        body = json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": prompt},
            ],
            "stream": True,
            "options": {"temperature": 0.7, "num_predict": 120},
        }).encode()

        req = urllib.request.Request(
            OLLAMA, data=body, headers={"Content-Type": "application/json"})

        t0 = time.perf_counter()
        stream_times: list[tuple[float, str]] = []
        text = ""
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                for line in resp:
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    piece = (obj.get("message") or {}).get("content", "")
                    if piece:
                        stream_times.append((time.perf_counter() - t0, piece))
                        text += piece
                    if obj.get("done"):
                        break
        except Exception as exc:  # noqa: BLE001
            print(f"    [{model}] '{prompt[:20]}…' 실패: {type(exc).__name__}: {exc}")
            return None

        total = time.perf_counter() - t0
        if i == 0:
            t_load = total          # 첫 호출 = 콜드(모델 로드 포함)

        ttft = stream_times[0][0] if stream_times else None
        first = first_sentence_time(stream_times)
        if ttft is not None:
            ttfts.append(ttft * 1000)
        if first is not None:
            firsts.append(first * 1000)
        totals.append(total * 1000)
        ratios.append(hangul_ratio(text))
        texts.append(text.strip())

    if not totals:
        return None

    return {
        "model": model,
        "ttft": statistics.mean(ttfts) if ttfts else None,
        "first": statistics.mean(firsts) if firsts else None,
        "total": statistics.mean(totals),
        "hangul": statistics.mean(ratios),
        "sample": texts[0][:70] if texts else "",
        "cold_included": t_load is not None,
    }


def main() -> int:
    models = sys.argv[1:] or DEFAULT_MODELS
    print(f"측정 모델 {len(models)}개 / 프롬프트 {len(PROMPTS)}개\n")

    results = []
    for m in models:
        print(f"── {m} ──")
        r = run_model(m)
        if r:
            results.append(r)
            print(f"  {r['sample']!r}")
            print(f"  ttft {r['ttft']:.0f}ms  첫문장 {r['first']:.0f}ms  "
                  f"총 {r['total']:.0f}ms  한글 {r['hangul']*100:.0f}%")
        print()

    if not results:
        print("측정 결과 없음")
        return 1

    print("=" * 92)
    print(f"{'모델':26s} {'ttft':>8s} {'첫문장':>9s} {'총':>9s} {'한글':>6s}")
    print("-" * 92)
    base = next((r for r in results if r["model"].startswith("qwen2.5-coder")), None)
    for r in results:
        gain = ""
        if base and r is not base and base["first"] and r["first"]:
            gain = f"  (첫문장 {base['first']/r['first']:.1f}× 빠름)"
        print(f"{r['model']:26s} {r['ttft']:7.0f}ms {r['first']:8.0f}ms "
              f"{r['total']:8.0f}ms {r['hangul']*100:5.0f}%{gain}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
