#!/usr/bin/env python3
"""firered_vad_client.py — FireredVAD 워커(firered_vad_worker.py)의 부모 측 클라이언트.

배경
  FireRedVAD(torch) 와 faster-whisper(ctranslate2) 는 libiomp5 를 각자 링크해
  한 프로세스에서 같이 쓰면 `OMP: Error #15` 로 죽는다(SIGABRT).
  그래서 VAD 는 **별도 프로세스**의 워커로 돌리고, 턴 엔진은 PCM 조각만 넘긴다.

인터페이스 (pcm_turn_engine.TurnEngine 에 꽂는 용도)
  vad = FireRedVadClient()          # 워커 기동 + ready 대기
  r = vad.push(pcm_chunk)           # {"speech","end","starts","frames","prob","raw_prob"}
  vad.reset()                       # 턴 시작 시 누적 상태 초기화
  vad.close()

  ready 실패(모델/패키지 없음) → available=False, push() 는 None 반환.
  호출부는 그 경우 기존 RMS 판정으로 폴백할 것.

단위 주의
  · 입력은 Int16 mono 16 kHz PCM **조각 아무 길이**(16kHz 기준 짝수 바이트).
  · 워커 내부 frame = 10 ms. `frames` 는 이번 조각이 프레임으로 바꾼 개수다.
  · `end=True` 는 "이번 조각에서 speech_end 관측" → 턴 확정 후보(아직 NAMO 판정 필요).
"""
from __future__ import annotations

import atexit
import json
import os
import struct
import subprocess
import sys
from typing import Any, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.join(_HERE, "firered_vad_worker.py")
DEFAULT_TIMEOUT_S = float(os.environ.get("MAYA_FIRERED_TIMEOUT_S", "20"))


def _read_exact(stream, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)


class FireRedVadClient:
    """워커 서브프로세스 래퍼. 실패 시 조용히 available=False."""

    def __init__(
        self,
        python: Optional[str] = None,
        model: Optional[str] = None,
        threshold: Optional[float] = None,
        min_silence_frame: Optional[int] = None,
    ) -> None:
        self.available = False
        self.info: dict[str, Any] = {}
        self._proc: Optional[subprocess.Popen] = None

        cmd = [python or os.environ.get("MAYA_FIRERED_PYTHON") or sys.executable, WORKER]
        if model:
            cmd += ["--model", model]
        if threshold is not None:
            cmd += ["--threshold", str(threshold)]
        if min_silence_frame is not None:
            cmd += ["--min-silence-frame", str(min_silence_frame)]

        try:
            self._proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, bufsize=0,
            )
            status, payload = self._recv()
        except Exception:  # noqa: BLE001 — 워커 기동 실패는 폴백 대상
            self._kill()
            return

        if status != 0 or not payload or not payload.get("ready"):
            # 패키지/모델 없음 — stderr 마지막 줄을 info 에 남긴다
            err = b""
            if self._proc and self._proc.stderr is not None:
                try:
                    err = self._proc.stderr.read() or b""
                except Exception:  # noqa: BLE001
                    pass
            self.info = {"error": (payload or {}).get("error", ""),
                         "stderr": err[-400:].decode("utf-8", "replace")}
            self._kill()
            return

        self.info = payload
        self.available = True
        atexit.register(self.close)

    # ── IPC ───────────────────────────────────────────────────────────
    def _recv(self) -> tuple[int, Optional[dict]]:
        assert self._proc and self._proc.stdout
        head = _read_exact(self._proc.stdout, 1)
        if not head:
            return 1, None
        n_raw = _read_exact(self._proc.stdout, 4)
        if len(n_raw) != 4:
            return 1, None
        n = struct.unpack(">I", n_raw)[0]
        body = _read_exact(self._proc.stdout, n)
        try:
            return head[0], json.loads(body)
        except Exception:  # noqa: BLE001
            return 1, None

    def _send(self, pcm: bytes) -> bool:
        if not self._proc or not self._proc.stdin:
            return False
        try:
            self._proc.stdin.write(struct.pack(">I", len(pcm)) + pcm)
            self._proc.stdin.flush()
            return True
        except (BrokenPipeError, OSError):
            self.available = False
            return False

    # ── 공개 API ──────────────────────────────────────────────────────
    def reset(self) -> Optional[dict]:
        if not self.available:
            return None
        if not self._send(b""):
            return None
        _, payload = self._recv()
        return payload

    def push(self, pcm: bytes) -> Optional[dict]:
        """PCM 조각 → VAD 결과. 워커가 죽었으면 None + available=False."""
        if not self.available or not pcm:
            return None
        if not self._send(pcm):
            return None
        status, payload = self._recv()
        if status != 0 or payload is None:
            self.available = False
            return None
        return payload

    def is_speech_end(self, pcm: bytes) -> bool:
        r = self.push(pcm)
        return bool(r and r.get("end"))

    def close(self) -> None:
        if self._proc is None:
            return
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        self._kill()

    def _kill(self) -> None:
        proc, self._proc = self._proc, None
        self.available = False
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":  # 자가 검증: 레퍼런스 클립으로 발화/종료 카운트
    import time
    import wave

    root = os.path.dirname(_HERE)
    ref = os.path.join(root, "maya", "models", "tts", "reference", "clip_ref_speaker_b.wav")
    t0 = time.perf_counter()
    vad = FireRedVadClient()
    print(f"[worker] available={vad.available} {vad.info} ({(time.perf_counter()-t0)*1000:.0f}ms)")
    if not vad.available:
        raise SystemExit(1)
    with wave.open(ref, "rb") as w:
        sr, pcm = w.getframerate(), w.readframes(w.getnframes())
    assert sr == 16_000, sr
    vad.reset()
    fps, chunk_ms, ends = [], 50, 0
    n = int(sr * chunk_ms / 1000) * 2
    for i in range(0, len(pcm), n):
        c = pcm[i:i + n]
        t = time.perf_counter()
        r = vad.push(c)
        fps.append((time.perf_counter() - t) * 1000)
        if r and r.get("end"):
            ends += 1
    vad.close()
    fps.sort()
    print(f"[vad] audio={len(pcm)/32000:.2f}s  ends={ends}  "
          f"p50={fps[len(fps)//2]:.2f}ms  p95={fps[int(len(fps)*0.95)]:.2f}ms  "
          f"max={fps[-1]:.2f}ms")
