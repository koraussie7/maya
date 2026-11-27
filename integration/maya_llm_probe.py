"""사용 가능한 LLM 엔드포인트 / 모델 슬러그 프로빙.

각 후보에 대해:
 1) `/v1/models` GET (또는 동등) — 키 인증 확인 + 지연(latency)
 2) 짧은 completion 1회 — 실제 응답 + 첫 토큰까지의 시간
 3) (선택) 사용 가능 모델 슬러그 목록의 첫 N개

출력은 JSON 한 줄으로 stdout. 그래도 읽기 좋게 stderr에 표도 같이 찍는다.

사용:
    /tmp/maya-py.sh integration/maya_llm_probe.py \\
        --out /tmp/maya-llm-probe.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from typing import Any

import httpx  # type: ignore


# ── 후보 정의 (코드베이스에서 발굴) ─────────────────────────────────────────
# 출처:
#   maya-ui/backend/app/config.py        — llm_provider / OPENAI_BASE 기본값
#   maya-ui/backend/app/llm_gateway.py  — SMART_ROUTER_PATHS / KEIROUTER_ALIASES
#   ~/maya/backend.env                   — OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL
#   docs/DEPLOY_privseai_maya.md        — nginx /llm/ 매핑, ws.privseai.com, ws.privseai.com

def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


CANDIDATES: list[dict[str, Any]] = [
    # ── Tier 1: privseai 내부(가장 빠르고 비용 0, 키 필요) ───────────────
    {
        "tier": 1,
        "name": "keirouter-auto/best-fast",
        "base": _env("PRIVSEAI_BASE") or "https://privseai.com/privseai/llm/v1",
        "model": "auto/best-fast",
        "kind": "openai",
        "needs_key": True,
        "api_key": _env("OPENAI_API_KEY"),
        "extra_headers": {"HTTP-Referer": "https://privseai.com/maya",
                          "X-Title": "Maya Voice Assistant"},
        "note": "자체 keirouter — 무료 제공자 자동 라우팅 (MAYA_VOICE_CONSOLIDATION_PLAN.md)",
    },
    {
        "tier": 1,
        "name": "keirouter-smart/best-fast",
        "base": "https://privseai.com/privseai/llm/v1",
        "model": "smart/best-fast",
        "kind": "openai",
        "needs_key": True,
        "api_key": _env("OPENAI_API_KEY"),
        "note": "smart-router 포크 (자체 정의 별칭)",
    },
    {
        "tier": 1,
        "name": "keirouter-direct-internal",
        "base": "http://127.0.0.1:20128/v1",
        "model": "auto/best-fast",
        "kind": "openai",
        "needs_key": True,
        "api_key": _env("OPENAI_API_KEY"),
        "note": "로컬 keirouter (110 서버에서만 접근 가능 — 여기선 거의 확실히 unreachable)",
    },
    # ── Tier 2: OpenRouter (공개, 키 셋 됨) ───────────────────────────────
    {
        "tier": 2,
        "name": "openrouter-or-default",
        "base": _env("OPENAI_BASE_URL") or "https://openrouter.ai/api/v1",
        "model": _env("OPENAI_MODEL") or "qwen/qwen3-32b",
        "kind": "openai",
        "needs_key": True,
        "api_key": _env("OPENAI_API_KEY"),
        "extra_headers": {"HTTP-Referer": "https://privseai.com/maya",
                          "X-Title": "Maya Voice Assistant"},
        "note": "env 의 OPENAI_BASE_URL/OPENAI_MODEL 그대로",
    },
    {
        "tier": 2,
        "name": "openrouter-gemini-flash",
        "base": "https://openrouter.ai/api/v1",
        "model": "google/gemini-2.5-flash",
        "kind": "openai",
        "needs_key": True,
        "api_key": _env("OPENAI_API_KEY"),
        "note": "ZDR 가드레일에 막힐 수 있음 (이전 측정)",
    },
    {
        "tier": 2,
        "name": "openrouter-llama-3.1-8b-free",
        "base": "https://openrouter.ai/api/v1",
        "model": "meta-llama/llama-3.1-8b-instruct:free",
        "kind": "openai",
        "needs_key": True,
        "api_key": _env("OPENAI_API_KEY"),
        "note": "free 슬러그 — ZDR 회피 가능성",
    },
    # ── Tier 3: 로컬 Ollama (대화 경로 부적합하지만 최후 fallback) ────────
    {
        "tier": 3,
        "name": "ollama-local-gemma3-4b",
        "base": _env("OLLAMA_HOST", "http://127.0.0.1:11434") + "/v1",
        "model": "gemma3:4b",
        "kind": "openai",
        "needs_key": False,
        "api_key": "",
        "note": "config.py 의 OLLAMA_HOST 기본값",
    },
]


PROMPT = "안녕. 한 문장으로 자기소개 해줘."
TIMEOUT = 8.0


async def _probe(client: httpx.AsyncClient, c: dict[str, Any]) -> dict[str, Any]:
    base = c["base"].rstrip("/")
    if not base.endswith("/v1") and "openrouter" not in base and "/v1" not in base.split("://", 1)[-1].split("/", 1)[1]:
        base_for_chat = base + "/v1"
    else:
        base_for_chat = base
    headers = {"Content-Type": "application/json"}
    if c.get("api_key"):
        headers["Authorization"] = f"Bearer {c['api_key']}"
    headers.update(c.get("extra_headers") or {})

    out: dict[str, Any] = {
        "tier": c["tier"],
        "name": c["name"],
        "base": c["base"],
        "model": c["model"],
        "needs_key": c["needs_key"],
        "has_key": bool(c.get("api_key")),
        "reachable": False,
        "models_list_ok": False,
        "completion_ok": False,
        "models_count": 0,
        "first_models": [],
        "latency_models_ms": None,
        "latency_completion_ms": None,
        "first_byte_ms": None,
        "completion_text": None,
        "http_status_models": None,
        "http_status_completion": None,
        "error": None,
    }

    # 1) /v1/models — 키 인증 확인 + latency
    try:
        t0 = time.monotonic()
        resp = await client.get(f"{base_for_chat}/models", headers=headers,
                                timeout=httpx.Timeout(TIMEOUT, connect=4.0))
        out["http_status_models"] = resp.status_code
        out["latency_models_ms"] = round((time.monotonic() - t0) * 1000, 1)
        out["reachable"] = resp.status_code < 500
        if resp.status_code == 200:
            try:
                data = resp.json()
                items = data.get("data") or data.get("models") or []
                out["models_count"] = len(items)
                out["first_models"] = [m.get("id") or m.get("name") for m in items[:8]]
                out["models_list_ok"] = True
            except Exception as exc:  # noqa: BLE001
                out["error"] = f"models parse: {exc}"
        else:
            out["error"] = f"models HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"models: {type(exc).__name__}: {str(exc)[:200]}"
        out["reachable"] = False

    # 2) 짧은 completion — 실제 응답 가능 여부
    if out["reachable"]:
        payload = {
            "model": c["model"],
            "messages": [{"role": "user", "content": PROMPT}],
            "max_tokens": 60,
            "temperature": 0.5,
            "stream": False,
        }
        try:
            t0 = time.monotonic()
            resp = await client.post(f"{base_for_chat}/chat/completions",
                                     headers=headers, json=payload,
                                     timeout=httpx.Timeout(TIMEOUT, connect=4.0))
            out["http_status_completion"] = resp.status_code
            out["latency_completion_ms"] = round((time.monotonic() - t0) * 1000, 1)
            if resp.status_code == 200:
                data = resp.json()
                txt = ""
                try:
                    txt = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
                except Exception:
                    pass
                out["completion_text"] = txt[:200]
                out["completion_ok"] = bool(txt.strip())
            else:
                out["error"] = f"completion HTTP {resp.status_code}: {resp.text[:200]}"
        except Exception as exc:  # noqa: BLE001
            out["error"] = f"completion: {type(exc).__name__}: {str(exc)[:200]}"

    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/maya-llm-probe.json")
    args = ap.parse_args()

    async with httpx.AsyncClient() as client:
        results = []
        for c in CANDIDATES:
            if c["needs_key"] and not c.get("api_key"):
                results.append({**c, "reachable": False,
                                "error": "no api key in env",
                                "http_status_models": None,
                                "http_status_completion": None,
                                "models_list_ok": False, "completion_ok": False,
                                "models_count": 0, "first_models": []})
                continue
            r = await _probe(client, c)
            results.append(r)

    # JSON 저장
    Path = __import__("pathlib").Path
    Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=2),
                              encoding="utf-8")

    # stderr 표
    print("================ LLM ENDPOINT PROBE ================", file=sys.stderr)
    print(f"{'tier':4} {'name':40} {'reach':6} {'models':7} {'comp':5} {'lat_ms':>8} {'text':40}",
          file=sys.stderr)
    print("-" * 120, file=sys.stderr)
    for r in results:
        print(f"{r['tier']:>4} {r['name']:40} "
              f"{('OK' if r['reachable'] else '-'):>6} "
              f"{('Y' if r.get('models_list_ok') else '-'):>3}({r.get('models_count',0):>3}) "
              f"{('Y' if r.get('completion_ok') else '-'):>5} "
              f"{str(r.get('latency_completion_ms') or '-'):>8} "
              f"{(r.get('completion_text') or r.get('error') or '')[:40]!s:40}",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
