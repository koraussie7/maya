/**
 * Backend endpoints — 빌드 시 또는 런타임에 결정
 * 우선순위:
 *   1) import.meta.env.VITE_MAYA_WS_URL (Vite 빌드 시 주입)
 *   2) window.__MAYA_WS_URL__  (privseai.com/maya 호스팅 HTML에서 인라인 주입)
 *   3) 기본값 (로컬 dev)
 */
const FALLBACK_WS = "ws://localhost:8765/ws";
const FALLBACK_HTTP = "http://localhost:8765";

import { base } from "$app/paths";

/**
 * SvelteKit 의 `base`("/maya"). 빌드에서 상수로 치환되므로
 * `/maya/decisions` 같은 하위 라우트에서도 항상 `/maya` 를 준다.
 * (import.meta.env.BASE_URL 은 빌드에서 "./" 로 치환될 수 있어 못 믿는다 — 실측)
 * @returns {string} trailing slash 없는 base (루트면 "")
 */
function kitBase() {
  return (base || "").replace(/\/$/, "");
}

function pick(...vals) {
  for (const v of vals)
    if (v && typeof v === "string" && v.length > 0) return v;
  return undefined;
}

/**
 * 같은 오리진 기준 기본값 (옵션 D: 서버 직접 서빙).
 *
 * 이전엔 원격일 때 `maya.privseai.com/ws` 를 하드코딩했는데 그 호스트는
 * nginx·DNS 어디에도 없어서 WS 가 항상 실패했다. 배포 경로가 `privseai.com/maya/` 이므로
 * BASE_URL 옆의 `/ws` 를 쓴다:
 *   https://privseai.com/maya/ → wss://privseai.com/maya/ws
 * 로컬 개발에서 8765 를 직접 쓰려면 `VITE_MAYA_WS_URL` 로 덮으면 된다.
 */
function sameOriginWs() {
  // 2026-11-24 실측: SvelteKit 의 `base` 는 빌드 시점에 `import.meta.env.BASE_URL`
  // 로 들어가지 않는다(빌드 결과에서 "./" 로 치환됨).
  // → `$app/paths` 의 `base` 를 쓴다. 이건 "/maya" 로 치환되므로
  //   `/maya/decisions` 같은 하위 라우트에서도 정확히 `/maya` 를 준다.
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}${kitBase()}/ws`;
}

function sameOriginHttp() {
  return `${location.protocol}//${location.host}`;
}

export function resolveWsUrl() {
  if (typeof window === "undefined") return FALLBACK_WS;
  const env = /** @type {any} */ (import.meta).env ?? {};
  return pick(window.__MAYA_WS_URL__, env.VITE_MAYA_WS_URL, sameOriginWs(), FALLBACK_WS);
}

export function resolveHttpBase() {
  if (typeof window === "undefined") return FALLBACK_HTTP;
  const env = /** @type {any} */ (import.meta).env ?? {};
  return pick(
    window.__MAYA_HTTP_BASE__,
    env.VITE_MAYA_HTTP_BASE,
    sameOriginHttp(),
    FALLBACK_HTTP,
  );
}
