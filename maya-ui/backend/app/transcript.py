"""대화 기록 저장소 — **기록은 전부 남기고, 전송만 줄인다**.

왜 (2026-11-24 실측)
  `Session.messages` 에 대화를 무한 누적해 매 턴 **전체**를 LLM 에 보내고 있었다.
  100턴에서 입력 5,805 토큰/턴, 1,000턴 누적 $2.10 — 턴 수의 **제곱**으로 증가.
  반면 1,000턴 원문을 파일로 저장하는 비용은 수 MB, 사실상 0 이다.
  → **보관(store)과 전송(context)을 분리**한다.

구조
  turn 1, 2, 3 …  ──▶  JSONL(append-only, 한 줄 = 한 턴)   ← 영구 기록, 절대 삭제 안 함
                          │
                          └─ 읽기: 최근 N턴만 → LLM 컨텍스트

  · append-only JSONL: 한 줄 append 라 세션 중 끊겨도 앞 기록이 남는다.
  · **날짜 단위 대화 로그**: `<root>/<YYYY-MM-DD>/day.jsonl`
      15시간 대화는 세션이 계속 끊긴다(재접속·재시작). 기억이 세션 경계에 갈라지지 않게
      기록은 **날짜별 한 파일**에 모은다(누적 요약도 같은 날짜 기준).
  · 세션 단위 파일(`<session_id>.jsonl`)은 디버그용으로 따로 남긴다(선택).
  · 토큰 추산·요약·검색(voicemem)은 이 파일을 나아서 처리하면 된다.

요약 상태
  `<root>/<YYYY-MM-DD>/summary.json` — {summary, through_ts, turns}
  → 20턴마다 앞부분을 요약하고 `through_ts` 를 옮긴다. 이 지점 이후만 원문으로 보낸다.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ROOT = Path(os.environ.get(
    "MAYA_TRANSCRIPTS_DIR", str(_REPO_ROOT / "maya" / "transcripts")))


class TranscriptStore:
    """세션 대화의 append-only JSONL 로그(날짜 단위 + 세션 단위)."""

    def __init__(self, session_id: str, root: Path | str | None = None) -> None:
        self.session_id = session_id
        self.root = Path(root) if root else DEFAULT_ROOT
        self.turns = 0

    # ── 날짜 단위(15시간 대화의 정본) ──────────────────────────────────
    @property
    def day_dir(self) -> Path:
        return self.root / time.strftime("%Y-%m-%d")

    @property
    def day_path(self) -> Path:
        return self.day_dir / "day.jsonl"

    @property
    def summary_path(self) -> Path:
        return self.day_dir / "summary.json"

    @property
    def path(self) -> Path:
        day = time.strftime("%Y-%m-%d")
        return self.root / day / f"{self.session_id}.jsonl"

    def append(self, role: str, content: str, *, persona: str = "",
               extra: dict[str, Any] | None = None) -> None:
        """한 턴을 기록. 실패해도 대화는 계속되어야 하므로 예외를 삼킨다.

        날짜 파일(`day.jsonl`)과 세션 파일(`<session>.jsonl`) 양쪽에 쓴다.
        날짜 파일이 정본(15시간 대화·요약 기준)이고, 세션 파일은 디버그·재생용이다.
        """
        rec = {
            "ts": time.time(),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "session": self.session_id,
            "persona": persona,
            "role": role,
            "content": content,
        }
        if extra:
            rec.update(extra)
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        for target in (self.day_path, self.path):
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("a", encoding="utf-8") as f:
                    f.write(line)
            except Exception as exc:  # noqa: BLE001
                log.warning("transcript 기록 실패(%s): %s", target, exc)
        if role == "assistant":
            self.turns += 1

    # ── 요약 상태 ────────────────────────────────────────────────────────
    def load_summary(self) -> dict[str, Any]:
        """오늘의 누적 요약. 없으면 빈 요약."""
        try:
            with self.summary_path.open(encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            return {"summary": "", "through_ts": 0.0, "turns": 0}

    def save_summary(self, summary: str, through_ts: float, turns: int) -> None:
        try:
            self.summary_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.summary_path.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump({"summary": summary, "through_ts": through_ts,
                           "turns": turns, "iso": time.strftime("%Y-%m-%dT%H:%M:%S")},
                          f, ensure_ascii=False, indent=2)
            tmp.replace(self.summary_path)   # 원자적 교체(중간에 깨져도 이전 요약 유효)
        except Exception as exc:  # noqa: BLE001
            log.warning("요약 저장 실패(%s): %s", self.summary_path, exc)

    def turns_since(self, through_ts: float) -> int:
        """요약 이후 새로 쌓인 assistant 턴 수(요약 주기 판단용)."""
        n = 0
        try:
            for rec in replay(self.day_path):
                if rec.get("ts", 0) > through_ts and rec.get("role") == "assistant":
                    n += 1
        except FileNotFoundError:
            pass
        return n

    def new_since(self, through_ts: float) -> list[dict[str, Any]]:
        """요약 이후의 원문 기록(요약 입력)."""
        try:
            return [r for r in replay(self.day_path) if r.get("ts", 0) > through_ts]
        except FileNotFoundError:
            return []

    def read(self) -> list[dict[str, Any]]:
        """이 세션의 기록 전체(디버그/요약/검색 인덱싱용)."""
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out


def recent_turns(messages: list[dict[str, str]], keep_turns: int) -> list[dict[str, str]]:
    """LLM 에 보낼 컨텍스트: system + **최근 keep_turns 턴**만.

    · system 메시지는 항상 맨 앞 1개만 유지(페르소나 전환 시 교체됨).
    · 한 '턴' = user + assistant 두 메시지. assistant 없이 끝난 user 는 1턴으로 센다.
    · 기록 삭제는 하지 않는다 — 이 함수는 **사본**을 만든다.
    """
    if not messages:
        return []
    system = [m for m in messages[:1] if m.get("role") == "system"]
    rest = messages[1:] if system else list(messages)

    # 뒤에서부터 턴을 세어 잘라낸다
    kept: list[dict[str, str]] = []
    turns = 0
    for m in reversed(rest):
        kept.append(m)
        if m.get("role") == "user":
            turns += 1
            if turns >= keep_turns:
                break
    kept.reverse()
    return system + kept


def replay(path: Path) -> Iterable[dict[str, Any]]:
    """JSONL 파일을 순회(요약 배치/검색 인덱싱용)."""
    with Path(path).open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
