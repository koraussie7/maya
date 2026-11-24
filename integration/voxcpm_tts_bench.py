#!/usr/bin/env python3
"""VoxCPM 워커(`voxcpm_worker.py`) 를 우리 `tts_local.LocalTTS` 규약으로 직접 호출해 측정.

이 스크립트는 **메인 venv(.venv-voicemem)에서 돈다** — 워커만 별도 venv 에서 뜨므로
torch 버전 충돌이 없다. 측정 항목:
  · 워커 준비(모델 로드) 시간 — 프로세스 1회성 비용
  · 문장별 첫 오디오 청크까지 / 전체 합성 시간 / RTF / 출력 오디오 길이
  · (--asr) 합성 결과를 다시 SenseVoice 로 전사해 CER 를 계산 (한국어 발음 검증)
  · (--baseline-piper) 기존 piper 엔진과 같은 문장으로 시간 비교

사용 예
  # 1) 복제 모드(마야 녹음 클립 10초 + 전사문)
  .venv-voicemem/bin/python integration/voxcpm_tts_bench.py \
      --worker-python /tmp/voxcpm-venv/bin/python \
      --model /Users/brianyeon/CodeGPT/maya/pretrained_models/VoxCPM2 \
      --reference /Users/brianyeon/CodeGPT/maya/models/tts/reference/clip_ref.wav \
      --prompt-text "어 분위기 확 바뀌네 근데 이 새벽에 나 데려오겠다고 그렇게 쓰는 거 보면 좀 감..."

  # 2) Voice Design 모드(레퍼런스 없이)
  .venv-voicemem/bin/python integration/voxcpm_tts_bench.py --worker-python ... --model ...

  # 3) piper 기준선만
  .venv-voicemem/bin/python integration/voxcpm_tts_bench.py --baseline-piper
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
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKER = os.path.join(ROOT, "integration", "voxcpm_worker.py")

SENTENCES = [
    "안녕하세요, 오늘 기분이 어떠세요?",
    "제가 도와드릴 일이 있을까요?",
    "지금 서울의 날씨는 맑고 기온은 영상 십 도입니다.",
]


def _read_exact(stream, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)


class WorkerClient:
    """tts_local.LocalTTS 와 동일한 5바이트 헤더 IPC."""

    def __init__(self, python: str, model: str, reference: str | None = None,
                 device: str | None = None, extra: list[str] | None = None):
        cmd = [python, WORKER, "--model", model]
        if reference:
            cmd += ["--reference-wav", reference]
        if device:
            cmd += ["--device", device]
        cmd += extra or []
        t = time.time()
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=None, bufsize=0)
        status, payload = self._read()
        self.load_s = time.time() - t
        if status != 2:
            raise RuntimeError(f"워커 준비 실패: {payload.decode('utf-8', 'replace')}")
        self.meta = json.loads(payload.decode())
        self._id = 0

    def _read(self) -> tuple[int, bytes]:
        head = _read_exact(self.proc.stdout, 5)
        if len(head) < 5:
            raise RuntimeError("워커 연결 끊김")
        status = head[0]
        n = struct.unpack(">I", head[1:5])[0]
        payload = _read_exact(self.proc.stdout, n) if n else b""
        return status, payload

    def synth(self, text: str, reference_wav: str | None = None) -> tuple[bytes, float]:
        self._id += 1
        req = {"id": self._id, "text": text}
        if reference_wav:
            # 측정 스크립트에서 넘기면 워커가 매 요청 build_prompt_cache 를 다시 한다.
            # (모델별 복제 경로가 동작하는지 확인하는 용도 — 속도 측정에는 불리하다)
            req["reference_wav"] = reference_wav
        body = json.dumps(req, ensure_ascii=False).encode()
        t = time.time()
        self.proc.stdin.write(struct.pack(">I", len(body)) + body)
        self.proc.stdin.flush()
        status, payload = self._read()
        dt = time.time() - t
        if status != 0:
            raise RuntimeError(payload.decode("utf-8", "replace"))
        return payload, dt

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=20)
        except Exception:  # noqa: BLE001
            self.proc.kill()


def _wav_seconds(pcm: bytes, rate: int = 16000) -> float:
    return len(pcm) / 2 / rate


def _save(pcm: bytes, path: str, rate: int = 16000) -> None:
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


def run_voxcpm(args) -> dict:
    cli = WorkerClient(args.worker_python, args.model, args.reference,
                       args.device, args.worker_extra)
    print(f"[worker] 준비 {cli.load_s:.1f}s  meta={cli.meta}")
    rows = []
    for s in SENTENCES:
        pcm, dt = cli.synth(s)
        sec = _wav_seconds(pcm)
        # 두 번째 호출부터는 모델/캐시가 따뜻한 상태
        rows.append({"text": s, "seconds": round(dt, 3), "audio_seconds": round(sec, 2),
                     "rtf": round(dt / max(sec, 1e-6), 2), "bytes": len(pcm)})
        print(f"  {dt:6.2f}s  audio={sec:5.2f}s  RTF={dt / max(sec, 1e-6):5.2f}  {s}")
        if args.out_dir:
            os.makedirs(args.out_dir, exist_ok=True)
            _save(pcm, os.path.join(args.out_dir, f"voxcpm_{len(rows)}.wav"))
    # 예열 효과 확인용: 같은 문장 재합성
    if rows and args.repeat:
        s = SENTENCES[0]
        pcm, dt = cli.synth(s)
        print(f"  [repeat] {dt:6.2f}s  audio={_wav_seconds(pcm):5.2f}s")
        rows.append({"text": s + " (repeat)", "seconds": round(dt, 3),
                     "audio_seconds": round(_wav_seconds(pcm), 2),
                     "rtf": round(dt / max(_wav_seconds(pcm), 1e-6), 2)})
    cli.close()
    return {"mode": "clone" if args.reference else "design",
            "load_seconds": round(cli.load_s, 2), "meta": cli.meta, "rows": rows}


def run_piper(out_dir: str | None) -> dict:
    sys.path.insert(0, os.path.join(ROOT, "integration"))
    from tts_local import LocalTTS  # noqa: PLC0415

    t = time.time()
    tts = LocalTTS(engine="piper")
    load = time.time() - t
    print(f"[piper] 엔진={tts.engine} 준비 {load:.2f}s")
    rows = []
    for s in SENTENCES:
        t = time.time()
        pcm = tts.synthesize(s)
        dt = time.time() - t
        sec = _wav_seconds(pcm)
        rows.append({"text": s, "seconds": round(dt, 3), "audio_seconds": round(sec, 2),
                     "rtf": round(dt / max(sec, 1e-6), 2)})
        print(f"  {dt:6.2f}s  audio={sec:5.2f}s  RTF={dt / max(sec, 1e-6):5.2f}  {s}")
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
            _save(pcm, os.path.join(out_dir, f"piper_{len(rows)}.wav"))
    tts.close()
    return {"engine": tts.engine, "load_seconds": round(load, 2), "rows": rows}


def run_asr(paths: list[str], texts: list[str]) -> list[dict]:
    """합성 wav → SenseVoice 전사 → CER. 발음이 깨졌는지 수치로 본다."""
    sys.path.insert(0, os.path.join(ROOT, "integration"))
    from bench_stt_hard import SenseVoice, cer  # noqa: PLC0415

    sv = SenseVoice()
    out = []
    for p, ref in zip(paths, texts):
        with wave.open(p, "rb") as w:
            pcm = w.readframes(w.getnframes())
        hyp, _ = sv.transcribe(pcm)
        out.append({"wav": os.path.basename(p), "ref": ref, "hyp": hyp,
                    "cer": round(cer(ref, hyp), 3)})
        print(f"  CER {out[-1]['cer'] * 100:5.1f}%  hyp={hyp}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--worker-python", help="VoxCPM 전용 venv 의 python")
    ap.add_argument("--model", help="로컬 VoxCPM 모델 디렉터리")
    ap.add_argument("--reference", help="복제용 레퍼런스 클립(wav/mp3)")
    ap.add_argument("--device", default=None)
    ap.add_argument("--worker-extra", nargs="*", default=[],
                    help="워커에 넘길 추가 인자")
    ap.add_argument("--out-dir", default=os.path.join(tempfile.gettempdir(), "voxcpm_bench"))
    ap.add_argument("--repeat", action="store_true", help="같은 문장 재합성(예열 효과)")
    ap.add_argument("--asr", action="store_true", help="합성 결과를 SenseVoice 로 전사해 CER 측정")
    ap.add_argument("--baseline-piper", action="store_true", help="piper 기준선도 측정")
    args = ap.parse_args()

    report: dict = {"sentences": SENTENCES}
    if args.baseline_piper:
        report["piper"] = run_piper(args.out_dir)
    if args.worker_python and args.model:
        try:
            report["voxcpm"] = run_voxcpm(args)
        except Exception as exc:  # noqa: BLE001
            report["voxcpm"] = {"error": f"{type(exc).__name__}: {exc}"}
            print(f"[voxcpm] 실패: {type(exc).__name__}: {exc}")
    if args.asr and os.path.isdir(args.out_dir):
        wavs = sorted(f for f in os.listdir(args.out_dir) if f.endswith(".wav"))
        texts = [report[k]["rows"][i]["text"].replace(" (repeat)", "")
                 for k in ("voxcpm", "piper") if k in report and "rows" in report[k]
                 for i in range(min(len(report[k]["rows"]), len(wavs)))]
        if wavs:
            report["asr"] = run_asr([os.path.join(args.out_dir, w) for w in wavs], texts)

    out = os.path.join(ROOT, "integration", "voxcpm_tts_bench.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n보고서: {out}  (오디오: {args.out_dir})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
