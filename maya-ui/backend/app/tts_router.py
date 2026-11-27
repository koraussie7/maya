"""TTS 라우터 — Pocket / Piper / VoxCPM 우선순위 + 가용성 자동 폴백.

기존 `app/tts.py` 의 `synthesize(text, ref_wav, prompt_text)` 시그니처는
**0% 유지**된다. 이 모듈은 `synthesize_with_provider()` 라는 얇은 shim 을
추가만 한다:

    await synthesize_with_provider(text, provider="pocket", voice_pref="male")

라우터는:
  · `MAYA_TTS_PROVIDERS` (csv) 로 우선순위 결정 (기본: pocket,piper,voxcpm)
  · 부팅 시 가용성 체크 → 죽은 프로바이더는 자동 제외
  · 동시성 제한(`asyncio.Semaphore`, 기본 2) — HF Space 보호
  · 폴백 시 `provider_used` 메타데이터를 audio.out 페이로드에 실어 보낸다

호출자(예: session.py) 는 `synthesize_with_provider()` 결과를 그대로
오디오 청크로 송출하면 된다.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger(__name__)


# ──────────────────────────────────────
# 우선순위 + 프로바이더 메타
# ──────────────────────────────────────
_PROVIDERS_ENV = "MAYA_TTS_PROVIDERS"
# 가용한 프로바이더 식별자 — Pocket / Piper(시스템 폴백) / VoxCPM(클론)
_PROVIDER_POCKET = "pocket"
_PROVIDER_PIPER = "piper"
_PROVIDER_VOXCPM = "voxcpm"
_VALID = (_PROVIDER_POCKET, _PROVIDER_PIPER, _PROVIDER_VOXCPM)


def _resolve_priority() -> tuple[str, ...]:
    """환경변수에서 우선순위 결정. 모르는 값은 버리고, 비면 기본."""
    raw = os.environ.get(_PROVIDERS_ENV, f"{_PROVIDER_POCKET},{_PROVIDER_PIPER},{_PROVIDER_VOXCPM}")
    items = tuple(p.strip().lower() for p in raw.split(",") if p.strip())
    items = tuple(p for p in items if p in _VALID)
    return items or (_PROVIDER_POCKET, _PROVIDER_PIPER, _PROVIDER_VOXCPM)


@dataclass(slots=True)
class ProviderStatus:
    name: str
    alive: bool
    detail: str = ""
    last_check_at: float = 0.0


# ──────────────────────────────────────
# 라우터
# ──────────────────────────────────────
class TTSRouter:
    """단일 세션 또는 프로세스 단위 TTS 라우터.

    · `priority` — 시도 순서. 부팅 시 alive 체크로 죽은 것은 skip.
    · `voice_pref` — UI 음성 메뉴에서 선택한 음성 id (예:male", "piper", "voxcpm").
      우선순위와 **모두** 매칭되면 그 프로바이더로 시도하고, 아니면 priority 순.
    · 동시성 제한 — HF Space 보호용. Piper/VoxCPM 은 1로 둔다 (in-process).
    """

    def __init__(self, priority: tuple[str, ...] | None = None) -> None:
        self.priority = priority or _resolve_priority()
        self._status: dict[str, ProviderStatus] = {}
        # 동시성: pocket(원격) 만 보호한다.
        self._pocket_sema = asyncio.Semaphore(int(os.environ.get("MAYA_TTS_POCKET_CONCURRENCY", "1")))
        # pocket 클라이언트는 lazy
        self._pocket = None  # type: ignore[assignment]
        # 마지막 가용성 체크 시각
        self._last_check = 0.0
        self._check_interval_s = float(os.environ.get("MAYA_TTS_CHECK_INTERVAL_S", "60"))

    # ── 가용성 ────────────────────────────────────────────────────────
    async def check_alive(self, force: bool = False) -> dict[str, ProviderStatus]:
        """모든 프로바이더의 가용성을 체크하고 캐시한다.

        `force=True` 면 무조건 다시 본다. 그 외에는 마지막 체크로부터
        `MAYA_TTS_CHECK_INTERVAL_S` 가 지나야 다시 본다.
        """
        now = time.monotonic()
        if not force and (now - self._last_check) < self._check_interval_s and self._status:
            return self._status
        from . import tts_pocket
        from .tts import _get_tts  # 내부 캐시 — init 성공 여부로 alive 추정

        self._status = {}

        # pocket — 원격 체크
        st = ProviderStatus(name=_PROVIDER_POCKET, alive=False)
        if _PROVIDER_POCKET in self.priority:
            try:
                st.alive = await asyncio.to_thread(tts_pocket.is_alive)
                if not st.alive:
                    st.detail = "pocket HF Space 응답 없음"
            except Exception as exc:  # noqa: BLE001
                st.detail = f"pocket check 실패: {exc.__class__.__name__}"
        self._status[_PROVIDER_POCKET] = st

        # piper — LocalTTS 캐시가 init 되었는지
        st_p = ProviderStatus(name=_PROVIDER_PIPER, alive=False)
        if _PROVIDER_PIPER in self.priority:
            try:
                tts = _get_tts()
                st_p.alive = tts is not None
                if not st_p.alive:
                    st_p.detail = "integration.tts_local import 실패 또는 piper 모델 없음"
            except Exception as exc:  # noqa: BLE001
                st_p.detail = f"piper check 실패: {exc.__class__.__name__}"
        self._status[_PROVIDER_PIPER] = st_p

        # voxcpm — LocalTTS.engine 이 voxcpm-api 인지
        st_v = ProviderStatus(name=_PROVIDER_VOXCPM, alive=False)
        if _PROVIDER_VOXCPM in self.priority:
            try:
                tts = _get_tts()
                if tts is not None:
                    eng = getattr(tts, "engine", "")
                    st_v.alive = (str(eng).startswith("voxcpm"))
                    if not st_v.alive:
                        st_v.detail = f"engine={eng or '?'} — voxcpm 아님 (TTS_ENGINE=voxcpm-api)"
            except Exception as exc:  # noqa: BLE001
                st_v.detail = f"voxcpm check 실패: {exc.__class__.__name__}"
        self._status[_PROVIDER_VOXCPM] = st_v

        self._last_check = now
        self._log_status()
        return self._status

    def _log_status(self) -> None:
        alive = [n for n, s in self._status.items() if s.alive]
        log.info("tts_router priority=%s active=%s", self.priority, alive or ["(none)"])
        for n, s in self._status.items():
            if not s.alive and s.detail:
                log.info("  %s inactive — %s", n, s.detail)

    # ── 합성 ──────────────────────────────────────────────────────────
    async def synthesize(
        self,
        text: str,
        *,
        voice_pref: str = "",
        ref_wav: str = "",
        prompt_text: str = "",
    ) -> tuple[bytes, str]:
        """`text` 를 (bytes, provider_used) 로 합성한다.

        우선순위:
          1. voice_pref 가 우선순위 프로바이더 id 와 일치하면 그것부터 시도
          2. 아니면 priority 순으로 첫 alive 프로바이더로 시도
          3. 다 실패하면 piper 로 최후 폴백(piper 자체도 실패면 빈 bytes)
        """
        if not text or not text.strip():
            return b"", ""

        # 음성 선호 → 프로바이더 매핑
        pref_provider = self._voice_pref_to_provider(voice_pref)

        # 1) 가용성 (캐시가 비었거나 만료면 즉시)
        await self.check_alive()
        alive_providers = [n for n, s in self._status.items() if s.alive]

        # 후보 순서 만들기
        ordered: list[str] = []
        if pref_provider in alive_providers:
            ordered.append(pref_provider)
        for p in self.priority:
            if p in alive_providers and p not in ordered:
                ordered.append(p)
        # 최후 폴백 (piper)
        if not ordered and _PROVIDER_PIPER in self.priority:
            ordered.append(_PROVIDER_PIPER)

        # 2) 순서대로 시도
        last_err: str = ""
        for name in ordered:
            try:
                out = await self._call(name, text, voice_pref=voice_pref,
                                       ref_wav=ref_wav, prompt_text=prompt_text)
                if out:
                    if name != pref_provider:
                        log.info("tts fallback: %s → %s (voice_pref=%s)",
                                 pref_provider or "(none)", name, voice_pref)
                    return out, name
            except Exception as exc:  # noqa: BLE001
                last_err = f"{name}: {exc.__class__.__name__}"
                log.warning("tts provider %s 실패: %s", name, exc)
                continue

        log.warning("모든 TTS 프로바이더 실패 (text=%.20s…, last=%s)", text, last_err)
        return b"", ""

    async def _call(
        self,
        provider: str,
        text: str,
        *,
        voice_pref: str = "",
        ref_wav: str = "",
        prompt_text: str = "",
    ) -> bytes:
        """프로바이더별 합성 위임."""
        if provider == _PROVIDER_POCKET:
            return await self._call_pocket(text, voice_pref=voice_pref)
        # piper / voxcpm 은 기존 synthesize() 로 위임 — 단일 시그니처 보장
        from .tts import synthesize
        return await synthesize(text, ref_wav=ref_wav, prompt_text=prompt_text)

    async def _call_pocket(self, text: str, *, voice_pref: str) -> bytes:
        """pocket 합성 — 동시성 보호 + cold-start 보호."""
        from . import tts_pocket
        if self._pocket is None:
            self._pocket = tts_pocket.MayaPocketTTS()
        async with self._pocket_sema:
            out = await self._pocket.synthesize(text, voice=voice_pref or "default")
            if out is None:
                # 가용성 false 로 마킹 → 다음 호출에서 재시도 가능
                if _PROVIDER_POCKET in self._status:
                    self._status[_PROVIDER_POCKET].alive = False
                    self._status[_PROVIDER_POCKET].detail = "합성 실패 (cold-start 또는 Space down)"
                raise RuntimeError("pocket 합성 실패")
            return out

    @staticmethod
    def _voice_pref_to_provider(voice_pref: str) -> str:
        """UI 의 voice_pref 를 프로바이더 id 로 변환.

        · 'piper' / 'voxcpm' / 'pocket' / 'male' / 'female' / 'default' / 'clone'
          → 매핑된 프로바이더. 모르는 값이면 빈 문자열(= 선호 없음).
        """
        v = (voice_pref or "").strip().lower()
        if not v:
            return ""
        if v in _VALID:
            return v
        # Pocket 의 음성 슬롯
        if v in ("default", "male", "female", "clone"):
            return _PROVIDER_POCKET
        return ""

    # ── UI 노출용 ─────────────────────────────────────────────────────
    def describe(self) -> dict[str, Any]:
        """프론트엔드 `session.ready` payload 에 실어 보낼 정보."""
        alive = {n: (self._status.get(n) or ProviderStatus(name=n, alive=False)).alive
                 for n in self.priority}
        return {
            "priority": list(self.priority),
            "alive": alive,
            "voices": self._voices_for_ui(),
        }

    @staticmethod
    def _voices_for_ui() -> list[dict[str, str]]:
        """프론트에 노출할 음성 메뉴. import 시점 의존성을 줄이기 위해
        tts_pocket 가 있으면 그쪽 카탈로그를, 없으면 정적 기본값을 쓴다."""
        try:
            from . import tts_pocket  # type: ignore
            return tts_pocket.list_voices()
        except Exception:  # noqa: BLE001
            return [
                {"id": "default", "label": "기본 여성 음성"},
                {"id": "male",    "label": "남성 음성"},
                {"id": "piper",   "label": "시스템 Piper (한국어 로컬)"},
            ]


# ──────────────────────────────────────
# 모듈 싱글톤 (lazy)
# ──────────────────────────────────────
_router_singleton: Optional[TTSRouter] = None


def get_router() -> TTSRouter:
    """프로세스 단위 TTSRouter 인스턴스."""
    global _router_singleton
    if _router_singleton is None:
        _router_singleton = TTSRouter()
    return _router_singleton


async def synthesize_with_provider(
    text: str,
    *,
    provider: str = "",
    voice_pref: str = "",
    ref_wav: str = "",
    prompt_text: str = "",
) -> tuple[bytes, str]:
    """편의 함수 — `synthesize()` 와 호환되는 외부 시그니처.

    Returns:
        (audio_bytes, provider_used) — provider_used 는 "pocket"/"piper"/"voxcpm"/""
    """
    # `provider` 인자가 명시되면 voice_pref 보다 우선
    voice_pref = provider or voice_pref
    router = get_router()
    return await router.synthesize(
        text, voice_pref=voice_pref, ref_wav=ref_wav, prompt_text=prompt_text,
    )
