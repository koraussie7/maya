import adapter from "@sveltejs/adapter-static";
import { vitePreprocess } from "@sveltejs/vite-plugin-svelte";

/** @type {import('@sveltejs/kit').Config} */
const config = {
  preprocess: vitePreprocess(),
  kit: {
    paths: { base: "/maya" }, // privseai.com/maya
    adapter: adapter({
      pages: "build",
      assets: "build",
      fallback: "index.html",
      precompress: false,
      strict: false,
    }),
    // "*" 프리렌더는 페이지 안의 링크(/memory, /settings)까지 따라가서 404로 빌드를 죽인다.
    // 루트만 정적 생성하고 나머지는 fallback(index.html)에서 SPA 라우팅한다.
    prerender: {
      entries: ["/"],
      // 페이지 내 링크를 크롤링하지 않는다 — 라우트 대부분이 클라이언트 전용
      // (/decisions, /knowledge/**)이라 크롤링하면 미생성 라우트에서 실패한다.
      crawl: false,
      handleHttpError: "warn",
      handleUnseenRoutes: "ignore",
    },
  },
};

export default config;
