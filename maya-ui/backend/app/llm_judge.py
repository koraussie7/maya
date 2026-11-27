"""LLM Tier 선택을 위한 Jev/Laya 판단기.

`app/router_laya.py` 의 LayaReranker 와 같은 System-1 / System-2 패턴을 LLM
프로바이더 선택에 적용한다. 라우터가 "이 발화가 어떤 의도인가?" 를 결정하듯,
LLM judge 는 "이 요청에 어떤 tier 가 가장 잘 맞나?" 를 결정한다.

구조
  · System-1 (rule) — 사전 정의된 우선순위 + 휴리스틱
      - tier.cost_hint ("zero" > "low" > "med" > "high")
      - tier.tier (1=internal > 2=cloud > 3=in-process)
      - 롤링 성공률 (최근 N회) — 죽은 tier 는 자동으로 강등
  · System-2 (rerank) — laya.Agent 가 있으면 활용, 없으면 mock
      - 입력 features: query text, message count, persona pace, max_tokens
      - 출력: 각 tier 의 success probability
  · Combine — System-1 의 rule prior × System-2 의 probability

활성화
  · `MAYA_LLM_JUDGE=1`  (기본) → judge 사용
  · `MAYA_LLM_JUDGE=0`  → judge 비활성. tier 정의 순서 그대로 사용.
  · `MAYA_LLM_JUDGE_MOCK=1` → laya 미설치여도 mock 점수만 사용 (기본 동작).

롤링 통계
  · `_TierStats` — tier 이름별 최근 N(=32)회 시도 결과/지연을 메모리에 보관.
  · judge 호출 시점에 이 통계를 읽어 tier 점수에 반영한다.
  · 성공률 < 0.2 인 tier 는 자동으로 최하위로 강등.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:
    # 타입 힌트 전용. 실제 임포트는 함수 내부에서 지연 → llm_fallback ↔ llm_judge
    # 순환을 피한다.
    from .llm_fallback import Tier

log = logging.getLogger(__name__)


# ── Tier 별 롤링 통계 ────────────────────────────────────────────────────────
class _TierStats:
    """tier 이름별 최근 N회 결과/지연.

    lock 으로 보호 (백엔드는 asyncio 단일 스레드지만 mock 폴링에서도 안전).
    """
    def __init__(self, maxlen: int = 32) -> None:
        self._lock = threading.Lock()
        self._attempts: dict[str, deque[tuple[bool, float]]] = {}
        self._maxlen = maxlen

    def record(self, tier_name: str, ok: bool, latency_ms: float) -> None:
        with self._lock:
            dq = self._attempts.setdefault(tier_name, deque(maxlen=self._maxlen))
            dq.append((ok, latency_ms))

    def stats(self, tier_name: str) -> dict[str, float]:
        with self._lock:
            dq = self._attempts.get(tier_name)
            if not dq:
                return {"n": 0, "success": 0.0, "mean_latency_ms": 0.0, "fail_streak": 0}
            n = len(dq)
            succ = sum(1 for ok, _ in dq if ok) / n
            lats = [lat for ok, lat in dq if ok]
            mean_lat = sum(lats) / len(lats) if lats else 0.0
            # 최근 연속 실패 카운트
            streak = 0
            for ok, _ in reversed(dq):
                if not ok:
                    streak += 1
                else:
                    break
            return {"n": n, "success": succ, "mean_latency_ms": mean_lat, "fail_streak": streak}


STATS = _TierStats()


# ── System-1: Rule 기반 prior ────────────────────────────────────────────────
_COST_PRIOR = {"zero": 1.00, "low": 0.70, "med": 0.50, "high": 0.20}
_TIER_PRIOR = {1: 0.90, 2: 0.60, 3: 0.30}  # internal > cloud > in-process


def _rule_prior(tier: Tier) -> float:
    """rule 기반 사전 확률. 비용/위치/이름 패턴 휴리스틱."""
    cost = _COST_PRIOR.get(tier.cost_hint, 0.5)
    loc = _TIER_PRIOR.get(tier.tier, 0.4)
    # smart-router 별칭은 자격증명 이슈로 거의 항상 실패 → prior 낮춤
    name_pen = 0.0
    if "smart" in tier.name:
        name_pen = -0.4
    return max(0.0, min(1.0, 0.4 * cost + 0.4 * loc + 0.2 + name_pen))


# ── System-2: 쿼리 feature → rerank 점수 ─────────────────────────────────────
_KO_RE = re.compile(r"[\uac00-\ud7af]")


def _query_features(messages: list[dict[str, str]], persona_pace: str | None = None) -> dict[str, float]:
    """입력 메시지에서 간단한 feature 추출. laya.Agent 가 학습할 단서."""
    user_texts = [m.get("content", "") for m in messages if m.get("role") == "user"]
    last_user = user_texts[-1] if user_texts else ""
    text_len = len(last_user)
    ko_chars = len(_KO_RE.findall(last_user))
    ko_ratio = (ko_chars / text_len) if text_len else 0.0
    # 대화 길이 (메시지 수) — 길수록 컨텍스트 큰 모델 유리
    n_msgs = len(messages)
    return {
        "text_len": min(text_len, 2000) / 2000,    # 0~1 정규화
        "ko_ratio": ko_ratio,                       # 0~1
        "n_messages": min(n_msgs, 20) / 20,         # 0~1
        "pace_narrate": 1.0 if persona_pace == "narrate" else 0.0,
        "pace_patient": 1.0 if persona_pace == "patient" else 0.0,
        "pace_eager": 1.0 if persona_pace == "eager" else 0.0,
    }


def _rerank_score(tier: Tier, features: dict[str, float]) -> float:
    """모델 기반 rerank 점수 (laya.Agent 없을 때의 mock).

    휴리스틱:
      · 한국어 비율이 높으면 한국어 잘하는 모델(gpt-4o-mini, llama-3.1) 선호
      · 텍스트가 길면 큰 컨텍스트 모델 선호 (gpt-4o-mini > llama > qwen)
      · 롤링 성공률이 낮으면 강하게 페널티
    """
    # 모델별 가중 (실측 기반 — 운영 데이터 쌓이면 laya.Agent 로 교체)
    # 로컬 tier 는 name 패턴 (local-llm/*) 으로 매칭 — 모델 슬러그가 동적.
    if tier.name.startswith("local-llm/"):
        base_score = 0.70   # 로컬 uncensored — 빠르고 무제한
    else:
        base_score = {
            "keirouter-auto/best-fast": 0.55,
            "keirouter-smart/best-fast": 0.10,
            "openrouter-qwen3-32b-no-reasoning": 0.65,
            "openrouter-llama-3.1-8b": 0.80,
            "openrouter-gpt-4o-mini": 0.92,
        }.get(tier.name, 0.5)
    # 한국어 비율이 높으면 +0.1 (실측상 모든 모델이 한국어 잘함)
    base_score += 0.10 * features["ko_ratio"]
    # 긴 텍스트면 큰 모델에 가산
    if features["text_len"] > 0.5:
        if "gpt-4o" in tier.name:
            base_score += 0.10
        elif "qwen" in tier.name or "llama-3.1" in tier.name:
            base_score += 0.05
    return min(1.0, max(0.0, base_score))


# ── 결과 ─────────────────────────────────────
@dataclass
class JudgeVerdict:
    """LLM tier judge 의 최종 결정."""
    ordered: list[tuple[Tier, float]]   # (tier, score) — 점수 내림차순
    source: str                         # "laya" | "mock" | "rule"
    elapsed_ms: float
    abstained: bool = False
    note: str = ""

    def top(self) -> tuple[Tier, float] | None:
        return self.ordered[0] if self.ordered else None


# ── 메인 Judge ───────────────────────────────────────────────────────────────
class JevLayaJudge:
    """LLM tier 순서를 결정하는 judge. 사용:

        judge = JevLayaJudge.from_env()
        verdict = judge.judge(tiers, messages, persona_pace="narrate")
        for tier, score in verdict.ordered:
            ...
    """
    def __init__(self, *, enabled: bool = True, mock: bool = True, threshold: float = 0.30) -> None:
        self._enabled = enabled
        self._mock = mock
        self._threshold = threshold
        self._agent: Any | None = None
        self._import_failed = False
        if enabled and not mock:
            try:
                import laya  # type: ignore
                self._agent = laya.Agent()
                log.info("JevLayaJudge: laya.Agent() 초기화 완료")
            except Exception as exc:  # noqa: BLE001
                log.warning("JevLayaJudge: laya import 실패 → mock 폴백 (%s)", exc)
                self._import_failed = True
                self._mock = True

    @classmethod
    def from_env(cls) -> "JevLayaJudge":
        mode = (os.environ.get("MAYA_LLM_JUDGE", "1") or "1").strip()
        enabled = mode != "0"
        mock = (os.environ.get("MAYA_LLM_JUDGE_MOCK", "1") or "1") == "1"
        threshold = float(os.environ.get("MAYA_LLM_JUDGE_THRESHOLD", "0.30"))
        return cls(enabled=enabled, mock=mock, threshold=threshold)

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    # ── 판정 ────────────────────────────────────────────────────────────
    def judge(
        self,
        tiers: list[Tier],
        messages: list[dict[str, str]],
        *,
        persona_pace: str | None = None,
    ) -> JudgeVerdict:
        if not self._enabled or not tiers:
            return JudgeVerdict(
                ordered=[(t, 1.0) for t in tiers],
                source="rule", elapsed_ms=0.0,
                note="judge disabled or no tiers",
            )
        t0 = time.perf_counter()
        features = _query_features(messages, persona_pace)
        scored: list[tuple[Tier, float]] = []
        for tier in tiers:
            stats = STATS.stats(tier.name)
            # System-1 rule prior
            rule = _rule_prior(tier)
            # System-2 rerank score
            if self._mock or self._import_failed:
                rerank = _rerank_score(tier, features)
                src = "mock"
            else:
                rerank = self._rerank_laya(tier, messages)
                src = "laya"
            # 롤링 성공률 보정
            if stats["n"] >= 5:
                sr = stats["success"]
                if sr < 0.2:
                    # 거의 죽은 tier → 강하게 페널티
                    rerank *= 0.10
                elif sr < 0.5:
                    rerank *= 0.50
                else:
                    rerank *= 0.50 + 0.50 * sr  # 0.5 + 0.5·sr
            # 페일 streak 3회 이상이면 일시적 강등
            if stats["fail_streak"] >= 3:
                rerank *= 0.30
            # Combine: 0.3·rule + 0.7·rerank (rerank 가 더 학습된 정보)
            combined = 0.3 * rule + 0.7 * rerank
            scored.append((tier, combined))
        scored.sort(key=lambda x: -x[1])
        elapsed = (time.perf_counter() - t0) * 1000
        return JudgeVerdict(
            ordered=scored,
            source=src, elapsed_ms=elapsed,
            abstained=not any(s >= self._threshold for _, s in scored),
            note=f"features={features}",
        )

    def _rerank_laya(self, tier: Tier, messages: list[dict]) -> float:
        """실제 laya.Agent 가 있을 때 rerank. 없으면 mock 으로 폴백."""
        # laya.Agent.predict 는 choice 형식 질문을 받는다.
        # tier 의 name 을 choice 로 두고 점수를 받는다.
        try:
            questions = {
                "tier": {
                    "type": "choice",
                    "instructions": (
                        "이 사용자 요청에 대해 가장 잘 응답할 LLM tier 를 고른다. "
                        "최근 성공률과 지연을 종합적으로 고려한다."
                    ),
                    "criteria": {
                        tier.name: (
                            f"{tier.name} — 비용={tier.cost_hint}, "
                            f"위치={'internal' if tier.tier==1 else 'cloud' if tier.tier==2 else 'in-process'}, "
                            f"메모: {tier.note}"
                        ),
                        "none": "어느 tier 도 적합하지 않음",
                    },
                }
            }
            state_text = " ".join(m.get("content", "")[:200] for m in messages)
            out = self._agent.predict(questions=questions, state=state_text)
            q = out.get("tier") if isinstance(out, dict) else None
            if isinstance(q, dict):
                prob = q.get("probability") or q.get("prob") or q.get("confidence")
                if prob is not None:
                    return float(prob)
        except Exception as exc:  # noqa: BLE001
            log.warning("JevLayaJudge: laya.predict 실패 → mock (%s)", exc)
        # 폴백: mock 점수
        return _rerank_score(tier, _query_features(messages))
