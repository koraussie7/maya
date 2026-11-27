"""Pocket-TTS 어댑터 (HF Space `pocket-tts`).

왜 직접 어댑터를 만들까
-----------------------
digital-twin 프로젝트의 `digital_twin/tts/pocket.py` 는 1) 같은 gradio HTTP
스펙을 호출하고 2) HF Space 가 cold-start/다운 되면 즉시 죽는다. 같은 어댑터를
한 번만 작성해 두면:

  · `digital-twin/` 디렉터리 전체(0.5GB+ torch 의존) 를 가져올 필요 없다
  · `gradio-client` 한 개면 충분하다 — 가벼운 import
  · 우리 라우터가 가용성/폴백을 관리하므로 pocket 죽어도 piper 로 자동 복구

호출 스펙
---------
`pocket-tts` HF Space 의 gradio API 는 `/pocket_tts` 한 개이며 시그니처는
`(text: str, voice: str) -> (audio_path)`. `voice` 슬롯은 음성 선택 옵션:

  · "default"  — 기본 여성 음성
  · "male"     — 남성 음성
  · "clone"    — 클론 (ref_audio 가 있으면)

gradio-client 는 `predict()` 의 결과를 dict / filepath / str 등 다양하게 줄 수
있으므로 항상 bytes 로 정규화한다.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


# ──────────────────────────────────────
# 모듈-레벨 옵션
# ──────────────────────────────────────
_POCKET_URL_ENV = "POCKET_TTS_URL"
# 기본 HF Space — Kyutai 의 공개 데모. cold-start 가 자주 일어나므로
# 라우터는 1차로 Pocket, 죽으면 piper 로 폴백한다.
_POCKET_DEFAULT_URL = "https://kyutai-pocket-tts.hf.space"
_POCKET_ENDPOINT = "pocket_tts"
_POCKET_API_TIMEOUT_S = 30.0
_POCKET_ALIVE_TIMEOUT_S = 2.0


@dataclass(slots=True)
class PocketVoice:
    """음성 메뉴에 노출할 옵션."""
    id: str
    label: str
    description: str = ""


# 사용자에게 보여줄 음성 메뉴.
# label 은 한국어 친화 표기 — 프론트 드롭다운에서 그대로 쓴다.
DEFAULT_VOICES: tuple[PocketVoice, ...] = (
    PocketVoice("default", "기본 여성 음성 (Pocket TTS)"),
    PocketVoice("male",    "남성 음성 (Pocket TTS)"),
    PocketVoice("piper",   "시스템 Piper (한국어 로컬)"),
    PocketVoice("voxcpm",  "VoxCPM 클론 (페르소나 음색)"),
)


# ──────────────────────────────────────
# 가용성 체크
# ──────────────────────────────────────
def is_alive(url: str | None = None, *, timeout: float = _POCKET_ALIVE_TIMEOUT_S) -> bool:
    """Pocket-TTS Space 가 살아있는지 가볍게 확인.

    `gradio_client.Client.view_api()` 는 endpoint 메타데이터만 받아오므로
    실제 합성 없이도 1~2s 안에 응답한다. 실패하면 False.
    """
    url = (url or os.environ.get(_POCKET_URL_ENV) or _POCKET_DEFAULT_URL).strip()
    if not url:
        return False
    try:
        from gradio_client import Client  # type: ignore
    except Exception as exc:  # noqa: BLE001
        log.debug("gradio_client 미설치: %s", exc)
        return False
    try:
        Client(url).view_api(return_format="dict")
        return True
    except Exception as exc:  # noqa: BLE001
        log.info("pocket_tts(%s) 가용성 false: %s", url, exc.__class__.__name__)
        return False


# ──────────────────────────────────────
# 어댑터
# ──────────────────────────────────────
class MayaPocketTTS:
    """HF Space `pocket-tts` 를 호출하는 얇은 클라이언트.

    `synthesize(text, voice) -> bytes` 가 핵심. 결과는 WAV 바이트열이다
    (gradio 가 filepath 를 돌려주면 파일을 읽어 bytes 로 정규화).

    · `voice` 가 모르는 값이면 "default" 로 폴백한다.
    · 합성 실패 / 타임아웃 / HF cold-start 면 `None` 을 돌려준다
      (호출자가 piper 로 fallback 할 수 있도록).
    """

    def __init__(self, url: str | None = None) -> None:
        self.url = (url or os.environ.get(_POCKET_URL_ENV) or _POCKET_DEFAULT_URL).strip()
        self._client: Any | None = None
        self._lock = asyncio.Lock()

    # ── lazy client ─────────────────────────────────────────────────
    def _ensure_client(self) -> Any:
        if self._client is None:
            try:
                from gradio_client import Client  # type: ignore
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"gradio-client 가 설치되어 있지 않습니다: {exc}")
            self._client = Client(self.url)
        return self._client

    # ── 공개 API ────────────────────────────────────────────────────
    async def synthesize(
        self, text: str, voice: str = "default", *, timeout: float = _POCKET_API_TIMEOUT_S,
    ) -> Optional[bytes]:
        """`text` 를 음성으로 합성하여 WAV 바이트열을 돌려준다.

        · 실패하면 None — 호출자가 라우터의 다음 프로바이더로 폴백한다.
        · `voice` 는 Pocket 메뉴 ID (default / male / clone 등).
        """
        text = (text or "").strip()
        if not text:
            return None
        # 모르는 음성값은 default 로 폴백 (UI 메뉴 외 입력에 대비)
        voice = (voice or "default").strip() or "default"

        def _call() -> Any:
            cli = self._ensure_client()
            return cli.predict(
                text=text, voice=voice, api_name=_POCKET_ENDPOINT,
            )

        try:
            async with self._lock:
                # pocket Space 는 단일 GPU 인스턴스 → 동시성 직렬화
                raw = await asyncio.wait_for(
                    asyncio.to_thread(_call), timeout=timeout,
                )
        except asyncio.TimeoutError:
            log.warning("pocket_tts timeout (%.0fs) text=%.20s…", timeout, text)
            return None
        except Exception as exc:  # noqa: BLE001
            log.warning("pocket_tts 실패 (%s): %.20s…", exc.__class__.__name__, text)
            return None

        return _coerce_wav_bytes(raw)

    async def warm_up(self) -> bool:
        """cold-start 대비 짧은 합성. 실패는 False 로 무시."""
        try:
            out = await self.synthesize("테스트", voice="default", timeout=20.0)
            return bool(out)
        except Exception:  # noqa: BLE001
            return False


# ──────────────────────────────────────
# 결과 정규화
# ──────────────────────────────────────
def _coerce_wav_bytes(raw: Any) -> Optional[bytes]:
    """gradio-client 의 반환을 WAV bytes 로 정규화.

    gradio 는 다음 중 하나를 줄 수 있다:
      · filepath (str / Path) — 파일을 읽는다
      · dict — {"path": "..."} / {"data": "..."} 등
      · bytes — 그대로
      · None  — 합성 실패
    """
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw)
    if isinstance(raw, (str, Path)):
        p = Path(raw)
        if p.is_file():
            try:
                return p.read_bytes()
            except Exception as exc:  # noqa: BLE001
                log.warning("pocket wav 파일 읽기 실패: %s", exc)
                return None
        return None
    if isinstance(raw, dict):
        for k in ("path", "audio", "data", "url"):
            v = raw.get(k)
            if v is None:
                continue
            if isinstance(v, (bytes, bytearray)):
                return bytes(v)
            if isinstance(v, (str, Path)):
                p = Path(v)
                if p.is_file():
                    try:
                        return p.read_bytes()
                    except Exception:  # noqa: BLE001
                        continue
        return None
    if isinstance(raw, (list, tuple)) and raw:
        # gradio 가 (filepath, sr) 튜플을 줄 수도 있다
        return _coerce_wav_bytes(raw[0])
    return None


# ──────────────────────────────────────
# 보이스 카탈로그 노출 (UI 가 import 해서 쓴다)
# ──────────────────────────────────────
def list_voices() -> list[dict[str, str]]:
    """UI 에 노출할 음성 목록을 dict 리스트로 돌려준다."""
    return [{"id": v.id, "label": v.label, "description": v.description} for v in DEFAULT_VOICES]
