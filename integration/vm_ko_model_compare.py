#!/usr/bin/env python3
"""한국어 사실 추출 **모델 비교** — 형식/언어/품질/시간을 한 번에.

배경 (2026-11-24 실측)
  - gemma3:4b  : 규칙을 한국어 유지로 맞춰도 **중국어로 출력**(`用户表示自己是素食主义者…`), 284s
  - qwen3:1.7b : 사고 토큰으로 JSON 미완성, 182s+
  - qwen2.5-coder:1.5b : 형식 OK, 144s — 한국어 품질 미검증
  → "로컬 소형 모델로 무료 한국어 추출"이 성립하는지, 어떤 모델이 그나마 쓸만한지 판단한다.

설계
  * 프롬프트는 **덧붙여 말하지 않는 최소 버전** — 시스템 규칙을 요약한 한국어 지시 + JSON 스키마.
    (실제 파이프라인의 37K자 프롬프트를 쓰면 시간이 모델 비교가 아니라 프롬프트 처리 능력
     측정이 된다. 그래서 "능력"을 보려면 짧게, "제품 경로"를 보려면 --full.)
  * 결과는 **원본 JSON 문자열**도 그대로 저장한다 → 파싱 실패가 모델 탓인지 형식 탓인지 나중에 검증 가능.
  * 지표: JSON 완성도 / 한국어율 / **필수 사실 키워드 커버리지** / 시간 / 토큰
  * `think:false` (qwen3 류 사고 차단)와 `num_predict` 상한을 함께 넘긴다 — 실제 파이프라인도
    같은 옵션을 써야 하므로 비교가 의미를 가진다.

실행
  .venv-voicemem/bin/python integration/vm_ko_model_compare.py            # 기본 후보들
  .venv-voicemem/bin/python integration/vm_ko_model_compare.py --models gemma3:4b hermes3:8b
  .venv-voicemem/bin/python integration/vm_ko_model_compare.py --full     # 실제 추출기 프롬프트로
  .venv-voicemem/bin/python integration/vm_ko_model_compare.py --models exaone4:latest
  .venv-voicemem/bin/python integration/vm_ko_model_compare.py --all       # 위 기본 + exaone

측정 함정 (실측으로 고친 것)
  * 결과 파일(`vm_ko_model_compare.json`)은 **실행마다 덮어쓴다**. 후보를 나춰 돌릴 때는
    돌리기 전에 기존 파일을 백업하지 않으면 앞선 모델 결과가 사라진다(실측 사고).
  * `OPENAI_BASE_URL` 은 반드시 arg 로 전달된다. 스크립트 안에서 `setdefault` 하면
    이미 설정된 env 를 덮지 못해 엉뚝한 곳(과거 실패한 11434 등)으로 가는 경우가 있다.
  * `--all` 은 오래 걸린다(모델별 1~10분). 백그라운드로 돌려라.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FACTS = [
    "나는 채식주의자이고 견과류에 알레르기가 있어요.",
    "내 이름은 김민수이고 서울에 살아요.",
    "회사는 판교에 있고, 아침 9시에 출근해요.",
]
# 각 문장에서 반드시 남아야 하는 사실 키워드(어형 변형 허용)
EXPECT = [
    ["채식", "견과", "알레르기"],
    ["김민수", "서울"],
    ["판교", "9시", "아침", "출근"],
]
DEFAULT_MODELS = [
    "exaone4:latest",         # EXAONE-4.0-1.2B (GGUF Q8) — 한국어 특화 1순위 후보
    "llama3.1:8b",            # 짧은 프롬프트에서 9/9 달성(실측 최고 품질), 가장 느림
    "gemma3:4b",              # --full 프롬프트에서는 중국어로 샜던 모델
    "qwen2.5-coder:1.5b",     # 가장 빠름(9.4s) — 사실 1건만 뽑는 한계
    "hermes3:8b",
    "dolphin-llama3:8b",
    "minicpm-v4.5-16k:latest",
    "gemma4:12b",             # 가장 큼(시간 상한 주의)
]

SYSTEM = "You are a memory extractor. Output only JSON."

SHORT_PROMPT = """아래 사용자 발화에서 오래 남을 개인 사실을 뽑아라.

발화:
{utterances}

규칙:
- memory[].text 는 **입력과 같은 언어(한국어)** 로 쓴다. 번역 금지.
- 각 사실은 원문 문장 하나에 대응시킨다(존재하지 않는 추론 문장 금지).
- 고유명사·숫자·시간·장소는 원문 표기를 유지한다.

출력 형식(JSON 한 개만):
{{"memory": [{{"id": "0", "text": "<사실>", "attributed_to": "user", "attribute": ""}}]}}
"""

KO_RE = re.compile(r"[\uac00-\ud7a3]")
CJK_OTHER_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff]")  # 기타 한자/가나 = 언어 이탈 신호


def _strip_reasoning(raw: str) -> tuple[str, str]:
    """사고 블록을 분리한다.

    EXAONE-4.0 은 **기본값이 사고 모드**다(chat_template: `enable_thinking` 이 없으면
    ` thinking\n\n</think>\n\n` 를 붙이지 않고 모델이 스스로 사고를 시작한다). 실측:
    짧은 질문에도 647자 사고 후에야 본답(`…\n</think>\n\n대한민국의 수도는 **서울**입니다.`)이 나온다.
    → 본답은 `</think>` **이후**만 쓴다. 사고가 없으면 원문 그대로.
    """
    if "</think>" in raw:
        think, _, answer = raw.partition("</think>")
        return answer.strip(), think.strip()
    return raw, ""


def call(model: str, base: str, system: str, user: str, timeout: float,
         max_tokens: int, think: bool = False) -> dict:
    from openai import OpenAI
    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", "ollama"),
                    base_url=base, timeout=timeout, max_retries=0)
    kw = dict(model=model, temperature=0,
              messages=[{"role": "system", "content": system},
                        {"role": "user", "content": user}])
    if max_tokens:
        kw["max_tokens"] = max_tokens
    # ⚠️ 사고 제어는 **드라이버별로 다르다**:
    #   - ollama(openai 엔드포인트): `extra_body={"options": {...}}` 밖에 안 먹는다.
    #   - vLLM(EXAONE 가이드): `chat_template_kwargs={"enable_thinking": False}`.
    #   ollama 에 `think`/`enable_thinking` 을 넣으면 **무시되어 사고 모델이 계속 사고**한다.
    #   (`options` 만 넣으면 사고를 끌 수 없어 EXAONE-4 는 60s timeout 으로 죽는다 — 실측)
    options = {"num_predict": max_tokens or 1024, "temperature": 0}
    if not think:
        options["stop"] = ["[|endofturn|]"] if "exaone" in model.lower() else options.get("stop", [])
    kw["extra_body"] = {"options": options}
    if not think:
        kw["extra_body"]["chat_template_kwargs"] = {"enable_thinking": False}

    t = time.time()
    try:
        r = client.chat.completions.create(**kw)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}", "seconds": round(time.time() - t, 1)}
    dt = round(time.time() - t, 1)
    raw_all = (r.choices[0].message.content or "").strip()
    raw, think_txt = _strip_reasoning(raw_all)
    u = getattr(r, "usage", None)
    out = {"seconds": dt, "raw": raw, "finish": getattr(r.choices[0], "finish_reason", None),
           "reasoning_chars": len(think_txt),
           "tokens": {"prompt": getattr(u, "prompt_tokens", None),
                      "completion": getattr(u, "completion_tokens", None)} if u else None}
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        # 코드펜스(```json … ```)로 감싸는 모델이 많다(qwen2.5-coder 실측).
        # 파이프라인도 remove_code_blocks() 를 거치므로 여기서도 같은 관용을 준다 —
        # 안 그러면 "JSON 실패"가 실제로는 **측정 스크립트의 과잉 엄격**이다.
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S) \
            or re.search(r"\{.*\}", raw, re.S)
        try:
            data = json.loads(m.group(1) if (m and m.groups()) else (m.group(0) if m else ""))
            out["fenced"] = True
        except Exception:  # noqa: BLE001
            data = None
    if data is not None:
        mems = data.get("memory") if isinstance(data, dict) else data
        out["parsed"] = [{"text": (m or {}).get("text", ""),
                          "attribute": (m or {}).get("attribute", "")}
                         for m in (mems or []) if isinstance(m, dict)]
        out["json_ok"] = bool(out["parsed"])
    else:
        out["parsed"] = []
        out["json_ok"] = False
    return out


def score(rows: list[dict]) -> dict:
    texts = [r["text"] for r in rows]
    joined = " ".join(texts)
    korean_chars = len(KO_RE.findall(joined))
    other_cjk = len(CJK_OTHER_RE.findall(joined))
    total_cjkish = korean_chars + other_cjk
    covered = [sum(1 for k in expect if k in joined) for expect in EXPECT]
    return {
        "n_facts": len(texts),
        "korean_ratio": round(korean_chars / total_cjkish, 3) if total_cjkish else 0.0,
        "has_hangul": korean_chars > 0,
        "foreign_cjk": other_cjk,
        "coverage_per_sentence": covered,
        "coverage": f"{sum(covered)}/{sum(len(e) for e in EXPECT)}",
        "texts": texts,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="*", default=None)
    ap.add_argument("--all", action="store_true", help="DEFAULT_MODELS 전체")
    ap.add_argument("--out", default=None, help="결과 파일 경로(기본 integration/vm_ko_model_compare.json)")
    ap.add_argument("--base", default=os.environ.get("OPENAI_BASE_URL", "http://127.0.0.1:11434/v1"))
    ap.add_argument("--timeout", type=float, default=60.0,
                    help="per-call HTTP timeout (s). 기본 60 — 한국어 추출 모델 측정용.")
    ap.add_argument("--max-tokens", type=int, default=1024,
                    help="사고 모델(EXAONE-4)은 사고 토큰까지 이 예산을 쓴다")
    ap.add_argument("--think", action="store_true", help="사고 토큰을 허용(기본: 끄기 시도)")
    ap.add_argument("--full", action="store_true",
                    help="실제 파이프라인 프롬프트(37K자, use_input_language=True)로 호출")
    args = ap.parse_args()
    if not args.models:
        args.models = DEFAULT_MODELS if args.all else [DEFAULT_MODELS[0]]
    os.environ["OPENAI_BASE_URL"] = args.base
    # 비교 스크립트는 모델 여러 개를 직접 돌리므로 OPENAI_MODEL을 설정하지 않는다.
    # call()은 매번 model 인자를 명시적으로 넘긴다.
    os.environ.setdefault("OPENAI_API_KEY", "ollama")

    if args.full:
        sys.path.insert(0, os.path.join(ROOT, "integration"))
        import vm_ko_extractor_patch  # noqa: F401  (use_input_language=True 강제)
        from voicemem.leftbrain import mem0_additive_prompt_build as pb
        system = "x"
        user = pb.generate_additive_extraction_prompt(
            new_messages=[{"role": "user", "content": t} for t in FACTS],
            current_date="2026-11-24", timestamp="2026-11-24", use_input_language=True)
        prompt_size = {"chars": len(user), "mode": "full"}
    else:
        system = SYSTEM
        user = SHORT_PROMPT.format(utterances="\n".join(f"- {t}" for t in FACTS))
        prompt_size = {"chars": len(user), "mode": "short"}
    print(f"[prompt] {prompt_size}", flush=True)

    report = {"prompt": prompt_size, "facts_input": FACTS, "runs": []}
    for m in args.models:
        print(f"\n=== {m} ===", flush=True)
        r = call(m, args.base, system, user, args.timeout, args.max_tokens, think=args.think)
        r["model"] = m
        if r.get("error"):
            print(f"  ERROR {r['error']} ({r['seconds']}s)", flush=True)
            r["score"] = None
        else:
            sc = score(r.get("parsed") or [])
            r["score"] = sc
            print(f"  {r['seconds']}s  json_ok={r.get('json_ok')}  "
                  f"사실 {sc['n_facts']}  한국어율 {sc['korean_ratio']:.2f}  "
                  f"커버리지 {sc['coverage']}  사고 {r.get('reasoning_chars', 0)}자", flush=True)
            for t in sc["texts"]:
                print(f"    - {t}", flush=True)
            if not r.get("json_ok"):
                print(f"    raw: {r['raw'][:200]!r}", flush=True)
        report["runs"].append(r)

    out = args.out or os.path.join(ROOT, "integration", "vm_ko_model_compare.json")
    # 덮어쓰기 사고 방지: 기존 결과는 옆으로 비켜 둔다(모델별 결과를 잃지 않게).
    if os.path.exists(out):
        bak = out.replace(".json", f".prev-{time.strftime('%H%M%S')}.json")
        os.replace(out, bak)
        print(f"[out] 기존 결과 보관: {os.path.basename(bak)}", flush=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print("\n| 모델 | 시간 | JSON | 사실 | 한국어율 | 커버리지 |")
    print("|------|------|------|------|----------|----------|")
    for r in report["runs"]:
        sc = r.get("score") or {}
        print(f"| {r['model']} | {r['seconds']}s | {'O' if r.get('json_ok') else 'X'} | "
              f"{sc.get('n_facts', '-')} | {sc.get('korean_ratio', '-')} | {sc.get('coverage', '-')} |")
    print(f"\n보고서: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
