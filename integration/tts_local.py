#!/usr/bin/env python3
"""tts_local.py — 로컬 TTS.

엔진 우선순위 (TTS_ENGINE=auto)
  1. voxcpm-api  — OpenBMB/VoxCPM-Demo (HF Space, gradio_client). 사용자 음성 클론.
                   첫 콜드스타트 ~11s, 웜 ~3s (RTF ~8.3 실측). 자가 폴백 보유.
  2. piper       — 한국어 ko_KR-kss-medium ONNX. 첫 소리 ~276ms, RTF 0.141 (실측).
                   voxcpm 실패 시 즉시 사용.
  3. say         — macOS 내장. 첫 소리 ~3.8초 고정비용 (최후 폴백).

환경변수
  TTS_ENGINE=voxcpm-api | voxcpm_local | piper | auto    기본: voxcpm-api
  VOXCPM_ENGINE=voxcpm-api | voxcpm_local                  TTS_ENGINE=auto 일 때 사용
  VOXCPM_API_WORKER, VOXCPM_LOCAL_WORKER                   워커 경로 오버라이드
  VOXCPM_REF_WAV, VOXCPM_PROMPT_TEXT                      클론 레퍼런스/전사
  VOXCPM_CFG=2.0, VOXCPM_NORMALIZE=0, VOXCPM_DENOISE=0    HF Space 옵션

⚠ 왜 voxcpm-api 가 기본인가 (실측, 2026-11-24)
  사용자의 30초 녹음(어 분위기 확 바뀌네…)을 직접 클론하므로 piper 합성 음색이 아닌
  본인 음색으로 말한다. 인터넷이 필요하고 콜드스타트 ~11s 가 떨어지지만, 워커
  내부에서 1회 재시도 + 자가 폴백을 거치므로 안정적이다. 실패 시 piper 가 즉시
  이어받는다.

⚠ 왜 piper 를 서브프로세스로 돌리는가
  이 환경은 ctranslate2(whisper)와 onnxruntime(piper)가 각각 libiomp5.dylib 를
  링크하고 있어, 한 프로세스에서 함께 쓰면 다음이 발생한다(실측).
    OMP: Error #15: Initializing libiomp5.dylib ... already initialized.
    → piper 합성이 RuntimeError 로 실패하거나 SIGABRT
  piper 를 별도 프로세스(piper_worker.py)로 분리하면 onnxruntime 만 로드되어
  충돌이 사라진다. IPC 비용은 ~1ms 로 측정상 무시할 수 있다.

⚠ voxcpm 워커도 같은 이유로 서브프로세스 (gradio_client · ffmpeg 도 격리)
  호스트 프로세스는 libiomp5 충돌을 피하기 위해 추론 라이브러리를 직접 import 하지
  않는다. IPC 규약은 piper_worker.py 와 동일 (1B status + 4B big-endian length + payload).

⚠ 샘플레이트
  piper 는 22050Hz 로 합성한다. 서버와 브라우저는 16000Hz 를 쓴다.
  그대로 보내면 1.38배 느리고 낮게 재생되므로, PCM 을 16kHz 로 리샘플해
  돌려준다. (synthesize 의 반환 계약은 항상 "Int16 mono 16kHz")
"""
from __future__ import annotations

import array
import json
import logging
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path
from typing import Optional

_SAY_LOCK = threading.Lock()
_PIPER_LOCK = threading.Lock()
_VOXCPM_LOCK = threading.Lock()

#: Space가 동시 처리를 못 해 돌려주는 '지금은 바쁘다' 응답 패턴.
#: 폴백(say)으로 넘기면 **목소리가 바뀌고 그게 캐시**될 수 있어서, 재시도가 먼저다.
_BUSY_PATTERNS = (
    "backend is busy",
    "busy",
    "queue",
    "try again",
    "rate limit",
    "too many requests",
    "429",
)


def _is_busy(msg: str) -> bool:
    m = (msg or "").lower()
    return any(p in m for p in _BUSY_PATTERNS)


def _default_route_phrases() -> list[str]:
    """hybrid 모드가 미리 합성할 기본 상용구.

    `maya/dialog/routes.yaml` 의 `say` 를 읽으면 라우트와 자동으로 일치한다.
    파일이 없으면 내장 기본값을 쓴다 — 라우터 기본값과 같게 유지한다.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "maya", "dialog", "routes.yaml")
    phrases: list[str] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s.startswith("say:"):
                    v = s[4:].strip().strip('"').strip("'")
                    if v:
                        phrases.append(v)
    except OSError:
        pass
    if not phrases:
        phrases = ["응, 그만할게.", "알겠어, 멈췄어.", "다시 말해줄게.",
                   "천천히 말할게.", "응, 좋아.", "천만에.", "안녕. 부르면 바로 올게."]
    # 중복 제거(순서 유지)
    seen: set[str] = set()
    return [p for p in phrases if not (p in seen or seen.add(p))]

#: 합성 캐시(메모리). 디스크 사본은 _CACHE_DIR — 재시작 후에도 재사용된다.
_CACHE: dict[str, bytes] = {}
_CACHE_DIR = Path(os.environ.get(
    "MAYA_TTS_CACHE_DIR",
    os.path.join(os.path.expanduser("~"), ".cache", "maya-tts"),
))

log = logging.getLogger("tts_local")

# maya 프로젝트 루트 결정.
#   1) 우선 현재 스크립트의 부모 디렉터리를 ROOT 후보로 본다.
#   2) 그 안에 'models/' 가 있으면 그대로 ROOT.
#   3) 없으면 (예: integration/tts_local.py 처럼 한 단계 분리된 상태) 부모 디렉터리를 ROOT 로 본다.
#   4) 그것도 모델이 없으면 잘 알려진 maya 위치로 폴백.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_THIS_DIR)
_HARDCODED = "/Users/brianyeon/CodeGPT/maya"
if os.path.isdir(os.path.join(_THIS_DIR, "models")):
    _ROOT = _THIS_DIR
elif os.path.isdir(os.path.join(_PARENT, "models")):
    _ROOT = _PARENT
elif os.path.isdir(os.path.join(_HARDCODED, "models")):
    _ROOT = _HARDCODED
else:
    _ROOT = _PARENT
ROOT = _ROOT
# 워커는 이 스크립트와 같은 디렉터리(즉, integration/)에서 항상 찾는다.
# 모델/ref 가 다른 트리에 있더라도 워커는 호출 스크립트와 같은 자리에 둔다.
_WORKER_DIR = _THIS_DIR
DEFAULT_PIPER_MODEL = os.path.join(
    ROOT, "models", "tts", "piper-ko", "ko_KR-kss-medium.onnx"
)
# piper 워커는 tools/piper_worker.py 가 표준 위치 (없으면 _WORKER_DIR 에서도 찾음)
_PIPER_CANDIDATES = [
    os.environ.get("PIPER_WORKER"),
    os.path.join(ROOT, "tools", "piper_worker.py"),
    os.path.join(_WORKER_DIR, "piper_worker.py"),
]
WORKER = next((p for p in _PIPER_CANDIDATES if p and os.path.isfile(p)),
              _PIPER_CANDIDATES[1])
# voxcpm 워커는 옵션 A(HF Space API) / B(llama.cpp-omni local) 두 가지 경로를 지원한다.
VOXCPM_API_WORKER = os.environ.get(
    "VOXCPM_API_WORKER",
    os.path.join(_WORKER_DIR, "voxcpm_api_worker.py"),
)
VOXCPM_LOCAL_WORKER = os.environ.get(
    "VOXCPM_LOCAL_WORKER",
    os.path.join(_WORKER_DIR, "voxcpm_worker.py"),
)
TARGET_RATE = 16_000
# 음성 클론용 기본 레퍼런스 클립(환경변수로 오버라이드 가능)
DEFAULT_VOXCPM_REF = os.environ.get(
    "VOXCPM_REF_WAV",
    # A(본인 음성)가 기본. personas/*.yaml 의 voice.ref_wav 가 들어오면 그쪽이 우선.
    (lambda p: p if os.path.exists(p) else os.path.join(
        ROOT, "maya", "models", "tts", "reference", "clip_ref_speaker_a.wav"))(
        os.path.join(ROOT, "maya", "models", "tts", "reference", "clip_ref_speaker_a.wav")),
)
DEFAULT_VOXCPM_PROMPT = os.environ.get(
    "VOXCPM_PROMPT_TEXT",
    "어 분위기 확 바뀌네. 근데 이 새벽에 나 데려오겠다고 그렇게 쓰는 거 보면 좀 감동이네.",
)


class LocalTTS:
    """한국어 로컬 TTS. `synthesize(text) -> bytes` 는 항상 Int16 mono 16kHz."""

    def __init__(self, engine: str = "auto", voice: str = "Yuna",
                 model_path: Optional[str] = None,
                 reference_wav: Optional[str] = None,
                 prompt_text: Optional[str] = None):
        self.voice = voice
        self.model_path = model_path or os.environ.get(
            "PIPER_MODEL", DEFAULT_PIPER_MODEL
        )
        # 음성 클론용 레퍼런스 클립/프롬프트
        self.reference_wav = reference_wav or DEFAULT_VOXCPM_REF
        self.prompt_text = prompt_text or DEFAULT_VOXCPM_PROMPT
        self._piper = None
        self._piper_rate = TARGET_RATE
        self._piper_error: Optional[str] = None
        # voxcpm 워커 (옵션 A/B 양쪽 다 같은 IPC 규약)
        self._voxcpm = None
        self._voxcpm_worker_path: Optional[str] = None
        # 예열/keep-alive 상태 (스레드가 참조하므로 __init__ 에서 반드시 초기화)
        self._closing: bool = False
        self._keepalive_started: bool = False
        # 예열 완료 신호 — 첫 요청이 예열과 경쟁하지 않게 하는 게이트
        self._prewarm_done: threading.Event = threading.Event()
        # hybrid 모드: 실시간은 piper, **고정 상용구만** 클론 음성으로 사전 합성
        self._clone_for_routes: bool = False
        self._voxcpm_target_rate: int = TARGET_RATE
        self._voxcpm_meta: dict = {}
        self._voxcpm_error: Optional[str] = None

        # engine 해석 순서:
        #   TTS_ENGINE (생성자 인자) > VOXCPM_ENGINE (auto 일 때 voxcpm 디스패치) > "auto"
        # "auto" 체인: voxcpm-api → piper → say (각 단계 즉시 실패 시 다음)
        want = (engine or os.environ.get("TTS_ENGINE", "")).lower() or "auto"
        if want in ("voxcpm-api", "voxcpm_local", "voxcpm"):
            voxcpm_worker = (VOXCPM_API_WORKER
                             if want == "voxcpm-api" else VOXCPM_LOCAL_WORKER)
            if self._init_voxcpm(voxcpm_worker):
                self.engine = "voxcpm-api" if want == "voxcpm-api" else "voxcpm-local"
            else:
                # 명시 지정이면 워커 부재 → 즉시 say (piper 로 자꾸 올라가면 혼란)
                log.error("TTS_ENGINE=%s 워커 초기화 실패 → say 폴백", want)
                self.engine = "say"
        elif want == "piper":
            if self._init_piper():
                self.engine = "piper"
            else:
                log.error("TTS_ENGINE=piper 초기화 실패 → say 폴백")
                self.engine = "say"
        elif want == "say":
            self.engine = "say"
        else:  # auto
            # 1) voxcpm-api 시도
            voxcpm_kind = os.environ.get("VOXCPM_ENGINE", "voxcpm-api").lower()
            voxcpm_worker = (VOXCPM_API_WORKER
                             if voxcpm_kind != "voxcpm_local"
                             else VOXCPM_LOCAL_WORKER)
            if self._init_voxcpm(voxcpm_worker):
                self.engine = "voxcpm-api" if voxcpm_kind != "voxcpm_local" else "voxcpm-local"
            # 2) piper 시도 (voxcpm 실패 시)
            elif self._init_piper():
                log.warning("voxcpm 초기화 실패 → piper 폴백")
                self.engine = "piper"
            # 3) say
            else:
                self.engine = "say"
        # piper 를 실시간 기본으로 두고, 클론은 **고정 상용구만** 쓰는 모드
        if want == "hybrid":
            self.engine = "piper" if self._init_piper() else "say"
            self._clone_for_routes = True

        # piper 를 실시간 기본으로 두고, 클론은 **고정 상용구만** 쓰는 모드
        if want == "hybrid":
            self.engine = "piper" if self._init_piper() else "say"
            self._clone_for_routes = True
            # 사전 합성에는 VoxCPM 워커가 필요하다. piper 만 초기화되면
            # reference_wav/prompt_text 가 빈 상태라 클론 요청이
            # `requires a reference audio clip` 으로 전부 거절된다(실측).
            if not self._init_voxcpm(VOXCPM_API_WORKER):
                self._clone_for_routes = False
                log.warning("hybrid: voxcpm 초기화 실패 → 클론 상용구 비활성(piper 만)")

        log.info("TTS 엔진=%s%s (piper_rate=%s voxcpm_rate=%s ref=%s)",
                 self.engine, " +클론상용구" if self._clone_for_routes else "",
                 self._piper_rate, self._voxcpm_target_rate,
                 self.reference_wav if (self.engine.startswith("voxcpm")
                                        or self._clone_for_routes) else "-")
        # 예열: 생성 직후 백그라운드로 1회 합성(콜드스타트 선소비). 엔진 선택 후 호출.
        self.prewarm()
        # hybrid: 고정 상용구(라우트 say)를 클론 음성으로 미리 캐시한다.
        if self._clone_for_routes:
            self.prewarm_phrases()

    # ── 초기화 ────────────────────────────────────────────
    def _init_piper(self) -> bool:
        """piper_worker.py 서브프로세스를 띄운다."""
        if not os.path.isfile(WORKER):
            self._piper_error = f"워커 없음: {WORKER}"
            log.warning("piper 사용 불가 (%s)", self._piper_error)
            return False
        if not os.path.isfile(self.model_path):
            self._piper_error = f"모델 없음: {self.model_path}"
            log.warning("piper 사용 불가 (%s)", self._piper_error)
            return False
        try:
            self._piper = subprocess.Popen(
                [sys.executable, WORKER, self.model_path],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, bufsize=0,
            )
            status, payload = self._worker_read()
            if status != 0:
                raise RuntimeError(payload.decode("utf-8", "replace"))
            self._piper_rate = int(payload.decode())
            log.info("piper 워커 준비: pid=%s %s (%dHz)",
                     self._piper.pid, self.model_path, self._piper_rate)
            return True
        except Exception as exc:  # noqa: BLE001
            self._piper_error = f"{type(exc).__name__}: {exc}"
            log.warning("piper 로드 실패 (%s)", self._piper_error)
            if self._piper is not None:
                try:
                    self._piper.kill()
                except Exception:  # noqa: BLE001
                    pass
                self._piper = None
            return False

    # ── voxcpm 워커 초기화 ─────────────────────────────────
    def _init_voxcpm(self, worker_path: str) -> bool:
        """voxcpm_api_worker.py / voxcpm_worker.py 서브프로세스 기동.

        둘 다 piper_worker.py 와 동일한 IPC 규약을 따르므로 한 함수로 통합.
        ready 응답(payload=JSON)의 sample_rate_hz 로 출력 SR 을 학습한다.
        """
        if not os.path.isfile(worker_path):
            self._voxcpm_error = f"voxcpm 워커 없음: {worker_path}"
            log.warning("voxcpm 사용 불가 (%s)", self._voxcpm_error)
            return False
        try:
            # HF 토큰은 부모 프로세스 환경에서 그대로 상속
            env = os.environ.copy()
            # Space 이름은 환경변수로 갈아끼울 수 있게 넘긴다.
            # (2026-11-24: 기본 Space 가 바뀌어도 코드 수정 없이 대응)
            cmd = [sys.executable, worker_path, "--target-rate", str(TARGET_RATE)]
            space = os.environ.get("VOXCPM_SPACE", "").strip()
            if space:
                cmd += ["--space", space]
            self._voxcpm = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, bufsize=0,
                env=env,
            )
            status, payload = self._worker_read(self._voxcpm)
            if status == 1:
                raise RuntimeError(payload.decode("utf-8", "replace"))
            if status != 2:
                raise RuntimeError(f"voxcpm 워커 unexpected ready status={status}")
            try:
                meta = json.loads(payload.decode("utf-8"))
            except Exception:
                meta = {}
            self._voxcpm_target_rate = int(meta.get("sample_rate_hz") or TARGET_RATE)
            self._voxcpm_meta = meta
            self._voxcpm_worker_path = worker_path
            log.info("voxcpm 워커 준비: pid=%s worker=%s meta=%s",
                     self._voxcpm.pid, worker_path, meta)
            self._start_keepalive()
            return True
        except Exception as exc:  # noqa: BLE001
            self._voxcpm_error = f"{type(exc).__name__}: {exc}"
            log.warning("voxcpm 로드 실패 (%s)", self._voxcpm_error)
            if self._voxcpm is not None:
                try:
                    self._voxcpm.kill()
                except Exception:  # noqa: BLE001
                    pass
                self._voxcpm = None
            return False

    def _voxcpm_synth(self, text: str, ref_wav: str | None = None,
                      prompt_text: str | None = None) -> bytes:
        """voxcpm 워커에 클론 합성을 요청하고 Int16 mono 16kHz PCM 으로 정규화.

        ref_wav/prompt_text 를 인자로 주면 **이번 요청만** 그것을 쓴다(페르소나 B 전환).
        비우면 생성 시점 기본값(환경변수 VOXCPM_REF_WAV)을 쓴다.

        **in-flight 1 + 재시도** (field-notes/neurolink 의 batch-never-fan-out)
          HF Space 는 동시 처리를 못 한다 → 병렬로 보내면 `The backend is busy…` 429/400 이 온다.
          락으로 1건만 보내고, busy 응답은 **폴백(say)이 아니라 재시도** 한다.
          재시도 횟수/예산을 넘기면 그때만 폴백 — 그래야 목소리가 함부로 바뀌지 않는다.
        """
        ref = ref_wav if ref_wav is not None else self.reference_wav
        ptext = prompt_text if prompt_text is not None else self.prompt_text

        tries = max(1, int(os.environ.get("MAYA_TTS_BUSY_RETRIES", "3")))
        base_wait = float(os.environ.get("MAYA_TTS_BUSY_WAIT_S", "1.5"))
        budget = float(os.environ.get("MAYA_TTS_BUDGET_S", "45"))
        t_start = time.time()
        last_err: Exception | None = None

        for attempt in range(1, tries + 1):
            with _VOXCPM_LOCK:
                if self._voxcpm is None or self._voxcpm.poll() is not None:
                    log.warning("voxcpm 워커 종료 감지 → 재기동")
                    self._voxcpm = None
                    if not self._init_voxcpm(self._voxcpm_worker_path or VOXCPM_API_WORKER):
                        raise RuntimeError(self._voxcpm_error or "재기동 실패")
                req = {
                    "text": text,
                    "reference_wav": (ref if ref and os.path.isfile(ref) else None),
                    "prompt_text": ptext,
                    "use_prompt_text": bool(ptext),
                    "cfg": float(os.environ.get("VOXCPM_CFG", "2.0")),
                    "normalize": os.environ.get("VOXCPM_NORMALIZE", "0") == "1",
                    "denoise": os.environ.get("VOXCPM_DENOISE", "0") == "1",
                }
                data = json.dumps(req, ensure_ascii=False).encode("utf-8")
                self._voxcpm.stdin.write(struct.pack(">I", len(data)) + data)
                self._voxcpm.stdin.flush()
                status, payload = self._worker_read(self._voxcpm)

            if status == 0:
                if not payload:
                    return b""
                if self._voxcpm_target_rate == TARGET_RATE:
                    return payload
                return self._resample(payload, self._voxcpm_target_rate, TARGET_RATE)

            msg = payload.decode("utf-8", "replace")
            last_err = RuntimeError(msg)
            busy = _is_busy(msg)
            elapsed = time.time() - t_start
            if not busy or attempt >= tries or elapsed + base_wait > budget:
                # busy 가 아니거나(진짜 오류), 재시도 소진/예산 초과 → 상위에서 폴백 판단
                raise last_err
            wait = base_wait * attempt          # 1.5s → 3.0s (지수 대신 선형: Space 대기열은 짧다)
            log.warning("voxcpm busy (시도 %d/%d, %.0fs 경과) → %.1fs 후 재시도",
                        attempt, tries, elapsed, wait)
            time.sleep(wait)
        raise last_err or RuntimeError("voxcpm 실패")

    # ── 워커 IPC ──────────────────────────────────────────
    @staticmethod
    def _read_exact(stream, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = stream.read(n - len(buf))
            if not chunk:
                break
            buf.extend(chunk)
        return bytes(buf)

    def _worker_read(self, proc=None) -> tuple[int, bytes]:
        # 호출 시점에 어느 워커의 응답을 읽을지 명시적으로 받는다.
        # 기본은 piper(기존 호환)지만 voxcpm 초기화/합성 시에는 voxcpm 워커의
        # stdout 을 읽어야 한다.
        if proc is None:
            proc = self._piper
        head = self._read_exact(proc.stdout, 5)
        if len(head) < 5:
            raise RuntimeError("워커 연결 끊김")
        status = head[0]
        n = struct.unpack(">I", head[1:5])[0]
        payload = self._read_exact(proc.stdout, n) if n else b""
        if len(payload) < n:
            raise RuntimeError("워커 응답 잘림")
        return status, payload

    def _piper_synth(self, text: str) -> bytes:
        with _PIPER_LOCK:            # 워커는 단일 요청만 처리한다
            if self._piper is None or self._piper.poll() is not None:
                # 죽었으면 한 번 재기동
                log.warning("piper 워커 종료 감지 → 재기동")
                self._piper = None
                if not self._init_piper():
                    raise RuntimeError(self._piper_error or "재기동 실패")
            data = text.encode("utf-8")
            self._piper.stdin.write(struct.pack(">I", len(data)) + data)
            self._piper.stdin.flush()
            status, payload = self._worker_read()
        if status != 0:
            raise RuntimeError(payload.decode("utf-8", "replace"))
        if not payload:
            return b""
        if self._piper_rate == TARGET_RATE:
            return payload
        return self._resample(payload, self._piper_rate, TARGET_RATE)

    # ── 공개 API ──────────────────────────────────────────
    def synthesize(self, text: str, ref_wav: str = "", prompt_text: str = "") -> bytes:
        """텍스트 → Int16 mono 16kHz PCM.

        ref_wav / prompt_text 는 **페르소나 단위 음색 지정**이다(2026-11-24 추가).
        인자로 들어오면 그 요청만 그 클립을 쓴다 — 세션마다 다른 페르소나를 쓸 수 있다.
        빈 값이면 생성자 기본값(DEFAULT_VOXCPM_REF 등)을 계속 쓴다.
        """
        text = (text or "").strip()
        if not text:
            return b""
        # 인자는 "이번 요청 한정"이다. 이전 호출의 음색이남지 않도록 **지역 변수**로 쓴다
        # (self.reference_wav 를 덮어쓰면 다음 호출에서 페르소나와 다른 클립이 쓰인다).
        ref_for_call = ref_wav or self.reference_wav
        prompt_for_call = prompt_text or self.prompt_text

        # ── 캐시(대책 2) ────────────────────────────────────────────────
        # 인사·상투적 응답("네, 알겠습니다" 등)은 음성이 항상 같다.
        # 캐시하게 되면 그 턴은 합성(1~20s) 없이 **즉시** 응답한다.
        # 키에 페르소나 음색(ref/prompt)을 넣어 다른 화자와 섞이지 않게 한다.
        # 예열 중이면 첫 요청이 예열과 **경쟁하지 않도록** 게이트에서 기다린다.
        # (실측 2026-11-24: 백엔드 기동 직후 테스트에서 첫 유닛이 17.3초 — 예열과 겹쳤다)
        # 단, **이 엔진이 실제로 예열 중일 때만** 기다린다. piper/say 는 예열이 없고,
        # voxcpm 이 떠 있어도 이번 요청이 piper 면 기다릴 이유가 없다(불필요한 45초).
        using_voxcpm = self.engine in ("voxcpm-api", "voxcpm-local") and self._voxcpm is not None
        if using_voxcpm and not self._prewarm_done.is_set():
            wait = float(os.environ.get("MAYA_TTS_PREWARM_WAIT_S", "45"))
            if self._prewarm_done.wait(timeout=wait):
                log.info("voxcpm 예열 대기 완료")
            else:
                log.warning("voxcpm 예열 %.0fs 초과 — 그대로 진행", wait)
        cached = self._cache_get(text, ref_for_call or "", prompt_for_call or "")
        if cached:
            log.info("tts 캐시 hit: %s", text[:24])
            return cached
        if self.engine in ("voxcpm-api", "voxcpm-local") and self._voxcpm is not None:
            try:
                # 페르소나(예: lyra_vex_b)가 A 클립으로 되돌아가는 사고가 있었다:
                # `self.reference_wav` 가 생성 시점(DEFAULT=A)으로 고정되고, synthesize()
                # 에서 ref_wav 를 받으면 바꾸지만 **워커/엔진은 그 사이에 이미 떠 있을 수 있다**.
                # 워커 프로세스는 요청마다 reference_wav_path 를 받으므로,
                # 여기서 ref/prompt 를 **명시적으로 다시 넘긴다**.
                pcm = self._voxcpm_synth(
                    text,
                    ref_wav=ref_for_call or None,
                    prompt_text=prompt_for_call or None,
                )
                # 짧은 문장은 Space 가 **무음(0.19s, 전부 0)** 을 돌려주는 경우가 있다
                # (실측 2026-11-24). 길이만 보면 성공처럼 보이므로 에너지도 검사한다.
                if pcm and self._has_voice(pcm):
                    self._cache_put(text, ref_for_call or "", prompt_for_call or "", pcm)
                    return pcm
                log.warning("voxcpm 무음/빈 결과(%.2fs) → 문장 병합 후 재시도",
                            len(pcm) / 2 / TARGET_RATE if pcm else 0)
                pcm2 = self._voxcpm_synth(
                    text + "  그래서요.",
                    ref_wav=ref_for_call or None,
                    prompt_text=prompt_for_call or None,
                )
                if pcm2 and self._has_voice(pcm2):
                    self._cache_put(text, ref_for_call or "", prompt_for_call or "", pcm2)
                    return pcm2
                log.warning("재시도도 무음 → piper/say 폴백(목소리가 달라짐)")
            except Exception as exc:  # noqa: BLE001
                log.error("voxcpm 합성 실패 → say 폴백: %s", exc)
        if self.engine == "piper" and self._piper is not None:
            try:
                pcm = self._piper_synth(text)
                if pcm:
                    return pcm
                log.warning("piper 빈 결과 → say 폴백")
            except Exception as exc:  # noqa: BLE001
                log.error("piper 합성 실패 → say 폴백: %s", exc)
        # 폴백 결과는 **캐시하지 않는다**(위 자체에서 return 하지 않는 이유).
        # say 음성이 캐시에 들어가면 이후 짧은 응답이 영원히 다른 목소리로 나간다(실측).
        log.warning("TTS 폴백(say) — 페르소나 음색과 다름: %s", text[:24])
        return self._say(text)

    # ── 합성 캐시 (TTS 지연 대책 2) ─────────────────────────────────────
    # 짧고 고정적인 응답은 같은 오디오를 다시 만들 이유가 없다.
    #   1) 메모리 LRU(프로세스 수명) + 2) 디스크 파일(재시작 후에도 재사용)
    # 키: sha1(페르소나음색 + text) → 같은 문장이라도 화자가 다르면 다른 항목.
    CACHE_MIN_CHARS = 1
    CACHE_MAX_CHARS = 30        # 30자 이하만(인사·확인 응답). 긴 문장은 매번 달라진다.
    CACHE_MAX_BYTES = 32 * 1024 * 1024

    def _cache_key(self, text: str, ref: str, prompt: str) -> str:
        import hashlib

        h = hashlib.sha1()
        h.update((ref or "").encode())
        h.update((prompt or "").encode())
        h.update(text.strip().encode("utf-8"))
        return h.hexdigest()

    def _cache_get(self, text: str, ref: str, prompt: str) -> bytes | None:
        if os.environ.get("MAYA_TTS_CACHE", "1") != "1":
            return None
        if not (self.CACHE_MIN_CHARS <= len(text.strip()) <= self.CACHE_MAX_CHARS):
            return None
        key = self._cache_key(text, ref, prompt)
        mem = _CACHE.get(key)
        if mem:
            return mem
        path = _CACHE_DIR / f"{key}.pcm"
        try:
            if path.exists():
                data = path.read_bytes()
                _CACHE[key] = data
                return data
        except Exception:  # noqa: BLE001
            return None
        return None

    def _cache_put(self, text: str, ref: str, prompt: str, pcm: bytes) -> None:
        if os.environ.get("MAYA_TTS_CACHE", "1") != "1":
            return
        if not (self.CACHE_MIN_CHARS <= len(text.strip()) <= self.CACHE_MAX_CHARS):
            return
        if not pcm or len(pcm) > 4 * 1024 * 1024:
            return
        key = self._cache_key(text, ref, prompt)
        _CACHE[key] = pcm
        # 메모리 상한: 오래된 항목부터 버린다(dict 는 삽입 순서 유지)
        while sum(len(v) for v in _CACHE.values()) > self.CACHE_MAX_BYTES and _CACHE:
            _CACHE.pop(next(iter(_CACHE)), None)
        try:
            _CACHE_DIR.mkdir(parents=True, exist_ok=True)
            (_CACHE_DIR / f"{key}.pcm").write_bytes(pcm)
        except Exception:  # noqa: BLE001
            pass

    # ── 예열 / keep-alive (TTS 지연 대책 1/2) ───────────────────────────
    # HF Space 는 일정 시간 요청이 없으면 잔다. 그러면 다음 요청이
    # 30~60초(콜드스타트)를 먹는다 — 실측으로 첫 응답 15초 중 대부분이 이 값이다.
    #   · MAYA_TTS_PREWARM=1  → 생성 직후 짧은 문장 1회 합성(콜드스타트 선소비)
    #   · MAYA_TTS_KEEPALIVE_S=600 → 주기적으로 아주 짧은 요청으로 Space 를 깨워 둔다
    # 음성이 필요 없는 시점이므로 **별도 스레드**에서 돌려 대화를 막지 않는다.
    def prewarm_phrases(self) -> None:
        """hybrid 모드 — 라우트 상용구를 **클론 음성**으로 미리 합성해 캐시에 넣는다.

        왜: "그만", "응", "고마워" 같은 고정 응답은 문장이 변하지 않는다.
        이걸 클론으로 한 번만 만들어 두면 **목소리는 클론, 지연은 0초**가 된다.
        실시간 대화는 piper(0.2초)라 Space 대기열에 걸리지 않는다.

        문장 목록: MAYA_CLONE_PHRASES(콤마)
               또는 personas 와 함께 쓰는 기본 세트(routes.yaml 의 say)
        """
        if os.environ.get("MAYA_TTS_CLONE_PREWARM", "1") != "1":
            return
        raw = os.environ.get("MAYA_CLONE_PHRASES", "").strip()
        phrases = [p.strip() for p in raw.split(",") if p.strip()]
        if not phrases:
            phrases = _default_route_phrases()
        if not phrases:
            log.info("hybrid: 사전 합성할 상용구 없음")
            return

        def _run() -> None:
            ok = 0
            for ph in phrases:
                if self._closing:
                    return
                # 이미 캐시에 있으면 건너뛰기(재시작 시 반복 호출 방지)
                if self._cache_get(ph, self.reference_wav or "", self.prompt_text or ""):
                    ok += 1
                    continue
                try:
                    pcm = self._voxcpm_synth(ph, ref_wav=self.reference_wav or None,
                                             prompt_text=self.prompt_text or None)
                    if pcm and self._has_voice(pcm):
                        self._cache_put(ph, self.reference_wav or "",
                                        self.prompt_text or "", pcm)
                        ok += 1
                        log.info("hybrid: 클론 캐시 '%s' (%.1fs)", ph[:20],
                                 len(pcm) / 2 / TARGET_RATE)
                except Exception as exc:  # noqa: BLE001
                    log.warning("hybrid: '%s' 클론 실패(busy/한도?) → 건너뜀: %s", ph[:20], exc)
            log.info("hybrid: 상용구 %d/%d 클론 캐시 완료", ok, len(phrases))

        threading.Thread(target=_run, name="clone-prewarm", daemon=True).start()

    def prewarm(self) -> None:
        """콜드스타트를 세션 전에 소모한다(비동기, 실패는 무시)."""
        if os.environ.get("MAYA_TTS_PREWARM", "1") != "1":
            return
        if self.engine not in ("voxcpm-api", "voxcpm-local") or self._voxcpm is None:
            # piper/say 엔진이면 예열할 것이 없다. 그리고 **게이트를 즉시 열어야** 한다.
            # (안 열면 첫 합성이 45초 대기를 하고, 그 시간이 그대로 합성 시간처럼 보인다 — 실측)
            self._prewarm_done.set()
            return

        def _run() -> None:
            t0 = time.time()
            try:
                # 예열도 일반 합성과 **같은 락**을 쓴다 → 첫 실제 요청이 예열과 겹쳐
                # 동시에 워커를 호출하는 사고를 막는다(되돌리면 큐가 꼬인다).
                pcm = self._voxcpm_synth("안녕하세요.", ref_wav=self.reference_wav or None,
                                         prompt_text=self.prompt_text or None)
                log.info("voxcpm 예열 완료 %.1fs (%d bytes)", time.time() - t0, len(pcm))
            except Exception as exc:  # noqa: BLE001
                log.info("voxcpm 예열 건너뜀: %s", exc)
            finally:
                # 예열이 끝나면 대기 중인 첫 요청들을 풀어준다(실패해도 해제).
                self._prewarm_done.set()

        threading.Thread(target=_run, name="voxcpm-prewarm", daemon=True).start()

    def _start_keepalive(self) -> None:
        """주기적으로 Space 를 깨워 둔다(기본 10분). 0 이면 끔."""
        if getattr(self, "_keepalive_started", False):
            return
        try:
            period = float(os.environ.get("MAYA_TTS_KEEPALIVE_S", "600"))
        except ValueError:
            period = 600.0
        if period <= 0:
            return
        self._keepalive_started = True

        def _loop() -> None:
            while True:
                time.sleep(period)
                if self._closing or self._voxcpm is None:
                    return
                try:
                    # 아주 짧은 text — 생성 시간 최소화하면서 Space 웜업 유지
                    self._voxcpm_synth("네.", ref_wav=None, prompt_text=None)
                    log.debug("voxcpm keep-alive ok")
                except Exception:  # noqa: BLE001
                    pass

        threading.Thread(target=_loop, name="voxcpm-keepalive", daemon=True).start()

    @staticmethod
    def _has_voice(pcm: bytes) -> bool:
        """16-bit PCM 에 실제 소리가 있는지(무음 판별).

        Space 가 짧은 입력에 대해 0.19초짜리 전부-0 파형을 돌려주는 사례가 있어,
        `if pcm:` 만으로는 성공으로 오판한다(재생해도 아무 소리 안 남).
        """
        try:
            import array

            n = len(pcm) // 2
            if n < 1600:                      # 0.1초 미만은 소리로 보지 않는다
                return False
            a = array.array("h")
            a.frombytes(pcm[: n * 2])
            # 최대 진폭이 너무 작으면 무음/저잡음
            peak = max(max(a), -min(a))
            return peak > 500
        except Exception:  # noqa: BLE001
            return bool(pcm)

    @staticmethod
    def _resample(pcm: bytes, src: int, dst: int) -> bytes:
        """Int16 mono PCM 리샘플. ffmpeg 있으면 ffmpeg, 없으면 선형 보간."""
        if src == dst or not pcm:
            return pcm
        if shutil.which("ffmpeg"):
            try:
                r = subprocess.run(
                    ["ffmpeg", "-y", "-loglevel", "error",
                     "-f", "s16le", "-ar", str(src), "-ac", "1", "-i", "-",
                     "-ar", str(dst), "-ac", "1", "-f", "s16le", "-"],
                    input=pcm, capture_output=True, check=True,
                )
                if r.stdout:
                    return r.stdout
            except Exception as exc:  # noqa: BLE001
                log.warning("ffmpeg 리샘플 실패 → numpy 폴백: %s", exc)
        # numpy 선형 보간 폴백
        try:
            import numpy as np
            a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
            n_out = int(round(len(a) * dst / src))
            if n_out <= 1:
                return pcm
            x_old = np.arange(len(a), dtype=np.float32)
            x_new = np.linspace(0, len(a) - 1, n_out, dtype=np.float32)
            out = np.interp(x_new, x_old, a)
            return np.clip(out, -32768, 32767).astype(np.int16).tobytes()
        except Exception as exc:  # noqa: BLE001
            log.error("리샘플 완전 실패 (%s) — 원본 반환", exc)
            return pcm

    # ── say (폴백) ────────────────────────────────────────
    def _say(self, text: str) -> bytes:
        """macOS say 명령으로 합성.

        ⚠ `--data-format=LEF16@16000` 은 최신 macOS say 에서 지원되지 않아
          exit 1 로 실패한다(실측). AIFF 로 만들고 ffmpeg 로 16k 모노 PCM 변환.
        ⚠ 고정 파일명 + 동시 호출이 원인이었다:
          여러 세션이 /tmp/webapp_tts.aiff 를 동시에 쓰면 say 가 실패하고
          capture_output 때문에 예외 메시지가 비어 보였다(실측).
          → 호출마다 유니크 파일 + 락 + 상세 에러 로그
        """
        fd, tmp_aiff = tempfile.mkstemp(suffix=".aiff", prefix="maya_tts_")
        os.close(fd)
        try:
            with _SAY_LOCK:
                r1 = subprocess.run(
                    ["say", "-v", self.voice, "-o", tmp_aiff, text],
                    capture_output=True, text=True,
                )
            if r1.returncode != 0:
                log.error("say 실패 rc=%s stderr=%r", r1.returncode, (r1.stderr or "")[:300])
                return b""
            if not os.path.exists(tmp_aiff) or os.path.getsize(tmp_aiff) == 0:
                log.error("say 출력 파일이 비었습니다: %s", tmp_aiff)
                return b""
            r2 = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error",
                 "-i", tmp_aiff, "-ar", str(TARGET_RATE), "-ac", "1",
                 "-f", "s16le", "-"],
                capture_output=True,
            )
            if r2.returncode != 0:
                log.error("ffmpeg 실패 rc=%s stderr=%r", r2.returncode, (r2.stderr or b"")[:300])
                return b""
            return r2.stdout
        except Exception as exc:  # noqa: BLE001
            log.error("TTS 합성 실패 (say): %r", exc)
            return b""
        finally:
            try:
                os.unlink(tmp_aiff)
            except OSError:
                pass

    def close(self) -> None:
        """워커 프로세스 정리."""
        self._closing = True          # keep-alive 스레드 종료 신호
        if self._piper is not None:
            try:
                self._piper.stdin.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                self._piper.wait(timeout=2)
            except Exception:  # noqa: BLE001
                try:
                    self._piper.kill()
                except Exception:  # noqa: BLE001
                    pass
            self._piper = None
        if self._voxcpm is not None:
            try:
                self._voxcpm.stdin.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                self._voxcpm.wait(timeout=2)
            except Exception:  # noqa: BLE001
                try:
                    self._voxcpm.kill()
                except Exception:  # noqa: BLE001
                    pass
            self._voxcpm = None

    def play(self, pcm: bytes):
        """macOS afplay 로 즉시 재생 (블로킹)."""
        if not pcm:
            return
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp_wav = f.name
        try:
            with wave.open(tmp_wav, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(TARGET_RATE)
                wf.writeframes(pcm)
            subprocess.run(["afplay", tmp_wav], capture_output=True)
        finally:
            try:
                os.unlink(tmp_wav)
            except OSError:
                pass


if __name__ == "__main__":
    import sys
    import time

    logging.basicConfig(level=logging.INFO)
    tts = LocalTTS(engine=os.environ.get("TTS_ENGINE", "voxcpm-api"))
    print(f"engine={tts.engine}")
    for s in ["안녕하세요.", "오늘 날씨가 정말 좋네요.", "무엇을 도와드릴까요?"]:
        t0 = time.perf_counter()
        pcm = tts.synthesize(s)
        ms = (time.perf_counter() - t0) * 1000
        print(f"{ms:7.0f}ms  {len(pcm):6d}B  {len(pcm)/2/16000:.2f}s  | {s}")
