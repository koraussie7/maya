#!/usr/bin/env python3
"""VoiceMem 한국어 기억 검증 (텍스트 모드 = leftbrain_only, ASR 미사용).

목적
  1) 한국어 문장을 ingest 했을 때 사실이 제대로 추출되는가
  2) 저장된 기억이 **한국어 원문**으로 유지되는가 (영어로 번역/변환되지 않는가)
  3) 한국어 질의로 search 했을 때 관련 기억을 찾아오는가

왜 leftbrain_only 인가
  기본 모드의 ASR 은 paraformer-zh-streaming 이고 최종 전사가 language="zh"
  하드코딩이라 한국어 음성을 못 쓴다(코드 확인). 텍스트 경로만 떼어 평가하고,
  LLM 호출 비용을 줄이기 위해 좌뇌(사실 기억)만 켠다.

이전 실패 2건 (2026-11-24 08:45 실행) 과 원인
  - `attempt to write a readonly database` / `no such table: session_state`
      room 을 shutil.rmtree 로 지운 **뒤에** VoiceMem 을 만들지 않고, qdrant 가
      디렉터리 핸들을 잡고 있는 상태에서 지웠다. 지워진 inode 에 qdrant 가 계속
      쓰고, SessionTracker(sqlite)는 새 파일을 만들지만 잡고 있던 옛 연결이
      깨진 상태로 남아 두 번째 ingest 부터 실패한다. → 이제 **room 을 새로 만들고,
      import/생성 전에 환경변수로 고정**한다. 이전 room 은 옮겨 둔다(삭제 X).
  - `facts_count: 0` (실패는 아니지만 기억이 안 쌓임)
      모델(qwen2.5-coder:1.5b)이 한국어 사실을 안 뽑았는지, 뽑았는데 저장 단계에서
      사라졌는지가 한 덩어리로 보였다. → 이제 ingest 마다 추출 개수/저장 id 를
      따로 찍고, 0 개면 실패로 집계한다. 추출 자체는 vm_probe_extract.py 로 먼저 본다.

실행
  .venv-voicemem/bin/python integration/vm_ko_check.py            # 새 room 으로
  .venv-voicemem/bin/python integration/vm_ko_check.py --reuse    # 기존 room 유지
  OPENAI_MODEL=qwen3:1.7b .venv-voicemem/bin/python integration/vm_ko_check.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEM_ROOT = os.path.join(ROOT, ".vm-ko-test")

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


def _prepare_room(reuse: bool) -> str:
    """진짜 빈 room 을 준비한다.

    qdrant 로컬 모드는 디렉터리 핸들을 잡고 있으므로, 이 프로세스에서 qdrant 를 한 번도
    만들기 **전**에 파일 시스템을 정리한다. 기존 room 은 지우지 않고
    `.vm-ko-test.prev-<ts>` 로 옮긴다 — 실패를 재현하다가 증거를 날리지 않기 위해서.
    """
    if reuse and os.path.isdir(MEM_ROOT):
        return MEM_ROOT
    if os.path.isdir(MEM_ROOT):
        prev = f"{MEM_ROOT}.prev-{time.strftime('%H%M%S')}"
        shutil.move(MEM_ROOT, prev)
        print(f"[room] 이전 room 이동: {prev}")
    os.makedirs(MEM_ROOT, exist_ok=True)
    return MEM_ROOT


def _env_on(name: str) -> bool:
    return os.environ.get(name, "0") == "1"


def main() -> int:
    reuse = "--reuse" in sys.argv[1:]
    root = _prepare_room(reuse)

    # transformers 4.52.3 은 torch>=2.3 API 를 부른다. Intel Mac 의 torch 2.2.2 에서는
    # shim 이 필요하다(sentence-transformers 로딩 전에 실행돼야 함).
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import torch_compat  # noqa: F401  (import 부수효과로 shim 적용)
    # 기본 임베딩(OpenAILocalEmbedder)을 진짜 로컬 E5 로 교체 — API 키/과금 없이 평가.
    import voicemem_local_embed_shim
    print(f"[shim] local-embed patch = {voicemem_local_embed_shim.PATCHED}")
    # 한국어: (1) 추출 프롬프트의 "영어로 번역" 지시 제거 + 호출 상한
    #        (2) 검색 재정렬의 영어 전용 시간/어휘 정규식에 한국어 추가
    # 주의: 패치가 base_url 을 읽으므로 **엔드포인트 환경변수 설정 후** import 한다.
    #       (위에서 OpenAILocalEmbedder 도 import 시점에 env 를 읽는 같은 함정이 있다)
    os.environ.setdefault("OPENAI_API_KEY", "ollama")
    os.environ.setdefault("OPENAI_BASE_URL", "http://127.0.0.1:11434/v1")
    import vm_ko_extractor_patch
    import vm_ko_search_patch
    print(f"[patch] extractor = {vm_ko_extractor_patch.PATCHED}")
    print(f"[patch] search    = {sorted(k for k in vm_ko_search_patch.PATCHED if k.startswith('_'))}")

    t0 = time.time()
    from voicemem import VoiceMem

    # 사실 추출(쓰기)은 설계상 LLM 이 필수다. 외부 API 대신 로컬 Ollama 의
    # OpenAI 호환 엔드포인트를 쓴다(키는 더미).
    vmlm = os.environ["OPENAI_BASE_URL"]
    os.environ.setdefault("OPENAI_API_KEY", "ollama")
    # 기본 qwen3:1.7b 는 사고(thinking) 모델이라 같은 추출 프롬프트에 108~182초가 걸리고
    # 절단 모드에서도 JSON 을 못 닫았다(실측). 비사고 모델을 기본으로 쓴다.
    os.environ.setdefault("OPENAI_MODEL", "gemma3:4b")
    vm = VoiceMem(mode="leftbrain_only", memory_root=root, top_k=5, base_url=vmlm)
    # stdout 은 프로세스 종료 때 flush 되거나 버퍼에서 잘릴 수 있다(백그라운드 실행 시
    # 5분 넘게 아무 출력도 없었다. 진행을 지켜보려면 줄 단위 flush 가 필수.
    print(f"[init] VoiceMem 준비 {time.time() - t0:.1f}s (llm={vmlm}, "
          f"model={os.environ['OPENAI_MODEL']})", flush=True)

    report: dict = {"memory_root": root, "model": os.environ["OPENAI_MODEL"],
                    "concurrency": {"async_facts": False,
                                    "always_add": _env_on("VOICEMEM_ALWAYS_ADD")},
                    "facts_input": FACTS, "ingest": [], "search": [],
                    "raw_memory": [], "db_integrity": None}

    print("\n=== [3단계] 한국어 ingest ===")
    for text in FACTS:
        t = time.time()
        try:
            # async_facts=False: 사실 추출/충돌 판정은 LLM 호출이라 수십 초가 걸리고,
            # 백그라운드로 넘기면 이 스크립트가 **결과를 보기 전에** 다음 문장을 돌려
            # 2번째 ingest 부터 앞선 저장과 겹친다(측정 스크립트가 경합을 만든다).
            res = vm.ingest(text, async_facts=False)
            dt = time.time() - t
            n = res.get("facts_count") if isinstance(res, dict) else None
            ids = res.get("memory_ids") if isinstance(res, dict) else None
            ok = bool(ids) or bool(n)
            print(f"  {dt:6.2f}s  facts={n}  ids={ids}"
                  + ("" if ok else "   ← 추출/저장 0건"))
            print(f"         원문: {text}")
            report["ingest"].append({"text": text, "seconds": round(dt, 2),
                                     "facts_count": n, "memory_ids": ids, "ok": ok})
        except Exception as exc:  # noqa: BLE001
            dt = time.time() - t
            print(f"  {dt:6.2f}s  실패: {type(exc).__name__}: {exc}")
            report["ingest"].append({"text": text, "seconds": round(dt, 2),
                                     "error": f"{type(exc).__name__}: {exc}", "ok": False})

    print("\n=== [4단계] 한국어 search ===")
    for label, q in QUERIES:
        t = time.time()
        try:
            r = vm.search(q)
            dt = (time.time() - t) * 1000
            left = getattr(r, "result_leftbrain", None)
            right = getattr(r, "result_rightbrain", None)
            print(f"  [{label}] {dt:7.0f}ms  질의: {q}")
            print(f"    좌뇌: {str(left)[:300]}")
            if right:
                print(f"    우뇌: {str(right)[:200]}")
            report["search"].append({"label": label, "query": q, "ms": round(dt),
                                     "left": str(left), "right": str(right),
                                     "ok": bool(left)})
        except Exception as exc:  # noqa: BLE001
            print(f"  [{label}] 실패: {type(exc).__name__}: {exc}")
            report["search"].append({"label": label, "query": q,
                                     "error": f"{type(exc).__name__}: {exc}", "ok": False})

    # 저장된 기억 원문을 직접 확인 (한국어 유지 여부 판정용)
    #
    # 이전 판은 vm._o.utils.get("memory_engine") 을 먼저 봤는데, 그 컨테이너에는
    # 그 키가 없어서 늘 `(저장소 조회 API 를 찾지 못함)` 으로 끝났다. 실제 저장된
    # 사실은 repo 의 vector_store(mem0) → list_entries() 에 있다.
    print("\n=== 저장된 기억 원문 ===")
    try:
        repo = vm._o._get_repo()
        entries = repo.vector_store.list_entries(user_id="voice_user")
        for e in entries[:20]:
            text = e.get("text") or ""
            print(f"  - [{e.get('id', '')[:8]}] {text}")
            report["raw_memory"].append(text)

        # 한국어 원문이 유지됐는지: 정규화(공백/문장부호 제거) 후 원문이 기억 안에
        # 그대로 남아 있는지 본다. 번역되면 "견과류/서울/판교" 같은 고유값이 사라져
        # 한국어 질의와의 임베딩 거리가 벌어진다(검색이 조용히 무너지는 경로).
        import re as _re

        def _norm(s: str) -> str:
            return _re.sub(r"[\s.,!?~·'\"()]", "", s or "")

        for src in FACTS:
            n = _norm(src)
            report.setdefault("original_kept", {})[src] = any(
                n and n in _norm(t) for t in report["raw_memory"])
        kept = sum(1 for v in report.get("original_kept", {}).values() if v)
        print(f"  원문 유지: {kept}/{len(FACTS)}")
    except Exception as exc:  # noqa: BLE001
        print(f"  저장소 조회 실패: {type(exc).__name__}: {exc}")
        report["raw_memory_error"] = f"{type(exc).__name__}: {exc}"

    # DB 무결성: 이전 실행의 `readonly database` / `no such table` 재발 여부
    try:
        db = f"{root}/{os.path.basename(root)}.sqlite"
        alt = [p for p in os.listdir(root) if p.endswith(".sqlite")]
        db = os.path.join(root, alt[0]) if alt else db
        import sqlite3
        with sqlite3.connect(db) as c:
            tables = {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            c.execute("INSERT INTO session_state (user_id, last_session_id, turn_count, updated_at)"
                      " VALUES ('probe','probe',0,datetime('now'))"
                      " ON CONFLICT(user_id) DO UPDATE SET updated_at=datetime('now')")
            report["db_integrity"] = {"db": os.path.basename(db),
                                      "tables": sorted(tables),
                                      "session_state_ok": "session_state" in tables}
        print(f"\n[db] {os.path.basename(db)}  session_state={report['db_integrity']['session_state_ok']}"
              f"  tables={len(report['db_integrity']['tables'])}")
    except Exception as exc:  # noqa: BLE001
        print(f"\n[db] 무결성 확인 실패: {type(exc).__name__}: {exc}")
        report["db_integrity"] = {"error": f"{type(exc).__name__}: {exc}"}

    out = os.path.join(ROOT, "integration", "vm_ko_report.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n보고서: {out}")
    ok_ingest = sum(1 for r in report["ingest"] if r.get("ok"))
    ok_search = sum(1 for r in report["search"] if r.get("ok"))
    ok_keep = sum(1 for v in report.get("original_kept", {}).values() if v)
    db_ok = (report["db_integrity"] or {}).get("session_state_ok")
    print(f"요약: ingest {ok_ingest}/{len(FACTS)} 성공(사실 1건 이상), "
          f"search {ok_search}/{len(QUERIES)} 성공(좌뇌 결과 있음), "
          f"원문 유지 {ok_keep}/{len(FACTS)}, db={'OK' if db_ok else 'FAIL'}")
    return 0 if (ok_ingest == len(FACTS) and ok_search == len(QUERIES) and db_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
