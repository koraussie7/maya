"""Maya 음성대화 시뮬레이션 러너.

`maya_voice_simulation.py` 가 생성한 scenarios JSON 을 읽고
실제 FastAPI 백엔드(ws://127.0.0.1:8001/ws) 에 흘려서
  · 첫 audio.out 청크 도달 시간 (latency_ms)
  · 모든 audio.out 합산 바이트
  · 받은 decision.log (route_id, source, score, abstained)
  · 받은 phase 전이 (listening → thinking → speaking → idle 등)
  · 보낸 텍스트 vs 실제 적중한 route_id
를 측정해 시나리오별 결과로 저장한다.

실행:
    /tmp/maya-py.sh integration/maya_voice_simulation_runner.py \\
        --scenarios /tmp/maya-scenarios.json \\
        --out /tmp/maya-simulation-results.json
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import websockets  # type: ignore


WS_URL = os.environ.get("MAYA_WS_URL", "ws://127.0.0.1:8001/ws")
SAMPLE_RATE = 16000


def synth_silence(seconds: float) -> bytes:
    n = int(seconds * SAMPLE_RATE)
    return b"\x00\x00" * n


async def send_audio_then_text(ws, scenario: dict[str, Any]) -> dict[str, Any]:
    """시나리오 한 건을 실행하고 측정값을 반환한다.

    WS lifecycle:
      1) session.start 송신
      2) 서버의 session.ready + phase=idle 응답을 모두 소비한다 (preamble)
      3) 실제 입력 (chat.text 또는 audio.flush) 송신
      4) 그 시점을 sent_at 으로 두고 그 이후의 메시지만 trace
    """
    started = time.monotonic()
    res: dict[str, Any] = {
        "id": scenario["id"],
        "label": scenario.get("label", ""),
        "kind": scenario["kind"],
        "text": scenario.get("text"),
        "expect_route": scenario.get("expect_route"),
        "expect_phase": scenario.get("expect_phase", []),
        "sent_at_ms": 0,
        "ready_at_ms": None,
        "first_stt_at_ms": None,
        "first_delta_at_ms": None,
        "first_audio_at_ms": None,
        "idle_at_ms": None,
        "phase_trace": [],
        "audio_chunks": 0,
        "audio_bytes": 0,
        "audio_mimes": [],
        "decision": None,
        "route_match": None,
        "persona_changed": None,
        "errors": [],
    }

    # 1) session.start
    await ws.send(json.dumps({
        "type": "session.start",
        "refAudioId": "clip_ref.wav",
    }))

    # 2) preamble: session.ready 와 (서버 start 시 자동 송출되는) 첫 phase=idle 을 소비한다.
    # 최대 5개 메시지 또는 1초 안에 모은다.
    preamble_count = 0
    while preamble_count < 5 and (time.monotonic() - started) < 1.5:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
        except asyncio.TimeoutError:
            break
        msg = json.loads(raw)
        t = msg.get("type")
        if t == "session.ready":
            res["ready_at_ms"] = round((time.monotonic() - started) * 1000, 1)
        # phase=idle (서버가 Session.start()에서 보내는 것) 은 무시한다.
        elif t == "phase" and msg.get("phase") == "idle":
            pass
        else:
            preamble_count += 5  # 모르는 메시지면 더 받는다
            continue
        preamble_count += 1

    # 3) 실제 입력
    if scenario.get("text") is not None and scenario["kind"] in ("rule_hit", "free_text", "abstained", "boundary", "persona_switch"):
        await ws.send(json.dumps({
            "type": "chat.text",
            "text": scenario["text"],
            "persona": scenario.get("persona_id") if scenario["kind"] == "persona_switch" else "",
        }))
    elif scenario["kind"] == "audio_no_stt":
        seq = 0
        pcm = synth_silence(scenario.get("audio_seconds", 1.5))
        chunk_bytes = SAMPLE_RATE * 2 // 10  # 3200B = 100ms PCM16
        for off in range(0, len(pcm), chunk_bytes):
            seq += 1
            b64 = base64.b64encode(pcm[off:off + chunk_bytes]).decode("ascii")
            await ws.send(json.dumps({"type": "audio.chunk", "seq": seq, "data": b64}))
        await ws.send(json.dumps({"type": "audio.flush", "seq": seq}))
    elif scenario["kind"] == "interrupt":
        await ws.send(json.dumps({
            "type": "chat.text",
            "text": scenario["text"] or "오늘 부산 날씨와 주변 관광지를 자세히 알려줘",
        }))

    sent_at = time.monotonic()
    res["sent_at_ms"] = round((sent_at - started) * 1000, 1)

    # 4) 응답 수집. idle phase 가 두 번 오는 케이스(라우트 응답 후 speak→idle, 그리고
    # 그 후 서버의 다음 입력 대기 idle)를 구분해야 한다.
    # 방법: audio.out / decision.log / assistant.delta / route.match 가 한 번이라도
    # 관측된 **이후의** 첫 idle 을 진짜 종료 신호로 본다.
    deadline = sent_at + 90.0
    last_phase = None
    saw_response_signal = False
    idle_count = 0
    while time.monotonic() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=3.0)
        except asyncio.TimeoutError:
            break
        msg = json.loads(raw)
        t = msg.get("type")
        if t == "phase":
            last_phase = msg.get("phase")
            res["phase_trace"].append({"ms": round((time.monotonic() - sent_at) * 1000, 1), "phase": last_phase})
            if last_phase == "idle":
                idle_count += 1
                if saw_response_signal or idle_count >= 2:
                    res["idle_at_ms"] = round((time.monotonic() - sent_at) * 1000, 1)
                    break
                continue
        elif t == "stt.final":
            saw_response_signal = True
            if res["first_stt_at_ms"] is None:
                res["first_stt_at_ms"] = round((time.monotonic() - sent_at) * 1000, 1)
        elif t == "assistant.delta":
            saw_response_signal = True
            if res["first_delta_at_ms"] is None:
                res["first_delta_at_ms"] = round((time.monotonic() - sent_at) * 1000, 1)
        elif t == "assistant.final":
            saw_response_signal = True
        elif t == "audio.out":
            saw_response_signal = True
            if res["first_audio_at_ms"] is None:
                res["first_audio_at_ms"] = round((time.monotonic() - sent_at) * 1000, 1)
            res["audio_chunks"] += 1
            payload = base64.b64decode(msg.get("data") or msg.get("audio_b64") or "")
            res["audio_bytes"] += len(payload)
            res["audio_mimes"].append(msg.get("mime"))
        elif t == "decision.log":
            saw_response_signal = True
            res["decision"] = msg
        elif t == "route.match":
            saw_response_signal = True
            res["route_match"] = msg
        elif t == "persona.changed":
            saw_response_signal = True
            res["persona_changed"] = msg
        elif t == "error":
            res["errors"].append(msg)

    if res["idle_at_ms"] is None and last_phase != "idle":
        res["errors"].append({"warn": "no idle phase reached within 90s", "last_phase": last_phase})

    return res


async def run_set(seed: int, scenarios: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """시드별 시나리오 셋을 WS 한 세션에 흘린다.
    단, 각 시나리오마다 새 connection 을 만들면 latency 가 깨끗해진다.
    → 시나리오 1건당 WS 1개씩 새로 연결.
    """
    out = []
    for sc in scenarios:
        try:
            async with websockets.connect(WS_URL, max_size=None, open_timeout=30) as ws:
                r = await send_audio_then_text(ws, sc)
        except Exception as exc:
            r = {
                "id": sc["id"], "label": sc.get("label", ""), "kind": sc["kind"],
                "errors": [{"connect": str(exc)}],
            }
        out.append(r)
        print(f"[{sc['id']:30}] kind={sc['kind']:14} "
              f"first_audio={r.get('first_audio_at_ms')}ms "
              f"chunks={r.get('audio_chunks', 0)} "
              f"idle={r.get('idle_at_ms')}ms "
              f"route={r.get('route_match', {}).get('id') if r.get('route_match') else (r.get('decision', {}).get('route_id') if r.get('decision') else '-')}",
              flush=True)
    return out


def write_report(results_per_seed: dict[int, list[dict[str, Any]]], out_path: Path) -> None:
    """결과 JSON + 사람이 읽기 좋은 Markdown 리포트 동시 출력."""
    out_json = {
        "ws_url": WS_URL,
        "seeds": list(results_per_seed.keys()),
        "results_by_seed": {str(s): rs for s, rs in results_per_seed.items()},
    }
    out_path.write_text(json.dumps(out_json, ensure_ascii=False, indent=2), encoding="utf-8")

    md = out_path.with_suffix(".md")
    with md.open("w", encoding="utf-8") as f:
        f.write(f"# Maya 음성대화 시뮬레이션 결과\nWS: `{WS_URL}`\n\n")
        for seed, rs in results_per_seed.items():
            f.write(f"## Seed = {seed}\n\n")
            f.write("| id | kind | first_audio (ms) | chunks | bytes | idle (ms) | route_id |\n")
            f.write("|----|------|-----------------:|-------:|------:|----------:|----------|\n")
            for r in rs:
                rid = "-"
                if r.get("route_match"):
                    rid = r["route_match"].get("id", "-")
                elif r.get("decision"):
                    rid = r["decision"].get("route_id") or ("(abstain)" if r["decision"].get("abstained") else "-")
                f.write(f"| {r['id']} | {r['kind']} | "
                        f"{r.get('first_audio_at_ms') or '-'} | "
                        f"{r.get('audio_chunks', 0)} | "
                        f"{r.get('audio_bytes', 0)} | "
                        f"{r.get('idle_at_ms') or '-'} | "
                        f"{rid} |\n")
            f.write("\n")
    print(f"[emitted] {out_path}", flush=True)
    print(f"[emitted] {md}", flush=True)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenarios", required=True, help="시나리오 JSON 경로")
    ap.add_argument("--seeds", type=str, default="0,1,2",
                    help="콤마 구분 시드 리스트 — 각각 다른 시나리오 셋 생성")
    ap.add_argument("--out", default="/tmp/maya-simulation-results.json")
    args = ap.parse_args()

    sc_path = Path(args.scenarios)
    if not sc_path.is_file():
        print(f"시나리오 파일 없음: {sc_path}", file=sys.stderr)
        return 1
    sc_data = json.loads(sc_path.read_text(encoding="utf-8"))

    # 시드별 시나리오 생성
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from maya_voice_simulation import build_scenarios

    seeds = [int(s) for s in args.seeds.split(",")]
    results_per_seed: dict[int, list[dict[str, Any]]] = {}
    for seed in seeds:
        scenarios = [s.to_dict() if hasattr(s, "to_dict") else s for s in build_scenarios(seed=seed, noise=0.3)]
        print(f"\n========== Seed={seed}  scenarios={len(scenarios)} ==========", flush=True)
        results_per_seed[seed] = await run_set(seed, scenarios)

    write_report(results_per_seed, Path(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
