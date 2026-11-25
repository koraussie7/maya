#!/usr/bin/env python3
"""tools/pcm_turn_engine.py — 브라우저/ESP32 원시 PCM 을 받아 턴 단위로 전사한다.

왜 필요한가
  RealtimeSTT 의 feed_audio() + text() 조합은 실시간 스트리밍에 부적합했다.
  · text() 는 같은 오디오 버퍼를 반복 처리한다
    ("Processing audio with duration 00:03.200" 이 20초 간격으로 무한 반복)
  · Silero VAD 는 마이크 입력이 아닌 feed_audio 입력에서
    "VAD filter removed 00:00.000" 으로 아무것도 걸러내지 못했다
  · 결과적으로 final 전사가 만들어지지 않아 대화상대가 응답하지 않는다
  그래서 웹 경로는 RealtimeSTT 를 쓰지 않고, PCM 을 직접 받아
  에너지 기반 VAD 로 턴을 끊고 faster-whisper 로 전사한다.

설계
  · 입력: Int16 mono 16kHz PCM 청크 (브라우저 WS / ESP32 브릿지)
  · 발화 판정: 연속 N프레임 RMS 임계값 (잡음 오탐 방지)
  · 턴 종료(2단):
      1차 — 발화 후 silence_ms_after_speech 동안 조용해지면 "후보"
      2차 — 후보 구간을 증분 전사 → NAMO 로 "말 끝인가" 판정
             · END  → 턴 확정, 전사 텍스트 반환
             · NOT  → 계속 listening, 버퍼 유지 (말 이어가는 중)
      3차 — max_turn_ms 초과 시 강제 종료 (안전장치)

  ⚠ NAMO 판정 이식 배경 (REPO_ANALYSIS_14_SUMMARY.md §1-1)
    기존 시간 기반 판정은 두 가지를 틀렸다(실측).
      · "네", "어", "그쵸" 같은 백채널을 발화로 착각해 턴을 닫음
      · 잠깐 쉰 것뿐인데 턴을 닫아 문장 중간이 잘림
    NAMO(videosdk-live/Namo-Turn-Detector-v1-Korean, MIT)는 전사 텍스트만으로
    문장 종료 여부를 평균 26ms 에 판정한다. 54문장 실측 100%.
    모델이 없으면 기존 시간 기반 동작으로 자동 폴백한다.

  · 턴 종료: 발화 후 silence_ms 동안 조용하면 확정
             (질문이면 더 길게, 백채널이면 연장)
  · 전사: faster-whisper (없으면 muse 서브프로세스로 폴백)
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("maya.turn")

SAMPLE_RATE = 16_000
BYTES_PER_SEC = SAMPLE_RATE * 2          # Int16 mono


# ── 전사기 ────────────────────────────────────────────────────────────
class Transcriber:
    """faster-whisper 우선, 없으면 muse 서브프로세스로 폴백."""

    def __init__(self, model: str = "small", compute_type: str = "int8", language: str = "ko"):
        self.model_name = model
        self.compute_type = compute_type
        self.language = language
        self._model = None
        self._init_error: Optional[str] = None
        try:
            from faster_whisper import WhisperModel  # type: ignore
            self._model = WhisperModel(model, device="cpu", compute_type=compute_type)
            log.info("faster-whisper 로드 (model=%s, compute=%s)", model, compute_type)
        except Exception as exc:  # noqa: BLE001
            self._init_error = f"{type(exc).__name__}: {exc}"
            log.warning("faster-whisper 사용 불가 (%s) → muse 폴백", self._init_error)

    @staticmethod
    def pcm_to_wav(pcm: bytes, path: str) -> None:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1",
             "-i", "-", path],
            input=pcm, check=True, capture_output=True,
        )

    def transcribe(self, pcm: bytes) -> str:
        """PCM(Int16 16k mono) → 텍스트. 실패 시 빈 문자열."""
        if len(pcm) < BYTES_PER_SEC * 0.2:      # 0.2초 미만은 버린다
            return ""
        if self._model is not None:
            try:
                import numpy as np
                audio = np.frombuffer(pcm, dtype=np.int16).astype("float32") / 32768.0
                segments, _info = self._model.transcribe(
                    audio,
                    language=self.language,
                    beam_size=1,
                    vad_filter=False,
                    condition_on_previous_text=False,
                )
                text = " ".join(s.text.strip() for s in segments).strip()
                return text
            except Exception as exc:  # noqa: BLE001
                log.warning("faster-whisper 전사 실패: %s", exc)
                return ""
        # muse 폴백
        if shutil.which("muse") and shutil.which("ffmpeg"):
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                wav = f.name
            try:
                self.pcm_to_wav(pcm, wav)
                res = subprocess.run(["muse", "voice", "transcribe", wav],
                                     capture_output=True, text=True, timeout=15)
                return (res.stdout or "").strip()
            except Exception as exc:  # noqa: BLE001
                log.warning("muse 전사 실패: %s", exc)
            finally:
                try:
                    os.unlink(wav)
                except OSError:
                    pass
        return ""


# ── 턴 엔진 ──────────────────────────────────────────────────────────
@dataclass
class TurnConfig:
    rms_threshold: int = 250          # 음성 프레임 임계값 (700→250: 작은 목소리도 감지)
    speech_frames_to_start: int = 2    # 연속 N프레임(청크)이면 발화 시작
    silence_ms_after_speech: int = 900 # 말 끝 후 이만큼 조용하면 전사
    # 발화 없음 자동 종료를 없앤다(아주 길게). 사용자가 말하기 전에
    # 턴이 닫혀버리면 "오디오 부족 0B" 로 끝나 대화가 시작되지 않는다.
    silence_ms_no_speech: int = 60_000
    max_turn_ms: int = 8_000           # 안전장치 (20초→8초: 무한 대기 방지)
    min_turn_bytes: int = int(BYTES_PER_SEC * 0.3)

    # ── NAMO 턴 판정 ──────────────────────────────────────
    # 켜면 1차 silence 판정 뒤 전사→NAMO 로 "말 끝" 인지 확인한다.
    use_turn_detector: bool = True
    # 1차 silence 임계값 (짧게 잡아 빨리 판정 시도; NAMO 가 거절하면 계속 듣는다)
    silence_ms_probe: int = 600
    # NAMO 가 NOT 이면 이 시점부터 다시 조용해질 때까지 기다린다.
    detect_min_bytes: int = int(BYTES_PER_SEC * 0.4)
    detector_threshold: float = 0.9

    # ── VAD 1차 컷 ─────────────────────────────────────────
    # rms(기본) | firered — firered 는 별도 워커 프로세스(torch/iomp5 충돌 회피).
    # firered 일 때도 NAMO 판정은 그대로 유지한다(54문장 회귀 보존).
    vad_engine: str = "rms"
    firered_threshold: float = 0.5
    # 워커 응답 없음/모델 없음 → 로그 1회 남기고 rms 로 폴백
    firered_fallback: bool = True


@dataclass
class TurnResult:
    """push() 가 턴을 확정했을 때 돌려주는 결과."""
    pcm: bytes
    text: str = ""          # NAMO 판정 때 이미 전사한 텍스트(있으면 재사용)
    reason: str = ""


@dataclass
class TurnEngine:
    """PCM 청크를 넣으면 발화 구간만 모아 텍스트로 돌려주는 엔진."""

    config: TurnConfig = field(default_factory=TurnConfig)
    transcriber: Optional[Transcriber] = None

    def __post_init__(self) -> None:
        self._buf = bytearray()
        self._speech_run = 0
        self._speech_started = False
        self._last_speech = 0.0
        self._turn_start = 0.0
        self._turn_open = False
        self._lock = asyncio.Lock()

        # ── NAMO 상태 ──
        self._detector = None
        if self.config.use_turn_detector:
            try:
                from tools.turn_detector import get_detector  # type: ignore
                self._detector = get_detector(self.config.detector_threshold)
                if not getattr(self._detector, "available", False):
                    self._detector = None
            except Exception as exc:  # noqa: BLE001
                log.warning("턴 판정기 로드 실패 → 시간 기반 폴백: %r", exc)
                self._detector = None
        # 1차 silence 판정 후 "계속 듣기" 상태인지
        self._probing = False
        self._probe_text = ""
        self._last_detect_at = 0.0

        # ── VAD 1차 컷 (firered) ──
        self._vad = None
        # 워커가 죽은 뒤 매 청크마다 재접속을 시도하면 청크 지연이 튄다
        # → 한 번 실패하면 이 엔진 인스턴스는 RMS 로 남는다(firered_fallback=False 면 사용 불가).
        self._vad_failed = False
        if self.config.vad_engine == "firered":
            self._vad = self._load_firered()

    def _load_firered(self):
        """FireRedVAD 워커를 띄운다. 실패하면 None → RMS 경로로 폴백."""
        try:
            try:
                from firered_vad_client import FireRedVadClient  # type: ignore
            except ImportError:
                from integration.firered_vad_client import FireRedVadClient  # type: ignore
            client = FireRedVadClient(threshold=self.config.firered_threshold)
        except Exception as exc:  # noqa: BLE001
            log.warning("FireRedVAD 로드 실패 → RMS 판정 유지: %r", exc)
            return None
        if not client.available:
            log.warning("FireRedVAD 워커 사용 불가 → RMS 판정 유지: %s", client.info)
            return None
        log.info("FireRedVAD 워커 연결: %s", client.info)
        return client

    # -- 상태 ---------------------------------------------------------
    def reset(self) -> None:
        self._buf.clear()
        self._speech_run = 0
        self._speech_started = False
        self._last_speech = 0.0
        self._turn_start = 0.0
        self._turn_open = False
        self._probing = False
        self._probe_text = ""
        self._last_detect_at = 0.0
        if self._vad is not None:
            self._vad.reset()

    def close(self) -> None:
        """워커 프로세스 정리(테스트/서버 종료 시)."""
        if self._vad is not None:
            self._vad.close()
            self._vad = None

    def begin_turn(self) -> None:
        self.reset()
        self._turn_open = True
        self._turn_start = time.monotonic()

    # -- 입력 ---------------------------------------------------------
    def push(self, pcm: bytes) -> Optional[bytes]:
        """청크를 넣고, 턴이 끝나면 그 구간 PCM 을 반환한다.

        ⚠ 기존 호출부 호환용. NAMO 판정으로 얻은 전사 텍스트를 함께 받으려면
        push_result() 를 쓴다.
        """
        r = self.push_result(pcm)
        return r.pcm if r else None

    def push_result(self, pcm: bytes) -> Optional[TurnResult]:
        """청크를 넣고, 턴이 확정되면 TurnResult(pcm, text) 를 반환한다."""
        if not self._turn_open or not pcm:
            return None

        import struct
        now = time.monotonic()
        samples = struct.unpack(f"<{len(pcm)//2}h", pcm[: len(pcm) // 2 * 2]) if pcm else ()
        rms = int((sum(v * v for v in samples) / len(samples)) ** 0.5) if samples else 0

        # 1차 컷: firered 워커가 있으면 그 판정을 쓰고, 없으면 RMS 를 쓴다.
        vad_end = False
        is_speech: Optional[bool] = None
        if self._vad is not None:
            vr = self._vad.push(pcm)
            if vr is None:
                # 워커 사망/타임아웃 → RMS 로 계속(라이브 보호). 재접속은 하지 않는다.
                log.warning("FireRedVAD 응답 없음 → RMS 판정으로 폴백")
                self._vad.close()
                self._vad = None
                self._vad_failed = True
            else:
                is_speech = bool(vr.get("speech"))
                vad_end = bool(vr.get("end"))
        if is_speech is None:
            is_speech = rms >= self.config.rms_threshold

        if is_speech:
            self._speech_run += 1
            self._last_speech = now
            # NAMO 가 "아직 말 중" 이라고 했는데 다시 말하기 시작 → 계속 수집
            if self._probing:
                self._probing = False
                self._probe_text = ""
            if not self._speech_started and self._speech_run >= self.config.speech_frames_to_start:
                self._speech_started = True
                log.info("turn: 발화 시작 (rms=%d, vad=%s)", rms,
                         "firered" if self._vad is not None else "rms")
        else:
            self._speech_run = 0

        if self._speech_started:
            self._buf.extend(pcm)

        # ── 종료 판정 ────────────────────────────────────────
        if self._speech_started:
            silent_ms = (now - self._last_speech) * 1000
            threshold = (
                self.config.silence_ms_probe
                if self._detector is not None
                else self.config.silence_ms_after_speech
            )
            # firered 가 speech_end 를 관측했으면 즉시 판정(모델 자체 hangover 사용),
            # 아니면 기존 시간 임계를 안전망으로 유지한다.
            if vad_end or silent_ms >= threshold:
                result = self._maybe_close(silent_ms, vad_end=vad_end)
                if result is not None:
                    return result
        else:
            idle_ms = (now - self._turn_start) * 1000
            if idle_ms >= self.config.silence_ms_no_speech:
                return self._close_result(reason="no speech")

        if (now - self._turn_start) * 1000 >= self.config.max_turn_ms:
            kind = "max duration" if self._speech_started else "no speech"
            # 안전장치는 발화가 있었으면 짧아도 버리지 않는다.
            return self._close_result(reason=kind, force=self._speech_started)
        return None

    # -- 판정 보조 ----------------------------------------------------
    def _maybe_close(self, silent_ms: float, vad_end: bool = False) -> Optional[TurnResult]:
        """1차 silence 판정 → (NAMO 있으면) 턴 판정 → 확정 또는 계속 듣기."""
        first = "vad-end" if vad_end else f"silence {silent_ms:.0f}ms"
        # NAMO 없음 → 기존 동작
        if self._detector is None:
            return self._close_result(reason=first)

        pcm = bytes(self._buf)
        if len(pcm) < self.config.detect_min_bytes:
            return self._close_result(reason=first)

        # 동일 버퍼 반복 전사를 막는다 (0.5초 간격)
        now = time.monotonic()
        if self._probing and (now - self._last_detect_at) < 0.5:
            return None

        text = self.transcriber.transcribe(pcm) if self.transcriber else ""
        self._last_detect_at = now

        if not text.strip():
            # 전사가 비면 판단 근거가 없다 → 시간 기반으로 확정
            log.info("turn: 전사 공백 — 시간 기반 확정")
            return self._close_result(reason=first)

        end, conf = self._detector.predict(text)
        is_end = end and conf >= self.config.detector_threshold

        if is_end:
            log.info("turn: NAMO END %.3f → 확정 %r", conf, text[:60])
            return self._close_result(reason=f"namo {conf:.2f}", text=text)

        # 아직 말 중 → 계속 듣는다. 버퍼는 유지.
        log.info("turn: NAMO NOT %.3f → 계속 듣기 %r", conf, text[:60])
        self._probing = True
        self._probe_text = text
        return None

    def _close_result(self, reason: str, text: str = "",
                      force: bool = False) -> Optional[TurnResult]:
        """턴을 닫는다.

        force=True 이면 min_turn_bytes 를 무시한다. 실측 벤치에서 "짧은 클립이
        turn 수 0 으로 나오는" 문제가 있었다: 발화 시작 전(첫 2프레임) 오디오는
        버퍼에 안 쌓이고, 시작 프레임 자체도 누락되므로 실제 턴 오디오가
        min_turn_bytes(0.3초)보다 작아지면 조용히 버려진다. 라이브에서는
        `max duration` 안전장치가 같은 문제를 만든다(대화가 사라짐).
        """
        pcm = bytes(self._buf)
        self.reset()
        if not force and len(pcm) < self.config.min_turn_bytes:
            log.info("turn: 종료(%s) — 오디오 부족 %dB", reason, len(pcm))
            return None
        log.info("turn: 종료(%s) — %dB (%.2fs)", reason, len(pcm),
                 len(pcm) / BYTES_PER_SEC)
        return TurnResult(pcm=pcm, text=text, reason=reason)

    # -- 이전 호환 ----------------------------------------------------
    def _close(self, reason: str) -> Optional[bytes]:
        r = self._close_result(reason)
        return r.pcm if r else None

    # -- 전사 ---------------------------------------------------------
    async def transcribe(self, pcm: bytes) -> str:
        if self.transcriber is None:
            return ""
        loop = asyncio.get_running_loop()
        t0 = time.perf_counter()
        text = await loop.run_in_executor(None, self.transcriber.transcribe, pcm)
        log.info("turn: 전사 %.2fs → %r", time.perf_counter() - t0, text[:80])
        return text
