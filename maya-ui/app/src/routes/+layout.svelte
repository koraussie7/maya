<script>
  import '../app.css';
  import { base } from '$app/paths';
  import { page } from '$app/stores';

  let { children } = $props();

  /** @type {Array<{href: string, label: string, match: (p: string) => boolean}>} */
  const NAV = [
    { href: `${base}/`, label: '대화', match: (p) => p === `${base}/` || p === `${base}` || p === '/' },
    { href: `${base}/decisions`, label: '결정 로그', match: (p) => p.startsWith(`${base}/decisions`) },
    { href: `${base}/knowledge`, label: '지식', match: (p) => p.startsWith(`${base}/knowledge`) },
  ];
</script>

<div class="bg-aurora" aria-hidden="true"></div>

<header class="nav">
  <a class="brand" href={`${base}/`} aria-label="Maya 대화 화면">
    <span class="dot" aria-hidden="true"></span>
    <span class="name">Maya</span>
  </a>
  <nav aria-label="주요 화면">
    {#each NAV as item (item.href)}
      <a
        class="link"
        class:active={item.match($page.url.pathname)}
        href={item.href}
        aria-current={item.match($page.url.pathname) ? 'page' : undefined}
      >
        {item.label}
      </a>
    {/each}
  </nav>
</header>

{@render children()}

<style>
  .nav {
    position: sticky;
    top: 0;
    z-index: 10;
    display: flex;
    align-items: center;
    gap: 20px;
    padding: 10px 20px;
    border-bottom: 1px solid rgb(255 255 255 / 0.08);
    background: rgb(11 13 18 / 0.72);
    backdrop-filter: blur(12px);
    font-family: var(--font-display);
  }
  .brand {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    text-decoration: none;
    color: var(--fg-0);
  }
  .dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--brand);
    box-shadow: 0 0 10px rgb(124 92 255 / 0.8);
  }
  .name {
    font-size: 14px;
    font-weight: 600;
    letter-spacing: -0.01em;
  }
  nav {
    display: flex;
    gap: 4px;
  }
  .link {
    padding: 5px 10px;
    border-radius: var(--radius-pill);
    font-size: 13px;
    color: var(--fg-1);
    text-decoration: none;
    transition:
      color 0.15s ease,
      background 0.15s ease;
  }
  .link:hover {
    color: var(--fg-0);
    background: rgb(255 255 255 / 0.06);
  }
  .link.active {
    color: var(--fg-0);
    background: rgb(124 92 255 / 0.16);
    box-shadow: inset 0 0 0 1px rgb(124 92 255 / 0.35);
  }
  @media (max-width: 520px) {
    .nav {
      gap: 10px;
      padding: 8px 12px;
    }
    .name {
      display: none;
    }
  }
</style>
