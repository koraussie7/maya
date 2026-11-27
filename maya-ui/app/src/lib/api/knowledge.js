/**
 * Knowledge REST client — CODEPLAN §0.3 명세 그대로 7개 엔드포인트 래퍼.
 *
 * · MayaClient(WS) 와 별개. fetch 기반 동기 호출 — UI 페이지/컴포넌트가
 *   `onMount`/`$effect` 에서 직접 await.
 * · base URL 은 lib/config.js 의 resolveHttpBase() 와 동일하게 결정한다.
 *   (VITE_MAYA_HTTP_BASE → __MAYA_HTTP_BASE__ → 현재 origin).
 *
 * CODEPLAN §0.4 회귀 방지: backend 가 꺼져 있거나 /knowledge 라우터가
 * 미등록일 때 에러를 잡아서 throw — 호출자가 잡아서 토스트로 보여준다.
 */

import { resolveHttpBase } from "../config.js";

/**
 * @returns {string} base URL (e.g. "http://localhost:8765")
 */
function baseUrl() {
  return resolveHttpBase() ?? "http://localhost:8765";
}

/**
 * @template T
 * @param {string} path  e.g. "/knowledge/health"
 * @param {RequestInit & { json?: any }} [opts]
 * @returns {Promise<T>}
 */
async function request(path, opts = {}) {
  const url = baseUrl() + path;
  const headers = new Headers(opts.headers || {});
  /** @type {RequestInit} */
  const init = { method: opts.method ?? "GET", headers };
  if (opts.json !== undefined) {
    headers.set("Content-Type", "application/json");
    init.body = JSON.stringify(opts.json);
  } else if (opts.body !== undefined) {
    init.body = opts.body;
  }
  if (opts.signal) init.signal = opts.signal;
  const res = await fetch(url, init);
  if (!res.ok) {
    const txt = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText} — ${txt || url}`);
  }
  // 일부 엔드포인트는 비어있을 수 있음 (e.g. 204)
  const ct = res.headers.get("content-type") ?? "";
  if (!ct.includes("application/json")) {
    /** @type {any} */
    const empty = {};
    try {
      return /** @type {T} */ (await res.json());
    } catch {
      return /** @type {T} */ (empty);
    }
  }
  return /** @type {T} */ (await res.json());
}

/**
 * @returns {Promise<{status:string,laya_loaded:boolean,doc_count:number,graph_entity_count:number}>}
 */
export function health() {
  return request("/knowledge/health");
}

/**
 * @param {{profile_id: string, files: Array<{name: string, content_base64: string, mime: string}>}} payload
 * @returns {Promise<{doc_id: string, chunks_added: number, entities_added: number}>}
 */
export function ingest(payload) {
  return request("/knowledge/ingest", {
    method: "POST",
    json: payload,
  });
}

/**
 * @param {{query: string, top_k?: number, profile_id?: string|null}} payload
 * @returns {Promise<{hits: Array<any>}>}
 */
export function search(payload) {
  return request("/knowledge/search", {
    method: "POST",
    json: payload,
  });
}

/**
 * @param {{profile_id: string}} payload
 * @returns {Promise<{deleted: number}>}
 */
export function deleteProfile(payload) {
  return request("/knowledge/delete", {
    method: "POST",
    json: payload,
  });
}

/**
 * @returns {Promise<{profiles: Array<{id:string,name:string,doc_count?:number,created_at?:string}>}>}
 */
export function listProfiles() {
  return request("/knowledge/profiles");
}

/**
 * @param {{profile_id?: string|null}} [payload]
 * @returns {Promise<{nodes: Array<{id:string,label:string,type?:string}>, edges: Array<{src:string,dst:string,weight?:number,rel?:string}>}>}
 */
export function graph(payload = {}) {
  const qs = payload.profile_id
    ? `?profile_id=${encodeURIComponent(payload.profile_id)}`
    : "";
  return request(`/knowledge/graph${qs}`);
}

/**
 * @param {string} profile_id
 * @returns {Promise<{id:string,name:string,doc_count:number,top_entities: Array<{id:string,label:string,count:number}>}>}
 */
export function profile(profile_id) {
  return request(`/knowledge/profile/${encodeURIComponent(profile_id)}`);
}

/**
 * 클라이언트 측에서 파일을 base64 로 변환 (browser FileReader).
 * ingest.files[].content_base64 필드에 채워 넣는다.
 *
 * @param {File} file
 * @returns {Promise<{name: string, content_base64: string, mime: string}>}
 */
export function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = /** @type {string} */ (reader.result || "");
      // "data:<mime>;base64,<payload>" — payload 만 잘라 보낸다.
      const comma = result.indexOf(",");
      const payload = comma >= 0 ? result.slice(comma + 1) : result;
      resolve({
        name: file.name,
        content_base64: payload,
        mime: file.type || "text/plain",
      });
    };
    reader.onerror = () => reject(reader.error || new Error("FileReader 실패"));
    reader.readAsDataURL(file);
  });
}
