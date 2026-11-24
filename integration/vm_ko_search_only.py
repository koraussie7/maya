#!/usr/bin/env python3
"""검색만 따로 측정 — `vm_ko_check.py` 가 ingest 단계에서 죽어도 쓸 수 있는 복구 경로.

왜 따로 있나
  2·3건차 end-to-end 측정(2026-11-24 11:03)은 ingest 2건 저장까지 성공했지만,
  3번째 ingest 에서 `no such table: session_state`, 이어서 search 3건이 전부
  `no such table: touched_refs` 로 실패했다. 즉 그 실행의 **검색 수치는 없다**.

  원인 조사 결과(코드 확인):
    - `space.db()` 는 `<dir>/<dir명>.sqlite` 를 고르고, 없으면 `*.<suffix>` 첫 파일을 쓴다.
    - 이 저장소에는 이전 실행들이 남긴 라벨 디렉터리(`.vm-ko-test.prev-HHMMSS`)가
      여럿 있고, 그 안에 **`vectors/` 가 함께 들어 있다**.
    - qdrant 는 **relatively-addressed** 저장소다. `vectors/` 를 다른 경로로 옮기면
      payload sid/segment 참조가 깨져 **좋은 sqlite 를 지워도 사라진다**.
      실제로 그렇게 옮긴 room 들은 `.tables` 가 `kv`/`session_state` 하나도 없는 빈
      sqlite 가 됐다(이번 확인).
  → 그래서 이 스크립트는 (1) 백업을 **복사**하고(이동 금지), (2) qdrant 를 열기 전에
    무결성을 점검하고, (3) sqlite 스키마가 깨져 있으면 메타 기록이 필요 없는
    **직접 벡터 store 검색**으로 우회한다.

실행
  .venv-voicemem/bin/python integration/vm_ko_search_only.py            # 기본: 기존 room
  .venv-voicemem/bin/python integration/vm_ko_search_only.py --room .vm-ko-test.prev-110617
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUERIES = [
    ("식이 제한", "내 식이 제한이 뭐야?"),
    ("거주지/이름", "내가 사는 곳이 어디라고 했지?"),
    ("직장", "출근 시간이 몇 시였지?"),
]


def find_room(explicit: str | None) -> str:
    """저장된 기억이 **실제로 남아 있는** room 을 고른다(빈 sqlite 방지)."""
    if explicit:
        return explicit if os.path.isabs(explicit) else os.path.join(ROOT, explicit)
    cands = [os.path.join(ROOT, ".vm-ko-test")]
    cands += sorted((os.path.join(ROOT, d) for d in os.listdir(ROOT)
                     if d.startswith(".vm-ko-test.prev-")), reverse=True)
    for c in cands:
        db = os.path.join(c, os.path.basename(c) + ".sqlite")
        if not os.path.exists(db):
            continue
        try:
            with sqlite3.connect(db) as conn:
                tables = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                n = 0
                if "memories" in tables:
                    n = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            if n:
                print(f"[room] {c} (memories={n}, tables={len(tables)})")
                return c
        except Exception:  # noqa: BLE001
            continue
    raise SystemExit("저장된 기억이 있는 room 을 찾지 못했습니다 — ingest 부터 다시 필요")


def check_store(room: str) -> dict:
    """qdrant 디렉터리 상태. 열기 전에 보는 이유: 잘못 열면 그 자체로 손상된다."""
    qdir = os.path.join(room, "vectors")
    info = {"path": qdir, "exists": os.path.isdir(qdir)}
    try:
        import lmdb  # qdrant 로컬의 실제 저장 엔진
        env = lmdb.open(os.path.join(qdir, "meta"), readonly=True, lock=False)
        with env.begin() as txn:
            key = b"main" if txn.get(b"main") else (txn.cursor().first()[0] if txn.stat()["entries"] else None)
            raw = txn.get(key) if key else None
        env.close()
        info["meta_readable"] = raw is not None
        info["meta_bytes"] = len(raw) if raw else 0
    except Exception as exc:  # noqa: BLE001
        info["meta_readable"] = False
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info


def search_direct(room: str, queries) -> list[dict]:
    """sqlite 를 거치지 않는 순수 벡터 검색.

    `Mem0BackendStore.search()` 는 meta/`touched_refs` 기록을 읽고 쓴다. 그게 깨진
    room 에서는 store 를 직접 만들어 `search()` 만 호출하고, 실패하면 mem0 컬렉션을
    직접 질의한다(둘 다 셰임 덕분에 **로컬 E5** 로 임베딩된다).
    """
    sys.path.insert(0, os.path.join(ROOT, "integration"))
    # OPENAI_* 표준화 — BASE_URL/API_KEY 더미는 필요, MODEL 은 *기본값 박지 않음*.
    # (검색 자체는 임베딩으로만 동작하지만 CognitiveAnnotator 가 모델명을 읽으므로
    #  환경에 이미 설정된 OPENAI_MODEL 을 그대로 보존한다.)
    from vm_ko_common import setup_openai_env
    setup_openai_env()  # model 인자 없음 → 환경 OPENAI_MODEL 없으면 fail-fast.
    import torch_compat  # noqa: F401
    import voicemem_local_embed_shim
    import vm_ko_search_patch
    print(f"[shim] local-embed = {voicemem_local_embed_shim.PATCHED}")
    print(f"[patch] search = {sorted(k for k in vm_ko_search_patch.PATCHED if k.startswith('_'))}")

    from voicemem.leftbrain.local_e5_embedder import LocalE5Embedder
    from voicemem.leftbrain.mem0_backend_store import Mem0BackendStore

    emb = LocalE5Embedder()
    store = Mem0BackendStore(emb, memory_root=room)
    out = []
    for label, q in queries:
        t = time.time()
        try:
            hits = store.search(q, user_id="voice_user", top_k=5)
            rows = [{"text": h.text, "score": round(h.score, 4),
                     "base": round(getattr(h, "base_score", 0.0), 4),
                     "time_boost": getattr(h, "time_boost", False)} for h in hits]
            out.append({"label": label, "query": q, "ms": round((time.time() - t) * 1000),
                        "hits": rows, "path": "store.search"})
        except Exception as exc:  # noqa: BLE001
            out.append({"label": label, "query": q, "error": f"{type(exc).__name__}: {exc}",
                        "path": "store.search"})
        print(f"  [{label}] {out[-1].get('ms', '?')}ms  {q}")
        for r in out[-1].get("hits", []):
            print(f"     {r['score']:.3f} (base {r['base']:.3f}{', t+' if r['time_boost'] else ''})  {r['text']}")
        if out[-1].get("error"):
            print(f"     ERROR {out[-1]['error']}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--room", default=None)
    args = ap.parse_args()

    room = find_room(args.room)
    store_info = check_store(room)
    print(f"[store] {store_info}")

    # 백업은 **복사**한다. `vectors/` 를 옮기면 qdrant 상대경로가 깨진다(위 docstring).
    bak = room.rstrip("/") + f".bak-{time.strftime('%H%M%S')}"
    try:
        shutil.copytree(room, bak)
        print(f"[backup] {bak} (복사 완료)")
    except Exception as exc:  # noqa: BLE001
        print(f"[backup] 실패(무시하고 계속): {exc}")

    results = search_direct(room, QUERIES)
    report = {"room": os.path.basename(room), "store": store_info, "search": results}
    out = os.path.join(ROOT, "integration", "vm_ko_search_only.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n보고서: {out}")
    ok = sum(1 for r in results if r.get("hits"))
    print(f"요약: 검색 {ok}/{len(results)} 건 결과 있음")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
