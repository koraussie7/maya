#!/usr/bin/env python3
"""pcm_turn_engine VAD 1차 컷 A/B — 같은 오디오를 rms 경로와 firered 경로로 흘려 비교한다.

이 스크립트는 **TurnEngine 의 실제 push_result 를 그대로** 호출한다(최소 재현).
그래서 "1차 컷이 성공적으로 일어났는가"만 판정하고, 왜 턴이 안 닫혔는지를 같이 출력한다.

종료 판정 3경로 (pcm_turn_engine.push_result 기준)
  1. silence  — 발화 시작 후 silent_ms >= threshold (또는 firered speech_end 관측)
  2. idle     — 발화 시작 전 silence_ms_no_speech(기본 60s) 경과 → "no speech"
  3. guard    — 발화 시작 후 max_turn_ms(기본 8s) 경과 → "max duration"/"no speech"

⚠️ 기본 설정으로 6.9초 클립을 넣으면 **3번(guard, 8s)** 이 걸려야 정상이다.
   1초 안에 턴이 닫히면 silence 판정이 너무 일찍 걸린 것이다.

⏱ 판정 시계 (`--clock`)
   · `audio`(기본, 배치) — 청크 길이만큼 **오디오 시간**을 진행시켜 판정한다.
     `pcm_turn_engine` 은 `time.monotonic()` 만 쓰므로, 모듈의 `time` 을 shim 으로
     바꿔치기해 **엔진 코드는 한 줄도 고치지 않고** 라이브 타이밍을 재현한다.
     (예전 증상: 파일을 최대 속도로 밀면 벽시계가 오디오보다 짧아 silence 판정이
      절대 안 걸려 6.9s 클립이 rms 0턴으로 나왔다 — 그 원인을 여기서 제거)
   · `wall`(라이브 재현) — 청크마다 실제로 sleep. 느리지만 운영과 동일 조건.

실행
  .venv-voicemem/bin/python integration/bench_vad_firstcut.py
  .venv-voicemem/bin/python integration/bench_vad_firstcut.py --src x.wav --trace
  .venv-voicemem/bin/python integration/bench_vad_firstcut.py --no-firered
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from pcm_turn_engine import TurnConfig, TurnEngine  # noqa: E402

ROOT = os.path.dirname(HERE)
DEFAULT_SRC = os.path.join(ROOT, "maya", "models", "tts", "reference", "clip_ref_speaker_b.wav")


def read_wav(path: str) -> tuple[bytes, int]:
    with wave.open(path, "rb") as w:
        return w.readframes(w.getnframes()), w.getframerate()


def frame_rms(pcm: bytes) -> int:
    import struct

    n = len(pcm) // 2
    if not n:
        return 0
    s = struct.unpack(f"<{n}h", pcm[: n * 2])
    return int((sum(v * v for v in s) / n) ** 0.5)


class AudioClock:
    """오디오 시간 == 벽시계 로 만드는 최소 시계 (배치 벤치용)."""

    _now = 0.0

    @classmethod
    def advance(cls, seconds: float) -> None:
        cls._now += seconds

    @classmethod
    def monotonic(cls) -> float:
        return cls._now


class _ClockShim:
    """`time.monotonic` 만 AudioClock 으로 바꾼 프록시(perf_counter 등은 원본)."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    @staticmethod
    def monotonic() -> float:
        return AudioClock.monotonic()


def run(eng: TurnEngine, pcm: bytes, sr: int, chunk_ms: int,
        trace: bool = False, realtime: bool = False) -> dict:
    """TurnEngine 을 실제 청크 스트림처럼 밀어넣고 턴 확정만 기록한다.

    `--clock audio` 면 호출 전에 `pcm_turn_engine.time` 이 AudioClock shim 으로
    교체돼 있어야 한다(엔진은 건드리지 않는다). `realtime=True` 는 wall 모드로,
    청크마다 실제로 sleep 한다.
    """
    step = int(sr * chunk_ms / 1000) * 2
    turns: list[dict] = []
    lat_ms: list[float] = []
    speech_chunks = 0
    t0 = time.perf_counter()

    eng.begin_turn()
    for off in range(0, len(pcm), step):
        chunk = pcm[off:off + step]
        if not chunk:
            break
        AudioClock.advance(len(chunk) / 2 / sr)   # 청크 하나 = 오디오 chunk_ms 경과
        t = time.perf_counter()
        res = eng.push_result(chunk)
        lat_ms.append((time.perf_counter() - t) * 1000)
        if eng._speech_started:
            speech_chunks += 1
        if trace:
            print(f"    t={(off + len(chunk)) / 2 / sr:6.2f}s rms={frame_rms(chunk):5d} "
                  f"started={int(eng._speech_started)} run={eng._speech_run:2d} "
                  f"closed={res.reason if res else '-'}")
        if res is not None:
            turns.append({
                "reason": res.reason,
                "at_s": round((off + len(chunk)) / 2 / sr, 2),
                "audio_s": round(len(res.pcm) / 2 / sr, 2),
                "text_len": len(res.text or ""),
            })
            eng.begin_turn()
        if realtime:
            time.sleep(chunk_ms / 1000.0)
    total = time.perf_counter() - t0

    lat_ms.sort()
    return {
        "turns": turns,
        "turn_count": len(turns),
        "first_cut_s": turns[0]["at_s"] if turns else None,
        "reasons": [t["reason"] for t in turns],
        "speech_chunks": speech_chunks,
        "proc_total_s": round(total, 3),
        "chunk_ms_p50": round(lat_ms[len(lat_ms) // 2], 2) if lat_ms else 0.0,
        "chunk_ms_p95": round(lat_ms[int(len(lat_ms) * 0.95)], 2) if lat_ms else 0.0,
        "chunk_ms_max": round(lat_ms[-1], 2) if lat_ms else 0.0,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=DEFAULT_SRC)
    ap.add_argument("--chunk-ms", type=int, default=50)
    ap.add_argument("--sr", type=int, default=16000)
    ap.add_argument("--engines", default="rms,firered")
    ap.add_argument("--no-firered", action="store_true", help="firered 경로 제외(빠른 재현)")
    ap.add_argument("--trace", action="store_true", help="청크별 판정 상태 출력")
    ap.add_argument("--wait-extra-s", type=float, default=12.0,
                    help="오디오 종료 후 무음 청크를 계속 넣는 시간(s) — guard 판정 관찰용")
    ap.add_argument("--clock", choices=("audio", "wall"), default="audio",
                    help="audio=오디오 시간(배치, 기본) / wall=실제 대기(라이브 재현, 느림)")
    args = ap.parse_args()

    pcm, sr = read_wav(args.src)
    if sr != args.sr:
        print(f"[skip] {sr}Hz — 16kHz 클립이 필요합니다(ffmpeg 로 리샘플)")
        return 2
    audio_s = len(pcm) / 2 / sr
    print(f"[src] {os.path.basename(args.src)}  {audio_s:.2f}s  "
          f"chunk {args.chunk_ms}ms  trace={args.trace}")

    # 원본 오디오 뒤에 무음을 이어 붙인다 — 실제 마이크처럼 "말이 끝난 뒤"를 재현
    silence = b"\x00\x00" * int(sr * args.wait_extra_s)
    stream = pcm + silence

    engines = args.engines.split(",")
    if args.no_firered:
        engines = [e for e in engines if e != "firered"]

    # 판정 시계 — 엔진 소스는 건드리지 않고 모듈의 time 만 치환한다.
    import pcm_turn_engine as pte

    real_time = pte.time
    if args.clock == "audio":
        pte.time = _ClockShim(real_time)
    print(f"[clock] {args.clock}" + ("  (sleep 포함 — 느림)" if args.clock == "wall" else ""))

    out: dict[str, dict] = {}
    try:
        out = _run_engines(engines, stream, sr, args, audio_s)
    finally:
        pte.time = real_time

    if len(out) >= 2 and "rms" in out and "firered" in out:
        a, b = out["rms"], out["firered"]
        print("\n=== 비교 ===")
        print(f"  1차 컷 시각   rms {a['first_cut_s']}s  vs  firered {b['first_cut_s']}s")
        print(f"  턴 개수       rms {a['turn_count']}  vs  firered {b['turn_count']}")
        print(f"  사유          rms {a['reasons']}  vs  firered {b['reasons']}")
        print(f"  처리 총시간   rms {a['proc_total_s']}s  vs  firered {b['proc_total_s']}s")
        print(f"  청크 p95      rms {a['chunk_ms_p95']}ms  vs  firered {b['chunk_ms_p95']}ms")

    print("\n=== 판정 기준 ===")
    max_turn = TurnConfig().max_turn_ms / 1000
    for name, r in out.items():
        if r["turn_count"] == 0:
            print(f"  {name:8s} ✗ 턴이 닫히지 않음 — 컷 실패")
            continue
        first = r["turns"][0]
        ok_cut = first["at_s"] >= audio_s * 0.5
        print(f"  {name:8s} {'✓' if ok_cut else '✗'} 첫 컷 {first['at_s']}s "
              f"(오디오 {audio_s}s 의 {first['at_s'] / audio_s * 100:.0f}%), "
              f"사유 '{first['reason']}', 확정 오디오 {first['audio_s']}s")
        if first["reason"] in ("max duration", "no speech") and first["at_s"] > max_turn:
            print(f"           → guard(max_turn_ms={max_turn}s) 가 걸린 것으로 보임. "
                  f"이건 'silence 판정이 안 걸렸다'는 뜻 — 안전장치는 정상 동작")
    return 0


def _run_engines(engines, stream, sr, args, audio_s) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for name in engines:
        cfg = TurnConfig(vad_engine=name, use_turn_detector=False)
        eng = TurnEngine(config=cfg)
        if name == "firered" and getattr(eng, "_vad", None) is None:
            print(f"\n[firered] 워커 사용 불가 → 건너뜀")
            eng.close()
            continue
        print(f"\n=== {name} (vad={name}, detector off) ===")
        res = run(eng, stream, sr, args.chunk_ms, trace=args.trace,
                  realtime=(args.clock == "wall"))
        eng.close()
        out[name] = res
        for k in ("turn_count", "first_cut_s", "reasons", "speech_chunks",
                  "proc_total_s", "chunk_ms_p50", "chunk_ms_p95"):
            print(f"  {k:14s} {res[k]}")
        for t in res["turns"]:
            print(f"    · {t['at_s']:6.2f}s  {t['reason']:14s} audio={t['audio_s']}s")
    return out


if __name__ == "__main__":
    raise SystemExit(main())
