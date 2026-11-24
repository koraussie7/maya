<script>
  import { onMount, onDestroy } from 'svelte';
  import VoiceOrb from '$lib/components/VoiceOrb.svelte';
  import Waveform from '$lib/components/Waveform.svelte';
  import Presence from '$lib/components/Presence.svelte';
  import Chat from '$lib/components/Chat.svelte';
  import { sessionState, setPhase, setMic, setLevel, setSpeaking, setError } from '$lib/stores/session.svelte.js';
  import { addMessage, appendDelta, finalizeMessage, upsertUserMessage } from '$lib/stores/chat.svelte.js';
  import { MayaClient } from '$lib/mayaClient.js';
  import { base } from '$app/paths';
  // base 경로(`/maya`) 기준 링크 — 루트(`/maya/`)에서 직접 열어도 하위 라우트가 깨지지 않는다
  const NAV = [
    { label: '대화', href: `${base}/` },
    { label: '기억', href: `${base}/memory` },
    { label: '설정', href: `${base}/settings` },
  ];

  /** @type {AudioContext | undefined} */
  let audioCtx = /** @type {any} */ (undefined);
  /** @type {MediaStream | undefined} */
  let micStream;
  /** @type {MediaStreamAudioSourceNode | undefined} */
  let micNode;
  /** @type {AudioWorkletNode | undefined} */
  let workletNode;
  /** @type {AudioBufferSourceNode | undefined} */
  let currentSource;
  let ws = new MayaClient({});

  // ── 목소리 선택(페르소나) ───────────────────────────────────────────
  // 서버의 persona 파일 이름과 1:1. 값은 /maya/ 의 서버와 같은 규칙
  // (maya/personas/<file>.yaml). 기본은 A(본인 음성).
  /** @type {{id: string, label: string}[]} */
  const VOICES = [
    { id: 'lyra_vex', label: 'Lyra · 남성(A)' },
    { id: 'lyra_vex_b', label: 'Lyra · 여성(B)' },
    { id: 'sora', label: 'Sora · 여성(B)' },
  ];
  /** @type {string} */
  let voiceId = $state(VOICES[0].id);
  // 메뉴버튼(SillyTavern 의 캐릭터 선택 패널 계보: 좌측 사이드바 + 페르소나 카드 목록)
  // 이 컴포넌트는 **runes 모드**($state 사용)라 `$:` 반응형 대신 `$derived` 를 쓴다.
  /** @type {boolean} */
  let voiceMenuOpen = $state(false);
  /** @type {string} */
  let voiceLabel = $derived(VOICES.find((v) => v.id === voiceId)?.label ?? voiceId);
  /**
   * 상대(페르소나) 선택 — 카드 클릭.
   * @param {string} id
   */
  function pickVoice(id) {
    voiceId = id;
    voiceMenuOpen = false;
  }

  // Audio worklet 코드 — 16kHz로 다운샘플링하여 Float32Array 버퍼를 출력
  const WORKLET_SRC = `
    class PCM16kWorklet extends AudioWorkletProcessor {
      constructor() { super(); this.targetRate = 16000; this._buffer = []; this._lastSampleTime = 0; }
      process(inputs) {
        const input = inputs[0];
        if (!input || input.length === 0) return true;
        const ch = input[0];
        // 단순 비율 다운샘플 (sampleRate 48000 → 16000 가정, 그 외는 비율 산정)
        const inRate = sampleRate;
        const ratio = inRate / this.targetRate;
        const outLen = Math.floor(ch.length / ratio);
        const out = new Float32Array(outLen);
        for (let i = 0; i < outLen; i++) {
          const idx = Math.floor(i * ratio);
          out[i] = ch[idx];
        }
        this.port.postMessage(out, [out.buffer]);
        return true;
      }
    }
    registerProcessor('pcm16k-worklet', PCM16kWorklet);
  `;

  onMount(async () => {
    // WebSocket 연결
    ws = new MayaClient({
      onPartialSTT: (text) => {
        // 진행 중인 user 메시지 갱신
        upsertUserMessage('live', text, 'streaming');
      },
      onFinalSTT: (text) => {
        upsertUserMessage('live', text, 'final');
        // 빈 입력이면 무시
      },
      onAssistantDelta: (id, text) => {
        appendDelta(id, text);
      },
      onAssistantFinal: (id, text) => {
        finalizeMessage(id, text);
      },
      onAudioOut: (id, mime, bytes) => {
        playAudio(bytes, mime).catch((e) => setError(`audio.out failed: ${e.message}`));
      },
      onLevel: (rms, peak) => setLevel(rms, peak),
      onPhase: (phase) => {
        if (phase === 'speaking') setSpeaking(true);
        else if (phase === 'listening') setPhase('listening');
        else if (phase === 'thinking') setPhase('thinking');
        else if (phase === 'idle') { setPhase('idle'); setSpeaking(false); }
        else if (phase === 'error') setPhase('error');
      },
      onError: (code, message) => {
        setErrorMessage(`[${code}] ${message}`);
        setPhase('error');
      }
    });
    try {
      await ws.connect();
      setMic(true);
      ws.startSession({ refAudioId: 'clip_ref.wav' });
    } catch (e) {
      setError(`WebSocket 연결 실패: ${e.message ?? e}`);
    }

    // AudioContext + worklet 준비
    try {
      audioCtx = /** @type {any} */ (new (window.AudioContext || window.webkitAudioContext)());
      const blob = new Blob([WORKLET_SRC], { type: 'application/javascript' });
      const url = URL.createObjectURL(blob);
      await audioCtx.audioWorklet.addModule(url);
      URL.revokeObjectURL(url);
    } catch (e) {
      setError(`AudioContext 초기화 실패: ${e.message ?? e}`);
    }
  });

  onDestroy(() => {
    ws?.endSession();
    stopMic();
    try { currentSource?.stop(); } catch {}
    audioCtx?.close();
  });

  async function requestMic() {
    try {
      micStream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1 } });
      micNode = audioCtx.createMediaStreamSource(micStream);
      workletNode = new AudioWorkletNode(audioCtx, 'pcm16k-worklet');
      let seq = 0;
      workletNode.port.onmessage = (ev) => {
        const f32 = ev.data;
        seq++;
        ws.sendAudioChunk(f32, seq);
      };
      // 로컬 레벨 측정 (백엔드의 audio.level과 무관하게 UI에 즉각 반영)
      const analyser = audioCtx.createAnalyser();
      analyser.fftSize = 1024;
      micNode.connect(analyser);
      const buf = new Float32Array(analyser.fftSize);
      const tick = () => {
        if (!analyser) return;
        analyser.getFloatTimeDomainData(buf);
        let sum = 0, peak = 0;
        for (let i = 0; i < buf.length; i++) {
          const v = buf[i];
          sum += v * v;
          if (Math.abs(v) > peak) peak = Math.abs(v);
        }
        const rms = Math.sqrt(sum / buf.length);
        setLevel(rms, peak);
        requestAnimationFrame(tick);
      };
      tick();

      micNode.connect(workletNode);
      // workletNode는 destination에 연결하면 에코 → 연결하지 않음

      setMic(true);
      setPhase('listening');
    } catch (e) {
      setMic(false);
      setError(`마이크 권한 실패: ${e.message ?? e}`);
    }
  }

  function stopMic() {
    try { workletNode?.disconnect(); } catch {}
    try { micNode?.disconnect(); } catch {}
    try { micStream?.getTracks().forEach((t) => t.stop()); } catch {}
    workletNode = undefined;
    micNode = undefined;
    micStream = undefined;
  }

  let recording = $state(false);

  async function toggle() {
    if (!audioCtx) {
      setError('AudioContext 미준비');
      return;
    }
    if (audioCtx.state === 'suspended') await audioCtx.resume();
    if (recording) {
      stopMic();
      recording = false;
      setPhase('idle');
      ws.flushAudio(Date.now());
    } else {
      await requestMic();
      recording = true;
    }
  }

  /** @param {Uint8Array} bytes @param {string} [mime] */
  async function playAudio(bytes, mime = 'audio/wav') {
    if (!audioCtx) return;
    if (mime.startsWith('audio/opus')) {
      // Opus 는 decodeAudioData 로 풀 수 없다 → WebCodecs AudioDecoder
      // (봉투: [4byte BE len][opus frame] 반복. 서버 opus_stream.py 와 동일)
      return playOpus(bytes);
    }
    if (currentSource) {
      try { currentSource.stop(); } catch {}
      currentSource = undefined;
    }
    const ab = bytes.slice(0);
    const buf = await audioCtx.decodeAudioData(ab);
    const src = audioCtx.createBufferSource();
    src.buffer = buf;
    src.connect(audioCtx.destination);
    src.start();
    currentSource = src;
    setSpeaking(true);
    src.onended = () => {
      setSpeaking(false);
      if (currentSource === src) currentSource = undefined;
    };
  }

  /**
   * Opus 프레임들을 WebCodecs 로 디코드해 순서대로 재생한다.
   *
   * · 봉투 규격: 4바이트 big-endian 길이 + opus 프레임 (20ms, 16kHz mono)
   * · AudioDecoder 는 48kHz 로 출력하는 경우가 많아 AudioContext 샘플레이트로 리샘플
   * · 프레임을 모두 이어 붙인 뒤 한 번에 재생(문장 단위라 길이가 짧아 충분)
   * @param {Uint8Array} bytes
   */
  async function playOpus(bytes) {
    if (!audioCtx) return;
    const AD = /** @type {any} */ (window).AudioDecoder;
    if (!AD) {
      setError('이 브라우저는 Opus 디코딩(WebCodecs)을 지원하지 않습니다');
      return;
    }
    /** @type {Float32Array[]} */
    const chunks = [];
    let sr = 0;
    let ch = 1;
    const dec = new AD({
      output: (/** @type {any} */ frame) => {
        sr = frame.sampleRate;
        ch = frame.numberOfChannels;
        const data = new Float32Array(frame.numberOfFrames);
        frame.copyTo(data, { planeIndex: 0 });
        chunks.push(data);
        frame.close();
      },
      error: (/** @type {any} */ e) => setError(`opus decode: ${e?.message ?? e}`),
    });
    dec.configure({ codec: 'opus', sampleRate: 16000, numberOfChannels: 1 });

    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    let off = 0;
    let ts = 0;
    while (off + 4 <= bytes.length) {
      const n = view.getUint32(off, false);
      off += 4;
      if (off + n > bytes.length) break;
      const frame = bytes.slice(off, off + n);
      off += n;
      dec.decode(new (/** @type {any} */ (window).EncodedAudioChunk)({
        type: 'key',
        timestamp: ts,
        data: frame,
      }));
      ts += 20000; // 20ms
    }
    await dec.flush();
    dec.close();

    const total = chunks.reduce((a, c) => a + c.length, 0);
    if (!total) return;
    const merged = new Float32Array(total);
    let p = 0;
    for (const c of chunks) {
      merged.set(c, p);
      p += c.length;
    }
    // 디코더 출력 샘플레이트 ≠ 컨텍스트 샘플레이트면 브라우저가 알아서 리샘플한다.
    const buffer = audioCtx.createBuffer(1, merged.length, sr || 48000);
    buffer.copyToChannel(merged, 0);
    if (currentSource) {
      try { currentSource.stop(); } catch {}
    }
    const src = audioCtx.createBufferSource();
    src.buffer = buffer;
    src.connect(audioCtx.destination);
    src.start();
    currentSource = src;
    setSpeaking(true);
    src.onended = () => {
      setSpeaking(false);
      if (currentSource === src) currentSource = undefined;
    };
    void ch;
  }

  /** @param {string} text */
  function onSendText(text) {
    addMessage({ role: 'user', content: text, status: 'final' });
    // 선택한 목소리를 이 턴에 실어 보낸다(서버가 persona 전환 → 그 음색으로 응답)
    ws.sendText(text, voiceId);
    setPhase('thinking');
  }
</script>

<svelte:head>
  <title>Maya · 음성 비서</title>
  <meta name="description" content="Maya — 당신의 음성 비서" />
</svelte:head>

<div class="root">
  <aside class="sidebar">
    <header>
      <div class="brand">
        <div class="dot"></div>
        <span>Maya</span>
      </div>
      <Presence />
    </header>

    <nav>
      {#each NAV as item, i}
        <a class={i === 0 ? 'active' : ''} href={item.href}>{item.label}</a>
      {/each}
    </nav>

    <footer>
      <!-- 상대(페르소나) 선택 — 드롭다운 대신 메뉴 버튼 + 카드 패널 -->
      <div class="voice-menu">
        {#if voiceMenuOpen}
          <div class="voice-panel" role="menu" aria-label="상대 선택">
            <div class="voice-panel-head">
              <span>상대</span>
              <button class="icon" aria-label="닫기" onclick={() => (voiceMenuOpen = false)}>✕</button>
            </div>
            {#each VOICES as v (v.id)}
              <button
                class="voice-card"
                class:sel={v.id === voiceId}
                role="menuitemradio"
                aria-checked={v.id === voiceId}
                onclick={() => pickVoice(v.id)}
              >
                <span class="avatar" data-v={v.id}></span>
                <span class="meta">
                  <span class="name">{v.label.split(' · ')[0]}</span>
                  <small>{v.label.split(' · ')[1] ?? ''}</small>
                </span>
                {#if v.id === voiceId}<span class="check">✓</span>{/if}
              </button>
            {/each}
          </div>
        {/if}
        <button
          class="voice-trigger"
          aria-haspopup="menu"
          aria-expanded={voiceMenuOpen}
          onclick={() => (voiceMenuOpen = !voiceMenuOpen)}
        >
          <span>상대</span>
          <strong>{voiceLabel}</strong>
          <span class="chev">{voiceMenuOpen ? '▴' : '▾'}</span>
        </button>
      </div>
      <small>v0.1 · privseai.com/maya</small>
    </footer>
  </aside>

  <main>
    <section class="orb">
      <VoiceOrb {recording} onClick={toggle} />
      <Waveform />
    </section>

    <section class="chat">
      <Chat onSend={onSendText} />
    </section>
  </main>
</div>

<style>
  .root {
    display: grid;
    grid-template-columns: 220px 1fr;
    height: 100vh;
    width: 100vw;
    color: white;
  }
  .sidebar {
    border-right: 1px solid rgb(255 255 255 / 0.06);
    background: rgb(11 13 18 / 0.6);
    backdrop-filter: blur(10px);
    display: flex; flex-direction: column;
    padding: 18px 14px;
  }
  .sidebar header { display: flex; flex-direction: column; gap: 14px; margin-bottom: 18px; }
  .brand {
    display: flex; align-items: center; gap: 8px;
    font-weight: 700; letter-spacing: 0.4px;
  }
  .brand .dot {
    width: 10px; height: 10px; border-radius: 9999px;
    background: linear-gradient(135deg, #7c5cff, #58e1ff);
    box-shadow: 0 0 12px rgb(124 92 255 / 0.7);
  }
  nav { display: flex; flex-direction: column; gap: 4px; margin-top: 6px; }
  nav a {
    color: rgb(255 255 255 / 0.7);
    padding: 8px 12px;
    border-radius: 8px;
    text-decoration: none;
    font-size: 13px;
  }
  nav a:hover { color: white; background: rgb(255 255 255 / 0.04); }
  nav a.active { color: white; background: rgb(124 92 255 / 0.18); }
  .sidebar footer { margin-top: auto; color: rgb(255 255 255 / 0.4); font-size: 11px; }

  /* ── 상대 선택: 메뉴 버튼 + 카드 패널 (SillyTavern 좌측 캐릭터 패널 방식) ── */
  .voice-menu { position: relative; margin-bottom: 8px; }
  .voice-trigger {
    display: flex; align-items: center; gap: 8px; width: 100%;
    background: rgb(255 255 255 / 0.06);
    border: 1px solid rgb(255 255 255 / 0.12);
    border-radius: 8px; padding: 7px 10px; cursor: pointer;
    color: rgb(255 255 255 / 0.85); font-size: 12px;
  }
  .voice-trigger:hover { background: rgb(255 255 255 / 0.1); }
  .voice-trigger > span:first-child { color: rgb(255 255 255 / 0.5); }
  .voice-trigger strong { flex: 1; text-align: left; font-weight: 500; }
  .voice-trigger .chev { color: rgb(255 255 255 / 0.45); font-size: 10px; }
  .voice-panel {
    position: absolute; bottom: calc(100% + 6px); left: 0; right: 0; z-index: 20;
    background: #17171f;
    border: 1px solid rgb(255 255 255 / 0.12);
    border-radius: 10px; padding: 6px;
    box-shadow: 0 12px 30px rgb(0 0 0 / 0.5);
  }
  .voice-panel-head {
    display: flex; align-items: center; justify-content: space-between;
    padding: 2px 6px 6px; color: rgb(255 255 255 / 0.5); font-size: 11px;
  }
  .voice-panel-head .icon {
    background: none; border: 0; color: rgb(255 255 255 / 0.5); cursor: pointer; font-size: 11px;
  }
  .voice-card {
    display: flex; align-items: center; gap: 8px; width: 100%;
    background: none; border: 0; border-radius: 8px; padding: 7px 8px;
    cursor: pointer; color: rgb(255 255 255 / 0.8); text-align: left; font-size: 12px;
  }
  .voice-card:hover { background: rgb(255 255 255 / 0.06); }
  .voice-card.sel { background: rgb(124 92 255 / 0.2); color: white; }
  .voice-card .avatar {
    width: 22px; height: 22px; border-radius: 50%; flex: none;
    background: radial-gradient(circle at 35% 30%, #7ee7ff, #4f7dff 60%, #1b2140);
  }
  .voice-card .avatar[data-v='lyra_vex_b'] {
    background: radial-gradient(circle at 35% 30%, #ffd7ec, #ff7ab6 60%, #4a1b33);
  }
  .voice-card .meta { display: flex; flex-direction: column; line-height: 1.25; flex: 1; }
  .voice-card .meta small { color: rgb(255 255 255 / 0.45); font-size: 10px; }
  .voice-card .check { color: #a78bfa; font-size: 11px; }
  /* 드롭다운 시절 CSS 는 제거했다 — 메뉴버튼(.voice-menu)으로 교체되면서
     .voice span / .voice select 가 매칭 대상이 없어 빌드 경고를 냈다. */

  main {
    display: grid;
    grid-template-rows: minmax(0, 1fr) minmax(0, 1fr);
    grid-template-columns: 1fr;
    gap: 18px;
    padding: 22px;
    overflow: hidden;
    min-height: 0;
  }
  @media (min-width: 960px) {
    main { grid-template-rows: 1fr; grid-template-columns: 1fr 1fr; }
  }

  section.orb {
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    gap: 28px;
    background: radial-gradient(ellipse at top, rgb(124 92 255 / 0.12), transparent 60%);
    border-radius: 16px;
    border: 1px solid rgb(255 255 255 / 0.06);
    padding: 32px;
    min-height: 0;
  }
  section.chat {
    display: flex;
    min-height: 0;
  }
</style>
