/**
 * 세션/오디오 상태 (Svelte 5 runes 기반 reactive singleton).
 * Svelte 5 runes는 모듈 스코프에서 $state()를 export 하면 .svelte.js 파일을
 * import하는 모든 곳에서 동일한 reactive 참조를 공유합니다.
 */

export const sessionState = $state({
  /** 'idle' | 'listening' | 'thinking' | 'speaking' | 'error' */
  phase: "idle",
  /** WebSocket 연결 상태 */
  connected: false,
  /** 마이크 권한 */
  micGranted: false,
  /** 마지막 에러 메시지 */
  error: /** @type {string|null} */ (null),
  /** 현재 음성 RMS (0~1, Waveform 표시용) */
  level: 0,
  /** 피크 (0~1) */
  peak: 0,
  /** 음성 재생 중 여부 */
  speaking: false,
});

/** @typedef {'idle' | 'listening' | 'thinking' | 'speaking' | 'error'} Phase */

/** @param {Phase} phase */
export function setPhase(phase) {
  if (!["idle", "listening", "thinking", "speaking", "error"].includes(phase)) {
    sessionState.error = `invalid phase: ${phase}`;
    return;
  }
  sessionState.phase = phase;
  if (phase !== "error") sessionState.error = null;
}

/** 마이크 권한 상태 변경 */
/** @param {boolean|string|undefined} granted */
export function setMic(granted) {
  sessionState.micGranted = !!granted;
}

/** 입력 RMS/피크 갱신 (0~1) */
/** @param {number} rms @param {number} [peak] */
export function setLevel(rms, peak = rms) {
  sessionState.level = Math.max(0, Math.min(1, Number(rms) || 0));
  sessionState.peak = Math.max(0, Math.min(1, Number(peak) || 0));
}

/** 재생 중 여부 */
/** @param {boolean|string|undefined} on */
export function setSpeaking(on) {
  sessionState.speaking = !!on;
}

/** WebSocket 연결 상태 */
/** @param {boolean|string|undefined} on */
export function setConnected(on) {
  sessionState.connected = !!on;
}

export function resetPhase() {
  sessionState.phase = "idle";
  sessionState.error = null;
}

/** @param {string} msg */
export function setError(msg) {
  sessionState.phase = "error";
  sessionState.error = msg;
}
