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
from .rag.pipeline import answer_with_rag  # Agent 1: _maybe_rag hook
from .protocol import (
    ClientMsgType,
    Phase,
    ServerMsgType,
    WSMessage,
)
from .router import DialogRouter
from .router_laya import LayaReranker, RerankOutcome
from .sentence_split import SentenceSplitter
from .stt import transcribe_wav
from .summary import context_prefix, maybe_summarize
from .transcript import TranscriptStore, recent_turns
from .tts import synthesize
from .tts_router import get_router as _get_tts_router

# Opus 송출은 선택 기능 — opuslib/플랫폼 문제로 실패해도 대화는 계속돼야 한다.
try:  # noqa: SIM105
    from .codec.opus_stream import OpusStreamCodec

except Exception:  # noqa: BLE001
    OpusStreamCodec = None  # type: ignore[assignment,misc]

log = logging.getLogger(__name__)

# 연결된 세션 레지스트리 — decision.log 를 모든 세션(/decisions 화면 포함)에 전파한다.
# 세션은 자기 소켓으로만 송출하므로, 관측 화면이 다른 세션의 결정을 보려면 필요하다.
# Session 은 @dataclass(eq=True) 라 unhashable → id 기반 dict 로 둔다.
_ACTIVE_SESSIONS: "dict[str, Session]" = {}

#: 페르소나(prompt + voice) 를 매 세션에 새로 읽는다 — 운영 중에 `MAYA_PERSONA` 를
#: 바꾸면 다음 대화부터 적용된다(재시작 없이 프롬프트 실험 가능).
from .persona import Persona, load_persona, pace_policy  # noqa: E402


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
    #: Laya reranker (System-2). 기본은 `LayaReranker.from_env()` 의 lazy 싱글톤.
    #: 외부 주입이 필요하면 `handle_client` 직전에 세션 생성 시 넘겨준다.
    reranker: Any = None
    _send_q: asyncio.Queue[WSMessage | None] = field(default_factory=asyncio.Queue)
    _sender_task: asyncio.Task | None = None

    def __post_init__(self) -> None:
        if self.persona is None:
            self.persona = load_persona()
        if not self.messages:
            self.messages.append({"role": "system", "content": self.persona.system_message})

    def _resolve_reranker(self) -> "LayaReranker | None":
        """세션이 처음 라우팅을 호출할 때 lazy 초기화.

        * `MAYA_LAYA_DISABLE=1` 이면 즉시 None (rerank 자체를 끈다).
        * 그 외에는 `LayaReranker.from_env()` — mock 모드면 안전하게 fallback.
        * 한 번 만들면 캐시 — 프로세스당 1회만 import 부담을 진다.
        """
        if self.reranker is not None:
            return self.reranker
        if os.getenv("MAYA_LAYA_DISABLE") == "1":
            self.reranker = None
            return None
        try:
            from app.router_laya import LayaReranker
            self.reranker = LayaReranker.from_env()
        except Exception:  # noqa: BLE001 — import 실패/예외 시 안전하게 끈다
            self.reranker = None
        return self.reranker

    # ── TTS 메타 헬퍼 ─────────────────────────────────────────────────
    # 2026-11-26 — `tts_router.describe()` 가 priority/alive/voices 까지
    # 만들지만, 기존 SESSION_READY 페이로드에는 tts_provider/voice/url 만
    # 실려 프론트 음성 메뉴가 비어 있었다. 한 곳에 모은다.
    def _tts_ready_payload(self) -> dict[str, Any]:
        persona_meta = {
            "id":   self.persona.id,
            "name": self.persona.name,
            "pace": self.persona.pace,
        } if self.persona else {"id": "", "name": "", "pace": "normal"}
        tts_meta = {
            "tts_provider":   SETTINGS.tts_provider,
            "tts_voice":      SETTINGS.tts_voice,
            "pocket_tts_url": (
                SETTINGS.pocket_tts_url
                if SETTINGS.tts_provider == "pocket" else ""
            ),
        }
        # tts_router 가 죽어 있어도(예: 임포트 실패) 페이로드는 항상 보낸다.
        try:
            router_info = _get_tts_router().describe() or {}
        except Exception:  # noqa: BLE001
            router_info = {}
        # voices 가 객체 리스트(dict) 면 그대로, 문자열 리스트면 {id,label} 로 정규화.
        voices_raw = router_info.get("voices") or []
        voices_norm: list[dict[str, str]] = []
        for v in voices_raw:
            if isinstance(v, dict):
                voices_norm.append({
                    "id":    str(v.get("id", "")),
                    "label": str(v.get("label", v.get("id", ""))),
                })
            elif isinstance(v, str):
                voices_norm.append({"id": v, "label": v})
        return {
            "session_id": self.id,
            **persona_meta,
            **tts_meta,
            "tts_priority": list(router_info.get("priority") or []),
            "tts_alive":    dict(router_info.get("alive") or {}),
            "voices":       voices_norm,
        }

    async def start(self) -> None:
        self.id = self.id or str(uuid.uuid4())
        if self.transcript is None:
            self.transcript = TranscriptStore(self.id)
        _ACTIVE_SESSIONS[self.id] = self
        self._sender_task = asyncio.create_task(self._sender())
        # 2026-11-25 — persona 메타데이터(id/name/pace)를 페이로드에 실어 보낸다.
        # 2026-11-26 — 음성 메뉴 호환을 위해 helper 가 tts_provider/voice/url +
        # priority/alive/voices 까지 한 번에 실어 보낸다.
        await self._sendq(WSMessage(type=ServerMsgType.SESSION_READY.value,
                                    payload=self._tts_ready_payload()))
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

    async def _broadcast_decision(self, payload: dict[str, Any]) -> None:
        """decision.log 를 **모든 연결된 세션**에 전파.

        발화는 / 화면에서 일어나지만 관측은 /decisions 화면에서 한다.
        세션 로컬 송신만 하면 /decisions 는 자기 세션에서 발화할 때만
        로그를 받아 화면이 영원히 비어 있다(2026-11-27 실측).
        자기 자신은 _sendq 로 보내고, 나머지에는 _send_q 에 직접 넣는다.
        """
        for sess in list(_ACTIVE_SESSIONS.values()):
            if sess is self:
                continue
            sess._send_q.put_nowait(WSMessage(
                type=ServerMsgType.DECISION_LOG.value, payload=dict(payload)))
        await self._sendq(WSMessage(
            type=ServerMsgType.DECISION_LOG.value, payload=dict(payload)))

    async def _send_textq(self, type_: str, **payload: Any) -> None:
        await self._send_q.put(WSMessage(type=type_, payload=payload))

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
        _ACTIVE_SESSIONS.pop(self.id, None)
        # 2026-11-25 — 통화 종료를 IDLE 보다 명확한 신호(ENDED)로 송출.
        # 송신 루프가 아직 살아있을 때만 시도한다(None 신호 후에 enqueue 해도 안 나감).
        try:
            await self._sendq(WSMessage(type=ServerMsgType.PHASE.value,
                                        payload={"phase": Phase.ENDED.value}))
        except Exception:  # noqa: BLE001
            pass
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
        """진행 중 응답/재생 중단 (구현 단순화: 플래그만).

        2026-11-25 — Phase enum 확장에 맞춰 양보(YIELDED) 신호를 보낸다.
        프론트는 이 값을 받아 VoiceOrb/Voice 상태를 'yielded' 로 전환할 수 있고,
        미매핑 상태면 무시해도 무방하다(기존 no-op 정책 유지).
        """
        self.speaking = False
        await self._sendq(WSMessage(type=ServerMsgType.PHASE.value,
                                    payload={"phase": Phase.YIELDED.value}))

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
        # ── Laya reranker 통합 (2026-11-25) ───────────────────────────────
        # rule 매칭이 약하면 Laya(System-2 의사결정 모델) 에게 의사를 묻는다.
        # threshold(0.85) 이상이면 라우트 확정, 아니면 LLM 자유 대화로 빠진다.
        # `MAYA_LAYA_MOCK=1` 이거나 laya 미설치면 자동 mock — 실측 결과만 송출.
        # match_with_laya(text, reranker) → (RouteHit|None, debug dict)
        # debug keys: rule_hit(bool), laya_source(str|"disabled"),
        #             laya_top_prob(float), laya_abstained(bool),
        #             laya_candidates([{route_id, prob, source}])
        hit: RouteHit | None = None
        dbg: dict = {}
        if self.router is not None:
            hit, dbg = self.router.match_with_laya(
                text, self._resolve_reranker())
            if hit is not None:
                # 결정 로그 송출 — 프론트엔드 `/decisions` 페이지가 구독.
                laya_hit = bool(dbg.get("laya_hit", False))
                rule_hit = bool(dbg.get("rule_hit", not laya_hit))
                hit_source = "laya" if laya_hit else "rule"
                all_scores = list(dbg.get("laya_candidates") or [])
                # rule-only hit 일 때는 candidates 가 비어 있으므로 route_id만 채운다.
                if not all_scores:
                    all_scores = [{
                        "route_id": hit.route.id,
                        "score": 1.0,
                        "source": hit_source,
                    }]
                await self._broadcast_decision({
                        "text": text,
                        "route_id": hit.route.id,
                        "source": hit_source,
                        "score": round(
                            dbg.get("laya_top_prob", 1.0 if hit_source == "rule" else 0.0), 4),
                        "threshold": 0.85,
                        "abstained": False,
                        "rule_hit": rule_hit,
                        "laya_hit": laya_hit,
                        "all_scores": [
                            {
                                "route_id": c.get("route_id"),
                                "score": round(float(c.get("prob", 0.0)), 4),
                                "source": c.get("source"),
                            } for c in all_scores
                        ],
                        "ts": time.time(),
                    })
                await self._send_route(hit, start=time.monotonic())
                return
            # abstained — 결정 로그만 남기고 자유 대화로 계속.
            if dbg.get("laya_abstained"):
                await self._broadcast_decision({
                        "text": text,
                        "route_id": None,
                        "source": dbg.get("laya_source"),
                        "score": round(float(dbg.get("laya_top_prob", 0.0)), 4),
                        "threshold": 0.85,
                        "abstained": True,
                        "rule_hit": bool(dbg.get("rule_hit", False)),
                        "laya_hit": False,
                        "all_scores": [
                            {
                                "route_id": c.get("route_id"),
                                "score": round(float(c.get("prob", 0.0)), 4),
                                "source": c.get("source"),
                            } for c in (dbg.get("laya_candidates") or [])
                        ],
                        "ts": time.time(),
                    })

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
        # ── pace 정책 → LLM 토큰 예산 (2026-11-25) ────────────────────────
        # persona.pace 가 "narrate" 면 토큰을 더, "eager" 면 덜 — story-teller vs quick-reply.
        # 정책 테이블은 persona.pace_policy() 에 집중되어 있다.
        _pace = self.persona.pace if self.persona else "normal"
        _max_tokens = int(pace_policy(_pace)["max_tokens"])
        log.info("session=%s pace=%s max_tokens=%d", self.id, _pace, _max_tokens)
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
        async for msg in stream_chat(context, temperature=temperature, max_tokens=_max_tokens):
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
        # Agent 1: _maybe_rag hook — RAG가 활성화되면 검색 결과를 assistant 메시지에 추가
        # MAYA_RAG_ENABLED=0 이면 no-op, 기존 사용자 환경에 영향 없음
        rag_context = answer_with_rag(full) if os.environ.get("MAYA_RAG_ENABLED", "0") == "1" else ""
        if rag_context:
            full = f"{full}\n\n[참고] {rag_context}"
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
    # 2026-11-25 — 핸드셰이크 직후, session 준비 전 "CONNECTING" 신호 송출.
    # Session 인스턴스가 아직 만들어지지 않았으므로 직접 ws.send_json 을 쓴다.
    try:
        await ws.send_json({"type": ServerMsgType.PHASE.value,
                            "phase": Phase.CONNECTING.value})
    except Exception:  # noqa: BLE001
        log.warning("CONNECTING 송출 실패(클라이언트가 곧 끊겼을 수 있음)")
        return
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
                # 2026-11-26 — 음성 메뉴 호환: Session 헬퍼가 tts_provider/voice/url +
                # priority/alive/voices 까지 한 번에 실어 보낸다.
                await session._sendq(WSMessage(
                    type=ServerMsgType.SESSION_READY.value,
                    payload=session._tts_ready_payload(),
                ))
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
