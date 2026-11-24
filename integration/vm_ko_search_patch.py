"""한국어 검색 보강 패치 (테스트 전용 — site-packages 는 건드리지 않음).

왜 필요한가
  검색 재정렬 보너스는 `voicemem.leftbrain.local_memory_store` 의 영어 정규식에만 기대고 있다.
    - `_DURATION_Q_RE` : "how long", 多久 …   ← 한국어 "몇 시/몇 년/얼마나" 불일치
    - `_DATE_Q_RE`     : "when", 哪天 …       ← 한국어 "언제/어디/누구" 불일치
    - `_DURATION_RE` / `_DATE_RE` : 기억 본문의 기간/날짜 표현 ← 한국어 "지난주/9시" 불일치
  결과적으로 한국어 질의에서는 **시간 질문 분류 자체가 안 되고**(bonus 0),
  시간 기억을 구조적으로 끌어올릴 수단이 없다. 다만 기본 정렬은 `base_score`(코사인)
  기준이라 E5 의미 검색만으로도 어느 정도는 잡힌다 — 이 패치는 그 위에 얹는 보강이다.

방식
  모듈 속성(`_DATE_Q_RE` 등)을 한국어를 포함한 합성 정규식으로 교체한다.
  `mem0_backend_store` 는 import 시점에 이름을 복사해 가지만(`from ... import _DATE_Q_RE`),
  import 가 끝난 뒤에도 **모듈 속성**을 같은 이름으로 다시 바꿔 주므로,
  그 모듈(및 이를 참조하는 곳)이 쓰는 정규식이 함께 바뀐다.
"""
from __future__ import annotations

import re

KO_DURATION_Q = r"몇\s*(?:시|시간|분|일|년|개월|달|번)|얼마나|얼마|언제부터"
KO_DATE_Q = r"언제|어느\s*(?:때|날|해|달)|무슨\s*요일|며칠|어디|누구|뭐였지|뭐더라"
KO_DURATION = (r"\d+\s*(?:년|개월|달|주|일|시간|분)\s*(?:째|동안|만에|전|후)"
               r"|(?:지난|이번|다음|저번|작년|내년|올해)\s*(?:해|년|달|주|주말|월)"
               r"|매일|매주|매달|아침|저녁|점심")
KO_DATE = (r"\d{4}\s*년\s*\d{1,2}\s*월(?:\s*\d{1,2}\s*일)?"
           r"|\d{1,2}\s*월\s*\d{1,2}\s*일"
           r"|\d{1,2}\s*시(?:\s*\d{1,2}\s*분)?"
           r"|오늘|어제|그저께|내일|모레|지난주|이번\s*주|다음\s*주")

patched: dict = {}


def install() -> dict:
    try:
        from voicemem.leftbrain import local_memory_store as lms
        try:
            from voicemem.leftbrain import mem0_backend_store as mbs
        except Exception:  # noqa: BLE001
            mbs = None
    except Exception as exc:  # noqa: BLE001
        patched["error"] = f"import 실패: {exc}"
        return patched

    def _merge(orig, extra: str) -> re.Pattern:
        return re.compile(orig.pattern + "|" + extra, orig.flags | re.IGNORECASE)

    table = {
        "_DURATION_Q_RE": KO_DURATION_Q,
        "_DATE_Q_RE": KO_DATE_Q,
        "_DURATION_RE": KO_DURATION,
        "_DATE_RE": KO_DATE,
    }
    for name, extra in table.items():
        orig = getattr(lms, name, None)
        if orig is None:
            continue
        merged = _merge(orig, extra)
        setattr(lms, name, merged)
        if mbs is not None and hasattr(mbs, name):
            setattr(mbs, name, merged)
        patched[name] = merged.pattern

    # `_content_words` 는 [a-z0-9'] 만 뽑는다 → 한국어는 실어(實語)가 0개라
    # 어휘 겹침 보너스(_LEX_WEIGHT)가 항상 0 이었다. 2글자 이상 한글 토큰도 포함.
    orig_cw = lms._content_words  # noqa: SLF001
    if not getattr(orig_cw, "_vm_ko", False):
        def _content_words(text: str) -> set[str]:  # noqa: D401
            base = orig_cw(text)
            ko = {w for w in re.findall(r"[가-힣]{2,}", text or "")}
            return base | ko

        _content_words._vm_ko = True  # type: ignore[attr-defined]
        lms._content_words = _content_words
        if mbs is not None:
            mbs._content_words = _content_words
        patched["_content_words"] = "korean tokens added"

    # `time_question_kind()` 도 같은 정규식을 모듈 전역에서 읽으므로 자동 반영된다.
    return patched


PATCHED = install()
