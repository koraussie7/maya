#!/usr/bin/env python3
"""voxcpm_api_worker.py — VoxCPM Hugging Face Space API 워커 (Option A).

왜 별도 프로세스인가
  gradio_client 자체는 가볍지만, HF Space 호출은 3~10초가 걸린다.
  메인 파이프라인(piper 등)에서 asyncio 와 함께 호출하면 GIL 로 인해 다른 합성이
  모두 블록된다. → piper_worker.py 와 동일한 IPC 규약을 따르는 subprocess 워커로
  만들어서 메인 프로세스가 합성을 병렬로 던지고 받게 한다.

프로토콜 (tts_local.LocalTTS / piper_worker.py 와 동일)
  요청 : 4바이트 big-endian 길이 + UTF-8 JSON
         {"id": 7, "text": "안녕하세요",
          "reference_wav": "/abs/path/clip.wav" | null,
          "prompt_text": "어 분위기 확 바뀌네" | null,
          "cfg": 2.0, "normalize": false, "denoise": false}
  응답 : 1바이트 status + 4바이트 길이 + payload
         status 0 → payload = raw PCM (Int16 mono, target-rate 로 리샘플 완료)
         status 1 → payload = UTF-8 오류 메시지
         status 2 → 준비 완료 알림(payload = JSON {"sample_rate_hz": 16000,
                                                     "model_sr": 48000,
                                                     "engine": "voxcpm-api"})

실행
  .venv-stt/bin/python integration/voxcpm_api_worker.py [--target-rate 16000]
  # HF_TOKEN 이 환경에 있으면 자동으로 헤더에 붙는다.

인증
  `huggingface_hub` 가 설정된 토큰을 그대로 사용한다 (HF_TOKEN 또는 ~/.huggingface/token).
  토큰이 없으면 OpenBMB/VoxCPM-Demo 는 비로그인 호출을 허용하지만, 일일 quota 가
  매우 작다(≈ 5분/일). PRO 사용자는 HF_TOKEN 을 .env 에 넣어 quota 완화.

성능
  - 첫 호출 : Space cold-start 로 20~60초 (서버 깨어남)
  - 이후    : 텍스트 100자당 3~8초 (네트워크 + GPU 처리)
  - 로컬 대비 느리지만, torch 의존성이 전혀 없어 Intel Mac 에서 즉시 동작.
"""
from __future__ import annotations

import argparse
import json
import os
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path


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


def _wav_to_pcm16(path: str) -> tuple[bytes, int]:
    """WAV 파일을 Int16 mono PCM + sample_rate 로 디코딩한다 (ffmpeg 우선)."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
           "-i", path, "-f", "s16le", "-acodec", "pcm_s16le",
           "-ac", "1", "-ar", "0", "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True, check=True)
    except Exception as exc:
        raise RuntimeError(f"ffmpeg 디코드 실패: {exc}") from exc

    # probe 로 실제 sample_rate 를 얻는다
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=sample_rate", "-of", "csv=p=0", path],
        capture_output=True, text=True, check=True)
    sr = int(probe.stdout.strip() or "48000")
    return proc.stdout, sr


def _resample_pcm(pcm: bytes, src: int, dst: int) -> bytes:
    if src == dst:
        return pcm
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
           "-f", "s16le", "-ar", str(src), "-ac", "1", "-i", "pipe:0",
           "-f", "s16le", "-ar", str(dst), "-ac", "1", "pipe:1"]
    try:
        out = subprocess.run(cmd, input=pcm, capture_output=True, check=True).stdout
        if out:
            return out
    except Exception:
        pass
    # ffmpeg 실패 시 numpy 폴백
    import numpy as np
    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    n = int(round(len(a) * dst / src))
    x = np.linspace(0, len(a) - 1, n, dtype=np.float32)
    b = np.interp(x, np.arange(len(a), dtype=np.float32), a)
    return np.clip(b, -32768, 32767).astype(np.int16).tobytes()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--space", default="OpenBMB/VoxCPM-Demo",
                    help="HF Space 슬러그 (기본 OpenBMB/VoxCPM-Demo)")
    ap.add_argument("--target-rate", type=int, default=16000,
                    help="부모 파이프라인이 기대하는 출력 sample rate")
    ap.add_argument("--timeout", type=float, default=180.0,
                    help="HF Space 호출 1회 외부 타임아웃(초, predict 자체에는 넘기지 않음)")
    args = ap.parse_args()

    try:
        from gradio_client import Client, handle_file
    except ImportError:
        _write(1, b"gradio_client not installed. Run: pip install gradio_client")
        return 1

    # 토큰 있으면 사용, 없으면 anonymous (quota 가 작다)
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN")
    hf_kwargs = {"token": token} if token else {}
    try:
        client = Client(args.space, **hf_kwargs)
    except Exception as exc:
        _write(1, f"space connect failed: {type(exc).__name__}: {exc}".encode())
        return 1

    # prepare : server warm-up. 첫 predict 가 cold-start 라 워커 시작 시 한 번 호출.
    try:
        # 가벼운 no-op 호출은 /_on_toggle_instant (체크박스 토글)
        # 대신 실제 모델을 깨우는 작은 합성으로 워밍업한다.
        # 단, quota 보호를 위해 워밍업을 끄는 옵션도 둔다.
        warmup = os.environ.get("VOXCPM_API_WARMUP", "0") == "1"
        if warmup:
            t0 = time.time()
            _ = client.predict(
                text_input="hi",
                control_instruction="",
                reference_wav_path_input=None,
                use_prompt_text=False,
                prompt_text_input="",
                cfg_value_input=2.0,
                do_normalize=False,
                denoise=False,
                api_name="/generate",
            )
            print(f"[voxcpm-api] warmup done in {time.time() - t0:.1f}s",
                  file=sys.stderr, flush=True)
    except Exception as exc:
        print(f"[voxcpm-api] warmup 실패(무시): {exc}", file=sys.stderr, flush=True)

    _write(2, json.dumps({
        "sample_rate_hz": args.target_rate,
        "model_sr": 48000,
        "engine": "voxcpm-api",
        "space": args.space,
        "has_token": bool(token),
    }).encode())

    while True:
        head = _read_exact(sys.stdin.buffer, 4)
        if len(head) < 4:
            return 0
        n = struct.unpack(">I", head)[0]
        try:
            req = json.loads(_read_exact(sys.stdin.buffer, n).decode("utf-8") or "{}")
        except Exception as exc:
            _write(1, f"bad request json: {exc}".encode())
            continue

        text = (req.get("text") or "").strip()
        ref_path = req.get("reference_wav")
        prompt_text = req.get("prompt_text")
        use_prompt_text = bool(req.get("use_prompt_text")) if prompt_text else False
        cfg = float(req.get("cfg", 2.0))
        do_normalize = bool(req.get("normalize", False))
        denoise = bool(req.get("denoise", False))

        if not text:
            _write(0, b"")
            continue

        t0 = time.time()
        try:
            # gradio_client 가 요구하는 형식: handle_file 로 로컬 경로를 감싸거나 None
            ref_arg = handle_file(ref_path) if ref_path and Path(ref_path).exists() else None

            # 해당 Space 가 **kwargs 를 받지 않아 timeout 같은 메타 키워드를 넘기면 즉시
            # TypeError 가 난다. predict 는 외부 스레드에서 실행하고 join 으로 timeout 을
            # 적용한다.
            import threading
            holder: dict = {}

            def _call():
                last_err = None
                for attempt in range(2):  # HF queue busy 1회 재시도
                    try:
                        holder["result"] = client.predict(
                            text_input=text,
                            control_instruction="",
                            reference_wav_path_input=ref_arg,
                            use_prompt_text=use_prompt_text,
                            prompt_text_input=prompt_text or "",
                            cfg_value_input=cfg,
                            do_normalize=do_normalize,
                            denoise=denoise,
                            api_name="/generate",
                        )
                        holder["done"] = True
                        holder.pop("error", None)
                        return
                    except Exception as e:  # noqa: BLE001
                        last_err = e
                        msg = str(e)
                        # "backend is busy" 또는 queue 관련 오류만 짧은 대기로 재시도
                        if "busy" in msg.lower() or "queue" in msg.lower():
                            time.sleep(0.8)
                            continue
                        break
                holder["error"] = last_err
                holder["done"] = True

            th = threading.Thread(target=_call, daemon=True)
            th.start()
            th.join(timeout=max(5.0, args.timeout))
            if not holder.get("done"):
                raise TimeoutError(f"voxcpm-api predict 타임아웃 {args.timeout:.0f}s")
            if "error" in holder:
                raise holder["error"]
            result = holder["result"]
            # 실측(2026-11-24): Space 가 tuple `('안녕하세요' 같은 값, '/tmp/gradio/xxx.wav')`
            # 를 돌려주는 경우가 있다. 예전 코드는 tuple 에 `.get` 을 불러 AttributeError 로
            # 죽었고, tts_local 이 이를 piper/say 로 폴백해 **다른 목소리**가 나왔다.
            if isinstance(result, (tuple, list)):
                cands = [x for x in result if isinstance(x, (str, dict))]
                result = next((x for x in cands
                               if (isinstance(x, str) and (x.endswith(".wav") or x.startswith("http")))
                               or (isinstance(x, dict) and (x.get("path") or x.get("url")))),
                              result[-1] if result else None)
            _AUDIO_EXT = (".wav", ".mp3", ".m4a", ".flac", ".ogg", ".opus")
            is_audio = (
                isinstance(result, str)
                and (result.startswith("http") or result.lower().endswith(_AUDIO_EXT))
            )
            if not is_audio:
                # 경로도 URL 도 아닌 문자열은 오디오가 아니다(텍스트 에코 등).
                _write(1, f"unexpected result (not audio): {result!r}".encode())
                continue
            # result 는 보통 파일 경로(str) 또는 dict {path,url,...}
            audio_path = result if isinstance(result, str) else result.get("path") or result.get("url")
            if not audio_path:
                _write(1, f"unexpected result: {result!r}".encode())
                continue

            # 리모트가 URL 일 수도 있다. 캐시 안 되면 즉시 다운로드
            if audio_path.startswith("http"):
                # gradio 가 보통 임시 파일로 캐시해두므로, 그 경로를 받는다.
                # 진짜 URL 인 경우에만 다운로드
                import urllib.request
                fd, local = tempfile.mkstemp(suffix=".wav", prefix="voxcpm_api_")
                os.close(fd)
                urllib.request.urlretrieve(audio_path, local)
                audio_path = local
            elif not os.path.exists(audio_path):
                # 상대경로일 수 있음 — 같은 이름의 파일을 캐시 폴더에서 찾는다
                _write(1, f"audio file not found: {audio_path}".encode())
                continue

            pcm, sr = _wav_to_pcm16(audio_path)
            pcm = _resample_pcm(pcm, sr, args.target_rate)

            # 임시 파일 정리
            try:
                if audio_path.startswith(tempfile.gettempdir()):
                    os.unlink(audio_path)
            except OSError:
                pass

            dur = len(pcm) / 2 / args.target_rate
            print(f"[voxcpm-api] id={req.get('id')} total={time.time() - t0:.2f}s "
                  f"audio={dur:.2f}s ratio={(time.time()-t0)/max(dur,1e-6):.2f}",
                  file=sys.stderr, flush=True)
            # 빈 응답(44바이트 wav 헤더 등)을 status 0 으로 보내면 상위는 성공으로 오판한다.
            if len(pcm) <= 64:
                _write(1, f"empty_audio ({len(pcm)} bytes)".encode())
                continue
            _write(0, pcm)
        except Exception as exc:  # noqa: BLE001
            _write(1, f"{type(exc).__name__}: {exc}".encode())


if __name__ == "__main__":
    raise SystemExit(main())
