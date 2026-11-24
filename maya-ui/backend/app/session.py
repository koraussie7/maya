"""세션 단위 STT → LLM → TTS 오케스트레이션."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from .config import SETTINGS
from .llm import stream_chat
from .protocol import (
    ClientMsgType,
    Phase,
    ServerMsgType,
    WSMessage,
)
from .router import DialogRouter
from .sentence_split import SentenceSplitter
from .stt import transcribe_wav
from .summary import context_prefix, maybe_summarize
from .transcript import TranscriptStore, recent_turns
from .tts import synthesize

# Opus 송출은 선택 기능 — opuslib/플랫폼 문제로 실패해도 대화는 계속돼야 한다.
try:  # noqa: SIM105
    from .codec.opus_stream import OpusStreamCodec

except Exception:  # noqa: BLE001
    OpusStreamCodec = None  # type: ignore[assignment,misc]

log = logging.getLogger(__name__)

#: 페르소나(prompt + voice) 를 매 세션에 새로 읽는다 — 운영 중에 `MAYA_PERSONA` 를
#: 바꾸면 다음 대화부터 적용된다(재시작 없이 프롬프트 실험 가능).
from .persona import Persona, load_persona  # noqa: E402


def _system_prompt() -> str:
    """이전 하드코딩 SYSTEM_PROMPT 를 대신한다(호환용 이름 유지)."""
    return load_persona().system_message


async def _send(ws: WebSocket, msg: WSMessage) -> None:
    """순서가 보장된 단일 메시지 송신. 동시 송신 시 SSE처럼 깨질 수 있으므로
    Session._send_q 안에서만 호출한다(직접 호출하지 말 것).
    """
    await ws.send_json(msg.to_json())


async def _send_text(ws: WebSocket, type_: str, **payload: Any) -> None:
    await ws.send_json({"type": type_, **payload})


#: 라우터는 프로세스에 1개만 — 파일을 매 세션 다시 파싱하지 않는다.
_ROUTER: DialogRouter | None = None


def get_router() -> DialogRouter:
    global _ROUTER
    if _ROUTER is None:
        _ROUTER = DialogRouter.load()
    return _ROUTER


def _audio_payload(audio: bytes, mime_default: str = "audio/wav") -> dict:
    """오디오 → WS payload.

    `MAYA_AUDIO_CODEC=opus` 이고 opuslib 이 있으면 **Opus 로 인코딩**한다.
      10초 문장: wav base64 427KB → Opus 33KB (12.8배).
    프론트(`playOpus`)는 `[4byte len][frame]` 봉투를 전제한다 —
    실제 대역폭는 16kHz mono PCM16 이므로 그 계약을 지킨다.
    """
    if os.environ.get("MAYA_AUDIO_CODEC", "wav").lower() == "opus" and OpusStreamCodec:
        try:
            with OpusStreamCodec() as codec:
                frames = b"".join(codec.encode_to_wire(audio))
            return {"data": base64.b64encode(frames).decode("ascii"),
                    "audio_b64": base64.b64encode(frames).decode("ascii"),
                    "mime": "audio/opus"}
        except Exception as exc:  # noqa: BLE001
            log.warning("opus 인코딩 실패 → wav 폴백: %s", exc)
    b64 = base64.b64encode(audio).decode("ascii")
    return {"data": b64, "audio_b64": b64, "mime": mime_default}


class _TtsSeq:
    """병렬 TTS 의 **송출 순서** 관리(작은 유틸).

    워커는 합성을 병렬로 하지만, 재생은 문장 순서대로여야 한다.
    각 유닛에 seq 를 붙이고 `stats['next_seq']` 가 다음에 보낼 번호다.
    """

    @staticmethod
    def mark(stats: dict, done: set[int], seq: int) -> None:
        """seq 를 '처리 완료'로 표시하고 next_seq 를 전진시킨다.

        실패한 유닛도 호출해야 뒤 유닛이 영원히 막히지 않는다.
        """
        if seq == stats["next_seq"]:
            stats["next_seq"] = seq + 1
        else:
            done.add(seq)
        while stats["next_seq"] in done:
            done.discard(stats["next_seq"])
            stats["next_seq"] += 1


@dataclass
class Session:
    id: str
    ws: WebSocket
    messages: list[dict[str, str]] = field(default_factory=list)
    pcm_buffer: bytearray = field(default_factory=bytearray)
    speaking: bool = False
    closed: bool = False
    #: 세션에 묶인 페르소나. __post_init__ 에서 한 번 로드하고, 이후 TTS 음색도 이걸 쓴다.
    persona: Persona | None = None
    #: 고정 결정 라우터(공용 인스턴스 주입). None 이면 모든 발화가 자유 대화.
    router: Any = None
    #: 원문 기록(append-only). `messages` 는 **전송용 컨텍스트**일 뿐이다.
    transcript: Any = None
    _send_q: asyncio.Queue[WSMessage | None] = field(default_factory=asyncio.Queue)
    _sender_task: asyncio.Task | None = None

    def __post_init__(self) -> None:
        if self.persona is None:
            self.persona = load_persona()
        if not self.messages:
            self.messages.append({"role": "system", "content": self.persona.system_message})

    async def start(self) -> None:
        self.id = self.id or str(uuid.uuid4())
        if self.transcript is None:
            self.transcript = TranscriptStore(self.id)
        self._sender_task = asyncio.create_task(self._sender())
        await self._sendq(WSMessage(type=ServerMsgType.SESSION_READY.value,
                                    payload={"session_id": self.id}))
        await self._sendq(WSMessage(type=ServerMsgType.PHASE.value,
                                    payload={"phase": Phase.IDLE.value}))

    async def _sender(self) -> None:
        """큐에 들어온 메시지를 순서대로 직렬 송신 — race condition 방지."""
        while True:
            msg = await self._send_q.get()
            if msg is None:
                return
            try:
                await self.ws.send_json(msg.to_json())
            except Exception:  # noqa: BLE001
                return

    async def _sendq(self, msg: WSMessage) -> None:
        """큐 기반 송신. 직접 ws.send_json 호출 금지."""
        await self._send_q.put(msg)

    async def _send_textq(self, type_: str, **payload: Any) -> None:
        await self._send_q.put(WSMessage(type=type_, **payload))

    async def on_audio_chunk(self, payload: dict[str, Any]) -> None:
        b64 = payload.get("data", "")
        if not b64:
            return
        try:
            chunk = base64.b64decode(b64)
        except Exception:
            return
        self.pcm_buffer.extend(chunk)

    async def on_audio_flush(self, payload: dict[str, Any]) -> None:
        """발화 종료 신호 — 버퍼를 STT로 보내고 응답 생성."""
        if not self.pcm_buffer:
            return
        if self.speaking:
            await self._interrupt()
        await self._handle_utterance(bytes(self.pcm_buffer))
        self.pcm_buffer.clear()

    async def on_text(self, payload: dict[str, Any]) -> None:
        """텍스트 직접 입력 — STT 건너뛰고 LLM 호출.

        `payload.persona` 가 오면 **그 턴의 목소리/인격을 바꾼다**(세션 유지).
        UI 에서 "남성/여성" 토글을 눌렀을 때 클라이언트가 이 값을 보내면 된다.
        """
        text = (payload.get("text") or "").strip()
        if not text:
            return
        want = (payload.get("persona") or "").strip()
        if want:
            await self.switch_persona(want)
        await self._handle_text(text)

    async def switch_persona(self, name: str) -> bool:
        """세션의 페르소나(목소리+인격) 전환. 실패하면 기존 것을 유지한다.

        system 메시지만 교체하고 **대화 기록(user/assistant)은 유지**한다 —
        목소리 바꿨다고 맥락을 잃으면 안 된다.
        """
        new = load_persona(name)
        if new.id == name and self.persona and self.persona.id != name and name != new.id:
            # 이름이 매칭되지 않았다(기본값으로 떨어짐) → 기존 유지
            log.warning("persona '%s' 전환 실패 → %s 유지", name, self.persona.id)
            return False
        self.persona = new
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0] = {"role": "system", "content": new.system_message}
        log.info("session=%s persona → %s (voice=%s)", self.id, new.id,
                 Path(new.voice_ref_wav).name or "-")
        await self._sendq(WSMessage(type="persona.changed", payload={
            "id": new.id, "name": new.name,
            "voice": Path(new.voice_ref_wav).stem if new.voice_ref_wav else "",
        }))
        return True

    async def on_interrupt(self, _payload: dict[str, Any]) -> None:
        await self._interrupt()

    async def end(self) -> None:
        self.closed = True
        # 송신 루프 종료 신호
        await self._send_q.put(None)
        if self._sender_task:
            try:
                await asyncio.wait_for(self._sender_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._sender_task.cancel()
        try:
            await self.ws.close()
        except Exception:
            pass

    async def _interrupt(self) -> None:
        """진행 중 응답/재생 중단 (구현 단순화: 플래그만)."""
        self.speaking = False

    async def _handle_utterance(self, pcm: bytes) -> None:
        """오디오 → STT → LLM → TTS."""
        await self._sendq(WSMessage(type=ServerMsgType.PHASE.value,
                                    payload={"phase": Phase.LISTENING.value}))
        # PCM16 mono 16kHz → wav 헤더 붙여 저장
        import wave
        from pathlib import Path
        tmp = Path(f"/tmp/maya_session_{self.id}.wav")
        with wave.open(str(tmp), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(pcm)
        text = await asyncio.to_thread(transcribe_wav, tmp)
        await self._sendq(WSMessage(type=ServerMsgType.STT_FINAL.value,
                                    payload={"text": text}))
        if not text:
            await self._sendq(WSMessage(type=ServerMsgType.PHASE.value,
                                        payload={"phase": Phase.IDLE.value}))
            return
        await self._handle_text(text)

    async def _send_route(self, hit: Any, *, start: float) -> None:
        """고정 결정 응답 — 고정 문장 1개를 합성(첫 회)하고 즉시 송출.

        · 같은 문장은 `tts_local` 캐시에 걸려 이후에는 **합성 없이** 나간다.
        · 기록은 남긴다(에이전트 맥락 — 라우트 턴도 대화의 일부다).
        · `phase` 를 SPEAKING→IDLE 로 정확히 돌려 UI 가 멈추지 않게 한다.
        """
        route = hit.route
        say = route.say or "응."
        self.messages.append({"role": "assistant", "content": say})
        if self.transcript is not None:
            self.transcript.append("user", hit.text,
                                   persona=self.persona.id if self.persona else "",
                                   extra={"route": route.id})
            self.transcript.append("assistant", say,
                                   persona=self.persona.id if self.persona else "",
                                   extra={"route": route.id, "action": route.action})
        log.info("session=%s route=%s (action=%s) text=%r", self.id, route.id,
                 route.action or "-", hit.text)
        await self._sendq(WSMessage(type="route.match",
                                    payload={"id": route.id, "say": say,
                                             "action": route.action}))
        await self._sendq(WSMessage(type=ServerMsgType.PHASE.value,
                                    payload={"phase": Phase.SPEAKING.value}))
        t0 = time.monotonic()
        try:
            audio = await synthesize(
                say,
                ref_wav=self.persona.voice_ref_wav if self.persona else "",
                prompt_text=self.persona.voice_prompt_text if self.persona else "",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("session=%s route tts 실패: %s", self.id, exc)
            audio = b""
        dt = time.monotonic() - t0
        if audio:
            payload = _audio_payload(audio)
            payload.update({"text": say, "seq": 1, "route": route.id,
                            "persona": self.persona.id if self.persona else ""})
            await self._sendq(WSMessage(type=ServerMsgType.AUDIO_OUT.value,
                                        payload=payload))
        log.info("session=%s route reply in %.2fs (tts %.2fs, %dB)",
                 self.id, time.monotonic() - start, dt, len(audio))
        await self._sendq(WSMessage(type=ServerMsgType.PHASE.value,
                                    payload={"phase": Phase.IDLE.value}))

    async def _handle_text(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})
        # 원문은 **전부** 기록한다(삭제·잘라내기 없음). 전송량만 아래에서 줄인다.
        if self.transcript is not None:
            self.transcript.append("user", text,
                                   persona=self.persona.id if self.persona else "")
        await self._sendq(WSMessage(type=ServerMsgType.PHASE.value,
                                    payload={"phase": Phase.THINKING.value}))

        # ── 고정 결정 라우터(LLM·TTS 없이 즉답) ────────────────────────
        # "그만" / "응" / "다시 말해줘" 같은 유한한 발화는 모델도 합성도 필요 없다.
        # 라우트가 맞으면 고정 문장을 **캐시된 음성**으로 바로 보낸다(체감 0초).
        # 오탐 방지: 짧은 발화 + 발화 끝 일치만 인정(router.py).
        if self.router is not None:
            hit = self.router.match(text)
            if hit is not None:
                await self._send_route(hit, start=time.monotonic())
                return

        buf: list[str] = []
        start = time.monotonic()
        # ── 문장 단위 스트리밍 TTS ──────────────────────────────────────
        # 전체 응답을 기다리지 않고 **첫 문장**이 완성되면 바로 합성한다.
        #   첫 오디오까지의 시간 = 첫 문장까지의 시간(전체 답변 시간 아님)
        # 합성은 워커(별도 스레드)에서 돌아 응답 스트림을 막지 않는다.
        splitter = SentenceSplitter(
            min_chars=int(os.environ.get("MAYA_TTS_MIN_CHARS", "4")))
        tts_q: asyncio.Queue[tuple[int, str] | None] = asyncio.Queue()
        tts_stats = {"n": 0, "first_at": 0.0, "bytes": 0, "empty": 0, "next_seq": 0}
        # ── TTS 병렬화 ────────────────────────────────────────────────
        # 유닛이 순차 합성되면 4문장이면 17+5+6+12=40s 이다(실측).
        # 워커를 N개 띄워 **합성은 병렬**, **송출은 순서대로** 한다.
        #   MAYA_TTS_WORKERS=2 (기본) — HF Space 부하/레이트리밋과 응답 지연의 균형
        workers = max(1, int(os.environ.get("MAYA_TTS_WORKERS", "2")))
        seq_counter = {"v": 0}

        def stats_seq() -> int:
            seq_counter["v"] += 1
            return seq_counter["v"]

        tts_tasks = [asyncio.create_task(
            self._tts_worker(tts_q, start, tts_stats)) for _ in range(workers)]
        temperature = self.persona.temperature if self.persona else 0.7
        # ── 컨텍스트 창(비용 핵심) ─────────────────────────────────────
        # 매 턴 전체 기록을 보내면 입력 토큰이 턴 수의 제곱으로 커진다(1,000턴 $2.10 실측).
        # `self.messages` 는 계속 쌓아 **기록**으로 두고, LLM 에는 최근 N턴만 보낸다.
        #   MAYA_CONTEXT_TURNS=8 → 입력이 약 800토큰으로 고정
        keep = max(1, int(os.environ.get("MAYA_CONTEXT_TURNS", "8")))
        context = recent_turns(self.messages, keep)
        # 15시간 대화는 세션이 계속 끊긴다 → 오늘자 누적 요약을 system 바로 뒤에 붙인다.
        # (system 1개 + 요약 + 최근 N턴 = 고정 크기, 요약은 summary.py 가 관리)
        prefix = context_prefix(self.transcript)
        if prefix:
            context = [context[0], {"role": "system", "content": prefix}, *context[1:]]
        log.info("session=%s context %d/%d messages (keep_turns=%d, summary=%s)",
                 self.id, len(context), len(self.messages), keep, bool(prefix))
        async for msg in stream_chat(context, temperature=temperature):
            if msg.type == ServerMsgType.ASSISTANT_DELTA.value:
                token = msg.payload.get("text", "")
                buf.append(token)
                # 문장 단위로 끊어 합성 큐에 넣는다(첫 문장부터 소리가 나게).
                for unit in splitter.feed(token):
                    tts_q.put_nowait((stats_seq(), unit))
            await self._sendq(msg)
        for unit in splitter.flush():
            tts_q.put_nowait((stats_seq(), unit))
        for _ in range(workers):
            tts_q.put_nowait(None)          # 워커별 종료 신호
        full = "".join(buf).strip()
        if not full:
            await self._sendq(WSMessage(type=ServerMsgType.PHASE.value,
                                        payload={"phase": Phase.IDLE.value}))
            return
        self.messages.append({"role": "assistant", "content": full})
        if self.transcript is not None:
            self.transcript.append("assistant", full,
                                   persona=self.persona.id if self.persona else "")
            # 요약은 **응답을 막지 않는다**. 20턴이 쌓였으면 백그라운드로 압축한다.
            try:
                asyncio.create_task(maybe_summarize(self.transcript))
            except Exception:  # noqa: BLE001
                pass

        # TTS 는 문장별로 이미 끝났다. 큐 종료를 기다려 통계만 남긴다.
        try:
            await asyncio.wait_for(asyncio.gather(*tts_tasks), timeout=600)
        except Exception:  # noqa: BLE001
            pass
        if not tts_stats["n"]:
            await self._sendq(WSMessage(
                type=ServerMsgType.ERROR.value,
                payload={"code": "tts.empty",
                         "message": "TTS 결과가 비었습니다 (백엔드 로그에서 voxcpm/piper 확인)"},
            ))
        elif tts_stats["empty"]:
            log.warning("session=%s TTS 빈 유닛 %d개", self.id, tts_stats["empty"])
        log.info("session=%s reply in %.2fs, %d chars, tts %d units first=%.2fs total=%dB",
                 self.id, time.monotonic() - start, len(full), tts_stats["n"],
                 tts_stats["first_at"], tts_stats["bytes"])
        await self._sendq(WSMessage(type=ServerMsgType.PHASE.value,
                                    payload={"phase": Phase.IDLE.value}))

    async def _tts_worker(self, q: "asyncio.Queue[tuple[int, str] | None]", start: float,
                          stats: dict) -> None:
        """문장 큐를 **병렬로 합성**하되 **크기 순서대로 송출**한다.

        왜 병렬인가: 순차 합성이면 4문장 40초(실측 17+5+6+12). 워커 2개면 대략 반으로 줄어든다.
        왜 순서 보장인가: 재생 순서가 뒤바뀌면 문맥이 깨진다.

        구현: 각 유닛에 seq 를 붙이고, `stats['next_seq']` 가 내 차례일 때만 보낸다.
        실패한 유닛도 `_mark_sent()` 로 반드시 진행시켜 뒤 유닛이 막히지 않게 한다.
        """
        sent = stats.setdefault("done", set())
        phase_sent = False
        while True:
            item = await q.get()
            if item is None:
                break
            seq, unit = item
            if not phase_sent:
                phase_sent = True
                await self._sendq(WSMessage(type=ServerMsgType.PHASE.value,
                                            payload={"phase": Phase.SPEAKING.value}))
            t0 = time.monotonic()
            try:
                audio = await synthesize(
                    unit,
                    ref_wav=self.persona.voice_ref_wav if self.persona else "",
                    prompt_text=self.persona.voice_prompt_text if self.persona else "",
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("session=%s tts 실패(%.20s…): %s", self.id, unit, exc)
                stats["empty"] += 1
                _TtsSeq.mark(stats, sent, seq)
                continue
            dt = time.monotonic() - t0
            if not audio:
                stats["empty"] += 1
                _TtsSeq.mark(stats, sent, seq)
                continue

            # 내 차례(가장 낮은 미처리 seq)가 올 때까지 대기 — 합성은 이미 끝났다.
            deadline = time.monotonic() + 300
            while stats["next_seq"] != seq and time.monotonic() < deadline:
                await asyncio.sleep(0.01)

            stats["n"] += 1
            stats["bytes"] += len(audio)
            if not stats["first_at"]:
                stats["first_at"] = time.monotonic() - start
            payload = _audio_payload(audio)
            payload.update({
                "text": unit,           # 사운드와 함께 문장도 — UI 자막용
                "seq": seq,
                "persona": self.persona.id if self.persona else "",
            })
            await self._sendq(WSMessage(type=ServerMsgType.AUDIO_OUT.value,
                                        payload=payload))
            log.info("session=%s tts unit %d %.2fs → %dB (%.1fs elapsed)",
                     self.id, seq, dt, len(audio), time.monotonic() - start)
            _TtsSeq.mark(stats, sent, seq)


async def handle_client(ws: WebSocket) -> None:
    await ws.accept()
    session = Session(id="", ws=ws, router=get_router())
    await session.start()
    try:
        while not session.closed:
            data = await ws.receive_text()
            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                continue
            t = msg.get("type")
            # payload 를 쓰는 클라이언트(`{type, payload:{...}}`)와 필드를 최상위에
            # 두는 브라우저 클라이언트(`{type, text}`)가 둘 다 있다. 둘을 섞어 읽는다.
            payload = {k: v for k, v in msg.items() if k != "type"}
            if isinstance(msg.get("payload"), dict):
                payload = {**payload, **msg["payload"]}
            if t == ClientMsgType.AUDIO_CHUNK.value:
                await session.on_audio_chunk(payload)
            elif t == ClientMsgType.AUDIO_FLUSH.value:
                await session.on_audio_flush(payload)
            elif t in (ClientMsgType.TEXT.value, "chat.text"):
                # 프론트엔드는 `chat.text` 를 보낸다(protocol.py 의 enum 은 "text").
                # 예전엔 "text" 만 처리해서 브라우저 메시지가 **통째로 무시**됐다.
                await session.on_text(payload)
            elif t == ClientMsgType.SESSION_START.value:
                # 프론트는 연결 후 session.start 를 보낸다. 서버는 연결 시 이미
                # Session() 을 만들어 두었으므로 **빠뜨리지 말고** 준비 알림만 돌려준다.
                # (이걸 누락하면 레이스가 있어 클라이언트가 영원히 대기한다)
                await session._sendq(WSMessage(type=ServerMsgType.SESSION_READY.value,
                                               payload={"session_id": session.id}))
            elif t == ClientMsgType.INTERRUPT.value:
                await session.on_interrupt(payload)
            elif t == ClientMsgType.SESSION_END.value:
                break
    except WebSocketDisconnect:
        # 정상 종료(1000)는 오류가 아니다 — 예전에는 1000 에도 ERROR 로그를 찍었다.
        log.info("session closed by client")
    except Exception as exc:  # noqa: BLE001
        log.exception("ws loop error: %s", exc)
        try:
            await session._send_textq(ServerMsgType.ERROR.value, message=str(exc))
        except Exception:
            pass
    finally:
        await session.end()
