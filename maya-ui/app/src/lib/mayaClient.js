/**
 * mayaClient — 통합 음성 인터페이스
 *   • 오디오 캡처 → STT → LLM → TTS → 오디오 재생
 *   • 음성 레벨(level) 보고 → sessionState에 반영
 *   • 스트리밍 응답을 chat store에 push
 *
 * 실제 운영 시 backend는 FastAPI + WebSocket(/ws/chat)을 가정.
 * 백엔드 프로토콜(요약):
 *   클라이언트 → 서버:
 *     { type: 'session.start',  refAudioId?: string }
 *     { type: 'audio.chunk',    data: base64(PCM16 16kHz mono), seq: number }
 *     { type: 'audio.flush',    seq: number }
 *     { type: 'chat.text',      text: string }
 *     { type: 'chat.abort' }
 *   서버 → 클라이언트:
 *     { type: 'stt.partial',    text: string }
 *     { type: 'stt.final',      text: string }
 *     { type: 'chat.partial',   id: string, text: string }
 *     { type: 'chat.final',     id: string, text: string }
 *     { type: 'audio.out',      id: string, mime: 'audio/wav', data: base64 }
 *     { type: 'audio.level',    rms: number, peak: number }
 *     { type: 'session.state',  phase: 'idle'|'listening'|'thinking'|'speaking'|'error' }
 *     { type: 'error',          code: string, message: string }
 */
import { resolveWsUrl } from "./config.js";

export class MayaClient {
  /**
   * @param {{
   *   onPartialSTT?: (text: string) => void,
   *   onFinalSTT?:   (text: string) => void,
   *   onAssistantDelta?: (id: string, text: string) => void,
   *   onAssistantFinal?: (id: string, text: string) => void,
   *   onAudioOut?:    (id: string, mime: string, bytes: ArrayBuffer) => void,
   *   onLevel?:       (rms: number, peak: number) => void,
   *   onPhase?:       (phase: 'idle'|'listening'|'thinking'|'speaking'|'error') => void,
   *   onError?:       (code: string, message: string) => void,
   * }} hooks
   */
  constructor(hooks = {}) {
    this.hooks = hooks;
    this.ws = null;
    this.url = null;
    this.openPromise = null;
    this._sendQueue = [];
  }

  connect(url) {
    this.url = url ?? resolveWsUrl();
    return new Promise((resolve, reject) => {
      try {
        const ws = new WebSocket(this.url);
        this.ws = ws;
        ws.binaryType = "arraybuffer";
        ws.addEventListener("open", () => {
          // 큐에 쌓여있던 메시지 비우기
          for (const msg of this._sendQueue) ws.send(msg);
          this._sendQueue.length = 0;
          resolve();
        });
        ws.addEventListener("message", (ev) => this._onMessage(ev));
        ws.addEventListener("close", () => {
          this.ws = null;
        });
        ws.addEventListener("error", (e) => {
          // WebSocket 의 error 이벤트는 Error 가 아니라 Event 다.
          // 그대로 String() 하면 "[object Event]" 가 UI 에 찍힌다.
          const msg = e instanceof Error
            ? e.message
            : typeof (/** @type {any} */ (e))?.message === "string"
              ? /** @type {any} */ (e).message
              : `websocket error (${this.url})`;
          this.hooks.onError?.("ws.error", msg);
          reject(e);
        });
      } catch (e) {
        reject(e);
      }
    });
  }

  close() {
    try {
      this.ws?.close();
    } catch {}
    this.ws = null;
  }

  _send(obj) {
    const json = JSON.stringify(obj);
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(json);
    } else {
      this._sendQueue.push(json);
    }
  }

  _onMessage(ev) {
    if (typeof ev.data !== "string") return; // 우리는 텍스트(JSON)만 다룸
    let msg;
    try {
      msg = JSON.parse(ev.data);
    } catch {
      return;
    }
    switch (msg.type) {
      case "stt.partial":
        this.hooks.onPartialSTT?.(msg.text ?? "");
        break;
      case "stt.final":
        this.hooks.onFinalSTT?.(msg.text ?? "");
        break;
      // 백엔드 protocol.py: ASSISTANT_DELTA="assistant.delta", ASSISTANT_FINAL="assistant.final".
      // 예전에는 chat.partial / chat.final 만 처리해서 LLM 응답이 UI 에 전혀 안 보였다.
      case "assistant.delta":
      case "chat.partial":
        this.hooks.onAssistantDelta?.(msg.id ?? "", msg.text ?? "");
        break;
      case "assistant.final":
      case "chat.final":
        this.hooks.onAssistantFinal?.(msg.id ?? "", msg.text ?? "");
        break;
      case "audio.out":
        // base64 → ArrayBuffer
        if (msg.data) {
          const bin = atob(msg.data);
          const buf = new ArrayBuffer(bin.length);
          const view = new Uint8Array(buf);
          for (let i = 0; i < bin.length; i++) view[i] = bin.charCodeAt(i);
          this.hooks.onAudioOut?.(msg.id, msg.mime ?? "audio/wav", buf);
        }
        break;
      case "audio.level":
        this.hooks.onLevel?.(msg.rms ?? 0, msg.peak ?? 0);
        break;
      // 백엔드 Phase 송출 타입: PHASE = "phase" (payload.phase).
      // "session.state" 는 백엔드에 없어서 연결 점이 영원히 안 바뀌었다.
      case "phase":
      case "session.state":
        this.hooks.onPhase?.(msg.phase ?? msg.payload?.phase);
        break;
      // 라우팅 결정 로그 — `/decisions` 화면이 구독한다.
      case "decision.log":
        this.hooks.onDecision?.({
          ts: msg.ts,
          text: msg.text ?? "",
          route_id: msg.route_id ?? null,
          source: msg.source ?? "",
          score: typeof msg.score === "number" ? msg.score : null,
          threshold: typeof msg.threshold === "number" ? msg.threshold : null,
          abstained: !!msg.abstained,
          rule_hit: !!msg.rule_hit,
          laya_hit: !!msg.laya_hit,
          all_scores: Array.isArray(msg.all_scores) ? msg.all_scores : [],
        });
        break;
      case "error":
        this.hooks.onError?.(msg.code ?? "error", msg.message ?? "");
        break;
      default:
        /* ignore */ break;
    }
  }

  // --- High-level API ----------------------------------------------------

  startSession(opts = {}) {
    this._send({ type: "session.start", refAudioId: opts.refAudioId });
  }

  endSession() {
    this._send({ type: "session.end" });
    this.close();
  }

  /** @param {Float32Array} f32 16kHz mono */
  sendAudioChunk(f32, seq) {
    // Float32 → Int16 PCM → base64
    const pcm = new Int16Array(f32.length);
    for (let i = 0; i < f32.length; i++) {
      const s = Math.max(-1, Math.min(1, f32[i]));
      pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    const bytes = new Uint8Array(pcm.buffer);
    let bin = "";
    for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    this._send({ type: "audio.chunk", seq, data: btoa(bin) });
  }

  flushAudio(seq) {
    this._send({ type: "audio.flush", seq });
  }

  /** @param {string} text @param {string} [persona] */
  sendText(text, persona = '') {
    // persona 를 주면 서버가 **그 턴의 목소리/인격을 바꿔서** 응답한다(세션 유지).
    this._send(persona ? { type: "chat.text", text, persona } : { type: "chat.text", text });
  }

  abort() {
    this._send({ type: "chat.abort" });
  }

  get connected() {
    return !!this.ws && this.ws.readyState === WebSocket.OPEN;
  }
}
