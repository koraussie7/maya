/**
 * Backend endpoints — 빌드 시 또는 런타임에 결정
 * 우선순위:
 *   1) import.meta.env.VITE_MAYA_WS_URL (Vite 빌드 시 주입)
 *   2) window.__MAYA_WS_URL__  (privseai.com/maya 호스팅 HTML에서 인라인 주입)
 *   3) 기본값 (로컬 dev)
 */
const FALLBACK_WS = "ws://localhost:8765/ws";
const FALLBACK_HTTP = "http://localhost:8765";

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
  // 2026-11-24 실측: SvelteKit 의 `base` 는 **빌드 시점에** `import.meta.env.BASE_URL`
  // 에 들어가지 않는다(`""` 로 나온다). 그대로 쓰면 `wss://host./ws` 처럼 host 뒤에
  // 점이 붙은 주소가 만들어져 핸드신이 200 으로 깨진다.
  // → base 는 **현재 페이지 경로**에서 얻는다: `/maya/` → `/maya/ws`.
  const path = (typeof location !== "undefined" ? location.pathname : "/")
    .replace(/\/(index\.html)?$/, "")
    .replace(/\/$/, "");
  const base = path || (import.meta.env?.BASE_URL || "").replace(/\/$/, "");
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}${base}/ws`;
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
