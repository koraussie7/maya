// wsfix-check.mjs — 통화 시작 후 WebSocket 실제 연결 여부 확인
export default async function run(page, ui) {
  const log = [];

  // WebSocket 생성/상태를 가로채 기록한다
  await page.evaluate(() => {
    window.__wslog = [];
    const Orig = window.WebSocket;
    window.WebSocket = function (url, protocols) {
      window.__wslog.push({ event: "construct", url });
      const ws = protocols ? new Orig(url, protocols) : new Orig(url);
      ws.addEventListener("open", () =>
        window.__wslog.push({ event: "open", url }),
      );
      ws.addEventListener("error", () =>
        window.__wslog.push({ event: "error", url }),
      );
      ws.addEventListener("close", (e) =>
        window.__wslog.push({ event: "close", url, code: e.code }),
      );
      ws.addEventListener("message", (e) => {
        const kind = typeof e.data === "string" ? "text" : "binary";
        let t = kind;
        if (kind === "text") {
          try {
            t = JSON.parse(e.data).type;
          } catch (_) {}
        }
        window.__wslog.push({ event: "message", kind: kind, type: t });
      });
      return ws;
    };
    window.WebSocket.prototype = Orig.prototype;
    Object.assign(window.WebSocket, {
      CONNECTING: 0,
      OPEN: 1,
      CLOSING: 2,
      CLOSED: 3,
    });
  });

  const snap = await ui.snapshot();
  const btn = snap.match(/@(e\d+) button "📞 통화 시작"/)?.[1];
  if (!btn) return { error: "통화 시작 버튼 없음", snapshot: snap };

  await ui.click(btn);
  // 모델 로드 + WS 연결 대기
  await page.waitForTimeout(6000);

  const wslog = await page.evaluate(() => window.__wslog);
  let micError = null;
  try {
    await page.evaluate(async () => {
      await navigator.mediaDevices.getUserMedia({ audio: true });
    });
  } catch (e) {
    micError = String(e).slice(0, 120);
  }

  const opened = wslog.some((e) => e.event === "open");
  const errored = wslog.some((e) => e.event === "error");
  const closed = wslog.filter((e) => e.event === "close").map((e) => e.code);

  return {
    ws_construct_urls: wslog
      .filter((e) => e.event === "construct")
      .map((e) => e.url),
    ws_opened: opened,
    ws_errored: errored,
    ws_close_codes: closed,
    ws_messages: wslog.filter((e) => e.event === "message").slice(0, 8),
    mic_error: micError,
    verdict:
      opened && !errored ? "CONNECTED" : errored ? "FAILED" : "NO ATTEMPT",
  };
}
