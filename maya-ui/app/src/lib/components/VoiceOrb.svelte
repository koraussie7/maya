<script>
  import { sessionState } from '../stores/session.svelte.js';

  /** @type {{ onclick: () => void, size?: number }} */
  let { onclick, size = 220 } = $props();

  // Voice Orb 회전/맥동 각도 (스트리밍 시 변속)
  let t = $state(0);
  let raf = 0;

  $effect(() => {
    const phase = sessionState.phase;
    if (phase === 'idle') {
      cancelAnimationFrame(raf);
      return;
    }
    let last = performance.now();
    const speed = phase === 'speaking' ? 0.0028 : phase === 'listening' ? 0.0018 : 0.0013;
    const loop = (now) => {
      t += (now - last) * speed;
      last = now;
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
  });

  // 그라디언트 회전
  const angle = $derived(`${(t * 60) % 360}deg`);

  // 위상별 색조
  const hueA = $derived.by(() => {
    switch (sessionState.phase) {
      case 'listening': return '200';
      case 'thinking':  return '270';
      case 'speaking':  return '170';
      case 'error':     return '0';
      default:          return '255';
    }
  });

  // 위상별 스케일 (레벨/맥동)
  const pulse = $derived(
    sessionState.phase === 'listening' ? 1 + sessionState.level * 0.15 :
    sessionState.phase === 'speaking'  ? 1.04 :
    sessionState.phase === 'thinking'  ? 1.02 :
    1
  );
</script>

<button
  class="orb-btn"
  aria-label="음성 토글"
  onclick={onclick}
  style="--size:{size}px; --hueA:{hueA}; transform:scale({pulse});"
>
  <span class="ring ring-1" style="transform:rotate({angle})"></span>
  <span class="ring ring-2" style="transform:rotate({-angle})"></span>
  <span class="ring ring-3"></span>
  <span class="core">
    {#if sessionState.phase === 'listening'}
      <svg viewBox="0 0 24 24" width="40" height="40" fill="none">
        <path d="M12 4a3 3 0 0 0-3 3v5a3 3 0 0 0 6 0V7a3 3 0 0 0-3-3Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>
        <path d="M5 11a7 7 0 0 0 14 0M12 18v3" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>
      </svg>
    {:else if sessionState.phase === 'thinking'}
      <span class="dots"><span></span><span></span><span></span></span>
    {:else if sessionState.phase === 'speaking'}
      <svg viewBox="0 0 24 24" width="40" height="40" fill="none">
        <path d="M5 9h4l5-4v14l-5-4H5z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>
        <path d="M16 8a5 5 0 0 1 0 8" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>
      </svg>
    {:else if sessionState.phase === 'error'}
      <svg viewBox="0 0 24 24" width="40" height="40" fill="none">
        <path d="M12 8v5m0 3v.01M12 4a8 8 0 1 1 0 16 8 8 0 0 1 0-16Z" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>
      </svg>
    {:else}
      <svg viewBox="0 0 24 24" width="40" height="40" fill="none">
        <path d="M12 4a3 3 0 0 0-3 3v5a3 3 0 0 0 6 0V7a3 3 0 0 0-3-3Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>
        <path d="M5 11a7 7 0 0 0 14 0M12 18v3" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>
      </svg>
    {/if}
  </span>
</button>

<style>
  .orb-btn {
    --size: 220px;
    position: relative;
    width: var(--size);
    height: var(--size);
    border-radius: 9999px;
    border: 0;
    background: transparent;
    color: white;
    cursor: pointer;
    display: grid;
    place-items: center;
    transition: transform 120ms ease;
  }
  .orb-btn:hover { transform: scale(1.02); }

  .ring {
    position: absolute; inset: 0;
    border-radius: inherit;
    pointer-events: none;
    will-change: transform, opacity;
  }
  .ring-1 {
    background: conic-gradient(from var(--angle, 0deg), hsl(var(--hueA) 90% 65%), hsl(calc(var(--hueA) + 60) 90% 55%), hsl(calc(var(--hueA) + 180) 90% 70%), hsl(var(--hueA) 90% 65%));
    mask: radial-gradient(circle, transparent 56%, black 58%, black 64%, transparent 66%);
    -webkit-mask: radial-gradient(circle, transparent 56%, black 58%, black 64%, transparent 66%);
    filter: blur(.5px);
    opacity: .85;
  }
  .ring-2 {
    inset: 8%;
    background: conic-gradient(from 0deg, transparent, hsl(var(--hueA) 95% 70% / 0.7), transparent 25%, transparent 50%, hsl(calc(var(--hueA) + 90) 95% 65% / 0.5), transparent 75%);
    mask: radial-gradient(circle, transparent 64%, black 66%, black 70%, transparent 72%);
    -webkit-mask: radial-gradient(circle, transparent 64%, black 66%, black 70%, transparent 72%);
    opacity: .55;
  }
  .ring-3 {
    inset: 18%;
    background: radial-gradient(circle at 30% 30%, hsl(var(--hueA) 100% 80% / 0.45), transparent 60%);
    filter: blur(20px);
  }

  .core {
    width: 38%;
    height: 38%;
    border-radius: 9999px;
    background: radial-gradient(circle at 35% 30%, hsl(var(--hueA) 95% 70%), hsl(calc(var(--hueA) + 60) 90% 50%));
    display: grid;
    place-items: center;
    box-shadow:
      inset 0 0 24px rgb(255 255 255 / 0.25),
      0 0 36px hsl(var(--hueA) 90% 55% / 0.45);
  }

  .dots { display: flex; gap: 6px; align-items: center; }
  .dots span {
    width: 8px; height: 8px; border-radius: 9999px;
    background: white;
    animation: bounce 1.1s infinite ease-in-out;
  }
  .dots span:nth-child(2) { animation-delay: .15s; }
  .dots span:nth-child(3) { animation-delay: .3s; }
  @keyframes bounce {
    0%, 80%, 100% { transform: scale(0.6); opacity: 0.6; }
    40% { transform: scale(1); opacity: 1; }
  }
</style>
