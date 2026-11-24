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


class Phase(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    ERROR = "error"


@dataclass(slots=True)
class WSMessage:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"type": self.type, **self.payload}
