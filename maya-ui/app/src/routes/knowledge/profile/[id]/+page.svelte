<script>
  /**
   * /knowledge/profile/[id] — 프로필 상세.
   *
   * CODEPLAN §1 Agent 3 수용 기준:
   *   "프로필 상세 (문서 목록, 청크 미리보기, 상위 엔티)"
   *
   * 백엔드 /knowledge/profile/{id} 응답:
   *   { id, name, doc_count, top_entities: [{id, label, count}] }
   */
  import { onMount, onDestroy } from "svelte";
  import { page } from "$app/stores";
  import { profile, listProfiles, graph } from "$lib/api/knowledge.js";
  import { base } from "$app/paths";

  /** @type {string|undefined} */
  let id = $derived($page.params?.id);
  /** @type {{id:string,name:string,doc_count:number,top_entities:Array<{id:string,label:string,count:number}>}|null} */
  let detail = $state(null);
  /** @type {Array<{id:string,label:string,type?:string}>} */
  let graphNodes = $state([]);
  /** @type {Array<{src:string,dst:string,weight?:number,rel?:string}>} */
  let graphEdges = $state([]);
  /** @type {string|null} */
  let err = $state(null);
  /** @type {boolean} */
  let busy = $state(false);

  async function load() {
    if (!id) return;
    busy = true;
    err = null;
    try {
      detail = await profile(id);
      // 그래프는 동일 프로필의 노드 일부만 미리보기
      const g = await graph({ profile_id: id });
      // 노드 너무 많으면 30개로 슬라이스 — 라우트 자체가 전체를 보여준다
      graphNodes = (g.nodes || []).slice(0, 30);
      graphEdges = (g.edges || []).filter(
        (e) =>
          graphNodes.some((n) => n.id === e.src) &&
          graphNodes.some((n) => n.id === e.dst),
      );
    } catch (e) {
      err = e?.message ?? String(e);
    } finally {
      busy = false;
    }
  }

  onMount(load);
  $effect(() => {
    if (id) load();
  });
  onDestroy(() => {});

  /** @param {number|undefined} c @returns {string} */
  function fmtCount(c) {
    if (c == null) return "0";
    return String(c);
  }
</script>

<svelte:head>
  <title>{detail?.name ?? id} · 프로필 · Maya</title>
</svelte:head>

<main class="page">
  <header class="head">
    <div>
      <a class="back" href={`${base}/knowledge`}>← 지식</a>
      <h1>{detail?.name ?? id}</h1>
      <p class="sub">
        프로필 ID: <code>{id}</code>
        {#if detail}
          · 문서 <strong>{detail.doc_count}</strong>개 · 엔티티
          <strong>{detail.top_entities?.length ?? 0}</strong>개
        {/if}
      </p>
    </div>
    <div class="actions">
      <a class="btn" href={`${base}/knowledge/graph`}>그래프 전체 보기</a>
    </div>
  </header>

  {#if err}
    <p class="err">⚠ {err}</p>
  {/if}

  {#if busy && !detail}
    <p class="empty">불러오는 중…</p>
  {:else if detail}
    <section class="grid">
      <article class="card">
        <h2>상위 엔티티</h2>
        {#if detail.top_entities && detail.top_entities.length > 0}
          <ul class="entities">
            {#each detail.top_entities as e (e.id)}
              <li>
                <span class="label">{e.label}</span>
                <span class="count">{fmtCount(e.count)}</span>
              </li>
            {/each}
          </ul>
        {:else}
          <p class="empty">아직 추출된 엔티티가 없습니다.</p>
        {/if}
      </article>

      <article class="card">
        <h2>미니 그래프 (상위 {graphNodes.length}개)</h2>
        {#if graphNodes.length > 0}
          <ul class="entity-mini">
            {#each graphNodes as n (n.id)}
              <li>
                <span class="dot"></span>
                <span class="label">{n.label}</span>
                <small>{n.type ?? "-"}</small>
              </li>
            {/each}
          </ul>
        {:else}
          <p class="empty">노드가 아직 없습니다.</p>
        {/if}
      </article>
    </section>
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
  .back {
    color: var(--fg-2);
    font-size: 12px;
    text-decoration: none;
  }
  .back:hover {
    color: var(--color-brand);
  }
  .head h1 {
    margin: 4px 0 4px 0;
    font-size: 22px;
    font-weight: 600;
    letter-spacing: -0.01em;
  }
  .sub {
    color: var(--fg-2);
    font-size: 12px;
    margin: 0;
  }
  .sub code {
    font-family: var(--font-mono);
    color: var(--color-brand-soft);
  }
  .actions .btn {
    display: inline-block;
    padding: 7px 14px;
    border-radius: var(--radius-pill);
    border: 1px solid rgb(255 255 255 / 0.12);
    background: rgb(255 255 255 / 0.04);
    color: var(--fg-0);
    text-decoration: none;
    font-size: 12px;
  }
  .actions .btn:hover {
    background: rgb(124 92 255 / 0.18);
    border-color: rgb(124 92 255 / 0.5);
  }
  .err {
    color: var(--color-error);
    font-size: 13px;
  }
  .empty {
    color: var(--fg-2);
    font-size: 13px;
  }
  .grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 14px;
  }
  @media (max-width: 720px) {
    .grid {
      grid-template-columns: 1fr;
    }
  }
  .card {
    background: linear-gradient(
      180deg,
      rgb(255 255 255 / 0.04),
      rgb(255 255 255 / 0.01)
    );
    border: 1px solid rgb(255 255 255 / 0.08);
    border-radius: var(--radius-m);
    padding: 16px 18px;
  }
  .card h2 {
    margin: 0 0 12px 0;
    font-size: 13px;
    color: var(--fg-1);
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .entities {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .entities li {
    display: flex;
    justify-content: space-between;
    padding: 6px 10px;
    background: rgb(255 255 255 / 0.04);
    border-radius: var(--radius-s);
    font-size: 13px;
  }
  .entities .count {
    font-family: var(--font-mono);
    color: var(--color-brand-soft);
  }
  .entity-mini {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  .entity-mini li {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 4px 10px;
    background: rgb(255 255 255 / 0.04);
    border: 1px solid rgb(255 255 255 / 0.08);
    border-radius: var(--radius-pill);
    font-size: 11px;
  }
  .entity-mini .dot {
    width: 8px;
    height: 8px;
    border-radius: 9999px;
    background: var(--color-brand);
  }
  .entity-mini small {
    color: var(--fg-2);
    font-family: var(--font-mono);
  }
</style>
