#!/usr/bin/env python3
"""integration/patch_webapp_server.py — webapp_server.py 에 문장 단위 스트리밍 TTS 를 넣는다.

⚠ 왜 파일을 통째로 덮어쓰지 않는가
  webapp_server.py 는 500줄 넘는 운영 파일이다. 통째 교체는 다른 기능을
  조용히 날릴 위험이 크다. 그래서 텍스트 앵커 기반으로 정확히 두 군데만 바꾼다.
    1) handle_user_input() 의 TTS 블록 → 문장 단위 스트리밍
    2) turn push 호출 → push_result 로 바꿔 NAMO 전사 텍스트 재사용

idempotent: 이미 적용됐으면 아무것도 하지 않는다.
--revert 로 원복할 수 있다(백업 파일 기준).

사용:
  .venv-stt/bin/python ../maya-2/integration/patch_webapp_server.py --dry-run
  .venv-stt/bin/python ../maya-2/integration/patch_webapp_server.py
  .venv-stt/bin/python ../maya-2/integration/patch_webapp_server.py --revert
"""
from __future__ import annotations

import argparse
import pathlib
import re
import shutil
import sys

MAYA = pathlib.Path("/Users/brianyeon/CodeGPT/maya")
SERVER = MAYA / "tools" / "webapp_server.py"
BACKUP = MAYA / "tools" / "webapp_server.py.bak-streamtts"

MARK_STREAM = "# ── [stream] 문장 단위 스트리밍 TTS (Lumina/edge-s2s 패턴) ──"
MARK_NAMOTX = "# ── [namo] 턴 판정 전사 텍스트 재사용 ──"

# ── 1) 헬퍼 삽입 지점: handle_user_input 정의 바로 앞 ────────────
HELPER_ANCHOR = "# ── 사용자 입력 처리 (STT final → Jev → LLM → TTS) ─────────\n"

HELPERS = '''\
__MARK_HELPER__
# 문장 분할: 한국어/영어 종결부호 뒤에서 자른다.
# 출처 패턴: Posime/edge-s2s-voice-pipeline u_agent.py (20줄로 단순화)
SENTENCE_RE = re.compile(r"[^.!?。？！\\n]+[.!?。？！]?", re.M)

def split_sentences(text: str, min_len: int = 4) -> list[str]:
    """응답 텍스트를 TTS 단위 문장으로 나눈다.

    짧은 조각("네." 등)을 그대로 보내면 say 호출 횟수만 늘어난다.
    min_len 미만은 다음 문장과 합친다.
    """
    parts = [p.strip() for p in SENTENCE_RE.findall(text or "") if p.strip()]
    out: list[str] = []
    buf = ""
    for p in parts:
        buf = (buf + " " + p).strip() if buf else p
        if len(buf) >= min_len:
            out.append(buf)
            buf = ""
    if buf:
        if out:
            out[-1] = (out[-1] + " " + buf).strip()
        else:
            out.append(buf)
    return out

async def stream_tts(ws, tts, reply: str) -> None:
    """문장마다 합성·전송한다. 첫 소리가 빨리 나오는 것이 목적.

    ⚠ LocalTTS.synthesize 는 subprocess(say+ffmpeg) 라 blocking 이다.
      이벤트 루프에서 직접 부르면 WS ping 이 끊긴다 → executor 사용.
    ⚠ say 는 _SAY_LOCK 으로 직렬화되어 있어 문장 순서가 자연히 유지된다.
    """
    loop = asyncio.get_running_loop()
    sentences = split_sentences(reply)
    if not sentences:
        return
    for idx, sentence in enumerate(sentences):
        try:
            t0 = time.perf_counter()
            pcm = await loop.run_in_executor(None, tts.synthesize, sentence)
            if not pcm:
                log.warning("[stream] TTS 빈 결과 (%d/%d) %r", idx + 1, len(sentences), sentence[:40])
                continue
            b64 = base64.b64encode(pcm).decode("ascii")
            await ws.send_json({
                "type": "tts_audio",
                "audio": b64,
                "sample_rate": 16000,
                "seq": idx,
                "final": idx == len(sentences) - 1,
                "text": sentence,
            })
            log.info("[stream] TTS %d/%d %.2fs %dB %r", idx + 1, len(sentences),
                     time.perf_counter() - t0, len(pcm), sentence[:40])
        except Exception as exc:  # noqa: BLE001
            log.error("[stream] TTS 실패 (%d/%d): %s", idx + 1, len(sentences), exc)
            return

'''

# ── 2) TTS 블록 교체 앵커 ────────────────────────────────────────
TTS_OLD = '''    if tts and reply:
        try:
            loop = asyncio.get_running_loop()
            log.info("TTS 합성 시작 (%d자)", len(reply))
            pcm = await loop.run_in_executor(None, tts.synthesize, reply)
            if pcm:
                b64 = base64.b64encode(pcm).decode("ascii")
                await sess["ws"].send_json({
                    "type": "tts_audio",
                    "audio": b64,
                    "sample_rate": 16000,
                })
                log.info("TTS 전송 완료 %dB (%.2fs)", len(pcm), len(pcm) / 2 / 16000)
            else:
                log.warning("TTS 합성 결과가 비었습니다")
        except Exception as exc:  # noqa: BLE001
            log.error("TTS 처리 실패: %s", exc)
'''

TTS_NEW = '''__MARK_STREAM__
    # 기존: reply 전체를 한 번에 합성 → 첫 소리까지 13초 (긴 문단에서 체감 대기)
    # 변경: 문장마다 합성·전송 → 첫 소리 즉시, 총 시간도 단축
    if tts and reply:
        log.info("TTS 스트리밍 시작 (%d자, %d문장)", len(reply), len(split_sentences(reply)))
        await stream_tts(sess["ws"], tts, reply)
'''

# ── 3) 턴 엔진 호출 교체 (NAMO 전사 재사용) ──────────────────────
TURN_OLD = '''                        # 전사가 길어지면 WS keepalive 가 끊길 수 있다.
                        # 먼저 thinking 을 보내 클라이언트가 대기 중임을 알게 한다.
                        await ws.send_json({"type": "status", "status": "thinking"})
                        text = await engine.transcribe(finished)
'''

TURN_NEW = '''                        # 전사가 길어지면 WS keepalive 가 끊길 수 있다.
                        # 먼저 thinking 을 보내 클라이언트가 대기 중임을 알게 한다.
                        await ws.send_json({"type": "status", "status": "thinking"})
                        __MARK_NAMOTX__
                        # NAMO 판정 때 이미 전사한 텍스트가 있으면 2차 전사를 생략한다.
                        # (faster-whisper small 기준 0.3~1.0초 절약)
                        if namo_text:
                            text = namo_text
                            log.info("turn: NAMO 전사 재사용 %r", text[:60])
                        else:
                            text = await engine.transcribe(finished)
'''

PUSH_OLD = '''                    finished = engine.push(raw)
'''

PUSH_NEW = '''                    _res = engine.push_result(raw)
                    namo_text = _res.text if _res else ""
                    finished = _res.pcm if _res else None
'''


IMPORT_ANCHOR = "import asyncio\n"


def ensure_imports(src: str) -> tuple[str, list[str]]:
    """문장 분할/타이밍용 표준 라이브러리 import 를 idempotent 하게 추가한다."""
    added: list[str] = []
    out = src
    for mod in ("re", "time"):
        if not re.search(rf"^import {mod}\b", out, re.M):
            if IMPORT_ANCHOR not in out:
                raise SystemExit(f"앵커 없음: import {mod} 추가 지점")
            out = out.replace(IMPORT_ANCHOR, f"import {mod}\n" + IMPORT_ANCHOR, 1)
            added.append(f"import {mod} 추가")
    return out, added


def patch(src: str) -> tuple[str, list[str]]:
    notes: list[str] = []
    out = src

    if MARK_STREAM not in out:
        if HELPER_ANCHOR not in out:
            raise SystemExit("앵커 없음: handle_user_input 헤더")
        out = out.replace(
            HELPER_ANCHOR,
            HELPERS.replace("__MARK_HELPER__", MARK_STREAM) + HELPER_ANCHOR,
            1,
        )
        notes.append("헬퍼(split_sentences/stream_tts) 삽입")

        if TTS_OLD not in out:
            raise SystemExit("앵커 없음: TTS 블록")
        out = out.replace(
            TTS_OLD,
            TTS_NEW.replace("__MARK_STREAM__", MARK_STREAM),
            1,
        )
        notes.append("handle_user_input TTS → 문장 스트리밍")

    if MARK_NAMOTX not in out:
        if PUSH_OLD not in out:
            raise SystemExit("앵커 없음: engine.push 호출")
        out = out.replace(PUSH_OLD, PUSH_NEW, 1)
        if TURN_OLD not in out:
            raise SystemExit("앵커 없음: transcribe 호출 블록")
        out = out.replace(
            TURN_OLD,
            TURN_NEW.replace("__MARK_NAMOTX__", MARK_NAMOTX),
            1,
        )
        notes.append("engine.push → push_result, NAMO 전사 재사용")

    return out, notes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--revert", action="store_true")
    args = ap.parse_args()

    if not SERVER.is_file():
        print(f"없음: {SERVER}")
        return 1

    if args.revert:
        if not BACKUP.is_file():
            print(f"백업 없음: {BACKUP}")
            return 1
        shutil.copy2(BACKUP, SERVER)
        print(f"복원 완료: {SERVER} ← {BACKUP}")
        return 0

    src = SERVER.read_text(encoding="utf-8")

    try:
        src, imp_notes = ensure_imports(src)
    except SystemExit as e:
        print(f"❌ {e}")
        return 2

    # 필수 import 확인 (재확인)
    missing = [n for n in ("import re", "import time", "import base64", "import asyncio")
               if not re.search(rf"^import {n.split()[1]}\b", src, re.M)]
    if missing:
        print(f"⚠ 필요한 import 누락: {missing} — 수동 확인 필요")

    try:
        new, notes = patch(src)
    except SystemExit as e:
        print(f"❌ {e}")
        return 2

    if not notes:
        if imp_notes:
            for n in imp_notes:
                print(f"  · {n}")
        else:
            print("이미 적용됨 — 변경 없음")
            return 0
    else:
        for n in imp_notes + notes:
            print(f"  · {n}")

    # 문법 검사
    try:
        compile(new, str(SERVER), "exec")
    except SyntaxError as e:
        print(f"❌ 문법 오류 — 적용 취소: {e}")
        return 3

    if args.dry_run:
        diff_lines = sum(1 for a, b in zip(src.splitlines(), new.splitlines()) if a != b)
        print(f"[dry-run] 문법 OK, 변경 예정 {len(new.splitlines()) - len(src.splitlines()):+d}줄")
        return 0

    if not BACKUP.is_file():
        shutil.copy2(SERVER, BACKUP)
        print(f"백업: {BACKUP}")

    SERVER.write_text(new, encoding="utf-8")
    print(f"✅ 적용 완료: {SERVER}  ({len(new.splitlines()) - len(src.splitlines()):+d}줄)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
