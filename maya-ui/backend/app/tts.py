"""TTS 어댑터.

`integration/tts_local.py`의 `synthesize()`를 우선 사용하고, 실패/미가용 시
서버 사이드 Piper 폴백을 시도합니다. 둘 다 실패하면 빈 오디오 청크를 반환.

Lyra 보조 경로
--------------
`integration/lyra/` (Supertonic 3 기반)는 **별도 모듈**로 유지한다.
기존 `synthesize()` (voxcpm/piper/say 체인)는 **0% 수정**이며,
Lyra 진입점은 명시적으로 `synthesize_lyra()` 만 추가한다.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

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


# ─────────────────────────────────────────────────────────────────────
# MAYA_TTS_REF 환경변수로 화자 잠금
# ─────────────────────────────────────────────────────────────────────
#   speaker_a  → clip_ref_speaker_a.wav (기본)
#   speaker_b  → clip_ref_speaker_b.wav (f0 193.8Hz, 고음역)
#   그 외 값   → VOXCPM_REF_WAV 가 이미 처리 (0% 수정 원칙 유지)
#
# integration/tts_local.py 의 synthesize() 본문은 한 줄도 수정하지 않는다 —
# LocalTTS.__init__ 가 받는 reference_wav 를 환경변수에 따라 주입하는 방식이다.
_MAYA_REF_DIR = _PROJECT_ROOT / "maya" / "models" / "tts" / "reference"
_MAYA_REF_TABLE = {
    "speaker_a": "clip_ref_speaker_a.wav",
    "speaker_b": "clip_ref_speaker_b.wav",
}


def _resolve_env_ref_wav() -> Optional[str]:
    """MAYA_TTS_REF 환경변수를 실제 클립 경로로 변환. 미설정/미매칭 시 None."""
    key = os.environ.get("MAYA_TTS_REF", "").strip().lower()
    if not key:
        return None
    fname = _MAYA_REF_TABLE.get(key)
    if not fname:
        log.warning("MAYA_TTS_REF=%r 미지원 — speaker_a/speaker_b 중 선택", key)
        return None
    path = _MAYA_REF_DIR / fname
    if not path.exists():
        log.warning("MAYA_TTS_REF=%s → %s 없음", key, path)
        return None
    return str(path)


def _get_tts() -> Optional[object]:
    """LocalTTS 인스턴스를 한 번만 만들어 재사용."""
    global _TTS_INSTANCE
    if _LocalTTSClass is None:
        return None
    if _TTS_INSTANCE is None:
        try:
            # MAYA_TTS_PROVIDER(백엔드 설정) 를 LocalTTS 의 TTS_ENGINE 으로 넘긴다.
            # 두 이름이 다르면 지정이 무시되고 tts_local 기본값(voxcpm-api 원격)으로
            # 떨어져 첫 턴이 16~46초 걸린다(실측 2026-11-27).
            _prov = (os.environ.get("MAYA_TTS_PROVIDER", "") or "").strip().lower()
            _engine = {
                "piper": "piper",
                "pocket": "pocket",
                # voxcpm 를 직접 지정해도 tts_local 안의 게이트를 지나야 한다:
                # 게이트가 닫혀 있으면(옵트인/토큰 없음) piper 로 떨어진다.
                "voxcpm": "voxcpm-api",
                "voxcpm-api": "voxcpm-api",
                "local": "voxcpm_local",
                "lyra": "voxcpm-api",
            }.get(_prov, "")
            if _engine in ("voxcpm-api", "voxcpm_local"):
                _gate = (os.environ.get("MAYA_VOXCPM_ACTIVE", "").strip() == "1"
                         and bool(os.environ.get("MAYA_VOXCPM_HF_TOKEN")
                                  or os.environ.get("HF_TOKEN")))
                if not _gate:
                    log.warning(
                        "MAYA_TTS_PROVIDER=%s 이지만 MAYA_VOXCPM_ACTIVE=1 / HF 토큰이 "
                        "없어 piper 로 대체합니다 (voxcpm 은 원격이라 콜드스타트 "
                        "16~46초가 걸립니다)", _prov)
                    _engine = "piper"
            _kw = {"engine": _engine} if _engine else {}
            ref = _resolve_env_ref_wav()
            if ref:
                _TTS_INSTANCE = _LocalTTSClass(reference_wav=ref, **_kw)
                log.info("TTS 화자 잠금: MAYA_TTS_REF → %s", ref)
            else:
                _TTS_INSTANCE = _LocalTTSClass(**_kw)
            log.info("TTS 엔진 선택: MAYA_TTS_PROVIDER=%r → engine=%r (실제=%s)",
                     _prov or None, _engine or "auto",
                     getattr(_TTS_INSTANCE, "engine", "?"))
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

        # LocalTTS.synthesize() 는 **동기 블로킹**이다(piper 서브프로세스 /
        # 원격 HTTP). 이벤트 루프에서 직접 부르면 다른 asyncio 태스크가
        # 통째로 멈춘다(2026-11-27 실측: TTS 종료 신호를 넣는 코드조차
        # 실행되지 못해 턴이 600초 매달렸다). 그래서 스레드로 넘긴다.
        def _run() -> bytes:
            try:
                return tts.synthesize(text, **kw) if kw else tts.synthesize(text)
            except TypeError:
                # 구버전 LocalTTS 시그니처(synthesize(text))만 받는 경우
                log.warning("LocalTTS 가 ref_wav 를 받지 않음 → 기본 음색으로 합성")
                return tts.synthesize(text)

        out = await asyncio.to_thread(_run)
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


# ─────────────────────────────────────────────────────────────────────
# Lyra 보조 진입점 (Supertonic 3 기반, integration/lyra/)
# ─────────────────────────────────────────────────────────────────────
#
# 보장은 명확하다:
#   · 위쪽의 `synthesize()` 와 `say_fallback()` 는 **0% 수정**.
#   · Lyra 는 환경변수 `MAYA_TTS_LYRA=1` 일 때만 활성화.
#   · 보이스 스타일은 `integration/lyra/voices/<name>.json` (없으면 기본 F1).
#   · 실패/미설치 시 빈 바이트열을 돌려주며, 기존 체인은 그대로 동작한다.
#
# 출력 형식: integration/lyra/lyra_tts.LyraTTS.synthesize() 의 계약을 따른다.
# 즉 PCM16 mono (sr=44100 Hz) WAV 바이트열 — `tts_local` 의 16kHz 와는 다르다.
# 다운스트림이 형식을 가정하는 경우 호출자에서 리샘플링 책임을 진다.

import os  # noqa: F811  (파일 상단에서 이미 import — Lyra 섹션 하위호환 유지)

_MAYA_TTS_LYRA_FLAG = "MAYA_TTS_LYRA"  # "1" 일 때만 활성화
_MAYA_TTS_LYRA_VOICE = "MAYA_TTS_LYRA_VOICE"  # 보이스 스타일 이름 (기본 F1)
_MAYA_TTS_LYRA_SPEED = "MAYA_TTS_LYRA_SPEED"  # 속도 (기본 1.05)


def lyra_enabled() -> bool:
    """Lyra 보조 경로가 켜져 있는지."""
    return os.environ.get(_MAYA_TTS_LYRA_FLAG, "").strip() in ("1", "true", "yes")


def _import_lyra_tts():
    """integration/lyra/lyra_tts.LyraTTS 동적 import (실패 시 None)."""
    root = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(root))
    try:
        from integration.lyra.lyra_tts import LyraTTS  # type: ignore

        return LyraTTS
    except Exception as exc:  # noqa: BLE001
        log.warning("integration.lyra.lyra_tts import 실패: %s (Lyra 비활성)", exc)
        return None


_LyraClass: Optional[Any] = _import_lyra_tts()
_LyraInstance: Optional[Any] = None


def _get_lyra() -> Optional[Any]:
    """LyraTTS 인스턴스 1회 캐시."""
    global _LyraInstance
    if _LyraClass is None:
        return None
    if _LyraInstance is None:
        try:
            _LyraInstance = _LyraClass()
        except Exception as exc:  # noqa: BLE001
            log.warning("LyraTTS() init 실패: %s", exc)
            return None
    return _LyraInstance


async def synthesize_lyra(text: str, voice: str = "", speed: float = 0.0) -> bytes:
    """Lyra 음성 합성 (Supertonic 3, 44.1kHz mono PCM16 WAV 바이트열).

    · 기존 `synthesize()` (voxcpm/piper/say) 를 **변경하지 않는다**.
    · `lyra_enabled()` 이 False 이거나, 패키지가 없으면 `b""` 반환.
    · 보이스 스타일은 `integration/lyra/voices/{voice}.json` (없으면 기본 F1).
    """
    if not lyra_enabled():
        return b""
    if not text or not text.strip():
        return b""
    lyra = _get_lyra()
    if lyra is None:
        return b""
    try:
        voice_name = (voice or os.environ.get(_MAYA_TTS_LYRA_VOICE, "") or "F1").strip()
        spd = speed or float(os.environ.get(_MAYA_TTS_LYRA_SPEED, "1.05") or 1.05)

        # 보이스 스타일 파일이 있으면 파일 경로를 우선 사용, 없으면 이름만 전달
        # (이때 LyraTTS.base_voice 가 voice_name 으로 폴백되도록 한 번 갱신).
        voice_path: Optional[str] = None
        try:
            repo_root = Path(__file__).resolve().parents[3]
            cand = repo_root / "integration" / "lyra" / "voices" / f"{voice_name}.json"
            if cand.is_file():
                voice_path = str(cand)
        except Exception:  # noqa: BLE001
            voice_path = None

        # base_voice 는 인스턴스 필드라서 직접 교체하면 캐시된 인스턴스가 영향을 받는다.
        # 그 영향을 최소화하려고 파일 경로가 없을 때만 임시로 바꾼다.
        if not voice_path and getattr(lyra, "base_voice", None) != voice_name:
            try:
                setattr(lyra, "base_voice", voice_name)
            except Exception:  # noqa: BLE001
                pass

        wav, sr = lyra.synthesize(
            text=text,
            voice_style_path=voice_path,
            speed=spd,
        )
        if wav is None:
            return b""
        # WAV 바이트열로 직렬화 (soundfile 사용 — 기존 _runner.py 의 직렬화와 동일)
        try:
            import io
            import numpy as np
            import soundfile as sf  # type: ignore
            peak = float(np.max(np.abs(wav))) or 1.0
            pcm16 = np.clip(wav / peak * 32767.0, -32768, 32767).astype("int16")
            buf = io.BytesIO()
            sf.write(buf, pcm16, int(sr), format="WAV", subtype="PCM_16")
            return buf.getvalue()
        except Exception as ser_exc:  # noqa: BLE001
            log.warning("synthesize_lyra 직렬화 실패: %s", ser_exc)
            return b""
    except Exception as exc:  # noqa: BLE001
        log.warning("synthesize_lyra 실패: %s", exc)
        return b""


# ─────────────────────────────────────────────────────────────────────
# Pocket TTS 어댑터 (HF Space `/pocket_tts` Gradio 엔드포인트)
# ─────────────────────────────────────────────────────────────────────
#
# 디지털 트윈(digital-twin) 의 `pocket.py` 와 동일한 Gradio queue 호출을
# 그대로 사용한다. `gradio-client>=0.7` 가 있으면 그것으로, 없으면 내부
# `Client(...)` 가 SSE 폴링을 한다 (graceful degrade).
#
#   환경변수
#   --------
#   MAYA_TTS_PROVIDER       "pocket" / "piper" / "voxcpm" / "lyra" / "local"
#                           기본값은 config.py 의 tts_provider 필드 (=piper)
#   MAYA_TTS_VOICE          보이스 이름 (예: "alba", "default")
#   MAYA_POCKET_TTS_URL     HF Space 의 Gradio call URL
#                           (기본: https://pupunpu-jev-interaction.hf.space/gradio_api/call/pocket_tts)
#   MAYA_POCKET_TTS_TIMEOUT 합성 한 건당 최대 대기(초), 기본 30
#
# 출력 형식: WAV 바이트열 (PCM16 mono, 24kHz). 라우터에서 16kHz mono 로
# 강제 리샘플 — 기존 LocalTTS 와 형식을 맞춘다.

async def synthesize_with_provider(
    text: str,
    provider: str = "",
    voice: str = "",
    *,
    ref_wav: str = "",
    prompt_text: str = "",
) -> bytes:
    """환경변수/세션 설정으로 TTS 프로바이더를 골라서 합성한다.

    우선순위:
      1) 명시 인자 `provider` (세션 단위 override)
      2) 환경변수 `MAYA_TTS_PROVIDER`
      3) config.py 의 기본값 (현재 piper)

    Pocket TTS 는 gradio-client 가 없으면 내부 SSE 폴링으로 동작한다 —
    Pocket TTS 가 Unavailable 인 경우 piper → voxcpm 순으로 폴백한다.
    """
    import os

    from .config import SETTINGS

    prov = (provider or os.environ.get("MAYA_TTS_PROVIDER", "") or SETTINGS.tts_provider or "piper").strip().lower()
    chosen_voice = (voice or os.environ.get("MAYA_TTS_VOICE", "") or SETTINGS.tts_voice or "").strip()

    log.info("synthesize_with_provider: provider=%s voice=%s len=%d", prov, chosen_voice, len(text or ""))

    if prov == "pocket":
        out = await _synthesize_pocket(text, chosen_voice)
        if out:
            return _ensure_pcm16_mono_16k(out)
        log.warning("pocket TTS 실패 → piper 로 폴백")
        # 의도적 폴백: 아래로 진행

    if prov == "lyra":
        out = await synthesize_lyra(text, voice=chosen_voice)
        if out:
            return _ensure_pcm16_mono_16k(out)
        log.warning("lyra 실패 → piper 로 폴백")

    # 기본/폴백 경로: 기존 synthesize() (voxcpm → piper ��� say 순 자동 폴백)
    return await synthesize(text, ref_wav=ref_wav, prompt_text=prompt_text)


async def _synthesize_pocket(text: str, voice: str) -> bytes:
    """Pocket TTS 호출 — gradio-client 우선, 없으면 SSE 폴링.

    디지털 트윈 `pocket.py` 와 동일 입력 파라미터:
      · text (str)        — 합성 텍스트
      · voice (str)       — 보이스 이름 (HF Space 기본: "alba")
      · preset (str)      — "default"
      · lora (str)        — "None"
      · max_tokens (int)  — 기본 4096 (HF 기본 1200 보다는 길게 잡아둔다)
    """
    import asyncio
    import os

    from .config import SETTINGS

    if not text or not text.strip():
        return b""
    url = os.environ.get("MAYA_POCKET_TTS_URL", "").strip() or SETTINGS.pocket_tts_url
    timeout = float(os.environ.get("MAYA_POCKET_TTS_TIMEOUT", "0") or SETTINGS.pocket_tts_timeout_s)

    voice_name = voice or "alba"
    payload = [text, voice_name, "default", "None", 4096]
    fn_index = 0  # pocket_tts 는 단일 함수 → fn_index 0

    # 경로 1: gradio-client 사용 (가능하면 더 안정적)
    try:
        from gradio_client import Client, handle_file  # type: ignore
    except Exception as exc:  # noqa: BLE001
        log.info("gradio-client 없음 — SSE 폴링 경로 사용: %s", exc)
        return await _pocket_sse(url, payload, fn_index, timeout)

    try:
        client = Client(src=url, verbose=False)
        result = await asyncio.to_thread(
            client.predict,
            payload[0], payload[1], payload[2], payload[3], payload[4],
            api_name="/pocket_tts",
        )
        # predict 결과는 보통 (sample_rate, np.ndarray) 튜플, 혹은 파일 경로(str)
        if isinstance(result, tuple) and len(result) == 2:
            sr, audio = result
            return _audio_to_wav(audio, int(sr))
        if isinstance(result, str):
            try:
                with open(result, "rb") as f:
                    return f.read()
            except Exception:
                return b""
        if isinstance(result, (bytes, bytearray)):
            return bytes(result)
        return b""
    except Exception as exc:  # noqa: BLE001
        log.warning("gradio-client.predict 실패: %s — SSE 폴링으로 재시도", exc)
        return await _pocket_sse(url, payload, fn_index, timeout)


async def _pocket_sse(url: str, payload: list, fn_index: int, timeout: float) -> bytes:
    """Gradio queue API SSE 폴링 (gradio-client 미설치 시 폴백).

    디지털 트윈 `pocket.py` 의 poll 루프와 동일한 의미:
      POST {base}/call/pocket_tts  (hash + data)
      GET  {base}/call/pocket_tts/{event_id}
    """
    import asyncio
    import json
    import time

    import httpx

    # /call/<api> URL 로 base 만들기 (call_url 끝이 /gradio_api/call/pocket_tts 라고 가정)
    # 안전하게 : http(s)://host/gradio_api/call/pocket_tts 까지를 base 로
    base = url
    if "/gradio_api/call/" in url:
        base = url.split("/gradio_api/call/", 1)[0]
    call_endpoint = f"{base}/gradio_api/call/pocket_tts"

    headers = {"Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=timeout) as cx:
            # 1) submit
            r = await cx.post(
                call_endpoint,
                json={"data": payload, "fn_index": fn_index, "trigger_id": None, "session_hash": f"maya-{int(time.time()*1000)}"},
                headers=headers,
            )
            r.raise_for_status()
            evt = r.json()
            event_id = evt.get("event_id")
            if not event_id:
                log.warning("pocket SSE: event_id 없음 — 응답=%s", evt)
                return b""

            # 2) poll 결과 스트림 (SSE)
            deadline = time.monotonic() + timeout
            audio_path: Optional[str] = None
            async with cx.stream("GET", f"{call_endpoint}/{event_id}", headers={"Accept": "text/event-stream"}) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if time.monotonic() > deadline:
                        log.warning("pocket SSE: timeout %.0fs", timeout)
                        return b""
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "null":
                        continue
                    try:
                        evt_obj = json.loads(data)
                    except Exception:
                        continue
                    msg = evt_obj.get("msg", "")
                    if msg == "process_completed":
                        # output.data[0] 가 {path: "...", url: "..."} 형태 (Gradio 4.x)
                        out = evt_obj.get("output", {}).get("data", [])
                        if out and isinstance(out[0], dict):
                            audio_path = out[0].get("path") or out[0].get("url")
                        elif out and isinstance(out[0], str):
                            audio_path = out[0]
                        break
                    if msg == "process_failed":
                        log.warning("pocket SSE: process_failed detail=%s", evt_obj.get("output", {}).get("error"))
                        return b""

            if not audio_path:
                log.warning("pocket SSE: 오디오 경로 미수신")
                return b""

            # 3) audio 바이트 다운로드 — /file=... 혹은 절대 URL 모두 지원
            try:
                if audio_path.startswith("http"):
                    rr = await cx.get(audio_path)
                else:
                    # Gradio file path 는 보통 "/file=/tmp/gradio/..." 같은 상대경로
                    rr = await cx.get(f"{base}{audio_path}" if audio_path.startswith("/") else f"{base}/{audio_path}")
                rr.raise_for_status()
                return rr.content
            except Exception as exc:  # noqa: BLE001
                log.warning("pocket SSE: 오디오 다운로드 실패: %s", exc)
                return b""
    except Exception as exc:  # noqa: BLE001
        log.warning("pocket SSE 예외: %s", exc)
        return b""


def _audio_to_wav(audio, sr: int) -> bytes:
    """(sample_rate, np.ndarray) → WAV 바이트열."""
    try:
        import io
        import numpy as np
        import soundfile as sf  # type: ignore
        peak = float(np.max(np.abs(audio))) or 1.0
        if audio.dtype != np.int16:
            pcm16 = np.clip(audio / peak * 32767.0, -32768, 32767).astype("int16")
        else:
            pcm16 = audio
        buf = io.BytesIO()
        sf.write(buf, pcm16, int(sr), format="WAV", subtype="PCM_16")
        return buf.getvalue()
    except Exception as exc:  # noqa: BLE001
        log.warning("_audio_to_wav 실패: %s", exc)
        return b""


def _ensure_pcm16_mono_16k(wav_bytes: bytes) -> bytes:
    """Pocket/Lyra 출력을 Maya 표준 (PCM16 mono 16kHz) 으로 정규화.

    · 이미 16kHz mono 이면 그대로 반환
    · soundfile 이 없으면 원본 그대로 (호출자가 알아서 처리)
    """
    if not wav_bytes:
        return b""
    try:
        import io

        import numpy as np
        import soundfile as sf  # type: ignore

        data, sr = sf.read(io.BytesIO(wav_bytes), dtype="float32")
        # mono 화
        if data.ndim > 1:
            data = data.mean(axis=1)
        # 16kHz 로 리샘플
        if int(sr) != 16000:
            try:
                import scipy.signal  # type: ignore

                target_len = int(len(data) * 16000 / int(sr))
                data = scipy.signal.resample(data, target_len).astype("float32")
                sr = 16000
            except Exception:
                # 리샘플 실패 시 원본 sr 그대로 — caller 가 헤더를 해석 못 할 수 있지만
                # 최소한 다운스트림은 침묵하지 않는다.
                pass
        peak = float(np.max(np.abs(data))) or 1.0
        pcm16 = np.clip(data / peak * 32767.0, -32768, 32767).astype("int16")
        buf = io.BytesIO()
        sf.write(buf, pcm16, int(sr), format="WAV", subtype="PCM_16")
        return buf.getvalue()
    except Exception as exc:  # noqa: BLE001
        log.debug("_ensure_pcm16_mono_16k: 변환 실패(%s) — 원본 반환", exc)
        return wav_bytes
