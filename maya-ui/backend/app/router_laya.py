"""Laya Reranker — System-1 의사결정 모델을 DialogRouter 위에 얹는다.

배경 (2026-11-25)
  digital-twin(ShlokShah01/digital-twin) 의 Laya 호출 패턴을 차용해
  우리 DialogRouter 의 rule-based 매칭을 model-based 결제로 보강한다.
  Laya 는 NandhaKishorM/laya 의 33ms 단일 forward pass 로
  "이 발화가 어떤 route 의 의도인가" 를 확률로 답한다.

  우리 pipeline 에서는:
    1) rule-based 매칭 (router.DialogRouter.match) — 기존 빠른 경로
    2) Laya rerank (이 모듈) — rule 매칭 결과를 **확률로 보강**하거나
       rule 이 없을 때 최선의 route 를 **모델이 선택**
    3) 둘 중 확률/우선순위가 더 높은 쪽을 사용

  Laya 가 없거나(mock 모드 또는 미설치) 실패하면 → 조용히 rule-only 로 폴백.

사용
    from app.router_laya import LayaReranker
    rr = LayaReranker.from_env()
    ranked = rr.rerank("그만 말해", top_k=3)
    # → [{"route_id":"stop","prob":0.94,"say":"응, 그만할게."}, ...]
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

log = logging.getLogger(__name__)


@dataclass
class RerankResult:
    """Laya 가 반환한 단일 후보."""

    route_id: str
    prob: float            # 0..1
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class RerankOutcome:
    """Laya rerank 한 번의 결과."""

    query: str
    results: list[RerankResult]
    elapsed_ms: float
    source: str            # "laya" | "mock" | "fallback"
    abstained: bool = False

    def top(self) -> RerankResult | None:
        return self.results[0] if self.results else None


class LayaReranker:
    """DialogRouter 위에 얹는 Laya 기반 reranker.

    Laya 미설치/실패 시 mock 또는 fallback 으로 동작한다.
    """

    def __init__(
        self,
        *,
        agent: Any | None = None,
        mock: bool = False,
        threshold: float = 0.45,
    ) -> None:
        # `agent` 는 laya.Agent() 인스턴스. None 이고 mock=False 면 import 시도.
        self._agent = agent
        self._mock = mock
        self._threshold = threshold
        self._import_failed = False

        if not mock and agent is None:
            try:
                import laya  # type: ignore

                self._agent = laya.Agent()
                log.info("LayaReranker: laya.Agent() 초기화 완료")
            except Exception as exc:  # noqa: BLE001
                log.warning("LayaReranker: laya import 실패 → mock 폴백 (%s)", exc)
                self._import_failed = True
                self._mock = True

    # ── 팩토리 ──────────────────────────────────────────────────────────
    @classmethod
    def from_env(cls) -> "LayaReranker":
        """환경변수로 모드 결정.

        MAYA_LAYA=1           → 강제 활성 (없으면 import 시도 후 mock 폴백)
        MAYA_LAYA=0           → 비활성 (mock 모드로 시작 — 항상 즉시 답)
        MAYA_LAYA_MOCK=1      → 강제 mock (실제 모델 안 받음)
        MAYA_LAYA_THRESHOLD=0.45 → accept threshold
        """
        mode = (os.environ.get("MAYA_LAYA") or "0").strip()
        mock = mode == "0" or (os.environ.get("MAYA_LAYA_MOCK") or "0") == "1"
        threshold = float(os.environ.get("MAYA_LAYA_THRESHOLD") or "0.45")
        return cls(mock=mock, threshold=threshold)

    @property
    def is_mock(self) -> bool:
        return self._mock or self._import_failed

    # ── 매핑: route_id → choice 옵션 ────────────────────────────────────
    @staticmethod
    def build_questions(routes: Iterable[Any]) -> dict[str, Any]:
        """DialogRouter 의 routes 를 Laya 의 choice 질문으로 변환.

        각 route 는 id/say/criteria(이 라우트가 매칭되는 발화 예시)로 표현된다.
        Laya 는 criteria 를 보고 확률을 매긴다.
        """
        choices: dict[str, dict[str, str]] = {}
        for r in routes:
            rid = getattr(r, "id", None) or "?"
            say = getattr(r, "say", "") or ""
            # `any`/`all` 리스트를 criteria 설명으로 합친다.
            any_kw = getattr(r, "any", []) or []
            all_kw = getattr(r, "all", []) or []
            parts: list[str] = []
            if any_kw:
                parts.append("발화 끝 또는 안에 다음 단어가 있으면: " + ", ".join(any_kw))
            if all_kw:
                parts.append("다음 단어가 모두 있으면: " + ", ".join(all_kw))
            if not parts:
                # rule 이 없는 route 는 Laya 가 학습할 단서가 없으니 약한 설명.
                parts.append(f"라우트 '{rid}' 의도 (say='{say[:30]}')")
            choices[rid] = parts[0]
        return {
            "intent": {
                "type": "choice",
                "instructions": (
                    "사용자의 한국어 발화가 어떤 의도/명령 라우트에 해당하는지 고른다. "
                    "어느 것에도 강하게 해당하지 않으면 'none' 을 고른다."
                ),
                "criteria": {**choices, "none": "어느 라우트 의도와도 일치하지 않는 자유 대화"},
            },
        }

    # ── rerank ──────────────────────────────────────────────────────────
    def rerank(self, text: str, routes: Iterable[Any], *, top_k: int = 3) -> RerankOutcome:
        """한 발화에 대해 라우트별 확률을 계산.

        - mock 모드: 간단한 keyword 점수로 흉내낸다 (rule 과 동일 결과).
        - 실제 laya: laya.Agent.predict 사용.
        - 실패 시: 모든 후보 prob=0, source='fallback'.
        """
        routes_list = list(routes)
        if not routes_list:
            return RerankOutcome(query=text, results=[], elapsed_ms=0.0, source="fallback")

        if self._mock:
            return self._rerank_mock(text, routes_list, top_k=top_k)

        questions = self.build_questions(routes_list)
        t0 = time.perf_counter()
        try:
            # laya.Agent.predict() 의 반환 형식은 버전에 따라 다르다.
            # 안전한 경로: dict 접근 — question_name → {choice, probability, ...}
            out = self._agent.predict(questions=questions, state=text)
        except Exception as exc:  # noqa: BLE001
            log.warning("LayaReranker: predict 실패 → fallback (%s)", exc)
            return RerankOutcome(
                query=text, results=[], elapsed_ms=(time.perf_counter() - t0) * 1000,
                source="fallback",
            )
        elapsed = (time.perf_counter() - t0) * 1000

        # out 파싱 (버전 호환을 위해 관용적으로 시도)
        results = self._parse_laya_output(out, routes_list)
        # threshold 미만은 잘라낸다
        results = [r for r in results if r.prob >= self._threshold]
        results.sort(key=lambda r: -r.prob)
        return RerankOutcome(
            query=text,
            results=results[:top_k],
            elapsed_ms=elapsed,
            source="laya",
            abstained=not results,
        )

    def _parse_laya_output(self, out: Any, routes: Iterable[Any]) -> list[RerankResult]:
        """laya.predict 출력을 우리 RerankResult 로 정규화.

        지원 형태:
          · {"intent": {"choice": "stop", "probability": 0.94, ...}, ...}  (question-keyed)
          · [{"choice": "stop", "probability": 0.94, ...}]  (list per question)
          · {"intent": {"stop": 0.94, "none": 0.05}}  (raw probability dict)
        """
        results: list[RerankResult] = []
        try:
            q = out.get("intent") if isinstance(out, dict) else None
            if q is None and isinstance(out, list) and out:
                q = out[0]
            if isinstance(q, dict):
                # 1) choice/probability 키
                choice = q.get("choice") or q.get("answer")
                prob = q.get("probability") or q.get("prob") or q.get("confidence")
                if choice is not None and prob is not None:
                    return [RerankResult(route_id=str(choice), prob=float(prob), raw=q)]
                # 2) raw probability dict (per-option)
                probs = {k: float(v) for k, v in q.items() if isinstance(v, (int, float))}
                if probs:
                    return [
                        RerankResult(route_id=k, prob=v, raw=q)
                        for k, v in sorted(probs.items(), key=lambda kv: -kv[1])
                    ]
        except Exception as exc:  # noqa: BLE001
            log.warning("LayaReranker: 출력 파싱 실패 (%s)", exc)
        return results

    # ── mock 구현 ───────────────────────────────────────────────────────
    def _rerank_mock(self, text: str, routes: list[Any], *, top_k: int) -> RerankOutcome:
        """실제 laya 가 없을 때의 mock. rule 기반 매칭 점수를 그대로 사용.

        "모델 기반 rerank 의 동작" 만 검증하는 게 목적이지, 정확도를 보장하지 않는다.
        """
        t0 = time.perf_counter()
        # route.matches() 가 있으면 그대로 사용, 없으면 len(any) 비례로 가짜 점수
        scored: list[RerankResult] = []
        for r in routes:
            rid = getattr(r, "id", "?")
            try:
                ok = bool(r.matches(text))
            except Exception:  # noqa: BLE001
                ok = False
            # mock: 매칭되면 0.85, any 길이 비례 가짜 보조점, 아니면 0.05
            base = 0.85 if ok else 0.05
            scored.append(RerankResult(route_id=rid, prob=base, raw={"mock": True, "matched": ok}))
        scored.sort(key=lambda r: -r.prob)
        elapsed = (time.perf_counter() - t0) * 1000
        return RerankOutcome(
            query=text,
            results=scored[:top_k],
            elapsed_ms=elapsed,
            source="mock",
            abstained=not any(r.prob >= self._threshold for r in scored),
        )
