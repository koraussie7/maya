<script>
  import { chatState, clearChat } from '../stores/chat.svelte.js';
  import { sessionState } from '../stores/session.svelte.js';
  import { tick } from 'svelte';

  let scroller = $state(null);

  /** @type {{ onSend?: (text: string) => void }} */
  let { onSend } = $props();

  let input = $state('');
  let suggestions = ['오늘 일정 뭐야?', '가까운 카페 찾아줘', '회의 시작해줘', '메모해줘'];

  $effect(() => {
    // 의존성 추적: 메시지 목록이 변경될 때마다 스크롤
    chatState.messages.length;
    tick().then(() => {
      if (scroller) scroller.scrollTop = scroller.scrollHeight;
    });
  });

  function submit(e) {
    if (e && typeof e.preventDefault === 'function') e.preventDefault();
    const t = input.trim();
    if (!t) return;
    onSend?.(t);
    input = '';
  }

  function quick(s) {
    input = s;
    submit();
  }
</script>

<div class="chat">
  <header>
    <h2>대화</h2>
    <button class="clear" onclick={() => clearChat()} aria-label="대화 지우기">지우기</button>
  </header>

  <div class="scroller" bind:this={scroller}>
    {#if chatState.messages.length === 0}
      <div class="empty">
        <p>버튼을 누르고 말하거나, 텍스트로 입력해 보세요.</p>
        <div class="chips">
          {#each suggestions as s}
            <button class="chip" onclick={() => quick(s)}>{s}</button>
          {/each}
        </div>
      </div>
    {:else}
      <ul>
        {#each chatState.messages as m (m.id)}
          <li class="msg" class:user={m.role === 'user'} class:assistant={m.role === 'assistant'}>
            <div class="role">{m.role === 'user' ? '나' : '마야'}</div>
            <div class="bubble" data-status={m.status}>
              <span>{m.content || (m.status === 'streaming' ? '…' : '')}</span>
              {#if m.status === 'streaming'}<span class="caret"></span>{/if}
            </div>
          </li>
        {/each}
      </ul>
    {/if}
  </div>

  <form onsubmit={submit}>
    <input
      type="text"
      placeholder={sessionState.phase === 'listening' ? '듣고 있어요…' : '텍스트로 입력'}
      bind:value={input}
      disabled={sessionState.phase === 'thinking'}
    />
    <button type="submit" aria-label="보내기" disabled={!input.trim() || sessionState.phase === 'thinking'}>
      <svg viewBox="0 0 24 24" width="18" height="18" fill="none">
        <path d="M4 12h14m0 0-5-5m5 5-5 5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
    </button>
  </form>
</div>

<style>
  .chat {
    display: flex;
    flex-direction: column;
    height: 100%;
    background: rgb(15 16 26 / 0.55);
    border: 1px solid rgb(255 255 255 / 0.08);
    border-radius: 16px;
    backdrop-filter: blur(10px);
    overflow: hidden;
  }
  header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 14px 18px;
    border-bottom: 1px solid rgb(255 255 255 / 0.06);
  }
  header h2 { font-size: 14px; font-weight: 600; margin: 0; color: rgb(255 255 255 / 0.85); }
  .clear {
    background: transparent;
    border: 1px solid rgb(255 255 255 / 0.12);
    color: rgb(255 255 255 / 0.7);
    border-radius: 8px;
    padding: 4px 10px;
    font-size: 12px;
    cursor: pointer;
  }
  .clear:hover { color: white; border-color: rgb(255 255 255 / 0.3); }

  .scroller { flex: 1; overflow-y: auto; padding: 16px; }
  .empty { color: rgb(255 255 255 / 0.6); text-align: center; padding: 24px 8px; }
  .chips { display: flex; flex-wrap: wrap; gap: 8px; justify-content: center; margin-top: 14px; }
  .chip {
    background: rgb(255 255 255 / 0.06);
    color: rgb(255 255 255 / 0.85);
    border: 1px solid rgb(255 255 255 / 0.1);
    border-radius: 9999px;
    padding: 6px 12px;
    font-size: 12px;
    cursor: pointer;
    transition: background 120ms;
  }
  .chip:hover { background: rgb(124 92 255 / 0.18); border-color: rgb(124 92 255 / 0.45); }

  ul { list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: 12px; }
  .msg { display: flex; flex-direction: column; max-width: 85%; }
  .msg.user     { align-self: flex-end; }
  .msg.assistant{ align-self: flex-start; }
  .role { font-size: 11px; color: rgb(255 255 255 / 0.45); margin-bottom: 4px; }
  .bubble {
    padding: 10px 14px;
    border-radius: 14px;
    font-size: 14px;
    line-height: 1.55;
    word-break: break-word;
    white-space: pre-wrap;
  }
  .msg.user .bubble {
    background: linear-gradient(135deg, rgb(124 92 255 / 0.85), rgb(88 225 255 / 0.6));
    color: white;
    border-bottom-right-radius: 4px;
  }
  .msg.assistant .bubble {
    background: rgb(255 255 255 / 0.06);
    color: rgb(255 255 255 / 0.92);
    border-bottom-left-radius: 4px;
    border: 1px solid rgb(255 255 255 / 0.06);
  }
  .bubble[data-status="error"] { color: rgb(252 165 165); border-color: rgb(248 113 113 / 0.3); }
  .caret {
    display: inline-block;
    width: 7px; height: 14px;
    margin-left: 2px;
    background: rgb(255 255 255 / 0.7);
    animation: blink 1s infinite step-end;
    vertical-align: text-bottom;
  }
  @keyframes blink { 50% { opacity: 0; } }

  form {
    display: flex; gap: 8px; padding: 12px;
    border-top: 1px solid rgb(255 255 255 / 0.06);
  }
  input {
    flex: 1;
    background: rgb(255 255 255 / 0.05);
    border: 1px solid rgb(255 255 255 / 0.1);
    border-radius: 10px;
    color: white;
    padding: 10px 14px;
    font-size: 14px;
    outline: none;
  }
  input:focus { border-color: rgb(124 92 255 / 0.6); }
  input:disabled { opacity: 0.5; }
  form button {
    background: rgb(124 92 255);
    border: 0;
    border-radius: 10px;
    color: white;
    padding: 0 14px;
    cursor: pointer;
    display: grid; place-items: center;
  }
  form button:hover { background: rgb(140 108 255); }
  form button:disabled { opacity: 0.4; cursor: not-allowed; }
</style>
