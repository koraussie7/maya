#!/usr/bin/env python3
"""LLM 없이 **한국어 원문 그대로** 저장하고 검색 품질만 측정한다.

왜 필요한가 (2026-11-24 실측 근거)
  로컬 모델로는 한국어 사실 추출이 성립하지 않았다.
    - gemma3:4b  : 한국어 → **중국어**로 출력 (`用户表示自己是素食主义者…`, 284s)
    - qwen3:1.7b : JSON 미완성(사고 토큰), 182s+
  게다가 `VOICEMEM_INGEST_RAW_FALLBACK=1` 은 "추출 실패/0건일 때"만 발동하므로
  위처럼 **엉터리로 '성공'하는** 경우엔 원문이 저장되지 않는다.

  그래서 이 스크립트는 추출 단계를 아예 거치지 않고, 공개 API 인
  `LeftBrainMemoryRepository.append_extracted(..., user_id=...)` 에
  원문을 1건씩 넣는다(= 벡터 + JSON 미러를 정상 경로로 기록).
  목적은 단 하나: **한국어 문장 ↔ 한국어 질의에서 E5 검색이 되는가**.
  (추출 품질은 별개 문제 — 모델 교체 과제로 남긴다.)

실행
  .venv-voicemem/bin/python integration/vm_ko_raw_memory.py            # 새 room
  .venv-voicemem/bin/python integration/vm_ko_raw_memory.py --room .vm-ko-raw --reuse
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_ROOM = os.path.join(ROOT, ".vm-ko-raw")

FACTS = [
    "나는 채식주의자이고 견과류에 알레르기가 있어요.",
    "내 이름은 김민수이고 서울에 살아요.",
    "회사는 판교에 있고, 아침 9시에 출근해요.",
]
# 질의 → 기대 키워드(1위 결과에 이 중 하나가 있어야 정답)
QUERIES = [
    ("식이 제한", "내 식이 제한이 뭐야?", ["채식", "견과류", "알레르기"]),
    ("거주지/이름", "내가 사는 곳이 어디라고 했지?", ["서울"]),
    ("직장", "출근 시간이 몇 시였지?", ["판교", "9시", "아침"]),
    ("이름", "내 이름이 뭐라고 했지?", ["김민수"]),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--room", default=DEFAULT_ROOM)
    ap.add_argument("--reuse", action="store_true", help="기존 room 유지(이미 저장했으면 검색만)")
    ap.add_argument("--top-k", type=int, default=5)
    args = ap.parse_args()

    room = args.room if os.path.isabs(args.room) else os.path.join(ROOT, args.room)
    if os.path.isdir(room) and not args.reuse:
        prev = f"{room}.prev-{time.strftime('%H%M%S')}"
        shutil.move(room, prev)          # 이동 금지 원칙은 qdrant room 에만 해당(복사본 목적)
        print(f"[room] 이전 room 이동(보관): {os.path.basename(prev)}")
    os.makedirs(room, exist_ok=True)

    sys.path.insert(0, os.path.join(ROOT, "integration"))
    # OPENAI_* 표준화 — model 명시 필수 (404 함정 재발 방지).
    from vm_ko_common import setup_openai_env
    setup_openai_env(model="gemma3:4b")
    import torch_compat  # noqa: F401  (torch 2.2.2 shim)
    import voicemem_local_embed_shim
    import vm_ko_search_patch
    print(f"[shim] local-embed = {voicemem_local_embed_shim.PATCHED}")
    print(f"[patch] search = {sorted(k for k in vm_ko_search_patch.PATCHED if k.startswith('_'))}")

    from voicemem import VoiceMem

    t0 = time.time()
    vm = VoiceMem(mode="leftbrain_only", memory_root=room, top_k=args.top_k,
                  base_url=os.environ["OPENAI_BASE_URL"])
    repo = vm._o._get_repo()
    print(f"[init] VoiceMem {time.time() - t0:.1f}s  room={os.path.basename(room)}")

    report: dict = {"room": os.path.basename(room), "facts_input": FACTS,
                    "ingest": [], "search": [], "stored": []}

    print("\n=== 원문 저장(LLM 미사용) ===")
    if args.reuse and repo.vector_store.list_entries(user_id="voice_user"):
        print("  이미 저장된 기억이 있어 저장 단계를 건너뜁니다")
    else:
        from voicemem.leftbrain.extract_facts_openai import ExtractedAdditiveMemory
        for i, text in enumerate(FACTS):
            t = time.time()
            ids = repo.append_extracted(
                [ExtractedAdditiveMemory(local_id=str(i), text=text, attributed_to="user")],
                user_id="voice_user",
                extra_metadata={"source": "raw_korean_probe"},
            )
            rec = {"text": text, "seconds": round(time.time() - t, 2), "memory_ids": ids,
                   "ok": bool(ids)}
            report["ingest"].append(rec)
            print(f"  {rec['seconds']:5.2f}s  ids={ids}  {text}")

    entries = repo.vector_store.list_entries(user_id="voice_user")
    report["stored"] = [e.get("text") for e in entries]
    print(f"\n저장된 기억 {len(entries)}건")
    for t in report["stored"]:
        print(f"  - {t}")

    print("\n=== 한국어 질의 ===")
    for label, q, needles in QUERIES:
        t = time.time()
        hits = repo.search(q, user_id="voice_user", top_k=args.top_k)
        ms = round((time.time() - t) * 1000)
        rows = [{"text": h.text, "score": round(h.score, 4),
                 "base": round(getattr(h, "base_score", 0.0), 4)} for h in hits]
        top_ok = bool(rows) and any(n in rows[0]["text"] for n in needles)
        report["search"].append({"label": label, "query": q, "ms": ms, "hits": rows,
                                 "top1_ok": top_ok})
        print(f"  [{label}] {ms:5d}ms top1_ok={top_ok}  {q}")
        for r in rows[:3]:
            mark = "✓" if any(n in r["text"] for n in needles) else " "
            print(f"    {mark} {r['score']:.3f} (base {r['base']:.3f})  {r['text']}")

    ok = sum(1 for r in report["search"] if r["top1_ok"])
    s = [r for r in report["search"] if r["ms"] > 1000]
    print(f"\n요약: top1 정답 {ok}/{len(QUERIES)}  (로그를 키운 검색 {len(s)}건)")
    report["summary"] = {"stored": len(entries), "top1_ok": ok, "queries": len(QUERIES)}

    out = os.path.join(ROOT, "integration", "vm_ko_raw_memory.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"보고서: {out}")
    return 0 if ok == len(QUERIES) else 1


if __name__ == "__main__":
    raise SystemExit(main())
