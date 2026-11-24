#!/usr/bin/env python3
"""대화 경로 LLM 후보 측정 — 클라우드(OpenRouter) vs 로컬.

왜 필요한가
  2026-11-24 실측: 로컬 LLM 은 대화 경로에 못 쓴다(qwen2.5-coder 3/9·9~18s,
  llama3.1:8b 9/9·5분+, gemma3:4b 중국어 샘, EXAONE-4 사고 모드 제어 불가).
  → 클라우드로 옮기는 판단은 끝났고, 남은 것은 **어떤 모델로** 와 **실제로 몇 초인가**다.
  이 스크립트는 ①첫 토큰 지연(TTFT) ②전체 시간 ③한국어 여부 ④응답 길이 를 모델별로 낸다.
  대화 경로에서 중요한 건 TTFT 다(전체 시간은 문장이 길어지면 자연히 늘어난다).

실행
  # 키가 없으면 아무것도 하지 않고 안내만 한다(과금 사고 방지)
  OPENAI_API_KEY=sk-or-... .venv-voicemem/bin/python integration/llm_chat_bench.py
  OPENAI_API_KEY=... .venv-voicemem/bin/python integration/llm_chat_bench.py --models google/gemini-2.5-flash meta-llama/llama-3.1-8b-instruct
  # 로컬 비교(ollama)
  .venv-voicemem/bin/python integration/llm_chat_bench.py --base http://127.0.0.1:11434/v1 \
      --models gemma3:4b --api-key ollama
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time

SYSTEM = (
    "너의 이름은 마야(Maya)다. 사용자의 한국어 음성 비서로서 자연스럽고 친근하게 응답한다. "
    "답변은 1~3문장으로 짧게, 구어체 한국어로 한다."
)
# 음성 비서의 실제 턴 모양(짧은 질문 → 1~3문장 답)
TURNS = [
    "오늘 서울 날씨 어때?",
    "내일 아침 9시에 회의 있는 거 맞지?",
    "배고픈데 뭐 먹을까?",
]
DEFAULT_MODELS = [
    "google/gemini-2.5-flash",
    "meta-llama/llama-3.1-8b-instruct",
    "openai/gpt-4o-mini",
]
KO_RE = re.compile(r"[\uac00-\ud7a3]")
OTHER_CJK = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff]")


def one_turn(base: str, key: str, model: str, text: str, timeout: float) -> dict:
    import httpx

    payload = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": text}],
        "stream": True,
        "temperature": 0.7,
        "max_tokens": 200,
    }
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if "openrouter" in base:
        headers["HTTP-Referer"] = "https://privseai.com/maya"
        headers["X-Title"] = "Maya Voice Assistant"

    t0 = time.monotonic()
    ttft = None
    chunks: list[str] = []
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout, connect=8.0)) as c:
            with c.stream("POST", f"{base.rstrip('/')}/chat/completions",
                          json=payload, headers=headers) as r:
                if r.status_code >= 400:
                    return {"error": f"HTTP {r.status_code}", "body": r.read()[:300].decode("utf-8", "replace")}
                for line in r.iter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    for ch in obj.get("choices", []):
                        tok = (ch.get("delta") or {}).get("content") or ""
                        if tok:
                            if ttft is None:
                                ttft = time.monotonic() - t0
                            chunks.append(tok)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {str(exc)[:100]}"}

    full = "".join(chunks).strip()
    ko = len(KO_RE.findall(full))
    other = len(OTHER_CJK.findall(full))
    return {
        "ttft_s": round(ttft, 3) if ttft is not None else None,
        "total_s": round(time.monotonic() - t0, 3),
        "chars": len(full),
        "korean_ratio": round(ko / (ko + other), 2) if (ko + other) else 0.0,
        "reply": full[:120],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="*", default=DEFAULT_MODELS)
    ap.add_argument("--base", default=os.environ.get("OPENAI_BASE", "https://openrouter.ai/api/v1"))
    ap.add_argument("--api-key", default=os.environ.get("OPENAI_API_KEY", ""))
    ap.add_argument("--gateway", action="store_true",
                    help="자체 게이트웨이(키 없이 동작) 측정 — 빈 키 허용")
    ap.add_argument("--timeout", type=float, default=25)
    args = ap.parse_args()

    if not args.api_key and not args.gateway:
        print("OPENAI_API_KEY 가 없습니다 — 과금/실패를 피하려고 여기서 멈춥니다.")
        print("  예: OPENAI_API_KEY=sk-or-... .venv-voicemem/bin/python integration/llm_chat_bench.py")
        print("  자체 게이트웨이면: --gateway --base https://privseai.com/privseai/llm/v1")
        return 2

    report = {"base": args.base, "models": {}, "turns": TURNS}
    for m in args.models:
        print(f"\n=== {m} ===", flush=True)
        rows = []
        for t in TURNS:
            r = one_turn(args.base, args.api_key, m, t, args.timeout)
            rows.append({"turn": t, **r})
            if r.get("error"):
                print(f"  ERROR {r['error']}  {r.get('body', '')[:120]}")
            else:
                print(f"  TTFT {r['ttft_s']}s  total {r['total_s']}s  "
                      f"{r['chars']}자  ko={r['korean_ratio']}  | {r['reply'][:60]}")
        ok = [r for r in rows if not r.get("error")]
        report["models"][m] = {
            "rows": rows,
            "avg_ttft_s": round(sum(r["ttft_s"] for r in ok if r["ttft_s"]) / len(ok), 3) if ok else None,
            "avg_total_s": round(sum(r["total_s"] for r in ok) / len(ok), 3) if ok else None,
            "korean_ok": all((r.get("korean_ratio") or 0) >= 0.95 for r in ok) if ok else False,
        }

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "llm_chat_bench.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("\n| 모델 | 평균 TTFT | 평균 총시간 | 한국어 유지 |")
    print("|------|-----------|-------------|-------------|")
    for m, v in report["models"].items():
        print(f"| {m} | {v['avg_ttft_s']}s | {v['avg_total_s']}s | {'O' if v['korean_ok'] else 'X'} |")
    print(f"\n보고서: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
