// Maya prototype — minimal client logic
// 마이크 캡처 + (옵션) WebSocket 전송 + 로컬 폴백 응답
// privseai.com/maya에 정적 호스팅 가능하도록 외부 의존성 없음

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const orb = $("#orb");
const orbWave = $("#orbWave");
const micBtn = $("#micBtn");
const presenceText = $(".presence-text");
const presenceDot = $(".presence-dot");
const chatList = $("#chatList");
const chatEmpty = $("#chatEmpty");
const composer = $("#composer");
const textInput = $("#textInput");

const state = {
  listening: false,
  mediaStream: null,
  audioCtx: null,
  analyser: null,
  raf: null,
  history: [],
  ws: null,
};

const BACKEND_WS = (location.protocol === "https:" ? "wss://" : "ws://")
  + (location.hostname === "localhost" || location.hostname === "127.0.0.1"
      ? "127.0.0.1:8787"
      : "maya.privseai.com") + "/ws";

/* ---------- Presence / state ---------- */
function setState(s) {
  orb.dataset.state = s;
  presenceDot.dataset.state = s;
  presenceText.dataset.state = s;
  presenceText.textContent = ({
    idle: "대기 중",
    listening: "듣고 있어요",
    thinking: "생각 중…",
    speaking: "말하는 중",
    error: "오류",
  })[s] || s;
}

/* ---------- Voice Orb waveform ---------- */
function startWave() {
  if (!state.analyser) return;
  const ctx = orbWave.getContext("2d");
  const { width, height } = orbWave;
  const buf = new Uint8Array(state.analyser.frequencyBinCount);
  const t0 = performance.now();

  const draw = () => {
    state.analyser.getByteTimeDomainData(buf);
    ctx.clearRect(0, 0, width, height);

    const cx = width / 2, cy = height / 2;
    const baseR = Math.min(width, height) * 0.34;
    const elapsed = (performance.now() - t0) / 1000;

    // Concentric pulsing ring(s)
    for (let k = 0; k < 3; k++) {
      const phase = (elapsed * 0.6 + k * 0.33) % 1;
      ctx.beginPath();
      ctx.arc(cx, cy, baseR + phase * 40, 0, Math.PI * 2);
      ctx.strokeStyle = `rgba(255,255,255,${(1 - phase) * 0.18})`;
      ctx.lineWidth = 1.2;
      ctx.stroke();
    }

    // Waveform ring (driven by mic amplitude)
    let sum = 0;
    for (let i = 0; i < buf.length; i++) {
      const v = (buf[i] - 128) / 128;
      sum += v * v;
    }
    const rms = Math.sqrt(sum / buf.length);
    const amp = Math.min(1, rms * 4);

    ctx.beginPath();
    const N = 96;
    for (let i = 0; i <= N; i++) {
      const a = (i / N) * Math.PI * 2;
      const wob = Math.sin(elapsed * 3 + a * 4) * 0.5 + 0.5;
      const r = baseR + wob * 8 * amp;
      const x = cx + Math.cos(a) * r;
      const y = cy + Math.sin(a) * r;
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    }
    ctx.closePath();
    ctx.strokeStyle = "rgba(255,255,255,0.9)";
    ctx.lineWidth = 1.5;
    ctx.stroke();

    state.raf = requestAnimationFrame(draw);
  };
  draw();
}

function stopWave() {
  if (state.raf) cancelAnimationFrame(state.raf);
  state.raf = null;
  const ctx = orbWave.getContext("2d");
  ctx.clearRect(0, 0, orbWave.width, orbWave.height);
}

/* ---------- Mic ---------- */
async function toggleMic() {
  if (state.listening) {
    stopMic();
  } else {
    await startMic();
  }
}

async function startMic() {
  try {
    setState("listening");
    micBtn.setAttribute("aria-pressed", "true");
    $(".mic-label").textContent = "듣는 중… 탭해서 종료";

    state.mediaStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 },
    });
    state.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const src = state.audioCtx.createMediaStreamSource(state.mediaStream);
    state.analyser = state.audioCtx.createAnalyser();
    state.analyser.fftSize = 1024;
    src.connect(state.analyser);
    state.listening = true;
    startWave();

    // (실서비스) MediaRecorder로 chunk 전송 → STT → 백엔드
    // 프로토타입: 3초간 음성 입력 후 "음성 입력됨(프로토타입)" 메시지로 폴백
    setTimeout(() => {
      if (state.listening) {
        stopMic();
        sendUser("🎙️ 음성 입력(프로토타입)", true);
        runAssistant("(백엔드 미연결 상태) 음성 파이프라인이 연결되면 한국어 STT → LLM → TTS로 응답합니다.");
      }
    }, 3000);
  } catch (e) {
    console.error(e);
    setState("error");
    appendBubble("assistant", "마이크 권한이 필요합니다. 브라우저 설정에서 마이크 접근을 허용해 주세요.");
    stopMic();
  }
}

function stopMic() {
  state.listening = false;
  micBtn.setAttribute("aria-pressed", "false");
  $(".mic-label").textContent = "탭해서 말하기";
  stopWave();
  if (state.mediaStream) {
    state.mediaStream.getTracks().forEach((t) => t.stop());
    state.mediaStream = null;
  }
  if (state.audioCtx) {
    state.audioCtx.close().catch(() => {});
    state.audioCtx = null;
  }
  state.analyser = null;
  setState("idle");
}

/* ---------- Chat ---------- */
function showChat() {
  chatEmpty.hidden = true;
  chatList.hidden = false;
}

function appendBubble(role, text, opts = {}) {
  showChat();
  const li = document.createElement("li");
  li.className = `chat-item ${role}`;
  const roleLabel = role === "user" ? "나" : "마야";
  li.innerHTML = `<span class="role">${roleLabel}</span><div class="bubble${opts.thinking ? " thinking" : ""}"></div>`;
  const bubble = $(".bubble", li);
  bubble.textContent = text;
  if (opts.cursor) {
    const c = document.createElement("span");
    c.className = "cursor";
    bubble.appendChild(c);
  }
  chatList.appendChild(li);
  chatList.scrollTop = chatList.scrollHeight;
  return bubble;
}

async function sendUser(text, fromVoice = false) {
  if (!text.trim()) return;
  textInput.value = "";
  appendBubble("user", text);
  state.history.push({ role: "user", content: text });
  await runAssistant();
}

async function runAssistant(seedPrompt) {
  setState("thinking");
  // 시드 텍스트(예: 음성 입력 후 폴백)가 있으면 user 메시지로 한 번 더 푸시
  if (seedPrompt) {
    state.history.push({ role: "user", content: seedPrompt });
  }

  const bubble = appendBubble("assistant", "…", { thinking: true });

  // 백엔드 WebSocket 시도 → 실패 시 로컬 폴백
  let reply = "";
  try {
    if (!state.ws || state.ws.readyState > 1) {
      state.ws = new WebSocket(BACKEND_WS);
    }
    reply = await streamViaWs(bubble, state.history);
  } catch (e) {
    console.warn("WS 실패, 로컬 폴백:", e);
    reply = localFallback(state.history);
    bubble.textContent = reply;
  }

  state.history.push({ role: "assistant", content: reply });
  setState("speaking");
  setTimeout(() => setState("idle"), 1200);
}

function streamViaWs(bubble, history) {
  return new Promise((resolve, reject) => {
    const ws = state.ws;
    const onOpen = () => {
      ws.send(JSON.stringify({ type: "chat", messages: history }));
      bubble.classList.remove("thinking");
      bubble.textContent = "";
      const c = document.createElement("span");
      c.className = "cursor";
      bubble.appendChild(c);
    };
    const onMsg = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === "token") {
          const c = bubble.querySelector(".cursor");
          if (c) c.remove();
          bubble.appendChild(document.createTextNode(msg.text));
          bubble.appendChild(Object.assign(document.createElement("span"), { className: "cursor" }));
          chatList.scrollTop = chatList.scrollHeight;
        } else if (msg.type === "done") {
          const c = bubble.querySelector(".cursor");
          if (c) c.remove();
          resolve(bubble.textContent);
        } else if (msg.type === "error") {
          reject(new Error(msg.message));
        }
      } catch (e) { reject(e); }
    };
    const onErr = (e) => reject(e);
    ws.addEventListener("open", onOpen, { once: true });
    ws.addEventListener("message", onMsg);
    ws.addEventListener("error", onErr, { once: true });
    ws.addEventListener("close", () => {
      // 도중에 닫히면 실패로
    });
  });
}

function localFallback(history) {
  const last = history.filter((h) => h.role === "user").pop();
  const q = (last?.content || "").trim();
  if (!q) return "무엇을 도와드릴까요?";
  if (/일정|스케줄/.test(q)) return "오늘 일정 기능은 백엔드 연결 후 사용 가능합니다 (캘린더 연동 예정).";
  if (/이름/.test(q)) return "사용자의 이름은 저장된 메모리에서 검색해 답변드릴 수 있어요 (백엔드 연결 필요).";
  if (/시|시 작성/.test(q)) return "프로토타입 모드입니다 — 백엔드 연결 후 한국어 시를 생성해 드릴게요.";
  return `"${q}" — 백엔드가 연결되면 Whisper STT → OpenRouter LLM → VoxCPM TTS 로 응답합니다.`;
}

/* ---------- Wire up ---------- */
orb.addEventListener("click", toggleMic);
orb.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleMic(); }
});
micBtn.addEventListener("click", toggleMic);

composer.addEventListener("submit", (e) => {
  e.preventDefault();
  sendUser(textInput.value);
});

$$(".chip").forEach((b) =>
  b.addEventListener("click", () => sendUser(b.dataset.prompt))
);

setState("idle");

// visibility: 페이지 닫힐 때 마이크 정리
window.addEventListener("beforeunload", stopMic);
