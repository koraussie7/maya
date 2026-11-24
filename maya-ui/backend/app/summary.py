"""누적 요약 — 기록은 100% 보존하고, 컨텍스트만 고정 크기로 유지한다.

비용 근거 (2026-11-24 실측, ling-3-1-flash $0.075/$0.225 per 1M)
  · 매 턴 전체 재전송        : 월 3,000턴 = $18.90
  · 최근 8턴만                : $0.20
  · + 20턴마다 누적 요약      : **$0.27** (요약 150회 포함)
  → 요약은 대화량이 늘어도 **턴당 약 30%** 만 더 든다(선형).

동작
  1. 턴마다 `maybe_summarize()` 를 호출한다(백그라운드 — 응답을 막지 않는다).
  2. 오늘(`day.jsonl`)의 `through_ts` 이후 원문이 `MAYA_SUMMARY_EVERY`(기본 20)턴 쌓이면
     기존 요약 + 새 원문 → LLM 으로 **압축 요약**(약 200토큰)을 만든다.
  3. `summary.json` 에 저장하고 `through_ts` 를 갱신한다.
  4. 다음 턴부터 컨텍스트 = system + **요약** + 최근 8턴.
     → 세션이 끊겨 새로 붙어도 같은 날짜면 **앞 대화 기억이 살아있다**.

주의
  · 요약 호출이 실패하면 조용히 넘어간다(요약 없음 = 최근 8턴만 = 기존 동작).
  · 요약은 `day.jsonl` 만 읽는다 — 자정을 넘기면 새 파일로 자연스럽게 리셋된다.
  · LLM 호출이라 **비동기 + 동시 실행 1개**로 제한한다(로컬 모델 큐를 잡아먹지 않게).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from .llm_gateway import ensure_v1
from .transcript import TranscriptStore

log = logging.getLogger(__name__)

SUMMARY_SYSTEM = (
    "너는 대화 요약기다. 주어진 대화를 **나중에 같은 대화를 이어갈 수 있도록** 압축한다.\n"
    "반드시 한국어로, 6~10문장 이내. 다음을 보존한다:\n"
    "- 사용자에 대해 새로 알게 된 사실(이름, 선호, 일정, 습관)\n"
    "- 합의/결정된 내용(약속, 계획, 지시)\n"
    "- 아직 끝나지 않은 일(진행 중인 주제)\n"
    "인사·잡담·반복은 버린다. 추측을 사실로 쓰지 않는다."
)

_MAX_INPUT_CHARS = 12_000   # 요약 입력 상한(로컬 모델 보호)
_locks: dict[str, asyncio.Lock] = {}
_last_attempt: dict[str, float] = {}


def _every() -> int:
    try:
        return max(5, int(os.environ.get("MAYA_SUMMARY_EVERY", "20")))
    except ValueError:
        return 20


def _cooldown() -> float:
    try:
        return float(os.environ.get("MAYA_SUMMARY_COOLDOWN_S", "60"))
    except ValueError:
        return 60.0


def render(turns: list[dict[str, Any]]) -> str:
    """원문 레코드 → 요약기에 넣을 대화 텍스트."""
    lines = []
    for r in turns:
        role = r.get("role")
        who = "사용자" if role == "user" else "비서"
        text = (r.get("content") or "").strip().replace("\n", " ")
        if text:
            lines.append(f"{who}: {text}")
    out = "\n".join(lines)
    return out[-_MAX_INPUT_CHARS:]


async def _llm_summarize(previous: str, transcript_text: str) -> str:
    """OpenAI 호환 클라이언트로 요약 1회(비스트림)."""
    from .config import SETTINGS  # 순환 import 회피용 지역 import

    if not SETTINGS.openai_api_key and SETTINGS.llm_provider != "private":
        return ""

    user = (
        (f"[지금까지의 요약]\n{previous}\n\n" if previous else "")
        + "[이어지는 대화]\n"
        + transcript_text
        + "\n\n위 내용을 하나의 누적 요약으로 다시 써라."
    )
    try:
        from openai import AsyncOpenAI  # type: ignore

        client = AsyncOpenAI(
            api_key=SETTINGS.openai_api_key or "none",
            base_url=ensure_v1(SETTINGS.openai_base),
            timeout=float(SETTINGS.llm_timeout_s),
            max_retries=1,
        )
        r = await client.chat.completions.create(
            model=SETTINGS.openai_model,
            messages=[{"role": "system", "content": SUMMARY_SYSTEM},
                      {"role": "user", "content": user}],
            temperature=0.2,
            max_tokens=400,
        )
        return (r.choices[0].message.content or "").strip()
    except Exception as exc:  # noqa: BLE001
        log.warning("요약 LLM 실패: %s", exc)
        return ""


async def maybe_summarize(store: TranscriptStore) -> dict[str, Any] | None:
    """20턴마다 호출. 요약했으면 새 요약 dict, 아니면 None."""
    if os.environ.get("MAYA_SUMMARY_ENABLED", "1") != "1":
        return None

    state = store.load_summary()
    through = float(state.get("through_ts") or 0.0)
    pending = store.turns_since(through)
    if pending < _every():
        return None

    key = str(store.day_dir)
    now = time.monotonic()
    if now - _last_attempt.get(key, 0.0) < _cooldown():
        return None                      # 방금 시도했다(실패 포함) → 잠시 쉼
    _last_attempt[key] = now

    lock = _locks.setdefault(key, asyncio.Lock())
    if lock.locked():
        return None                      # 이미 요약 중
    async with lock:
        new = store.new_since(through)
        text = render(new)
        if len(text) < 80:
            return None
        t0 = time.monotonic()
        summary = await _llm_summarize(str(state.get("summary") or ""), text)
        if not summary:
            return None
        last_ts = float(new[-1].get("ts") or time.time())
        total_turns = int(state.get("turns") or 0) + sum(
            1 for r in new if r.get("role") == "assistant")
        store.save_summary(summary, last_ts, total_turns)
        log.info("요약 완료 %.1fs — %d턴 누적, %d자", time.monotonic() - t0,
                 total_turns, len(summary))
        return {"summary": summary, "through_ts": last_ts, "turns": total_turns}


def context_prefix(store: TranscriptStore | None) -> str:
    """LLM 컨텍스트 앞에 붙일 '오늘 대화 요약' 블록(없으면 빈 문자열)."""
    if store is None:
        return ""
    try:
        s = str(store.load_summary().get("summary") or "").strip()
    except Exception:  # noqa: BLE001
        return ""
    return f"[오늘 이전 대화 요약]\n{s}" if s else ""
