/**
 * voice.svelte.js — Pocket TTS / 페르소나 음성 선택 상태.
 *
 * CODEPLAN §1 Agent 2 산출물:
 *   "voice.svelte.js — 신규 (voices 배열, selectedVoice, setVoice())"
 *
 * · Svelte 5 runes 모드 (chat.svelte.js 패턴 계승)
 * · +page.svelte 의 인라인 voice state 를 외부로 빼서
 *   다른 페이지(/knowledge 등)에서도 보이스 정보를 공유할 수 있게 한다.
 */

/** @typedef {{ id: string, label: string, source?: 'persona' | 'tts' }} Voice */

/** @type {{ provider: string|null, selectedVoice: string|null, voices: Voice[], pocket_url: string|null, last_updated: number|null }} */
export const voiceState = $state({
  provider: null,
  selectedVoice: null,
  voices: [],
  pocket_url: null,
  last_updated: null,
});

/**
 * SESSION_READY 페이로드로부터 voice state 갱신.
 *
 * @param {{
 *   ttsProvider?: string|null,
 *   ttsVoice?: string|null,
 *   pocketTtsUrl?: string|null,
 *   voices?: Array<string | {id:string,label?:string}>|null,
 * }} info
 */
export function applySessionReady(info) {
  if (!info) return;
  const provider = info.ttsProvider ?? null;
  const ttsVoice = info.ttsVoice ?? null;
  /** @type {Voice[]} */
  const voices = [];
  if (Array.isArray(info.voices)) {
    for (const v of info.voices) {
      if (typeof v === "string") {
        voices.push({ id: v, label: v, source: "tts" });
      } else if (v && typeof v === "object" && v.id) {
        voices.push({ id: v.id, label: v.label ?? v.id, source: "tts" });
      }
    }
  }
  voiceState.provider = provider;
  voiceState.selectedVoice = ttsVoice;
  voiceState.voices = voices;
  voiceState.pocket_url = info.pocketTtsUrl ?? null;
  voiceState.last_updated = Date.now();
}

/** @param {string|null} p */
export function setProvider(p) {
  voiceState.provider = p;
  voiceState.last_updated = Date.now();
}

/** @param {string|null} v */
export function setVoice(v) {
  voiceState.selectedVoice = v;
  voiceState.last_updated = Date.now();
}

/** @param {string|null} u */
export function setPocketUrl(u) {
  voiceState.pocket_url = u;
}

export function resetVoice() {
  voiceState.provider = null;
  voiceState.selectedVoice = null;
  voiceState.voices = [];
  voiceState.pocket_url = null;
  voiceState.last_updated = null;
}
