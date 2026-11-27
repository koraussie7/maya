<script>
  /**
   * /decisions 페이지
   *
   * 백엔드의 `decision.log` 서버 메시지를 구독해서
   * Laya reranker PoC 결과를 시각화한다.
   *
   * 한 줄 항목 예:
   *   {
   *     text, route_id, source, score, threshold,
   *     abstained, rule_hit, laya_hit, all_scores, ts
   *   }
   *
   * MayaClient API:
   *   new MayaClient({ onPhase, onError, onDecision, ... })
   *   client.connect(url?)      -> Promise
   *   client.startSession({})
   *   client.endSession()
   *   client.close()
   *   client.connected (getter)
   */
  import { onMount, onDestroy } from "svelte";
  import { base } from "$app/paths";
  import { MayaClient } from "$lib/mayaClient.js";

  /** @type {MayaClient | null} */
  let client = null;
  /** @type {Array<any>} */
  let rows = [];
  /** @type {string | null} */
  let phase = null;
  /** @type {boolean} */
  let wsOpen = false;
  /** @type {string | null} */
  let lastError = null;
  /** @type {boolean} */
  let cleared = false;

  /** 결정 로그 한 건을 화면용 row로 정규화
   *
   * 백엔드 (session.py):
   *   - `all_scores` 는 항상 **배열** `[{route_id, score, source}, ...]` 형태.
   *     rule-only hit 일 때는 route_id 1개짜리 배열로 채워진다.
   *   - `ts` 는 epoch 초 (float). JS Date 는 초/밀리초 모두 받지만 명시적으로 ms 변환.
   *   - `source` 는 `"rule" | "laya" | "disabled" | "mock"` 4종.
   */
  /** @param {{ts?: number|string, text?: string, route_id?: string|null, source?: string, score?: number|null, threshold?: number|null, abstained?: boolean, rule_hit?: boolean, laya_hit?: boolean, all_scores?: Array<{route_id?: string, score?: number, source?: string}>}} d */
  function toRow(d) {
    const rawScores = d.all_scores || [];
    const all = Array.isArray(rawScores) ? rawScores : [];
    const top = all
      .slice()
      .sort((a, b) => (b.score || 0) - (a.score || 0))
      .slice(0, 3)
      .map((c) => [c.route_id || "?", Number(c.score) || 0]);

    let tsIso;
    if (typeof d.ts === "number") {
      // backend 는 time.time() — epoch 초. JS Date 는 초/밀리초 모두 자동 처리.
      tsIso = new Date(d.ts * 1000).toISOString();
    } else if (typeof d.ts === "string") {
      tsIso = d.ts;
    } else {
      tsIso = new Date().toISOString();
    }

    return {
      ts: tsIso,
      text: d.text || "",
      route_id: d.route_id || "(abstain)",
      source: d.source || (d.abstained ? "abstain" : "-"),
      score: typeof d.score === "number" ? d.score : null,
      threshold: typeof d.threshold === "number" ? d.threshold : null,
      abstained: !!d.abstained,
      rule_hit: !!d.rule_hit,
      laya_hit: !!d.laya_hit,
      top,
    };
  }

  /** @type {ReturnType<typeof setTimeout>|undefined} */
  let pollTimer;

  onMount(() => {
    client = new MayaClient({
      onPhase: (p) => {
        phase = p;
      },
      onError: (_code, message) => {
        lastError = message;
      },
      onDecision: (d) => {
        rows = [toRow(d), ...rows].slice(0, 200);
        cleared = false;
      },
    });

    client
      .connect()
      .then(() => {
        wsOpen = true;
        // 라우터 활성화 — 백엔드 session.py는 session.start 이후에만
        // chat.text 라우터 매칭과 decision.log 송출을 시작한다.
        client?.startSession({});
      })
      .catch((e) => {
        // WebSocket error 이벤트는 Error 가 아니라 Event 라서
        // e.message 가 없다. 그대로 String() 하면 "[object Event]".
        const msg =
          typeof e?.message === "string" && e.message
            ? e.message
            : e?.code != null
              ? `연결 실패 (code ${e.code})`
              : "백엔드에 연결하지 못했습니다";
        lastError = msg;
      });

    // 연결 상태 폴링 (1초) — MayaClient.connected getter
    pollTimer = setInterval(() => {
      wsOpen = !!(client && client.connected);
    }, 1000);
  });

  onDestroy(() => {
    if (pollTimer) clearInterval(pollTimer);
    if (client) {
      try {
        client.endSession();
      } catch {}
      try {
        client.close();
      } catch {}
    }
  });

  function clear() {
    rows = [];
    cleared = true;
  }

  /** @param {string} s @returns {"rule"|"laya"|"muted"|"abstain"} */
  function sourceBadge(s) {
    if (s === "rule") return "rule";
    if (s === "laya") return "laya";
    if (s === "abstain") return "abstain";
    // disabled, mock, 또는 기타 backend payload 변형 모두 무채색으로 처리
    return "muted";
  }

  /** @param {string|number} isoOrEpoch */
  function fmtTs(isoOrEpoch) {
    try {
      const d = new Date(isoOrEpoch);
      return d.toLocaleTimeString("ko-KR", { hour12: false });
    } catch {
      return String(isoOrEpoch);
    }
  }

  /** @param {number|null|undefined} p */
  function fmtProb(p) {
    if (p == null) return "—";
    return (p * 100).toFixed(1) + "%";
  }
</script>

<svelte:head>
  <title>결정 로그 · Maya</title>
</svelte:head>

<main class="page">
  <header class="head">
    <div>
      <h1>결정 로그</h1>
      <p class="sub">
        Laya reranker PoC — 룰 적중, Laya 후보, abstain 이벤트를 시간순으로
        표시.
      </p>
    </div>
    <div class="status">
      <span class="dot" class:on={wsOpen}></span>
      <span class="lbl">
        {#if !wsOpen}disconnected{:else if phase}{phase}{:else}connected{/if}
      </span>
      {#if lastError}
        <span class="err">· {lastError}</span>
      {/if}
    </div>
  </header>

  <section class="meta">
    <div class="pill">
      <span class="k">총</span><span class="v">{rows.length}</span>
    </div>
    <div class="pill">
      <span class="k">룰 적중</span><span class="v"
        >{rows.filter((r) => r.rule_hit).length}</span
      >
    </div>
    <div class="pill">
      <span class="k">Laya 적중</span><span class="v"
        >{rows.filter((r) => r.laya_hit).length}</span
      >
    </div>
    <div class="pill warn">
      <span class="k">Abstain</span><span class="v"
        >{rows.filter((r) => r.abstained).length}</span
      >
    </div>
    <button class="clear" on:click={clear} disabled={rows.length === 0}
      >지우기</button
    >
  </section>

  {#if cleared && rows.length === 0}
    <p class="empty">기록을 지웠습니다. 새 발화가 들어오면 다시 표시됩니다.</p>
  {:else if rows.length === 0}
    <p class="empty">
      {#if !wsOpen}
        백엔드 연결 대기 중 — <code>{base}/</code>에서 음성 세션을 시작하세요.
      {:else}
        세션 연결됨. <code>{base}/</code>에서 말을 걸어 보세요.
      {/if}
    </p>
  {:else}
    <table class="grid">
      <thead>
        <tr>
          <th class="ts">시각</th>
          <th class="text">발화</th>
          <th class="route">라우트</th>
          <th class="src">출처</th>
          <th class="score">점수 / 임계값</th>
          <th class="top">Laya 상위 후보</th>
        </tr>
      </thead>
      <tbody>
        {#each rows as r (r.ts + ":" + r.text + ":" + r.score)}
          <tr class:abstain={r.abstained}>
            <td class="ts mono">{fmtTs(r.ts)}</td>
            <td class="text">{r.text}</td>
            <td class="route mono">{r.route_id}</td>
            <td class="src">
              <span class="badge {sourceBadge(r.source)}">{r.source}</span>
              {#if r.laya_hit && !r.rule_hit}
                <span class="hint">(reranked)</span>
              {/if}
            </td>
            <td class="score mono">
              {fmtProb(r.score)} / {fmtProb(r.threshold)}
            </td>
            <td class="top mono">
              {#each r.top as [k, v]}
                <div class="cand">
                  <span class="cand-k">{k}</span>
                  <span class="cand-v">{fmtProb(v)}</span>
                </div>
              {/each}
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}
</main>

<style>
  .page {
    max-width: 1100px;
    margin: 0 auto;
    padding: 24px 20px 80px;
    color: #e8eaed;
  }
  .head {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 16px;
    margin-bottom: 18px;
  }
  .head h1 {
    margin: 0 0 4px 0;
    font-size: 22px;
    font-weight: 600;
    letter-spacing: -0.01em;
  }
  .sub {
    margin: 0;
    color: #9aa0a6;
    font-size: 13px;
  }
  .status {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
    color: #9aa0a6;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  .status .dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: #5f6368;
  }
  .status .dot.on {
    background: #34a853;
    box-shadow: 0 0 6px rgba(52, 168, 83, 0.6);
  }
  .status .err {
    color: #f28b82;
    margin-left: 6px;
  }
  .meta {
    display: flex;
    gap: 10px;
    align-items: center;
    margin-bottom: 14px;
    flex-wrap: wrap;
  }
  .pill {
    display: inline-flex;
    align-items: baseline;
    gap: 6px;
    padding: 6px 10px;
    border: 1px solid #2d2f33;
    border-radius: 8px;
    background: #1a1c1f;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 12px;
  }
  .pill .k {
    color: #9aa0a6;
  }
  .pill .v {
    color: #e8eaed;
    font-weight: 600;
  }
  .pill.warn {
    border-color: #5c3b1a;
    background: #2a1d10;
  }
  .pill.warn .v {
    color: #f28b82;
  }
  .clear {
    margin-left: auto;
    padding: 6px 10px;
    background: transparent;
    color: #9aa0a6;
    border: 1px solid #2d2f33;
    border-radius: 6px;
    cursor: pointer;
    font-size: 12px;
  }
  .clear:hover:not(:disabled) {
    color: #e8eaed;
    border-color: #5f6368;
  }
  .clear:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }
  .empty {
    margin-top: 40px;
    text-align: center;
    color: #9aa0a6;
    font-size: 14px;
  }
  .empty code {
    background: #1a1c1f;
    border: 1px solid #2d2f33;
    border-radius: 4px;
    padding: 1px 6px;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  .grid {
    width: 100%;
    border-collapse: collapse;
    background: #14161a;
    border: 1px solid #2d2f33;
    border-radius: 10px;
    overflow: hidden;
    font-size: 13px;
  }
  .grid th,
  .grid td {
    padding: 8px 10px;
    text-align: left;
    vertical-align: top;
    border-bottom: 1px solid #202125;
  }
  .grid thead th {
    background: #1a1c1f;
    color: #9aa0a6;
    font-weight: 500;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .grid tbody tr:hover {
    background: #181a1d;
  }
  .grid tbody tr.abstain {
    background: #1d1610;
  }
  .grid tbody tr.abstain:hover {
    background: #251c14;
  }
  .ts {
    width: 90px;
    color: #9aa0a6;
  }
  .text {
    width: 28%;
    color: #e8eaed;
  }
  .route {
    width: 110px;
    color: #c4c7c5;
  }
  .src {
    width: 130px;
  }
  .score {
    width: 130px;
    color: #c4c7c5;
  }
  .top {
    color: #9aa0a6;
  }
  .mono {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 12px;
  }
  .badge {
    display: inline-block;
    padding: 1px 8px;
    border-radius: 999px;
    font-size: 11px;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    border: 1px solid #2d2f33;
  }
  .badge.rule {
    color: #8ab4f8;
    border-color: #2c3a55;
    background: #16202d;
  }
  .badge.laya {
    color: #c4c7c5;
    border-color: #3a4a3a;
    background: #1d2a1d;
  }
  .badge.abstain {
    color: #f28b82;
    border-color: #5c3b1a;
    background: #2a1d10;
  }
  .badge.muted {
    color: #9aa0a6;
  }
  .hint {
    display: inline-block;
    margin-left: 6px;
    color: #f28b82;
    font-size: 11px;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  .cand {
    display: flex;
    justify-content: space-between;
    gap: 8px;
    line-height: 1.4;
  }
  .cand-k {
    color: #9aa0a6;
  }
  .cand-v {
    color: #e8eaed;
  }
</style>
