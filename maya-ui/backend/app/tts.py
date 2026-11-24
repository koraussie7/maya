"""TTS 어댑터.

`integration/tts_local.py`의 `synthesize()`를 우선 사용하고, 실패/미가용 시
서버 사이드 Piper 폴백을 시도합니다. 둘 다 실패하면 빈 오디오 청크를 반환.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# backend/app/.. 의 위치 (실제 ROOT)는 maya-2/ 이다.
# app/ → backend/ → maya-ui/ → maya-2/
_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _THIS_DIR.parent.parent.parent  # maya-2/
_INTEGRATION = _PROJECT_ROOT / "integration"


def _import_integration_tts():
    """integration.tts_local을 동적으로 import.

    실제 위치는 `<repo>/integration/tts_local.py` 이고, 이 파일은
    `<repo>/maya-ui/backend/app/tts.py` 이므로 parents[3] 가 repo 루트다.
    (예전 `_PROJECT_ROOT = parent.parent` 는 `maya-ui` 를 가리켜
     integration 을 못 찾고 늘 `synthesize` 가 빈 오디오를 돌려줬다 — 2026-11-24 수정)
    """
    root = Path(__file__).resolve().parents[3]
    integration = root / "integration"
    if not integration.exists():
        log.warning("integration/ not found at %s", integration)
        return None
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(integration))
    try:
        from integration.tts_local import LocalTTS  # type: ignore
        return LocalTTS
    except Exception as exc:  # noqa: BLE001
        log.warning("integration.tts_local import failed: %s", exc)
        return None


_LocalTTSClass = _import_integration_tts()
_TTS_INSTANCE: Optional[object] = None


def _get_tts() -> Optional[object]:
    """LocalTTS 인스턴스를 한 번만 만들어 재사용."""
    global _TTS_INSTANCE
    if _LocalTTSClass is None:
        return None
    if _TTS_INSTANCE is None:
        try:
            _TTS_INSTANCE = _LocalTTSClass()
        except Exception as exc:  # noqa: BLE001
            log.exception("LocalTTS() init failed: %s", exc)
            return None
    return _TTS_INSTANCE


async def synthesize(text: str, ref_wav: str = "", prompt_text: str = "") -> bytes:
    """TTS 결과 PCM16 mono 16kHz 바이트열을 반환.

    integration/tts_local.py의 `LocalTTS.synthesize(text) -> bytes`에 위임한다.

    ref_wav / prompt_text 는 **페르소나 음색 고정**용이다.
      · 음색은 VoxCPM 클론에만 적용되고, 엔진이 piper/say 로 떨어지면 무시된다
        (tts_local 이 클론을 못 하면 자동 폴백하기 때문). 이렇게 되면 대화는 계속되지만
        목소리가 달라진다 — 로그로 알 수 있게 경고를 남긴다.
    """
    tts = _get_tts()
    if tts is None:
        return b""
    try:
        kw = {}
        if ref_wav:
            kw["ref_wav"] = ref_wav
        if prompt_text:
            kw["prompt_text"] = prompt_text
        try:
            out = tts.synthesize(text, **kw) if kw else tts.synthesize(text)
        except TypeError:
            # 구버전 LocalTTS 시그니처(synthesize(text))만 받는 경우
            log.warning("LocalTTS 가 ref_wav 를 받지 않음 → 기본 음색으로 합성")
            out = tts.synthesize(text)
        if not out:
            return b""
        engine = getattr(tts, "engine", "?")
        if ref_wav and engine != "voxcpm-api":
            log.warning("클론 미적용(engine=%s) — TTS_ENGINE=voxcpm-api 및 VOXCPM_API_URL 확인", engine)
        return out
    except Exception as exc:  # noqa: BLE001
        log.exception("synthesize() failed: %s", exc)
        return b""


def say_fallback(text: str) -> bool:
    """macOS `say` 폴백 (서버 로컬에서 디버그용)."""
    if shutil.which("say") is None:
        return False
    try:
        subprocess.run(["say", text], check=False, timeout=30)
        return True
    except Exception:
        return False
