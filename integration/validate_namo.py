#!/usr/bin/env python3
"""NAMO 한국어 턴 판정 — 54문장 확장 검증.

REPO_ANALYSIS_14_SUMMARY.md §7 이 지적한 표본 한계(10문장)를 보완한다.
카테고리별로 나눠 오판이 어디서 나는지 본다.

실행:
  cd /Users/brianyeon/CodeGPT/maya
  PYTHONPATH=/Users/brianyeon/CodeGPT/maya-2/integration \
    .venv-stt/bin/python /Users/brianyeon/CodeGPT/maya-2/integration/validate_namo.py
"""
from __future__ import annotations

import logging
import statistics
import sys
import time

from turn_detector import get_detector

# (텍스트, 기대값) — True = 말 끝(END), False = 아직 말하는 중(NOT)
CASES: list[tuple[str, str, bool]] = [
    # ── 완결 문장 (END 기대) ──────────────────────────────
    ("안녕하세요.", "문장", True),
    ("오늘 날씨가 정말 좋네요.", "문장", True),
    ("그거 어떻게 하는지 알려주실 수 있나요?", "문장", True),
    ("감사합니다.", "문장", True),
    ("잘 모르겠어요.", "문장", True),
    ("내일 회의는 몇 시에 시작해요?", "문장", True),
    ("이거 좀 도와주실 수 있나요.", "문장", True),
    ("주문번호가 어떻게 되세요?", "문장", True),
    ("네 알겠습니다.", "문장", True),
    ("그럼 그렇게 하죠.", "문장", True),
    ("지금 바로 출발하겠습니다.", "문장", True),
    ("죄송한데 다시 한 번 말씀해 주세요.", "문장", True),
    ("예약은 이미 완료되었습니다.", "문장", True),
    ("결제 수단은 카드로 할게요.", "문장", True),
    ("그 부분은 제가 확인해 볼게요.", "문장", True),

    # ── 백채널 (NOT 기대) ─────────────────────────────────
    ("네", "백채널", False),
    ("어", "백채널", False),
    ("응", "백채널", False),
    ("아", "백채널", False),
    ("그쵸", "백채널", False),
    ("맞아", "백채널", False),
    ("맞아요", "백채널", False),
    ("그러니까", "백채널", False),
    ("음", "백채널", False),
    ("어어", "백채널", False),
    ("아 그렇구나", "백채널", False),
    ("뭐랄까", "백채널", False),

    # ── 미완결 (NOT 기대) ─────────────────────────────────
    ("그런데 말이죠, 제가 생각하기에", "미완결", False),
    ("음... 잠깐만요...", "미완결", False),
    ("그러니까 그게", "미완결", False),
    ("그리고 또 하나 있는데", "미완결", False),
    ("제가 말씀드리고 싶은 건", "미완결", False),
    ("일단 그건 그렇고", "미완결", False),
    ("혹시 그거 있잖아요", "미완결", False),
    ("그래서 제가 생각했을 때는", "미완결", False),
    ("저기 그러니까", "미완결", False),
    ("이게 좀 애매한데", "미완결", False),
    ("우선은 말이죠", "미완결", False),
    ("그건 좀 아닌 것 같고", "미완결", False),
    ("다시 말해서", "미완결", False),
    ("전화드린 이유는", "미완결", False),
    ("그게 아니라", "미완결", False),

    # ── 연결어미로 끝나는 문장 (가장 어려운 케이스) ────────
    ("배송이 늦어서", "연결어미", False),
    ("가격이 좀 비싸서", "연결어미", False),
    ("시간이 없어서", "연결어미", False),
    ("사람이 많아서", "연결어미", False),
    ("문제가 생겼는데", "연결어미", False),
    ("확인은 했는데", "연결어미", False),
    ("갔다 왔는데", "연결어미", False),
    ("생각보다 어려운데", "연결어미", False),
    ("결제하고", "연결어미", False),
    ("예약하고", "연결어미", False),
    ("먹고", "연결어미", False),
    ("그래서", "연결어미", False),
]


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    det = get_detector()
    if not det.available:
        print("NAMO 사용 불가 — 결과 없음")
        return 1

    latencies: list[float] = []
    fails: list[tuple[str, str, bool, bool, float]] = []
    per_cat: dict[str, list[int]] = {}

    for text, cat, expected in CASES:
        t0 = time.perf_counter()
        end, conf = det.predict(text)
        ms = (time.perf_counter() - t0) * 1000
        latencies.append(ms)

        ok = end == expected
        per_cat.setdefault(cat, [0, 0])
        per_cat[cat][0] += 1
        per_cat[cat][1] += int(ok)

        if not ok:
            fails.append((text, cat, expected, end, conf))
        mark = "OK " if ok else "MISS"
        exp = "END" if expected else "NOT"
        got = "END" if end else "NOT"
        print(f"{mark} exp={exp} got={got} {conf:.3f}  [{cat}] {text}")

    total = len(CASES)
    correct = total - len(fails)
    print("\n" + "=" * 58)
    print(f"정확도 {correct}/{total} ({correct/total*100:.1f}%)")
    print("카테고리별:")
    for cat, (n, c) in per_cat.items():
        print(f"  {cat:8s} {c}/{n} ({c/n*100:.0f}%)")
    print(
        f"지연 평균 {statistics.mean(latencies):.1f}ms / "
        f"p95 {statistics.quantiles(latencies, n=20)[-1]:.1f}ms / "
        f"최대 {max(latencies):.1f}ms"
    )
    if fails:
        print("\n오판 목록:")
        for text, cat, exp, got, conf in fails:
            print(f"  [{cat}] '{text}' — 기대 {'END' if exp else 'NOT'}, "
                  f"실제 {'END' if got else 'NOT'} ({conf:.3f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
