// SvelteKit prerender 설정 (정적 export → privseai.com/maya 서빙)
export const prerender = true;
export const ssr = true;
export const trailingSlash = "never";

// Backend 설정 — 환경별 오버라이드
// 1) VITE_MAYA_WS_URL 빌드 시 주입 → 권장
// 2) window.__MAYA_WS_URL__ 런타임 주입 (privseai.com/maya/index.html에 인라인)
// 3) 기본값 (로컬 dev): ws://localhost:8765/ws
export const load = async () => {
  // 클라이언트 빌드 시점에 ws URL을 가져오기 위해 layout에서 부트스트랩
  return {};
};
