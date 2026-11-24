/**
 * WebSocket 클라이언트.
 * 프론트엔드 ↔ FastAPI 백엔드 (/ws/audio) 프로토콜:
 *
 * client → server:
 *   - binary frame: 16kHz mono Int16 PCM 청크 (마이크)
 *   - text frame:   JSON {type:'text', text:'...'} (텍스트 입력)
 *   - text frame:   JSON {type:'control', action:'start'|'stop'|'ping'}
 *
 * server → client:
 *   - text frame:   JSON {type:'stt.partial'|'stt.final'|'llm.token'|'llm.done'|'error', ...}
 *   - binary frame: 16kHz mono WAV (TTS 출력) 또는 raw Int16 PCM
 */

import {
  sessionState,
  setError,
  resetPhase,
} from "../stores/session.svelte.js";
import {
  pushMessage,
  appendToMessage,
  updateMessage,
} from "../stores/chat.svelte.js";

export class MayaWS {
  /**
   * @param {string} url  ws(s)://host:port/ws/audio
   * @param {import('./audio.js').AudioPlayer} player
   */
  constructor(url, player) {
    this.url = url;
    this.player = player;
    this.ws = null;
    this.assistantId = null;
  }

  connect() {
    return new Promise((resolve, reject) => {
      try {
        this.ws = new WebSocket(this.url);
      } catch (e) {
        return reject(e);
      }
      this.ws.binaryType = "arraybuffer";
      this.ws.onopen = () => {
        sessionState.connected = true;
        resetPhase();
        resolve();
      };
      this.ws.onclose = () => {
        sessionState.connected = false;
        sessionState.phase = "idle";
      };
      this.ws.onerror = (e) => {
        setError(
          "WebSocket 연결 실패: 백엔드(/ws/audio)가 실행 중인지 확인하세요.",
        );
        reject(e);
      };
      this.ws.onmessage = (e) => this._onMessage(e);
    });
  }

  _onMessage(e) {
    if (e.data instanceof ArrayBuffer) {
      // TTS 바이너리 (WAV 또는 raw PCM)
      const bytes = new Uint8Array(e.data);
      if (bytes.byteLength >= 12 && bytes[0] === 0x52 && bytes[1] === 0x49) {
        this.player.enqueueWAV(bytes.buffer);
      } else {
        const view = new DataView(bytes.buffer);
        const pcm = new Int16Array(bytes.byteLength / 2);
        for (let i = 0; i < pcm.length; i++)
          pcm[i] = view.getInt16(i * 2, true);
        this.player.enqueuePCM(pcm);
      }
      sessionState.phase = "speaking";
      sessionState.speaking = true;
      return;
    }

    let msg;
    try {
      msg = JSON.parse(e.data);
    } catch {
      return;
    }
    switch (msg.type) {
      case "stt.partial":
        sessionState.phase = "thinking";
        if (this.assistantId == null) {
          this.assistantId = pushMessage("user", msg.text, "streaming");
        } else {
          updateMessage(this.assistantId, {
            content: msg.text,
            status: "streaming",
          });
        }
        break;
      case "stt.final":
        if (this.assistantId != null) {
          updateMessage(this.assistantId, {
            content: msg.text,
            status: "done",
          });
        } else {
          this.assistantId = pushMessage("user", msg.text, "done");
        }
        // 어시스턴트 응답 자리 만들기
        this.assistantId = pushMessage("assistant", "", "streaming");
        sessionState.phase = "thinking";
        break;
      case "llm.token":
        if (this.assistantId != null) {
          appendToMessage(this.assistantId, msg.delta ?? "");
        }
        break;
      case "llm.done":
        if (this.assistantId != null) {
          updateMessage(this.assistantId, { status: "done" });
          this.assistantId = null;
        }
        sessionState.phase = "idle";
        sessionState.speaking = false;
        break;
      case "error":
        setError(msg.message ?? "알 수 없는 오류");
        if (this.assistantId != null) {
          updateMessage(this.assistantId, { status: "error" });
          this.assistantId = null;
        }
        break;
    }
  }

  sendPCM(chunk) {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(chunk);
    }
  }

  sendText(text) {
    if (this.ws?.readyState !== WebSocket.OPEN) return;
    this.ws.send(JSON.stringify({ type: "text", text }));
  }

  sendControl(action) {
    if (this.ws?.readyState !== WebSocket.OPEN) return;
    this.ws.send(JSON.stringify({ type: "control", action }));
  }

  close() {
    try {
      this.ws?.close();
    } catch {}
    this.ws = null;
  }
}
