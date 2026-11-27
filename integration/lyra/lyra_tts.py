"""Lyra TTS: Supertonic 3 기반 보이스 클로닝 옵션.

Maya 의 메인 TTS 경로(voxcpm/piper/say)는 **무변경**. 이 모듈은 "Lyra 음성" 이라는
명시적 옵션이 켜졌을 때만 호출된다 (백엔드에서 ``synthesize_lyra()`` 진입).

Supertonic 3 시그니처 (v1.3.1)
-------------------------------
* ``TTS(model="supertonic-3", model_dir=..., auto_download=True, intra_op_num_threads=...)``
* ``tts.voice_style_names`` : 사용 가능한 베이스 보이스 리스트 ("F1".."F5", "M1".."M5")
* ``tts.get_voice_style(name)`` / ``tts.get_voice_style_from_path(json_path)``
* ``tts.synthesize(text, voice_style, total_steps=8, speed=1.05,
                   max_chunk_length=None, silence_duration=0.3,
                   lang=None, verbose=False) -> (wav: np.ndarray, sr: int)``
* ``tts.save_audio(wav, output_path)`` : 44.1kHz 16-bit wav 저장
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

import numpy as np

log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# 경로 / 상수
# ──────────────────────────────────────────────────────────────────────────────
_THIS_DIR = Path(__file__).resolve().parent                 # integration/lyra/
_VOICES_DIR = _THIS_DIR / "voices"                          # 보이스 스타일 json 저장
_VOICES_DIR.mkdir(parents=True, exist_ok=True)

# "Lyra" 에 가장 가까운 Supertonic 내장 보이스 — 여성 차분 톤
LYRA_BASE_VOICE = os.environ.get("LYRA_BASE_VOICE", "F1")
LYRA_DEFAULT_SPEED = float(os.environ.get("LYRA_DEFAULT_SPEED", "1.0"))
LYRA_DEFAULT_STEPS = int(os.environ.get("LYRA_DEFAULT_STEPS", "8"))


# ──────────────────────────────────────────────────────────────────────────────
# Supertonic 인터프리터 선택
# ──────────────────────────────────────────────────────────────────────────────
def _resolve_supertonic_python() -> str:
    """Supertonic 이 설치된 python 인터프리터 경로.

    우선순위:
    1) ``$SUPERTONIC_PYTHON`` 환경변수 (명시 지정)
    2) ``supertonic`` 콘솔 스크립트가 들어있는 venv 의 ``python``
    3) 현재 인터프리터 (supertonic 패키지 import 가능 시)
    """
    explicit = os.environ.get("SUPERTONIC_PYTHON")
    if explicit and Path(explicit).exists():
        return explicit

    # venv 자동 탐지: PATH 에서 supertonic 실행파일 → 그 옆 python
    sup = shutil.which("supertonic")
    if sup:
        cand = Path(sup).resolve().parent.parent / "bin" / "python"
        if cand.exists():
            return str(cand)

    # 폴백: 현재 인터프리터
    return sys.executable


def _supertonic_importable() -> bool:
    """현재 프로세스에서 ``supertonic`` import 가능한지 (PoC 환경과 분리 시 False)."""
    try:
        import supertonic  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


# ──────────────────────────────────────────────────────────────────────────────
# 본체
# ──────────────────────────────────────────────────────────────────────────────
@dataclass
class LyraTTS:
    """Supertonic 3 기반 Lyra 보이스 합성기.

    모듈은 ``supertonic`` 이 같은 인터프리터에 설치돼 있으면 즉시 사용 가능하고,
    그렇지 않으면 ``supertonic_env()`` 로 별도 venv 를 만든 뒤 서브프로세스 호출로
    합성한다. 두 경로 모두 동일한 ``(wav, sr)`` 시그니처를 반환한다.
    """

    base_voice: str = LYRA_BASE_VOICE
    speed: float = LYRA_DEFAULT_SPEED
    total_steps: int = LYRA_DEFAULT_STEPS
    model_dir: Optional[str] = None
    lang: str = "ko"

    # ───────────────────────── 합성 ─────────────────────────
    def synthesize(
        self,
        text: str,
        *,
        voice_style_path: Optional[str] = None,
        speed: Optional[float] = None,
        lang: Optional[str] = None,
        total_steps: Optional[int] = None,
    ) -> Tuple[np.ndarray, int]:
        """텍스트를 (wav, sr) 로 합성한다.

        ``voice_style_path`` 가 주어지면 그 사용자 정의 보이스로, 아니면
        ``self.base_voice`` (기본 F1) 로 합성한다.
        """
        if not text.strip():
            raise ValueError("text is empty")

        spd = self.speed if speed is None else speed
        steps = self.total_steps if total_steps is None else total_steps
        lng = self.lang if lang is None else lang

        # 1) 같은 프로세스에 supertonic 이 있으면 직접 호출 (가장 빠름)
        if _supertonic_importable():
            return self._synthesize_inproc(
                text, voice_style_path, spd, lng, steps
            )

        # 2) 별도 venv 에서는 서브프로세스 호출 (느리지만 안전)
        return self._synthesize_subprocess(
            text, voice_style_path, spd, lng, steps
        )

    # ── in-proc 경로 ──────────────────────────────────────
    def _synthesize_inproc(
        self,
        text: str,
        voice_style_path: Optional[str],
        speed: float,
        lang: Optional[str],
        total_steps: int,
    ) -> Tuple[np.ndarray, int]:
        from supertonic import TTS  # type: ignore

        tts = TTS(
            model="supertonic-3",
            model_dir=self.model_dir,
            auto_download=False,
            intra_op_num_threads=1,
        )

        # 보이스 스타일 결정
        if voice_style_path:
            style = tts.get_voice_style_from_path(voice_style_path)
        else:
            # 안전장치: 요청 보이스가 실제 내장 보이스 리스트에 있는지 확인
            available = list(getattr(tts, "voice_style_names", []) or [])
            name = self.base_voice if self.base_voice in available else (
                available[0] if available else "F1"
            )
            style = tts.get_voice_style(name)

        wav, sr = tts.synthesize(
            text,
            voice_style=style,
            total_steps=total_steps,
            speed=speed,
            lang=lang,
            verbose=False,
        )
        return np.asarray(wav), int(sr)

    # ── subproc 경로 ──────────────────────────────────────
    def _synthesize_subprocess(
        self,
        text: str,
        voice_style_path: Optional[str],
        speed: float,
        lang: Optional[str],
        total_steps: int,
    ) -> Tuple[np.ndarray, int]:
        py = _resolve_supertonic_python()
        runner = _THIS_DIR / "_runner.py"
        args = [
            py, str(runner),
            "--text", text,
            "--base-voice", self.base_voice,
            "--speed", str(speed),
            "--steps", str(total_steps),
            "--model-dir", str(self.model_dir or ""),
        ]
        if lang:
            args += ["--lang", lang]
        if voice_style_path:
            args += ["--voice-style-path", voice_style_path]
        # 합성 결과를 stdout 의 numpy raw bytes + 1줄 헤더(JSON) 로 받음
        proc = subprocess.run(args, capture_output=True, timeout=120)
        if proc.returncode != 0:
            raise RuntimeError(
                f"lyra subprocess failed (rc={proc.returncode}): "
                f"{proc.stderr.decode('utf-8', 'ignore')}"
            )
        # 첫 줄 = 헤더(JSON): {"sr":44100,"n":12345,"dtype":"float32"}
        out = proc.stdout
        nl = out.find(b"\n")
        if nl < 0:
            raise RuntimeError("lyra subprocess: no header")
        import json
        header = json.loads(out[:nl].decode("utf-8"))
        sr = int(header["sr"])
        dtype = np.dtype(header["dtype"])
        n = int(header["n"])
        wav = np.frombuffer(out[nl + 1 : nl + 1 + n * dtype.itemsize], dtype=dtype)
        return wav, sr

    # ───────────────────────── 유틸 ─────────────────────────
    def save_wav(self, wav: np.ndarray, sr: int, output_path: str) -> None:
        """44.1kHz 16-bit mono wav 저장 (Supertonic 출력 사양과 동일)."""
        # 만약 sr 이 44.1k 가 아니면 scipy 로 리샘플 — 옵션 처리
        if sr != 44100:
            try:
                from scipy.signal import resample_poly
                # 단순 비율 리샘플 (16kHz → 44.1kHz 는 44100/16000 = 2205/800)
                from math import gcd
                g = gcd(sr, 44100)
                wav = resample_poly(wav, 44100 // g, sr // g).astype(np.float32)
                sr = 44100
            except Exception as exc:  # noqa: BLE001
                log.warning("resample skipped (scipy missing?): %s", exc)

        # float32 → int16
        peak = float(np.max(np.abs(wav))) or 1.0
        pcm16 = np.clip(wav / peak * 32767.0, -32768, 32767).astype(np.int16)

        import soundfile as sf  # type: ignore
        sf.write(output_path, pcm16, sr, subtype="PCM_16")
        log.info("lyra wav saved: %s (sr=%d, dur=%.2fs)", output_path, sr, len(pcm16) / sr)

    @staticmethod
    def list_voices() -> list[str]:
        """사용 가능한 Supertonic 내장 보이스 이름 (가능할 때만)."""
        if _supertonic_importable():
            from supertonic import TTS  # type: ignore
            tts = TTS(model="supertonic-3", auto_download=False, intra_op_num_threads=1)
            return list(getattr(tts, "voice_style_names", []) or [])
        return []
