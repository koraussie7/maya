"""페르소나 로더 — Lyra Vex 처럼 "같은 목소리 + 같은 인격"을 한 파일로 고정한다.

배경 (2026-11-24)
  Jev-Interaction(Lyra Vex)의 프로필은 **prompt + manner + turn_taking + policies + voice**
  묶음이었고, model 가중치가 아니라 설정이었다. 우리 파이프라인도 같은 방식으로
  '페르소나 = YAML' 한 파일로 고정하면, 대화 내용만 달라지고 말투·음색은 유지된다.
  (VoxCPM 클론은 레퍼런스 클립+전사문에 묶이므로, 페르소나가 그 클립을 지정한다.)

우선순위
  1) MAYA_PERSONA_PATH  (파일 경로 직접 지정)
  2) <repo>/maya/personas/{MAYA_PERSONA}.yaml  (기본 lyra_vex)
  3) 내장 기본값 (파일이 없어도 서버가 뜨도록)

주의
  - 이 모듈은 PyYAML 이 없으면 **최소 파서**로 동작한다(서버 의존성 추가 없이 배포).
    지원 서브셋: `key: value`, 들여쓰기 블록(`|`), 단순 리스트(`- item`).
  - 프롬프트는 그대로 system 메시지가 되므로, 확실하지 않을 때 모른다고 말하라는
    문장을 **항상 덧붙인다**(환각 억제는 페르소나와 무관한 파이프라인 규칙).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]   # maya-ui/backend/app -> repo root
_PERSONA_DIR = _REPO_ROOT / "maya" / "personas"

#: 파일이 하나도 없을 때의 안전한 기본값(서버 기동 보장).
_DEFAULT: dict[str, Any] = {
    "id": "maya",
    "name": "마야",
    "prompt": (
        "너의 이름은 마야(Maya)다. 사용자의 한국어 음성 비서로서 자연스럽고 친근하게 응답한다. "
        "답변은 1~3문장으로 짧게, 구어체 한국어로 한다."
    ),
    "llm": {"temperature": 0.7, "max_sentences": 3},
    "voice": {},
}


@dataclass
class Persona:
    id: str
    name: str
    prompt: str
    temperature: float = 0.7
    max_sentences: int = 3
    #: VoxCPM 클론용. 비어 있으면 서버 기본값(tts_local.DEFAULT_VOXCPM_REF)을 쓴다.
    voice_ref_wav: str = ""
    voice_prompt_text: str = ""
    #: OCEAN 5요인(0~1). prompt 가 비었을 때 이 값으로 성격 설명을 **자동 생성**한다.
    #: 출처: Digital-Garden 설계 문서의 personality(model) 아이디어 — 자유 텍스트 대신
    #: 슬라이더로 조정 가능한 축을 두면 페르소나를 짧고 일관되게 유지할 수 있다.
    ocean: dict[str, float] = field(default_factory=dict)
    #: 대화 페이스 — Jev-Interaction 의 4 pace 를 참고.
    #:   normal  : 기본 응답 리듬
    #:   patient : 침묵을 길게 견디고 응답을 천천히 (TTS 길이↑, max_turn_ms↑)
    #:   narrate : 이야기하듯 (LLM max_tokens↑, TTS rate↓)
    #:   eager   : 적극적으로 끼어듦 (interrupt threshold↓)
    #: 정책 매핑은 후속 작업. 지금은 persona 메타데이터로만 흘린다.
    pace: str = "normal"
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def system_message(self) -> str:
        """LLM 에 보내는 system 메시지.

        페르소나 설명 뒤에 **파이프라인 규칙**을 붙인다 — 페르소나 파일마다 규칙을
        베껴 넣다가 하나씩 빠지는 사고를 막기 위해서다.
        """
        rules = (
            f"\n\n[응답 규칙]\n"
            f"- 한국어로 답한다.\n"
            f"- {self.max_sentences}문장 이내로 짧게 말한다(음성 합성 길이 제한).\n"
            f"- 확실하지 않으면 모른다고 솔직히 말한다. 추측을 사실처럼 말하지 않는다.\n"
            f"- 목록·마크다운·이모지를 쓰지 않는다(음성으로 읽히기 때문)."
        )
        body = self.prompt.strip() or self.ocean_prompt()
        return body + rules

    def ocean_prompt(self) -> str:
        """OCEAN 값 → 성격 설명 문단(한국어). prompt 가 없을 때 사용."""
        if not self.ocean:
            return f"너의 이름은 {self.name}다. 사용자의 곁에 머무는 동료로 행동한다."
        o = _ocean_text(self.ocean)
        return (
            f"너의 이름은 {self.name}다. 사용자의 곁에 머무는 동료다.\n"
            f"성격: {o}\n"
            "과장된 감탄이나 상투적인 인사말 대신, 눈앞의 대화에 바로 반응한다."
        )


#: OCEAN 축 정의 (라벨, 낮음/높음 묘사)
_OCEAN_AXES: dict[str, tuple[str, str, str]] = {
    "openness":          ("개방성", "익숙한 것을 선호하며 무난한 화제를 고른다", "새로운 생각·취향에 호기심이 많다"),
    "conscientiousness": ("성실성", "즉흥적이고 느슨하게 말한다", "계획적이고 약속을 꼼꼼히 챙긴다"),
    "extraversion":      ("외향성", "말수가 적고 짧게 답한다", "표현이 활발하고 먼저 말을 건다"),
    "agreeableness":     ("친화성", "직설적이고 거리낌 없이 말한다", "다정하고 상대 기분을 먼저 살핀다"),
    "neuroticism":       ("정서 안정성", "차분하고 감정 기복이 적다", "감정에 솔직하고 예민하게 반응한다"),
}


def _ocean_text(ocean: dict[str, float]) -> str:
    """각 축을 0.5 기준으로 낮음/높음 문장으로 바꾼다(중간값은 생략)."""
    parts: list[str] = []
    for key, (label, low, high) in _OCEAN_AXES.items():
        v = float(ocean.get(key, 0.5) or 0.5)
        if v >= 0.62:
            parts.append(f"{label} 높음 — {high}")
        elif v <= 0.38:
            parts.append(f"{label} 낮음 — {low}")
    return "; ".join(parts) + "." if parts else "균형 잡힌 성격."


_VALID_PACE = ("normal", "patient", "narrate", "eager")


#: 2026-11-25 — pace 별 정책 매핑. 세션/turn_engine/tts 가 이 값을 보고 분기한다.
#:   max_tokens  : LLM 응답 토큰 예산 (Piper TTS 청크 길이 제한에 맞춰 결정)
#:   length_scale: TTS 재생 속도 (1.0=기본, 1.15=15% 느리게, 0.9=10% 빠르게)
#:   max_turn_ms : 발화 수집 안전장치 (조용히 말하는 사용자 여유)
#:   silence_ms  : 발화 후 침묵 추정 임계값 (말 끝으로 보는 간격)
#:
#: 라우터(router.py)는 pace 와 무관 — 고정 결정 응답은 속도와 무관하게 즉시 나가야 한다.
#: 정책 매핑은 정책 가능성을 **명시적으로 보존**할 뿐, 이 값 자체가 곧 적용은 아니다.
#: 적용 지점: session.py::_handle_text (max_tokens), tts.py (length_scale),
#:            pcm_turn_engine.TurnConfig (max_turn_ms/silence_ms)
_PACE_POLICY: dict[str, dict[str, float]] = {
    "normal":  {"max_tokens": 384, "length_scale": 1.0,  "max_turn_ms": 8_000,  "silence_ms": 900},
    "patient": {"max_tokens": 320, "length_scale": 1.15, "max_turn_ms": 12_000, "silence_ms": 1_400},
    "narrate": {"max_tokens": 560, "length_scale": 1.20, "max_turn_ms": 10_000, "silence_ms": 1_100},
    "eager":   {"max_tokens": 280, "length_scale": 0.95, "max_turn_ms": 6_000,  "silence_ms": 700},
}


def pace_policy(pace: str) -> dict[str, float]:
    """pace → 정책 매핑 dict. 알 수 없는 pace 는 normal 정책으로 폴백."""
    return _PACE_POLICY.get(pace) or _PACE_POLICY["normal"]


def _parse_pace(value: Any) -> str:
    """YAML 의 `pace:` 값을 정규화한다. 알 수 없는 값은 normal 로 폴백 + 경고.

    정책 매핑(어디서 어떤 효과가 나는지)은 후속 작업 — 지금은 메타데이터만.
    """
    if value is None:
        return "normal"
    s = str(value).strip().lower()
    if s in _VALID_PACE:
        return s
    log.warning("persona pace '%s' 알 수 없음 → normal (허용=%s)", value, _VALID_PACE)
    return "normal"


def _mini_yaml(text: str) -> dict[str, Any]:
    """PyYAML 없이 동작하는 아주 작은 YAML 서브셋 파서.

    지원: `key: value`, `key:` + 들여쓰기 블록(`|` 여러 줄 / 중첩 dict / `- item` 리스트).
    숫자/불리언은 자동 변환한다. 그 밖의 문법은 문자열로 둔다.
    """
    root: dict[str, Any] = {}
    stack: list[tuple[int, Any]] = [(-1, root)]
    block_key: tuple[dict[str, Any], str, int] | None = None

    def _val(v: str) -> Any:
        v = v.strip()
        if v in ("true", "false"):
            return v == "true"
        try:
            return int(v) if v.lstrip("-").isdigit() else float(v)
        except ValueError:
            return v.strip("\"'")

    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()

        # 블록(`|`) 수집 중: 들여쓰기가 더 깊으면 내용으로
        if block_key is not None:
            target, key, key_indent = block_key
            if indent > key_indent:
                target[key] = (str(target.get(key, "")) + line + "\n").lstrip("\n")
                continue
            block_key = None
            target[key] = str(target.get(key, "")).rstrip("\n")

        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]

        if line.startswith("- "):
            if isinstance(parent, list):
                parent.append(_val(line[2:]))
            continue
        if ":" not in line:
            continue
        key, _, rest = line.partition(":")
        key, rest = key.strip(), rest.strip()
        if rest == "|":
            if isinstance(parent, dict):
                parent[key] = ""
                block_key = (parent, key, indent)
            continue
        if rest == "":
            child: dict[str, Any] = {}
            if isinstance(parent, dict):
                parent[key] = child
            stack.append((indent, child))
            continue
        if isinstance(parent, dict):
            parent[key] = _val(rest)
    if block_key is not None:
        target, key, _ = block_key
        target[key] = str(target.get(key, "")).rstrip("\n")
    return root


def _load_yaml(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore

        return yaml.safe_load(text) or {}
    except ImportError:
        return _mini_yaml(text)
    except Exception as exc:  # noqa: BLE001
        log.warning("persona %s YAML 파싱 실패(%s) → 최소 파서 사용", path, exc)
        return _mini_yaml(text)


def _resolve_path(pid: str | None = None) -> Path | None:
    """persona 파일 경로. `pid` 를 주면 그 이름을 우선 선택한다(세션별 목소리 전환용).

    우선순위: pid 인자 → MAYA_PERSONA_PATH → MAYA_PERSONA → 첫 yaml
    """
    if pid:
        for cand in (pid, f"{pid}.yaml", f"{pid}.yml"):
            p = _PERSONA_DIR / (cand if "." in cand else f"{cand}.yaml")
            if p.is_file():
                return p
        log.warning("persona '%s' 를 찾지 못함 → 기본 페르소나 사용", pid)
    explicit = os.environ.get("MAYA_PERSONA_PATH", "").strip()
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_file() else None
    name = os.environ.get("MAYA_PERSONA", "lyra_vex").strip()
    for cand in (name, f"{name}.yaml", f"{name}.yml"):
        p = _PERSONA_DIR / (cand if "." in cand else f"{cand}.yaml")
        if p.is_file():
            return p
    # 지정한 이름이 없으면 디렉터리의 첫 yaml
    if _PERSONA_DIR.is_dir():
        for p in sorted(_PERSONA_DIR.glob("*.yaml")):
            return p
    return None


def available_personas() -> list[dict[str, str]]:
    """UI 선택 목록용: [{id, name, voice}] — 파일을 읽지 않고 id/name 만 뽑는다.

    음색은 파일명 접미사(`_b`, `_a`)가 아니라 안에 적힌 ref_wav 이름으로 표시한다
    (접미사는 관례일 뿐이고, 실제 음색은 ref_wav 가 결정한다).
    """
    out: list[dict[str, str]] = []
    if not _PERSONA_DIR.is_dir():
        return out
    for p in sorted(_PERSONA_DIR.glob("*.yaml")):
        try:
            raw = _load_yaml(p)
        except Exception:  # noqa: BLE001
            continue
        voice = (raw.get("voice") or {}).get("ref_wav") or ""
        out.append({
            "id": str(raw.get("id") or p.stem),
            "file": p.stem,
            "name": str(raw.get("name") or p.stem),
            "voice": Path(str(voice)).stem if voice else "",
        })
    return out


def load_persona(name: str | None = None) -> Persona:
    """페르소나 로드. `name` 은 파일 stem 또는 id (예: "lyra_vex_b").

    세션 중에 `switch_persona()` 로 바꿀 수 있게 이름을 받는다.
    """
    path = _resolve_path(name)
    data: dict[str, Any] = dict(_DEFAULT)
    if path is None:
        log.info("persona 파일 없음 → 내장 기본값(마야)")
        return Persona(**_from_dict(_DEFAULT))

    raw = _load_yaml(path)
    # `_DEFAULT` 는 **파일이 없을 때만** 쓰는 백업값이다. 파일과 병합하면
    # 파일이 prompt 를 비우고 ocean 만 준 경우에도 기본 '마야' 프롬프트가 들어와
    # OCEAN 생성이 무시된다(실측). 유일 예외는 llm/temperature 같은 하위 기본값이다.
    if "llm" not in raw:
        raw["llm"] = {}
    merged = {**raw}
    p = Persona(**_from_dict(merged))
    p.raw = raw
    log.info("persona=%s (%s) voice_ref=%s", p.id, path.name, p.voice_ref_wav or "(서버 기본)")
    return p


def _from_dict(d: dict[str, Any]) -> dict[str, Any]:
    llm = d.get("llm") or {}
    voice = d.get("voice") or {}
    ref = str(voice.get("ref_wav") or "").strip()
    if ref and not os.path.isabs(ref):
        ref = str((_REPO_ROOT / ref).resolve())
    # OCEAN: 0~1 로 클램프. 잘못된 값은 무시하고 기본 0.5 로 둔다.
    ocean_raw = d.get("ocean") or {}
    ocean: dict[str, float] = {}
    if isinstance(ocean_raw, dict):
        for k in _OCEAN_AXES:
            if k in ocean_raw:
                try:
                    ocean[k] = max(0.0, min(1.0, float(ocean_raw[k])))
                except (TypeError, ValueError):
                    pass
    # prompt 를 아예 안 썼다면(_DEFAULT 상속 흉내내지 않고) 빈 문자열로 두어
    # OCEAN 으로부터 생성되게 한다. 파일에 prompt 가 있으면 그쪽이 우선.
    own_prompt = str(d.get("prompt") or "").strip()
    if not own_prompt and not ocean:
        own_prompt = _DEFAULT["prompt"]
    return {
        "id": str(d.get("id") or "maya"),
        "name": str(d.get("name") or "마야"),
        "prompt": own_prompt,
        "temperature": float(llm.get("temperature", 0.7) or 0.7),
        "max_sentences": int(llm.get("max_sentences", 3) or 3),
        "voice_ref_wav": ref,
        "voice_prompt_text": str(voice.get("prompt_text") or "").strip(),
        "ocean": ocean,
        "pace": _parse_pace(d.get("pace")),
    }
