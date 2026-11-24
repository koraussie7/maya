#!/usr/bin/env python3
"""1단계 진단: 한국어 문장이 **사실 추출 단계**에서 살아남는지 본다.

vm_ko_report.json 의 `facts_count: 0` 이
  (A) LLM 추출이 한국어 사실을 "버린" 것인지,
  (B) 추출은 됐는데 그 뒤(충돌 판정/저장) 단계에서 사라진 것인지
를 갈라내는 것이 목적이다. DB 는 건드리지 않는다(추출 호출만).

핵심 변수: `--full` 없이 돌면 **max_tokens 를 잘라서** 돌린다.
  실측(2026-11-24): qwen3:1.7b(사고 모델)가 실제 추출 prompt(system 37K자)로
  응답을 182초 안에 못 끝냈다 → `APITimeoutError`. 추출기 클라이언트 기본
  timeout 은 60초이고 max_tokens 를 지정하지 않는다. 사고 토큰 + 긴 prompt +
  무제한 출력이 겹친 결과다. 절단 모드로 먼저 "한국어 사실을 뽑는 능력"만 본다.

실행
  .venv-voicemem/bin/python integration/vm_probe_extract.py                 # 모델 1개, 절단
  .venv-voicemem/bin/python integration/vm_probe_extract.py --all           # 후보 전체, 절단
  .venv-voicemem/bin/python integration/vm_probe_extract.py --full          # 실제 추출기 그대로
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

FACTS = [
    "나는 채식주의자이고 견과류에 알레르기가 있어요.",
    "내 이름은 김민수이고 서울에 살아요.",
    "회사는 판교에 있고, 아침 9시에 출근해요.",
]
# 로컬 모델 비교용: 한국어에 강한 순서대로
CANDIDATE_MODELS = ["qwen3:1.7b", "gemma3:4b", "gemma4:12b", "qwen2.5-coder:1.5b"]


def _system_prompt() -> str:
    """실제 추출기가 쓰는 system prompt 를 그대로 가져온다(번역 규칙/속성 필드 포함)."""
    from voicemem.leftbrain.extract_facts_openai import OpenAIMem0V3AdditiveExtractor
    return OpenAIMem0V3AdditiveExtractor()._system


def _user_prompt(texts: list[str]) -> str:
    from voicemem.leftbrain.extract_facts_openai import generate_additive_extraction_prompt
    return generate_additive_extraction_prompt(
        new_messages=[{"role": "user", "content": t} for t in texts],
        current_date="2026-11-24", timestamp="2026-11-24",
    )


def _short_prompt(texts: list[str]) -> str:
    """긴 프롬프트의 **행동 규칙**만 남긴 축약판 (능력 측정용, 제품 경로 아님)."""
    joined = "\n".join(f"- {t}" for t in texts)
    return f"""Extract atomic facts about the user from these Korean utterances.

{joined}

Reply with JSON only: {{"memory": [{{"id": "0", "text": "<fact>", "attributed_to": "user", "attribute": ""}}]}}
Write each fact in the SAME language as the utterance (Korean stays Korean).
Include every stable personal fact: diet/allergy, name, residence, workplace, commute time.
"""


def _call(model: str, base: str, system: str, user: str, max_tokens: int | None,
          timeout: float = 120.0) -> dict:
    from vm_ko_common import openai_client
    client = openai_client(timeout=timeout)
    kw = dict(model=model, temperature=0,
              messages=[{"role": "system", "content": system},
                        {"role": "user", "content": user}],
              response_format={"type": "json_object"})
    if max_tokens:
        kw["max_tokens"] = max_tokens
    t = time.time()
    try:
        r = client.chat.completions.create(**kw)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}", "seconds": round(time.time() - t, 1)}
    dt = round(time.time() - t, 1)
    raw = (r.choices[0].message.content or "").strip()
    out = {"seconds": dt, "raw_chars": len(raw),
           "finish": getattr(r.choices[0], "finish_reason", None)}
    try:
        data = json.loads(raw)
        mems = data.get("memory") if isinstance(data, dict) else None
        out["facts"] = [{"text": m.get("text", ""), "attribute": m.get("attribute", "")}
                        for m in (mems or []) if isinstance(m, dict)]
    except Exception:  # noqa: BLE001
        out["facts"] = []
        out["raw_tail"] = raw[-300:]
    return out


def _hangul(s: str) -> bool:
    return bool(re.search(r"[\uac00-\ud7a3]", s or ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=os.environ.get("OPENAI_MODEL", "qwen3:1.7b"))
    ap.add_argument("--all", action="store_true", help="후보 모델 전체 비교")
    ap.add_argument("--full", action="store_true", help="실제 추출기 프롬프트/설정 그대로")
    ap.add_argument("--max-tokens", type=int, default=0,
                    help="절단 모드에서 출력 상한(0 = 모델이 멈출 때까지)")
    ap.add_argument("--timeout", type=float, default=300.0,
                    help="모델 1회 호출 상한(초). 로컬 4B 는 첫 로딩에 시간이 걸린다")
    args = ap.parse_args()

    base = os.environ.get("OPENAI_BASE_URL", "http://127.0.0.1:11434/v1")
    os.environ["OPENAI_BASE_URL"] = base
    os.environ.setdefault("OPENAI_API_KEY", "ollama")

    if args.full:
        system, user, mt, mode = _system_prompt(), _user_prompt(FACTS), (args.max_tokens or None), "full"
    else:
        system, user, mt, mode = _short_prompt(FACTS), _short_prompt(FACTS), (args.max_tokens or 512), "short"
        system = "You extract personal facts as JSON."

    models = CANDIDATE_MODELS if args.all else [args.model]
    report = {"mode": mode, "base_url": base, "prompt": {"system_chars": len(system),
                                                         "user_chars": len(user)},
              "facts_input": FACTS, "runs": []}
    print(f"[mode={mode}] system={len(system)}자 user={len(user)}자 max_tokens={mt}")

    for m in models:
        print(f"\n=== model={m} ===", flush=True)
        r = _call(m, base, system, user, mt, timeout=args.timeout)
        r["model"] = m
        if r.get("error"):
            print(f"  ERROR {r['error']}  ({r['seconds']}s)")
        else:
            print(f"  {r['seconds']}s  facts={len(r['facts'])}  finish={r['finish']}")
            for f in r["facts"]:
                print(f"    - [{ 'KO' if _hangul(f['text']) else 'NON-KO' }] {f['text']}"
                      f"   [attr={f['attribute']!r}]")
            if r.get("raw_tail"):
                print(f"    raw_tail={r['raw_tail']!r}")
        r["korean_facts"] = sum(1 for f in r.get("facts", []) if _hangul(f["text"]))
        report["runs"].append(r)

    out = os.path.join(ROOT, "integration", "vm_probe_extract.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n보고서: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
