<script>
  import { sessionState } from '../stores/session.svelte.js';

  /** @type {{ bars?: number, height?: number }} */
  let { bars = 48, height = 64 } = $props();

  // sessionState.level (0~1 RMS) 를 부드럽게 누적해서 표시
  let history = $state(new Array(bars).fill(0));
  let raf = 0;
  let last = performance.now();

  $effect(() => {
    const loop = (now) => {
      const dt = now - last;
      last = now;
      // 입력 level → 다음 막대
      const lvl = sessionState.level;
      const next = [...history.slice(1), lvl];
      // 감쇠
      history = next.map((v, i) => v * (0.92 + i * 0.0017));
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
  });
</script>

<div class="wf" style="--h:{height}px;">
  {#each history as v, i (i)}
    <span
      class="bar"
      style:--v={v}
      style:--i={i}
      class:hot={sessionState.phase === 'speaking'}
      class:listening={sessionState.phase === 'listening'}
    ></span>
  {/each}
</div>

<style>
  .wf {
    display: flex;
    align-items: center;
    gap: 3px;
    width: 100%;
    height: var(--h);
  }
  .bar {
    flex: 1 1 0;
    height: calc(8% + var(--v) * 92%);
    border-radius: 4px;
    background: linear-gradient(180deg, rgb(124 92 255 / 0.85), rgb(88 225 255 / 0.55));
    box-shadow: 0 0 6px rgb(124 92 255 / 0.35);
    transition: background 200ms;
  }
  .bar.listening {
    background: linear-gradient(180deg, rgb(88 225 255 / 0.95), rgb(124 92 255 / 0.6));
  }
  .bar.hot {
    background: linear-gradient(180deg, rgb(74 222 128 / 0.95), rgb(88 225 255 / 0.7));
  }
</style>
