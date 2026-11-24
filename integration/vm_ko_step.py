#!/usr/bin/env python3
"""한국어 기억 end-to-end, **단계별로 끊어서** 재측정 (2·3건차가 끝나지 않는 문제 대응).

왜 새 파일인가
  `vm_ko_check.py` 는 세 문장을 한 실행에서 연속 ingest 한다. 실측(2026-11-24 11:03):
    1문장 244s(성공) → 2문장 601s(성공) → 3문장 822s **실패**
    (`OperationalError: no such table: session_state`)
  한 프로세스에 수십 분씩 몰아넣으면 **어느 문장이 느리고 어느 문장이 깨지는지**가
  한 덩어리로 뭉개지고, 앞 단계의 지연이 뒤 단계의 실패 원인처럼 보인다.
  → 이 스크립트는 문장 단위(또는 ingest 전체를 건너뛰기)로 실행을 쪼갤 수 있게 하고,
    각 단계 결과를 **즉시 JSON 에 덧붙인다**(중간에 죽어도 앞 결과는 남는다).

단계
  --step ingest   : 지정 문장만 ingest (기본: 아직 저장 안 된 문장)
  --step search   : 저장된 기억으로 한국어 질의 3건 검색 (LLM 불필요)
  --step all      : ingest(미저장 문장) → search

실행
  # 1) 저장 현황만 보기
  .venv-voicemem/bin/python integration/vm_ko_step.py --step status
  # 2) 아직 없는 문장 하나만 (디폴트 room 재사용, 초기화 안 함)
  .venv-voicemem/bin/python integration/vm_ko_step.py --step ingest --index 2
  # 3) 검색 3건
  .venv-voicemem/bin/python integration/vm_ko_step.py --step search
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOM = os.path.join(ROOT, ".vm-ko-test")
STATE = os.path.join(ROOT, "integration", "vm_ko_step_state.json")

FACTS = [
    "나는 채식주의자이고 견과류에 알레르기가 있어요.",
    "내 이름은 김민수이고 서울에 살아요.",
    "회사는 판교에 있고, 아침 9시에 출근해요.",
]
QUERIES = [
    ("식이 제한", "내 식이 제한이 뭐야?"),
    ("거주지/이름", "내가 사는 곳이 어디라고 했지?"),
    ("직장", "출근 시간이 몇 시였지?"),
]
# 이번 측정의 목적: 한국어 질의 3건이 **각자의 사실**을 찾는지.
# 올바른 순위 1위 문장(부분 문자열로 판정).
EXPECT = [
    ("식이 제한", "채식"),
    ("거주지/이름", "서울"),
    ("직장", "판교"),
]


def _db() -> str:
    cands = [p for p in os.listdir(ROOM) if p.endswith(".sqlite")]
    return os.path.join(ROOM, cands[0]) if cands else os.path.join(ROOM, ".vm-ko-test.sqlite")


def status() -> dict:
    db = _db()
    out = {"room": os.path.basename(ROOM), "db": os.path.basename(db), "exists": os.path.exists(db)}
    if not out["exists"]:
        return out
    with sqlite3.connect(db) as c:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        out["tables"] = len(tables)
        out["missing_core_tables"] = sorted({"memories", "session_state", "touched_refs"} - tables)
        if "memories" in tables:
            # 실제 스키마(확인): memories(id, user_id, slot, memory_type, content, …)
            # — 컬럼명은 `memory` 가 아니라 `content` 다.
            rows = c.execute("SELECT id, content FROM memories ORDER BY rowid").fetchall()
            out["memories"] = [{"id": r[0][:8], "text": r[1]} for r in rows]
    out["saved_indices"] = [i for i, f in enumerate(FACTS)
                            if any(f[:12] in (m.get("text") or "")
                                   for m in out.get("memories", []))]
    return out


def _load_state() -> dict:
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {"ingest": [], "search": []}


def _save_state(st: dict) -> None:
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


def do_ingest(index: int | None) -> int:
    sys.path.insert(0, os.path.join(ROOT, "integration"))
    # OPENAI_* 표준화 (404 함정 재발 방지 — setup_openai_env 는 default 박지 않음).
    from vm_ko_common import setup_openai_env
    setup_openai_env(model="gemma3:4b")
    import torch_compat  # noqa: F401
    import voicemem_local_embed_shim  # noqa: F401
    import vm_ko_extractor_patch
    import vm_ko_search_patch  # noqa: F401
    print(f"[patch] extractor = {vm_ko_extractor_patch.PATCHED}", flush=True)

    st = status()
    todo = ([index] if index is not None
            else [i for i in range(len(FACTS)) if i not in st.get("saved_indices", [])])
    if not todo:
        print(f"[ingest] 이미 전부 저장됨: {st.get('memories')}")
        return 0

    from voicemem import VoiceMem
    vm = VoiceMem(mode="leftbrain_only", memory_root=ROOM, top_k=5,
                  base_url=os.environ["OPENAI_BASE_URL"])
    state = _load_state()
    fail = 0
    for i in todo:
        text = FACTS[i]
        t = time.time()
        rec = {"index": i, "text": text, "at": time.strftime("%H:%M:%S")}
        try:
            res = vm.ingest(text, async_facts=False)
            rec.update(seconds=round(time.time() - t, 2),
                       facts_count=res.get("facts_count"),
                       memory_ids=res.get("memory_ids"), ok=bool(res.get("memory_ids")))
            print(f"[ingest {i}] {rec['seconds']}s facts={rec['facts_count']} "
                  f"ids={rec['memory_ids']}", flush=True)
        except Exception as exc:  # noqa: BLE001
            rec.update(seconds=round(time.time() - t, 2), ok=False,
                       error=f"{type(exc).__name__}: {exc}")
            print(f"[ingest {i}] {rec['seconds']}s FAILED {rec['error']}", flush=True)
            fail += 1
        state["ingest"].append(rec)
        _save_state(state)
    return fail


def do_search() -> int:
    sys.path.insert(0, os.path.join(ROOT, "integration"))
    # OPENAI_* 표준화 — 기본값 박지 않음. --model 또는 환경 OPENAI_MODEL 사용.
    from vm_ko_common import setup_openai_env
    setup_openai_env()
    import torch_compat  # noqa: F401
    import voicemem_local_embed_shim  # noqa: F401
    import vm_ko_search_patch
    print(f"[patch] search = {sorted(k for k in vm_ko_search_patch.PATCHED if k.startswith('_'))}",
          flush=True)

    from voicemem import VoiceMem
    vm = VoiceMem(mode="leftbrain_only", memory_root=ROOM, top_k=5,
                  base_url=os.environ["OPENAI_BASE_URL"])
    state = _load_state()
    state["search"] = []
    hit = 0
    for (label, q), (_, needle) in zip(QUERIES, EXPECT):
        t = time.time()
        rec = {"label": label, "query": q, "expect_contains": needle}
        try:
            r = vm.search(q)
            left = getattr(r, "result_leftbrain", None)
            rec["ms"] = round((time.time() - t) * 1000)
            rec["leftbrain"] = str(left)
            rec["top1_ok"] = needle in str(left)[:400]
            hit += int(rec["top1_ok"])
            print(f"[search] {rec['ms']}ms {label}: top1_ok={rec['top1_ok']}", flush=True)
            print(f"    {str(left)[:220]}", flush=True)
        except Exception as exc:  # noqa: BLE001
            rec.update(ms=round((time.time() - t) * 1000), error=f"{type(exc).__name__}: {exc}")
            print(f"[search] {label} FAILED {rec['error']}", flush=True)
        state["search"].append(rec)
        _save_state(state)
    print(f"[search] top1 정답 {hit}/{len(QUERIES)}")
    return 0 if hit else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", choices=["ingest", "search", "all", "status"], default="status")
    ap.add_argument("--index", type=int, default=None, help="ingest 할 문장 번호(0~2)")
    args = ap.parse_args()

    st = status()
    print(f"[status] {json.dumps(st, ensure_ascii=False)}", flush=True)
    if st.get("missing_core_tables"):
        print(f"[status] 경고: 필수 테이블 누락 {st['missing_core_tables']} "
              f"(sqlite 와 vectors/ 가 어긋난 room) → 새 room 이 필요할 수 있다")

    if args.step == "status":
        return 0
    rc = 0
    if args.step in ("ingest", "all"):
        rc |= do_ingest(args.index)
    if args.step in ("search", "all"):
        rc |= do_search()
    print(f"[state] {STATE}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
