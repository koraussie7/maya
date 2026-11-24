#!/usr/bin/env python3
"""VoxCPM 벤치 워커 — 별도 venv 에서 돈다(메인 파이프라인과 torch 버전이 다름).

왜 별도 프로세스인가
  VoxCPM 은 `torch>=2.5`, 이 저장소의 `integration/tts_local.py`/voicemem 환경은
  Intel Mac 때문에 torch 2.2.2 를 고정하고 있다. 한 venv 에 섞으면 둘 다 깨진다.
  → tts_local.py 의 `piper_worker.py` 와 **같은 IPC 규약**(5바이트 헤더: status 1 +
  big-endian 길이 4 + UTF-8 JSON 본문)을 쓰는 새 워커를 만들고, 엔진만 갈아끼운다.

프로토콜 (tts_local.LocalTTS 와 동일)
  요청 : 4바이트 big-endian 길이 + UTF-8 JSON
         {"id": 3, "text": "안녕하세요", "reference_wav": "...wav", "prompt_text": null}
  응답 : 1바이트 status + 4바이트 길이 + payload
         status 0 → payload = raw PCM (Int16 mono, `sample_rate_hz` 로 리샘플 완료)
         status 1 → payload = UTF-8 오류 메시지
         status 2 → 준비 완료 알림(payload = JSON {"sample_rate_hz": 48000})

실행 (venv 는 docs/VOXCPM_TTS_REVIEW.md 의 설치 절차로 만든다)
  voxcpm/bin/python integration/voxcpm_worker.py --model ./pretrained_models/VoxCPM2
  → 첫 응답 status 2 로 준비 완료를 알린 뒤 요청을 받는다.

VoxCPM 은 내부적으로 bf16/float16 을 고를 수 있는데, Intel Mac 의 MPS 는 bf16
 지원이 제한적이다. 로드 구간만 기본 dtype 을 fp32 로 바꾸고 autocast 를 무력화한다
(다른 라이브러리에 영향이 안 가도록 로드 직후 원복).
"""
from __future__ import annotations

import argparse
import io
import json
import os
import struct
import subprocess
import sys
import tempfile
import time


def _read_exact(stream, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)


def _write(status: int, payload: bytes = b"") -> None:
    sys.stdout.buffer.write(bytes([status]) + struct.pack(">I", len(payload)) + payload)
    sys.stdout.buffer.flush()


def _resample_pcm(pcm: bytes, src: int, dst: int) -> bytes:
    """Int16 mono PCM 리샘플. ffmpeg 있으면 그쪽이 정확하다."""
    if src == dst:
        return pcm
    try:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
               "-f", "s16le", "-ar", str(src), "-ac", "1", "-i", "pipe:0",
               "-f", "s16le", "-ar", str(dst), "-ac", "1", "pipe:1"]
        out = subprocess.run(cmd, input=pcm, capture_output=True, check=True).stdout
        if out:
            return out
    except Exception:  # noqa: BLE001  ffmpeg 없음/실패 → 선형 보간
        pass
    import numpy as np
    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    n = int(round(len(a) * dst / src))
    x = np.linspace(0, len(a) - 1, n, dtype=np.float32)
    b = np.interp(x, np.arange(len(a), dtype=np.float32), a)
    return np.clip(b, -32768, 32767).astype(np.int16).tobytes()


def _to_wav(path_in: str) -> str:
    """레퍼런스 클립을 16kHz mono wav 로 정규화(원본이 mp3/m4a/48k 여도 안전)."""
    fd, out = tempfile.mkstemp(suffix=".wav", prefix="voxcpm_ref_")
    os.close(fd)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-i", path_in, "-ac", "1", "-ar", "16000", out], check=True)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, help="로컬 모델 경로(예: ./pretrained_models/VoxCPM2)")
    ap.add_argument("--device", default=None, help="auto|cpu|mps|cuda")
    ap.add_argument("--target-rate", type=int, default=16000,
                    help="마야 파이프라인은 Int16 16kHz 를 기대한다")
    ap.add_argument("--reference-wav", default=None,
                    help="기본 레퍼런스 클립(복제). 없으면 Voice Design 경로")
    ap.add_argument("--denoiser", action="store_true",
                    help="ZipEnhancer 로딩(느리고 메모리 큼) — 기본은 끔")
    ap.add_argument("--predict-timesteps", type=int, default=10)
    ap.add_argument("--cfg", type=float, default=2.0)
    args = ap.parse_args()

    try:
        import numpy as np
        import torch
        # 모델 로드 구간에서만 fp32 로 강제한다(Intel Mac MPS 의 bf16 지원 제한 회피).
        # 설정 원본은 voxcpm.model.utils.pick_runtime_dtype() — device/dtype
        # 조합으로 골라진다. 로드 후 바로 원복해 다른 코드에는 영향이 없게 한다.
        orig_get_dtype = torch.get_default_dtype
        orig_autocast = (torch.autocast, torch.amp.autocast)

        class _NoAutocast:                                        # noqa: D401
            def __init__(self, device_type=None, **kw):
                self._dev = device_type

            def __enter__(self):
                return None

            def __exit__(self, *a):
                return False

        from voxcpm import VoxCPM

        torch.get_default_dtype = lambda: torch.float32            # noqa: E731
        torch.autocast = _NoAutocast                              # type: ignore[assignment]
        torch.amp.autocast = _NoAutocast                          # type: ignore[assignment]
        t0 = time.time()
        try:
            model = VoxCPM(
                voxcpm_model_path=args.model,
                enable_denoiser=args.denoiser,
                optimize=False,      # torch.compile 은 첫 호출이 수십 초 걸린다
                device=args.device,
            )
        finally:
            torch.get_default_dtype = orig_get_dtype
            torch.autocast, torch.amp.autocast = orig_autocast
        sr = int(model.tts_model.sample_rate)
        print(f"[voxcpm] loaded in {time.time() - t0:.1f}s  sr={sr} "
              f"device={model.tts_model.device}", file=sys.stderr, flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[voxcpm] 로드 실패: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        _write(1, f"load_failed: {type(exc).__name__}: {exc}".encode())
        return 1

    # 레퍼런스 클립 준비 + 프롬프트 캐시(매 요청 재인코딩 방지)
    cache = None
    ref_ready = None
    try:
        if args.reference_wav:
            ref_ready = _to_wav(args.reference_wav)
            t = time.time()
            cache = model.tts_model.build_prompt_cache(reference_wav_path=ref_ready)
            print(f"[voxcpm] reference cache built in {time.time() - t:.1f}s "
                  f"({args.reference_wav})", file=sys.stderr, flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[voxcpm] reference 준비 실패: {exc}", file=sys.stderr, flush=True)

    _write(2, json.dumps({"sample_rate_hz": args.target_rate, "model_sr": sr,
                          "cloned": cache is not None}).encode())

    while True:
        head = _read_exact(sys.stdin.buffer, 4)
        if len(head) < 4:
            break
        n = struct.unpack(">I", head)[0]
        req = json.loads(_read_exact(sys.stdin.buffer, n).decode("utf-8") or "{}")
        text = (req.get("text") or "").strip()
        ref = req.get("reference_wav")
        if not text:
            _write(0, b"")
            continue
        t = time.time()
        try:
            kw = dict(target_text=text, inference_timesteps=args.predict_timesteps,
                      cfg_value=args.cfg)
            if ref:
                kw["reference_wav_path"] = _to_wav(ref)
            elif cache is not None:
                kw["prompt_cache"] = cache

            # 진짜 첫 오디오 청크가 나온 시각(체감 지연의 핵심 지표)
            chunks = []
            first = None
            for wav in model.tts_model.generate_with_prompt_cache_streaming(**kw):
                if isinstance(wav, tuple):          # (wav, _, _)
                    wav = wav[0]
                if first is None:
                    first = time.time() - t
                chunks.append(np.asarray(wav, dtype=np.float32).reshape(-1))
            if not chunks:
                _write(1, b"empty_audio")
                continue
            audio = np.concatenate(chunks)
            pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
            pcm = _resample_pcm(pcm, sr, args.target_rate)
            dur = len(audio) / sr
            print(f"[voxcpm] id={req.get('id')} total={time.time() - t:.2f}s "
                  f"first_chunk={first if first is None else round(first, 3)}s "
                  f"audio={dur:.2f}s RTF={(time.time() - t) / max(dur, 1e-6):.2f}",
                  file=sys.stderr, flush=True)
            _write(0, pcm)
        except Exception as exc:  # noqa: BLE001
            _write(1, f"{type(exc).__name__}: {exc}".encode())

    for p in ({ref_ready} if ref_ready else set()):
        try:
            os.unlink(p)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
