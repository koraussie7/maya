"""프론트 ↔ 백 WebSocket 메시지 프로토콜.

프론트의 `mayaClient.js`의 메시지 키와 1:1 대응되도록 정의합니다.
추가/변경 시 양쪽을 동시에 갱신해야 합니다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ClientMsgType(str, Enum):
    SESSION_START = "session.start"
    SESSION_END = "session.end"
    AUDIO_CHUNK = "audio.chunk"
    AUDIO_FLUSH = "audio.flush"
    TEXT = "text"
    INTERRUPT = "interrupt"


class ServerMsgType(str, Enum):
    SESSION_READY = "session.ready"
    STT_PARTIAL = "stt.partial"
    STT_FINAL = "stt.final"
    ASSISTANT_DELTA = "assistant.delta"
    ASSISTANT_FINAL = "assistant.final"
    AUDIO_OUT = "audio.out"
    LEVEL = "audio.level"
    PHASE = "phase"
    ERROR = "error"
    # 2026-11-25 — Laya reranker PoC: 라우팅 의사결정 로그
    #  - 클라이언트 `/decisions` 페이지에 rule/Laya/threshold/abstain 원본을 그대로 보냄
    #  - JSON 안에 라우트 id·say·action은 포함하지 않는다(클라이언트가 routes.yaml을 받음)
    DECISION_LOG = "decision.log"


class Phase(str, Enum):
    IDLE = "idle"
    # 2026-11-25 — Jev-Interaction 의 9단계 phase 를 참고해 점진 확장.
    # v1 (Jev 와 1:1 대응 목표):
    #   CONNECTING : WS 핸드셰이크 직후, session.ready 전
    #   LISTENING  : 사용자 발화 수집 중 (마이크 입력 활성)
    #   THINKING   : LLM 응답 대기
    #   SPEAKING   : TTS 송출 중
    #   YIELDED    : 사용자가 interrupt, agent 가 양보
    #   HELD       : agent 가 자발적으로 잠시 멈춤 (예: 호흡/생각 사이)
    #   ENDED      : 통화 종료 (IDLE 보다 명확한 신호)
    #   ERROR      : 오류
    # v0 의 talking (사용자 발화 중)은 우리 LISTENING 과 중복이라 추가하지 않았다.
    CONNECTING = "connecting"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    YIELDED = "yielded"
    HELD = "held"
    ENDED = "ended"
    ERROR = "error"


@dataclass(slots=True)
class WSMessage:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"type": self.type, **self.payload}
