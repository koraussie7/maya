"""Whisper 기반 STT (faster-whisper 우선, whisper 폴백, 미설치 시 no-op)."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import wave
from pathlib import Path
from typing import AsyncIterator

from .config import SETTINGS
from .protocol import ServerMsgType, WSMessage

log = logging.getLogger(__name__)

_TMP_WAV = Path("/tmp/maya_stt.wav")
_SAMPLE_RATE = 16000
_CHANNELS = 1


def _try_load_whisper():
    """설치된 Whisper 구현체 중 사용 가능한 것을 반환. 없으면 None."""
    try:
        from faster_whisper import WhisperModel  # type: ignore

        return ("faster", WhisperModel("small", device="cpu", compute_type="int8"))
    except Exception:  # noqa: BLE001
        pass
    try:
        import whisper  # type: ignore

        return ("openai", whisper.load_model("small"))
    except Exception:  # noqa: BLE001
        pass
    return None


# ── SenseVoice 워커 (메인 venv 서브프로세스) ─────────────────────────────────
# 백엔드 venv 에는 torch 가 없다 → STT 만 무거운 venv 에서 실행한다.
# 경로: MAYA_STT_WORKER_PYTHON(기본 .venv-voicemem/bin/python)
_WORKER_SCRIPT = Path(__file__).with_name("stt_sensevoice_worker.py")
_REPO = Path(__file__).resolve().parents[3]


class _SenseVoiceWorker:
    def __init__(self) -> None:
        self.proc = None
        self.meta: dict = {}

    def _python(self) -> str:
        env = os.environ.get("MAYA_STT_WORKER_PYTHON", "").strip()
        if env:
            return env
        cand = _REPO / ".venv-voicemem" / "bin" / "python"
        return str(cand) if cand.exists() else ""

    def start(self) -> bool:
        py = self._python()
        if not py or not _WORKER_SCRIPT.exists():
            return False
        try:
            self.proc = subprocess.Popen(
                [py, str(_WORKER_SCRIPT)], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
            )
            status, payload = self._read()
            if status == 2:
                self.meta = json.loads(payload.decode() or "{}")
                log.info("SenseVoice 워커 준비: %s", self.meta)
                return True
            log.warning("STT 워커 준비 실패: %s", payload[:200])
        except Exception as exc:  # noqa: BLE001
            log.warning("STT 워커 시작 실패: %s", exc)
        self.kill()
        return False

    def _read(self) -> tuple[int, bytes]:
        head = self.proc.stdout.read(5)
        if not head or len(head) < 5:
            raise RuntimeError("STT 워커 연결 끊김")
        n = int.from_bytes(head[1:5], "big")
        payload = self.proc.stdout.read(n) if n else b""
        return head[0], payload

    def transcribe(self, pcm: bytes) -> str:
        if self.proc is None or self.proc.poll() is not None:
            if not self.start():
                return ""
        self.proc.stdin.write(len(pcm).to_bytes(4, "big") + pcm)
        self.proc.stdin.flush()
        status, payload = self._read()
        if status != 0:
            log.warning("STT 워커 오류: %s", payload[:200])
            return ""
        return payload.decode("utf-8", "replace").strip()

    def kill(self) -> None:
        if self.proc is not None:
            try:
                self.proc.kill()
            except Exception:  # noqa: BLE001
                pass
            self.proc = None


_SENSEVOICE: _SenseVoiceWorker | None = None


def sensevoice_available() -> bool:
    """워커 파이썬과 스크립트가 있으면 True (실제 로딩은 첫 전사 때)."""
    w = _SenseVoiceWorker()
    return bool(w._python()) and _WORKER_SCRIPT.exists()


def _get_sensevoice() -> _SenseVoiceWorker:
    global _SENSEVOICE
    if _SENSEVOICE is None:
        _SENSEVOICE = _SenseVoiceWorker()
        _SENSEVOICE.start()
    return _SENSEVOICE


def _sensevoice_transcribe_pcm(pcm: bytes) -> str:
    return _get_sensevoice().transcribe(pcm)


def warm_stt() -> bool:
    """STT 워커/모델을 미리 띄워 둔다(첫 발화 지연 제거).

    SenseVoice 워커가 있으면 그것만 올린다 — 워커 기동 + 모델 로드가
    첫 전사에서 수 초를 먹기 때문. 블로킹 호출이므로 스레드에서 부를 것.
    반환: 무엇이든 준비됐으면 True.
    """
    if sensevoice_available():
        try:
            got = _get_sensevoice()
            if got.proc is not None and got.proc.poll() is None:
                log.info("STT 예열 완료: SenseVoice %s", got.meta or "")
                return True
        except Exception as exc:  # noqa: BLE001
            log.warning("STT 예열 실패: %s", exc)
    return _WHISPER is not None


_WHISPER = _try_load_whisper()


def whisper_available() -> bool:
    return _WHISPER is not None


def transcribe_wav(wav_path: Path) -> str:
    """동기 전사 (백그라운드 태스크에서 호출).

    우선순위: SenseVoice 워커(한국어, 이미 쓰고 있는 모델) → Whisper → 빈 문자열.
    워커가 있으면 whisper 미설치 환경에서도 한국어 전사가 된다.
    """
    if sensevoice_available():
        try:
            with wave.open(str(wav_path), "rb") as w:
                pcm = w.readframes(w.getnframes())
            text = _sensevoice_transcribe_pcm(pcm)
            if text:
                return text
            log.warning("SenseVoice 전사가 비었음 → Whisper 폴백 시도")
        except Exception as exc:  # noqa: BLE001
            log.warning("SenseVoice 전사 실패(%s) → Whisper 폴백", exc)
    if _WHISPER is None:
        log.warning("Whisper not installed; returning empty transcript")
        return ""
    kind, model = _WHISPER
    if kind == "faster":
        segments, _info = model.transcribe(str(wav_path), language="ko", beam_size=1)
        return "".join(seg.text for seg in segments).strip()
    if kind == "openai":
        result = model.transcribe(str(wav_path), language="ko", fp16=False)
        return (result.get("text") or "").strip()
    return ""


async def stream_partial(text: str) -> WSMessage:
    return WSMessage(
        type=ServerMsgType.STT_PARTIAL.value,
        payload={"text": text},
    )


async def stream_final(text: str) -> WSMessage:
    return WSMessage(
        type=ServerMsgType.STT_FINAL.value,
        payload={"text": text},
    )


async def transcribe_stream(
    pcm_chunks: AsyncIterator[bytes],
) -> AsyncIterator[WSMessage]:
    """오디오 청크를 받아서 전사 결과를 WSMessage로 흘려보냄.

    간단한 구현: 청크를 누적해 발화 종료 시점에 한 번 전사.
    실시간 partial이 필요하면 VAD + 부분 전사로 확장.
    """
    import wave

    buf = bytearray()
    async for chunk in pcm_chunks:
        buf.extend(chunk)
        if len(buf) > _SAMPLE_RATE * 2 * 30:
            # 30초 이상 누적 시 강제 플러시
            break
    if not buf:
        return
    _TMP_WAV.write_bytes(bytes(buf))
    # 헤더 붙여서 wav 저장
    with wave.open(str(_TMP_WAV.with_suffix(".wav")), "wb") as wf:
        wf.setnchannels(_CHANNELS)
        wf.setsampwidth(2)
        wf.setframerate(_SAMPLE_RATE)
        wf.writeframes(bytes(buf))
    text = transcribe_wav(_TMP_WAV.with_suffix(".wav"))
    yield await stream_final(text)
