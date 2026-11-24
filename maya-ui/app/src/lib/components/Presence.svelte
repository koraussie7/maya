<script>
  import { sessionState } from '../stores/session.svelte.js';

  const label = $derived.by(() => {
    switch (sessionState.phase) {
      case 'listening': return '듣고 있어요';
      case 'thinking':  return '생각 중…';
      case 'speaking':  return '말하는 중';
      case 'error':     return '오류 발생';
      default:          return sessionState.connected ? '대기 중' : '연결 대기';
    }
  });
</script>

<div class="presence" class:error={sessionState.phase === 'error'}>
  <span class="dot" data-phase={sessionState.phase}></span>
  <span class="label">{label}</span>
</div>

<style>
  .presence {
    display: inline-flex;
    align-items: center;
    gap: 10px;
    padding: 8px 14px;
    border-radius: 9999px;
    background: rgb(255 255 255 / 0.06);
    border: 1px solid rgb(255 255 255 / 0.1);
    backdrop-filter: blur(8px);
    color: rgb(255 255 255 / 0.85);
    font-size: 13px;
    letter-spacing: 0.01em;
  }
  .dot {
    width: 8px; height: 8px; border-radius: 9999px;
    background: rgb(124 92 255);
    box-shadow: 0 0 8px rgb(124 92 255 / 0.6);
  }
  .dot[data-phase="listening"] { background: rgb(88 225 255); box-shadow: 0 0 8px rgb(88 225 255 / 0.6); }
  .dot[data-phase="thinking"]  { background: rgb(168 85 247); box-shadow: 0 0 8px rgb(168 85 247 / 0.6); }
  .dot[data-phase="speaking"]  { background: rgb(74 222 128); box-shadow: 0 0 8px rgb(74 222 128 / 0.6); }
  .dot[data-phase="error"]     { background: rgb(248 113 113); box-shadow: 0 0 8px rgb(248 113 113 / 0.6); }
  .presence.error { border-color: rgb(248 113 113 / 0.4); color: rgb(252 165 165); }
</style>
