// UI 종단 확인 — /aivoice 페이지를 띄우고, 마이크 대신 WS로 가짜 PCM을 주입한다.
export default async function run(page, ui) {
  const logs = [];
  page.on("console", (m) => logs.push(`${m.type()}: ${m.text()}`));
  page.on("pageerror", (e) => logs.push(`pageerror: ${e.message}`));

  await page.goto("http://127.0.0.1:8000/aivoice", {
    waitUntil: "domcontentloaded",
  });
  await page.waitForTimeout(800);

  const b64 = (await import("node:fs")).readFileSync("/tmp/maya-test-pcm.b64", "utf8");
  await page.evaluate((s) => { window.__TEST_PCM__ = s; }, b64);

  const snap = await ui.snapshot();
  await page.screenshot({ path: "/tmp/maya-aivoice-idle.png" });

  // 페이지의 WS 를 그대로 쓰지 않고, 같은 계약으로 별도 WS 주입 → TTS 수신 측정
  const result = await page.evaluate(async () => {
    const t0 = performance.now();
    return await new Promise((resolve) => {
      const url =
        (location.protocol === "https:" ? "wss://" : "ws://") +
        location.host +
        "/ws";
      const ws = new WebSocket(url);
      ws.binaryType = "arraybuffer";
      const out = { url, events: [], firstTts: null, ttsCount: 0, bytes: 0, pcmBytes: 0 };
      ws.onopen = async () => {
        ws.send(JSON.stringify({ type: "start" }));
        // 마이크 대신 합성 한국어 음성(say -v Yuna → 16k mono s16le)을 0.1초 단위로 밀어넣는다.
        // 주입 스크립트가 넘겨준 PCM 을 그대로 쓴다(브라우저에서 모델 없이 합성할 수 없으므로).
        const b64 = window.__TEST_PCM__ || "";
        const bin = atob(b64);
        const pcm = new Int16Array(bin.length / 2);
        for (let i = 0; i < pcm.length; i++) {
          pcm[i] = (bin.charCodeAt(2 * i) | (bin.charCodeAt(2 * i + 1) << 8)) << 16 >> 16;
        }
        out.pcmBytes = pcm.byteLength;
        const CH = 1600; // 0.1s
        for (let i = 0; i < pcm.length; i += CH) {
          ws.send(pcm.slice(i, i + CH).buffer);
          await new Promise((r) => setTimeout(r, 100));
        }
        // tts_audio 는 인터리브된 채널(stride)로 해석되므로, 남는 평면 PCM 을
        // 소수 채널로 보내면 버퍼가 계속 1초 이상 쌓여 발화가 끝나지 않는다.
        // → 이후 평면 제로 PCM(정상 무음)을 전송해 턴을 닫는다.
        for (let i = 0; i < 40; i++) {
          ws.send(new Int16Array(1600).buffer);
          await new Promise((r) => setTimeout(r, 100));
        }
        for (let i = 0; i < 25; i++) {
          ws.send(new Int16Array(1600).buffer);
          await new Promise((r) => setTimeout(r, 100));
        }
      };
      ws.onmessage = (ev) => {
        const ms = performance.now() - t0;
        if (typeof ev.data !== "string") return;
        const m = JSON.parse(ev.data);
        if (m.type === "tts_audio") {
          out.ttsCount++;
          out.bytes += Math.floor((m.audio || "").length * 0.75);
          if (out.firstTts === null) out.firstTts = Math.round(ms);
        }
        out.events.push(
          `${Math.round(ms)}ms ${m.type}${m.text ? " " + String(m.text).slice(0, 40) : ""}`,
        );
        if (m.type === "final_reply") {
          setTimeout(() => {
            try {
              ws.send(JSON.stringify({ type: "stop" }));
            } catch {}
            resolve(out);
          }, 3000);
        }
      };
      ws.onerror = () => resolve({ ...out, error: "ws error" });
      setTimeout(() => resolve({ ...out, timeout: true }), 45000);
    });
  });

  await page.waitForTimeout(300);
  await page.screenshot({ path: "/tmp/maya-aivoice-after.png" });

  return {
    idleSnapshot: snap.split("\n").slice(0, 25).join("\n"),
    ws: result,
    consoleErrors: logs.filter((l) => /error|pageerror/i.test(l)).slice(0, 10),
  };
}
