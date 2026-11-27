"""Maya 음성대화 시뮬레이션 시나리오 생성기.

코드베이스에서 직접 시나리오를 빌드한다:
  - maya/dialog/routes.yaml          → rule 경로 시나리오
  - maya/personas/*.yaml             → persona 전환 시나리오
  - maya-ui/backend/app/session.py   → interrupt / streaming / abstained 등
                                       파이프라인 경계 시나리오

생성된 시나리오는 JSON으로 stdout 에 떨어지고, --emit 만 주면 그 경로에 파일을 만든다.

실행:
    /tmp/maya-py.sh integration/maya_voice_simulation.py --emit /tmp/maya-scenarios.json

옵션:
    --seed N        시드(재현성)
    --noise 0.0~1.0  STT 흔들림 시뮬레이션 비율
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import random
import re
import sys
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[1]
ROUTES_YAML = REPO / "maya" / "dialog" / "routes.yaml"
PERSONAS_DIR = REPO / "maya" / "personas"


# ── 최소 YAML 파서 (PyYAML 의존성 회피) ───────────────────────────────────
def _strip_yaml_comments(text: str) -> str:
    out_lines = []
    for line in text.splitlines():
        # '#' 로 시작하거나 공백 뒤 # 만 주석 처리 (간단 버전)
        idx = -1
        in_str = False
        for i, ch in enumerate(line):
            if ch in "\"'" and (i == 0 or line[i - 1] != "\\"):
                in_str = not in_str
            elif ch == "#" and not in_str:
                idx = i
                break
        out_lines.append(line if idx < 0 else line[:idx].rstrip())
    return "\n".join(out_lines)


def _parse_routes(path: Path) -> list[dict[str, Any]]:
    """routes.yaml 의 routes: 항목만 가볍게 파싱한다.
    실제 라우터는 정규식 매칭이지만, 시나리오 생성은 어휘·구조만 알면 충분하다.
    """
    if not path.is_file():
        return []
    text = _strip_yaml_comments(path.read_text(encoding="utf-8"))
    routes: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    in_routes = False
    in_block = False
    block_buf: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line:
            continue
        if line.startswith("routes:"):
            in_routes = True
            continue
        if not in_routes:
            continue
        # 새 라우트 시작: "- id: ..."
        if line.startswith("  - id:"):
            if cur is not None:
                routes.append(cur)
            cur = {"id": line.split(":", 1)[1].strip()}
            continue
        if cur is None:
            continue
        # say 같은 일반 key
        m = re.match(r"^\s{4}(\w+):\s*(.*)$", line)
        if m:
            k, v = m.group(1), m.group(2).strip()
            if v.startswith("\"") and v.endswith("\""):
                v = v[1:-1]
            cur[k] = _coerce(v)
            in_block = False
            block_buf = []
            continue
        # 블록 문자열(| …): 다음 비공백 라인까지 모음
        m2 = re.match(r"^\s{4}(\w+):\s*\|\s*$", line)
        if m2:
            in_block = True
            block_buf = []
            cur[m2.group(1)] = ""
            continue
        if in_block:
            block_buf.append(line[6:] if line.startswith("      ") else line)
            cur[re.match(r"^\s{4}(\w+):", line).group(1) if re.match(r"^\s{4}(\w+):", line) else "_"] = "\n".join(block_buf)
            # 블록은 단일 키이므로 단순화: 다음에 새 key 가 나오면 끊는다
            in_block = False
    if cur is not None:
        routes.append(cur)
    return routes


def _coerce(v: str) -> Any:
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        if not inner:
            return []
        # 간단: "a", "b" 또는 a, b 형태
        items = [x.strip().strip('"').strip("'") for x in inner.split(",")]
        return items
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    return v


def _list_personas() -> list[dict[str, str]]:
    out = []
    if not PERSONAS_DIR.is_dir():
        return out
    for p in sorted(PERSONAS_DIR.glob("*.yaml")):
        out.append({"id": p.stem, "path": str(p)})
    return out


# ── 시나리오 데이터 모델 ─────────────────────────────────────────────────
@dataclasses.dataclass
class Scenario:
    id: str
    label: str
    kind: str            # rule_hit | laya_hit | abstained | free_text | persona_switch | interrupt | streaming | audio_no_stt | ambiguous | boundary
    persona_id: str
    text: str | None     # text 입력 발화
    audio_seconds: float = 0.0   # 오디오 입력 발화 길이
    expect_route: str | None = None
    expect_phase: list[str] = dataclasses.field(default_factory=list)
    expect_audio_chunks_min: int = 1
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


# ── 시나리오 빌더 ─────────────────────────────────────────────────────────
def build_scenarios(seed: int = 0, noise: float = 0.0) -> list[Scenario]:
    rng = random.Random(seed)
    routes = _parse_routes(ROUTES_YAML)
    personas = _list_personas() or [{"id": "lyra_vex", "path": ""}]
    out: list[Scenario] = []

    # 1) 라우터 1:1 매핑 — 각 rule 이 한 번씩 정상 매칭되는 시나리오.
    # say 문장이 그대로 정답이 되도록 트리거 발화를 만든다.
    rule_id_to_user = {
        "stop": ["그만해", "그만 할래", "멈춰줘", "스톱", "중지시켜"],
        "stop_speaking": ["조용히 해", "말하지마", "쉿 해줘"],
        "repeat": ["다시 말해줘", "한번더 말해줘", "뭐라고?"],
        "slower": ["천히 말해줘", "좀 더 천천히 말해줘"],
        "affirm": ["응", "네", "좋아", "오케이", "ok"],
        "thanks": ["고마워", "고맙습니다", "땡큐"],
        "hello": ["안녕", "하이", "헬로"],
    }
    for r in routes:
        rid = r.get("id", "")
        users = rule_id_to_user.get(rid, [])
        # routes.yaml 의 any 첫 항목을 한 번 더 직접 사용해 정확 매칭을 본다
        any_list = r.get("any") or []
        if isinstance(any_list, list) and any_list:
            users.append(str(any_list[0]))
        for u in users[:3]:
            out.append(Scenario(
                id=f"rule_{rid}_{_hash(u)}",
                label=f"[rule] {rid} ← {u!r}",
                kind="rule_hit",
                persona_id=personas[0]["id"],
                text=u,
                expect_route=rid,
                expect_phase=["thinking", "speaking", "idle"],
                expect_audio_chunks_min=1,
                notes=f"routes.yaml id={rid} say={r.get('say')!r}",
            ))

    # 2) 라우트 경계 — 발화가 max_chars 를 넘으면 자유 대화로 빠진다.
    # routes.yaml 의 max_chars=12 (stop) 라면 13자 이상은 매칭 실패.
    out.append(Scenario(
        id="boundary_long_text",
        label="[boundary] 30자 발화 → 자유 대화로",
        kind="boundary",
        persona_id=personas[0]["id"],
        text="오늘 하루 어땠어? 나는 좀 바빴어.",  # 16자 + 공백 = 18자 (>12)
        expect_route=None,
        expect_phase=["thinking", "speaking", "idle"],
        notes="max_chars=12 초과 → rule 미스 → 자유 대화",
    ))

    # 3) 라우트 일부 + 자유 대화 폴백 — 정확한 매칭 + 그 다음 자유 대화
    out.append(Scenario(
        id="anchor_exact_yes",
        label="[anchor-exact] 짧은 백채널 → affirm 라우트",
        kind="rule_hit",
        persona_id=personas[0]["id"],
        text="응",
        expect_route="affirm",
        notes="affirm 의 anchor=exact → 짧은 단어만 매칭",
    ))
    out.append(Scenario(
        id="anchor_exact_no",
        label="[anchor-exact] '응 알겠어' → 자유 대화 (발화 전체가 그 단어가 아님)",
        kind="boundary",
        persona_id=personas[0]["id"],
        text="응 알겠어",
        expect_route=None,
        notes="affirm 은 anchor=exact → '응 알겠어' 는 매칭 안 됨",
    ))

    # 4) Laya reranker 가 활성화된 상태에서 mock abstain
    out.append(Scenario(
        id="laya_abstained",
        label="[laya] 모호한 발화 → abstained → 자유 대화",
        kind="abstained",
        persona_id=personas[0]["id"],
        text="그게 좀 이상한 느낌이야",
        expect_route=None,
        notes="Laya reranker mock 모드에서 score=0.05 → abstain",
    ))

    # 5) 자유 대화 — 다문장 TTS 스트리밍
    out.append(Scenario(
        id="free_long",
        label="[free-text] 4문장 응답이 기대되는 질문",
        kind="free_text",
        persona_id=personas[0]["id"],
        text="오늘 부산 날씨 어때?",
        expect_phase=["thinking", "speaking", "idle"],
        expect_audio_chunks_min=2,
        notes="TTS 병렬 워커 2개가 여러 문장을 송출",
    ))
    out.append(Scenario(
        id="free_short",
        label="[free-text] 1~2문장 짧은 응답",
        kind="free_text",
        persona_id=personas[0]["id"],
        text="안녕",
        expect_phase=["thinking", "speaking", "idle"],
        notes="짧은 입력 → 짧은 응답 → TTS 1청크 예상",
    ))

    # 6) Persona 전환 (text 입력 시 payload.persona)
    other_persona = next((p for p in personas if p["id"] != personas[0]["id"]), personas[0])
    out.append(Scenario(
        id="persona_switch",
        label=f"[persona-switch] {personas[0]['id']} → {other_persona['id']}",
        kind="persona_switch",
        persona_id=personas[0]["id"],
        text="다른 목소리로 인사해줘",
        expect_audio_chunks_min=1,
        notes=f"sendText 가 두 번째 인자로 {other_persona['id']!r} 를 실어보냄",
    ))

    # 7) 인터럽트 — 자유 대화 응답 중간에 audio.flush 가 오면 즉시 끊고 새 발화 처리
    out.append(Scenario(
        id="interrupt_then_new",
        label="[interrupt] 첫 응답 말하는 도중 새 발화 → 즉시 IDLE 로 양보",
        kind="interrupt",
        persona_id=personas[0]["id"],
        text="오늘 부산 날씨와 주변 관광지를 자세히 알려줘",  # 첫 응답 길게
        audio_seconds=2.0,  # 그 응답 도중 2초 오디오 입력 → 인터럽트
        expect_phase=["thinking", "speaking", "yielded", "listening", "idle"],
        notes="Phase.YIELDED 가 송출되는지 검증",
    ))

    # 8) 오디오만 — STT 결과로 라우트 적중 (안정적인 short utterance)
    out.append(Scenario(
        id="audio_stt_to_rule",
        label="[audio-only] '그만' PCM 1.5초 → STT → stop 라우트",
        kind="audio_no_stt",
        persona_id=personas[0]["id"],
        audio_seconds=1.5,
        text=None,
        expect_phase=["listening", "speaking", "idle"],  # STT → route → speak
        notes="PCM 16kHz mono 1.5초 (실제로는 무음 STT 가 빈 결과를 줄 수 있음)",
    ))

    # 9) 오디오 무음 — STT가 빈 텍스트 → IDLE 복귀
    out.append(Scenario(
        id="audio_silence",
        label="[audio-silence] 무음 2초 → STT 빈 결과 → IDLE",
        kind="audio_no_stt",
        persona_id=personas[0]["id"],
        audio_seconds=2.0,
        text=None,
        expect_phase=["listening", "idle"],
        notes="빈 STT 는 _handle_text 로 가지 않고 즉시 idle",
    ))

    # 10) STT 흔들림 — 노이즈 비율만큼 라우터를 우회
    if noise > 0:
        for r in routes[:3]:
            any_list = r.get("any") or []
            if isinstance(any_list, list) and any_list:
                base = str(any_list[0])
                # 띄어쓰기 흔들림 (STT 흔한 패턴)
                noisy = " ".join(base) if rng.random() < noise else base
                out.append(Scenario(
                    id=f"noisy_{r.get('id')}_{_hash(noisy)}",
                    label=f"[noisy] {r.get('id')} ← {noisy!r}",
                    kind="rule_hit",
                    persona_id=personas[0]["id"],
                    text=noisy,
                    expect_route=r.get("id"),
                    notes="router._norm() 이 공백을 지워 매칭 — 노이즈는 통과해야 함",
                ))

    # 11) 다중 페르소나 전환 (가능하면 모든 페르소나를 한 번씩)
    for p in personas[1:]:
        out.append(Scenario(
            id=f"persona_{p['id']}",
            label=f"[persona] 시작 페르소나 {p['id']}",
            kind="persona_switch",
            persona_id=p["id"],
            text="오늘 기분이 어때?",
            notes=f"MAYA_PERSONA={p['id']} 로 띄울 때의 응답 톤 확인",
        ))

    return out


def _hash(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:6]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--noise", type=float, default=0.0)
    ap.add_argument("--emit", type=str, default=None,
                    help="JSON 출력 경로 (없으면 stdout)")
    args = ap.parse_args()

    scenarios = build_scenarios(seed=args.seed, noise=args.noise)
    payload = {
        "repo": str(REPO),
        "routes_yaml": str(ROUTES_YAML),
        "personas": _list_personas(),
        "seed": args.seed,
        "noise": args.noise,
        "scenarios": [s.to_dict() for s in scenarios],
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.emit:
        Path(args.emit).write_text(text, encoding="utf-8")
        print(f"[emitted] {args.emit} — {len(scenarios)} scenarios", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
