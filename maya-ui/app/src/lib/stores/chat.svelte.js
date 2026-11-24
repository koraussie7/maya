/**
 * 대화 메시지 로그.
 * - role: 'user' | 'assistant' | 'system'
 * - content: 표시할 텍스트 (스트리밍 중에는 부분 텍스트)
 * - status: 'pending' | 'streaming' | 'final' | 'error'
 */

export const chatState = $state({
  messages: [],
  /** 추천 프롬프트 (빈 대화일 때 표시) */
  suggestions: [
    "오늘 일정 알려줘",
    "한국어로 자기소개 해줘",
    "내 목소리로 짧게 인사해줘",
  ],
});

let nextId = 1;

/**
 * 메시지 추가.
 * @param {{ role: 'user'|'assistant'|'system', content: string, status?: 'pending'|'streaming'|'final'|'error', id?: number|string }} arg
 * @returns {number|string} 메시지 ID
 */
export function addMessage({ role, content, status = "final", id }) {
  const messageId = id ?? nextId++;
  if (typeof id === "number" && id >= nextId) nextId = id + 1;
  chatState.messages.push({
    id: messageId,
    role,
    content,
    status,
    ts: Date.now(),
  });
  return messageId;
}

/**
 * 진행 중인 user 메시지 갱신 (STT 부분 인식 결과용).
 * - 같은 key를 가진 user 메시지가 있으면 content/status만 갱신
 * - 없으면 새로 추가
 * @param {number|string} key
 * @param {string} content
 * @param {'streaming'|'final'} status
 */
export function upsertUserMessage(key, content, status = "streaming") {
  const existing = chatState.messages.find(
    (m) => m.role === "user" && m.id === key,
  );
  if (existing) {
    existing.content = content;
    existing.status = status;
  } else {
    addMessage({ id: key, role: "user", content, status });
  }
}

/**
 * assistant 메시지에 텍스트 델타를 이어붙인다.
 * @param {number|string} id
 * @param {string} delta
 */
export function appendDelta(id, delta) {
  const m = chatState.messages.find((m) => m.id === id);
  if (!m) {
    addMessage({ id, role: "assistant", content: delta, status: "streaming" });
    return;
  }
  m.content += delta;
  m.status = "streaming";
}

/**
 * assistant 메시지를 최종 텍스트로 확정.
 * @param {number|string} id
 * @param {string} content
 */
export function finalizeMessage(id, content) {
  const m = chatState.messages.find((m) => m.id === id);
  if (!m) {
    addMessage({ id, role: "assistant", content, status: "final" });
    return;
  }
  m.content = content;
  m.status = "final";
}

/** chat에 에러 메시지 1건 푸시 */
export function setErrorMessage(text) {
  addMessage({ role: "system", content: text, status: "error" });
}

export function updateMessage(id, patch) {
  const idx = chatState.messages.findIndex((m) => m.id === id);
  if (idx >= 0)
    chatState.messages[idx] = { ...chatState.messages[idx], ...patch };
}

export function appendToMessage(id, delta) {
  const m = chatState.messages.find((m) => m.id === id);
  if (m) m.content += delta;
}

export function clearChat() {
  chatState.messages = [];
}
