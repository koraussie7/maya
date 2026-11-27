/**
 * Knowledge base state — profiles, search hits, graph, ingest state.
 *
 * Svelte 5 runes mode. Mirrors chat.svelte.js: top-level $state object
 * with imperative helpers that mutate in place.
 *
 * 데이터 모델은 CODEPLAN §0.3 의 /knowledge REST 응답과 1:1.
 */

/**
 * @typedef {{
 *   id: string,
 *   name: string,
 *   doc_count?: number,
 *   created_at?: string,
 * }} KProfile
 *
 * @typedef {{
 *   chunk_id: string,
 *   text: string,
 *   score: number,
 *   source?: string,
 *   profile_id?: string,
 *   doc_name?: string,
 *   entity_ids?: string[],
 *   laya_score?: number,
 * }} KHit
 *
 * @typedef {{
 *   id: string,
 *   label: string,
 *   type?: string,
 * }} GNode
 *
 * @typedef {{
 *   src: string,
 *   dst: string,
 *   weight?: number,
 *   rel?: string,
 * }} GEdge
 */

/** @type {{ profiles: KProfile[], hits: KHit[], query: string, profile_id: string|null, last_search_at: number|null, graph: { nodes: GNode[], edges: GEdge[] }, graph_profile_id: string|null, busy: { list: boolean, search: boolean, ingest: boolean, delete: boolean, graph: boolean, profile: boolean }, error: string|null }} */
export const knowledgeState = $state({
  /** 프로필 목록 (가장 최근 ingest 가 위로) */
  profiles: [],
  /** 마지막 검색 결과 (점수 내림차순) */
  hits: [],
  /** 마지막 검색어 */
  query: "",
  /** 현재 선택된 프로필 (UI 컨텍스트, 검색/그래프 페이지에서 함께 쓰임) */
  profile_id: null,
  /** 마지막 검색 시각 (epoch ms) */
  last_search_at: null,
  /** /knowledge/graph 결과 */
  graph: { nodes: [], edges: [] },
  /** 그래프가 어느 프로필 기준인지 */
  graph_profile_id: null,
  /** 각 작업 in-flight 표시 — 버튼 disabled / spinner 표시용 */
  busy: {
    list: false,
    search: false,
    ingest: false,
    delete: false,
    graph: false,
    profile: false,
  },
  /** 마지막 에러 메시지 */
  error: null,
});

/** @param {KProfile[]} list */
export function setProfiles(list) {
  knowledgeState.profiles = Array.isArray(list) ? list.slice() : [];
}

/** @param {KProfile} p */
export function upsertProfile(p) {
  if (!p || !p.id) return;
  const i = knowledgeState.profiles.findIndex((x) => x.id === p.id);
  if (i >= 0) {
    knowledgeState.profiles[i] = { ...knowledgeState.profiles[i], ...p };
  } else {
    knowledgeState.profiles = [p, ...knowledgeState.profiles];
  }
}

/** @param {string} id */
export function removeProfile(id) {
  knowledgeState.profiles = knowledgeState.profiles.filter((p) => p.id !== id);
  if (knowledgeState.profile_id === id) knowledgeState.profile_id = null;
}

/** @param {string} id */
export function selectProfile(id) {
  knowledgeState.profile_id = id || null;
}

/** @param {string} q */
export function setQuery(q) {
  knowledgeState.query = q ?? "";
}

/** @param {KHit[]} hits */
export function setHits(hits) {
  knowledgeState.hits = Array.isArray(hits) ? hits.slice() : [];
  knowledgeState.last_search_at = Date.now();
}

/** @param {string} text @param {number} score @param {string} [doc_name] */
export function prependHit(text, score, doc_name) {
  knowledgeState.hits = [
    { chunk_id: "tmp-" + Date.now(), text, score, doc_name },
    ...knowledgeState.hits,
  ].slice(0, 20);
}

/** @param {{ nodes: GNode[], edges: GEdge[] }} g @param {string|null} profile_id */
export function setGraph(g, profile_id) {
  knowledgeState.graph =
    g && typeof g === "object"
      ? {
          nodes: Array.isArray(g.nodes) ? g.nodes.slice() : [],
          edges: Array.isArray(g.edges) ? g.edges.slice() : [],
        }
      : { nodes: [], edges: [] };
  knowledgeState.graph_profile_id = profile_id ?? null;
}

/**
 * @param {keyof typeof knowledgeState.busy} k
 * @param {boolean} v
 */
export function setBusy(k, v) {
  knowledgeState.busy[k] = !!v;
}

/** @param {string|null} e */
export function setError(e) {
  knowledgeState.error = e;
}

export function clearError() {
  knowledgeState.error = null;
}

export function clearHits() {
  knowledgeState.hits = [];
  knowledgeState.query = "";
  knowledgeState.last_search_at = null;
}
