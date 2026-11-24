"""고정 결정 라우터 — **LLM·TTS 없이 즉답**하는 경로.

왜 (jev-voice-agent / s2s.cpp 에서 얻은 것)
  두 저장소 모두 "응답 공간이 유한하면 모델을 루프에서 빼라"고 말한다.
  15시간 대화의 상당 부분은 결정적이다: "그만", "볼륨 줄여", "다음 곡", "다시 말해줘".
  그런 턴에 LLM(수 초) + TTS(10~24초)를 쓰는 건 손해다. 미리 합성해 둔 음성을
  재생하면 **체감 0초**가 된다.

  이식한 핵심 3가지
    1. **결정 vs 생성의 경계를 명시적으로** (프롬프트 지시가 아니라 라우트 노드로)
    2. **정규식이 아니라 짧은 발화 제한** — 길면 자유 대화로 보낸다(오탐 방지)
    3. **확신도가 낮으면 되묻기(clarify)** — STT 오인식은 결정 오류가 된다

라우트 파일 (maya/dialog/routes.yaml)
    routes:
      - id: stop
        say: "응, 그만할게."
        any: ["그만", "멈춰", "스톱", "그만해"]
        max_chars: 12
      - id: volume_down
        say: "볼륨 줄일게."
        any: ["볼륨", "소리"]
        all: ["줄", "작게", "낮"]
        action: volume_down        # (선택) 실행할 동작 이름 — 지금은 기록만
사용
    router = DialogRouter.load()
    hit = router.match(text)        # None 이면 자유 대화로
    if hit:
        await send_cached_audio(hit.route.say, hit.route.id)
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_DIR = _REPO_ROOT / "maya" / "dialog"


def _norm(text: str) -> str:
    """비교용 정규화: 소문자, 공백/문장부호 제거.

    STT 결과는 띄어쓰기가 흔들린다("그 만" / "그만"). 공백을 지워 부분 일치를 안정화한다.
    """
    return re.sub(r"[\s,.!?~…'\"()\[\]]+", "", (text or "").lower())


def _ends_with(norm_text: str, kw: str) -> bool:
    """발화가 그 어휘로 **끝나는지**.

    부분 포함으로 매칭했더니 `"그만큼 말해줘"` 가 stop 라우트에 걸렸다(실측).
    명령은 보통 발화 끝에 외친다 — 끝 일치가 훨씬 안전하다.
    """
    return bool(norm_text) and norm_text.endswith(kw)


def _starts_with(norm_text: str, kw: str) -> bool:
    return bool(norm_text) and norm_text.startswith(kw)


@dataclass
class Route:
    id: str
    say: str                       # 에이전트가 말할 문장(고정)
    any: list[str] = field(default_factory=list)    # 이 중 하나만 있으면 매칭
    all: list[str] = field(default_factory=list)    # 전부 있어야 매칭
    none: list[str] = field(default_factory=list)   # 이 중 하나라도 있으면 제외
    max_chars: int = 24            # 발화 길이 상한(넘으면 자유 대화로 간주)
    action: str = ""               # (선택) 실행 동작 이름
    priority: int = 0              # 높을수록 우선
    # any 매칭 방식:
    #   "end"(기본) 발화 끝 | "exact" 정확히 일치 | "start" 발화 시작 | "any" 부분 포함
    anchor: str = "end"

    def matches(self, text: str) -> bool:
        n = _norm(text)
        if not n or len(n) > self.max_chars:
            return False
        if any(_norm(x) in n for x in self.none):
            return False
        if self.all and not all(_norm(x) in n for x in self.all):
            return False
        if self.any:
            if self.anchor == "exact":
                ok = n in {_norm(x) for x in self.any}
            elif self.anchor == "start":
                ok = any(_starts_with(n, _norm(x)) for x in self.any)
            elif self.anchor == "any":
                ok = any(_norm(x) in n for x in self.any)
            else:
                ok = any(_ends_with(n, _norm(x)) for x in self.any)
            if not ok:
                return False
        # any/all 이 모두 비면 규칙이 없는 것 → 매칭하지 않는다(전부 삼키는 사고 방지)
        return bool(self.any or self.all)


@dataclass
class RouteHit:
    route: Route
    text: str


_BUILTIN: list[dict[str, Any]] = [
    # 응답 공간이 확실한 것만 넣는다. 애매하면 자유 대화로 보내는 게 안전하다.
    {"id": "stop", "say": "응, 그만할게.", "any": ["그만", "멈춰", "스톱", "그만해", "중지"],
     "max_chars": 10, "priority": 100},
    {"id": "stop_speaking", "say": "알겠어, 멈췄어.",
     "any": ["조용히", "말하지마", "말하지마요", "쉿"], "max_chars": 12, "priority": 95},
    {"id": "repeat", "say": "다시 말해줄게.", "any": ["다시말해", "한번더", "뭐라고"],
     "max_chars": 12, "priority": 90},
    {"id": "affirm", "say": "응, 좋아.", "any": ["응", "어", "네", "그래", "좋아", "오케이", "ok"],
     "max_chars": 4, "priority": 40, "anchor": "exact"},
    {"id": "thanks", "say": "천만에.", "any": ["고마워", "고맙습니다", "땡큐"], "max_chars": 10,
     "priority": 60},
]


class DialogRouter:
    """고정 결정 라우트 집합."""

    def __init__(self, routes: list[Route]) -> None:
        # 우선순위 높은 것부터 검사(중복 매칭 시 더 구체적인 규칙이 이긴다)
        self.routes = sorted(routes, key=lambda r: -r.priority)

    # ── 로딩 ────────────────────────────────────────────────────────────
    @classmethod
    def load(cls, path: Path | str | None = None) -> "DialogRouter":
        """routes.yaml 을 읽는다. 없거나 비었으면 내장 규칙을 쓴다.

        MAYA_ROUTES_ENABLED=0 이면 빈 라우터(= 모든 발화가 자유 대화).
        """
        routes: list[Route] = []
        if os.environ.get("MAYA_ROUTES_ENABLED", "1") != "1":
            log.info("고정 결정 라우터 비활성(MAYA_ROUTES_ENABLED=0)")
            return cls([])

        p = Path(path) if path else _DEFAULT_DIR / "routes.yaml"
        if p.is_file():
            routes = _parse(p)
            log.info("라우트 %d개 로드: %s", len(routes), p.name)
        else:
            log.info("routes.yaml 없음 → 내장 기본 라우트 %d개", len(_BUILTIN))
        if not routes:
            routes = [Route(**_route_kwargs(d)) for d in _BUILTIN]
        return cls(routes)

    # ── 매칭 ────────────────────────────────────────────────────────────
    def match(self, text: str) -> RouteHit | None:
        for r in self.routes:
            if r.matches(text):
                return RouteHit(route=r, text=text)
        return None


def _route_kwargs(d: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(d.get("id") or "route"),
        "say": str(d.get("say") or ""),
        "any": [str(x) for x in (d.get("any") or [])],
        "all": [str(x) for x in (d.get("all") or [])],
        "none": [str(x) for x in (d.get("none") or [])],
        "max_chars": int(d.get("max_chars") or 24),
        "action": str(d.get("action") or ""),
        "priority": int(d.get("priority") or 0),
        "anchor": str(d.get("anchor") or "end"),
    }


def _parse(path: Path) -> list[Route]:
    """routes.yaml → Route 목록. PyYAML 있으면 그걸, 없으면 최소 파서."""
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(text) or {}
        items = data.get("routes") or []
        return [Route(**_route_kwargs(i)) for i in items if isinstance(i, dict)]
    except ImportError:
        return _mini_parse(text)
    except Exception as exc:  # noqa: BLE001
        log.warning("routes.yaml 파싱 실패(%s) → 내장 규칙 사용", exc)
        return []


def _mini_parse(text: str) -> list[Route]:
    """PyYAML 없이: `routes:` 아래 `- key: value` / `- key: [a, b]` 만 지원."""
    out: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        s = line.strip()
        if s.startswith("- "):
            cur = {}
            out.append(cur)
            s = s[2:].strip()
        if cur is None or ":" not in s:
            continue
        k, _, v = s.partition(":")
        k, v = k.strip(), v.strip()
        if v.startswith("[") and v.endswith("]"):
            cur[k] = [x.strip().strip("\"'") for x in v[1:-1].split(",") if x.strip()]
        elif v.startswith(("\"", "'")):
            cur[k] = v.strip("\"'")
        elif v.isdigit() or (v.lstrip("-").isdigit()):
            cur[k] = int(v)
        elif v:
            cur[k] = v
    return [Route(**_route_kwargs(i)) for i in out if i.get("any") or i.get("all")]
