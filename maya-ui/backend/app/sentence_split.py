"""문장 단위 스트리밍 유닛 분리기 — LLM 토큰 스트림 → 합성 가능한 문장 단위.

왜 (s2s.cpp `src/sentence-split.h` + `docs/ARCHITECTURE.md` "Sentence splitting" 참고)
  지금은 LLM 응답 **전체**가 끝나야 TTS 를 시작한다. 실측 2문장 응답 9~15초.
  첫 문장이 완성되는 순간 합성을 시작하면
      첫 오디오까지의 시간 = **첫 문장까지의 시간**  (전체 답변 시간 아님)
  → 체감 지연이 크게 줄어든다.

s2s.cpp 에서 그대로 가져온 규칙
  1. **종결부호는 다음 문자로 확정** — "3.5" / "Mr." 같은 소수점·약어를 깨지 않는다.
     (부호만 보고 자르면 "3." + "5" 로 갈라져 숫자가 왜곡된다)
  2. **글자 수 제한으로 자르지 않는다** — 문자 수로 자르면 구문 중간이 잘린다.
     줄바꿈이 있으면 그 지점에서도 유닛을 끝낸다.
  3. **마크다운·이모지를 제거**하고, **글자/숫자가 하나도 없는 유닛은 내보내지 않는다**
     (말할 게 없는 유닛은 TTS 가 끝을 못 찾아 무음/지연이 된다 — 우리 실측과 동일)
  4. 스트림이 끝나면 남은 조각을 flush 한다.

우리 추가
  · `min_chars`: 너무 짧은 조각("네.")은 앞 문장과 합치도록 **짧으면 보류**한다.
    (캐시 대상이기도 하지만, 한 단어 문장은 억양이 어색해진다)
  · `max_chars`: 남용 방지용 상한만 둔다(절대 강제 분할은 하지 않고, 쉼표/공백에서만).

사용법
    sp = SentenceSplitter()
    for unit in sp.feed(token):   # 토큰 조각마다 호출
        await tts_and_send(unit)  # 완성된 문장
    tail = sp.flush()
    if tail:
        await tts_and_send(tail)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

#: 문장 종결로 볼 부호
_TERMINATORS = ".!?！？。…\n"

#: 약어 — 뒤에 대문자/숫자가 와도 문장 끝이 아니다(영문 혼용 대비)
_ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "e.g", "i.e",
    "no", "inc", "ltd", "co", "fig", "ex", "approx",
}

#: 마크다운 기호/꾸밈 — 음성으로 읽으면 안 되는 것
_MD_STRIP = re.compile(
    r"[`*_#>~|\[\]()]|^\s*[-•]\s+|https?://\S+",
    re.MULTILINE,
)
#: 이모지 — 발음되지 않는다
_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF"
    "\U0000FE0F\U0001F3FB-\U0001F3FF]+"
)
_HAS_ALNUM = re.compile(r"[0-9A-Za-z\uac00-\ud7a3]")


def clean(text: str) -> str:
    """마크다운·이모지를 제거하고 공백을 정리한다."""
    t = _EMOJI.sub(" ", text or "")
    t = _MD_STRIP.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def _ends_with_abbreviation(before: str) -> bool:
    """`Dr.` / `e.g.` 처럼 앞 단어가 약어면 문장 끝이 아니다."""
    m = re.search(r"([A-Za-z.]+)$", before)
    if not m:
        return False
    word = m.group(1).lower().rstrip(".")
    return word in _ABBREVIATIONS


@dataclass
class SentenceSplitter:
    """토큰 조각을 받아 완성된 문장 유닛을 돌려준다."""

    min_chars: int = 4          # 이보다 짧으면 다음 문장과 합친다
    max_chars: int = 220        # 이 이상이면 쉼표/공백에서 한 번 끊어 준다(강제 분할 아님)
    _buf: str = field(default="", init=False)
    _pending: str = field(default="", init=False)   # 너무 짧아 보류 중인 문장
    units: int = field(default=0, init=False)

    def feed(self, chunk: str) -> list[str]:
        """토큰 조각을 넣고, 이번에 완성된 유닛 목록을 받는다."""
        if not chunk:
            return []
        self._buf += chunk
        out: list[str] = []
        while True:
            cut = self._find_terminator(self._buf)
            if cut is None:
                # 종결부호가 없다 → 상한 초과 시에만 쉼표/공백에서 '보조' 분할
                if len(self._buf) >= self.max_chars:
                    soft = self._soft_break(self._buf)
                    if soft:
                        out.extend(self._emit(self._buf[:soft]))
                        self._buf = self._buf[soft:]
                break
            piece, self._buf = self._buf[: cut + 1], self._buf[cut + 1:]
            out.extend(self._emit(piece))
        return self._collect(out)

    def flush(self) -> list[str]:
        """스트림 종료 시 남은 조각을 내보낸다."""
        out: list[str] = []
        if self._buf.strip():
            out.extend(self._emit(self._buf))
            self._buf = ""
        tail = self._collect(out)
        if self._pending.strip():
            units, self._pending = [clean(self._pending)], ""
            tail.extend(u for u in units if u and _HAS_ALNUM.search(u))
        return tail

    # ── 내부 ────────────────────────────────────────────────────────────
    def _collect(self, raw_units: list[str]) -> list[str]:
        """짧은 유닛은 보류(다음 문장과 합침), 충분히 길면 방출."""
        ready: list[str] = []
        for u in raw_units:
            text = clean((self._pending + " " + u) if self._pending else u)
            self._pending = ""
            if not text or not _HAS_ALNUM.search(text):
                continue                     # 글자/숫자 없음 → 버림
            if len(text) < self.min_chars:
                self._pending = text         # 다음 문장과 합치기
                continue
            ready.append(text)
        if ready:
            self.units += len(ready)
        return ready

    def _emit(self, piece: str) -> list[str]:
        return [piece]

    def _find_terminator(self, s: str) -> int | None:
        """유효한 문장 종결 위치(인덱스) 또는 None.

        종결부호 뒤 **다음 문자**를 보고 확정한다:
          · 뒤가 공백/끝/닫는 따옴표/줄바꿈 → 문장 끝
          · 소수점(.5) 또는 소문자 이어짐 → 문장 끝 아님
        """
        n = len(s)
        for i, ch in enumerate(s):
            if ch not in _TERMINATORS:
                continue
            if ch == "\n":
                return i
            if ch == ".":
                # 소수점: 앞뒤가 숫자면 문장 끝이 아니다
                prev_digit = i > 0 and s[i - 1].isdigit()
                nxt = s[i + 1] if i + 1 < n else ""
                if prev_digit and nxt.isdigit():
                    continue
                if _ends_with_abbreviation(s[:i]):
                    continue
                if nxt and not (nxt.isspace() or nxt in "\"'”’)]}"):
                    # "etc.and" 처럼 붙어 있으면 계속
                    if nxt.isalnum() and nxt.islower():
                        continue
            if i + 1 < n:
                nxt = s[i + 1]
                if nxt in ".!?…":        # "??", "..." 같은 연속 부호는 함께 묶는다
                    continue
                if not (nxt.isspace() or nxt in "\"'”’)]}"):
                    # 부호 뒤에 곧바로 문자가 붙은 경우(비정상) → 일단 다음 부호 기다린다
                    continue
            return i
        return None

    @staticmethod
    def _soft_break(s: str) -> int:
        """상한 초과 시 쉼표/공백 위치. 없으면 0(자르지 않음)."""
        for sep in ("。", ",", "，", " "):
            idx = s.rfind(sep, 0, len(s))
            if idx > 0:
                return idx + 1
        return 0
