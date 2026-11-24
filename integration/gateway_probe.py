#!/usr/bin/env python3
"""자체 게이트웨이(privseai.com/privseai/llm) 상태 진단.

왜 필요한가 (2026-11-24 실사)
  curl/httpx 로는 `auto/best-fast` 가 200 + 응답을 줬지만, **Python urllib 로는 403**,
  그리고 재시도 시 400 `Model is unavailable`. 즉 "게이트웨이가 죽었다"가 아니라
  **요청마다 결과가 갈린다**(무료 제공자 라우팅 + 봇 차단 규칙 의심).
  대화 경로에 넣기 전에 "지금 되는가"를 판정할 수 있어야 한다.

측정
  · N회 반복: HTTP 코드 / 지연 / 응답 첫 문장 / 한국어 여부
  · User-Agent 별 비교(urllib vs MayaLive) — 봇 차단 여부 판별
  · 성공률이 낮으면 요약에서 명확히 알려준다

실행
  .venv-voicemem/bin/python integration/gateway_probe.py
  .venv-voicemem/bin/python integration/gateway_probe.py --runs 5 --model auto/best-fast
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import time
import urllib.error
import urllib.request

KO = re.compile(r"[\uac00-\ud7a3]")
CJK_OTHER = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff]")


def one(base: str, model: str, text: str, ua: str, timeout: float) -> dict:
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": text}],
        "stream": False,
        "max_tokens": 120,
    }).encode()
    req = urllib.request.Request(
        f"{base.rstrip('/')}/chat/completions", data=body,
        headers={"Content-Type": "application/json", "User-Agent": ua},
    )
    t = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
        dt = time.monotonic() - t
        reply = (data["choices"][0]["message"]["content"] or "").strip()
        ko = len(KO.findall(reply))
        other = len(CJK_OTHER.findall(reply))
        return {"code": 200, "s": round(dt, 2), "reply": reply[:80],
                "korean": (ko / (ko + other) >= 0.9) if (ko + other) else False}
    except urllib.error.HTTPError as exc:
        return {"code": exc.code, "s": round(time.monotonic() - t, 2),
                "body": exc.read()[:160].decode("utf-8", "replace")}
    except Exception as exc:  # noqa: BLE001
        return {"code": 0, "s": round(time.monotonic() - t, 2),
                "body": f"{type(exc).__name__}: {str(exc)[:100]}"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://privseai.com/privseai/llm/v1")
    ap.add_argument("--model", default="auto/best-fast")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--timeout", type=float, default=45)
    ap.add_argument("--text", default="한국어로 한 문장: 오늘 기분 어때?")
    args = ap.parse_args()

    report = {"base": args.base, "model": args.model, "by_ua": {}}
    for ua in ("python-urllib/3.11", "MayaLive/0.1 (+https://privseai.com/maya)"):
        rows = []
        print(f"\n=== UA: {ua} ===")
        for i in range(args.runs):
            r = one(args.base, args.model, args.text, ua, args.timeout)
            rows.append(r)
            if r["code"] == 200:
                print(f"  {i+1}/{args.runs} 200 {r['s']}s ko={r['korean']} | {r['reply'][:60]}")
            else:
                print(f"  {i+1}/{args.runs} {r['code']} {r['s']}s | {r.get('body', '')[:90]}")
        ok = [r for r in rows if r["code"] == 200]
        report["by_ua"][ua] = {
            "rows": rows,
            "success": f"{len(ok)}/{len(rows)}",
            "median_s": round(statistics.median([r["s"] for r in ok]), 2) if ok else None,
        }

    print("\n요약")
    for ua, v in report["by_ua"].items():
        print(f"  {ua:48s} 성공 {v['success']}  중앙지연 {v['median_s']}s")
    out = __file__.replace(".py", ".json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"보고서: {out}")

    best = max(report["by_ua"].values(),
               key=lambda v: int(v["success"].split("/")[0]))
    if int(best["success"].split("/")[0]) * 2 < args.runs * len(report["by_ua"]):
        print("\n판정: 성공률 절반 미만 → 대화 경로에 넣지 말 것 "
              "(게이트웨이 계정 재인증 또는 로컬/클라우드 대체 필요)")
        return 1
    print("\n판정: 사용 가능 범위 (일부 실패는 재시도로 흡수)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
