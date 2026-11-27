<script>
  /**
   * /knowledge/graph — 엔티/커뮤니티 그래프 시각화.
   *
   * CODEPLAN §1 Agent 3 수용 기준:
   *   "/knowledge/graph 페이지에서 노드 20개 이상이면 force layout 이 60fps 유지"
   *
   * EntityGraph.svelte 가 자체 force simulation 을 돌리므로 본 페이지는
   * - profile 선택 UI
   * - /knowledge/graph 호출 → 노드/엣지 주입
   * 만 담당한다.
   */
  import { onMount, onDestroy } from "svelte";
  import { listProfiles, graph } from "$lib/api/knowledge.js";
  import EntityGraph from "$lib/components/knowledge/EntityGraph.svelte";
  import { base } from "$app/paths";

  /** @type {Array<{id:string,name:string,doc_count?:number,created_at?:string}>} */
  let profiles = $state([]);
  /** @type {string|null} */
  let selectedProfile = $state(null);
  /** @type {Array<{id:string,label:string,type?:string}>} */
  let nodes = $state([]);
  /** @type {Array<{src:string,dst:string,weight?:number,rel?:string}>} */
  let edges = $state([]);
  /** @type {string|null} */
  let err = $state(null);
  /** @type {boolean} */
  let busy = $state(false);

  async function refreshProfiles() {
    try {
      const r = await listProfiles();
      profiles = r.profiles || [];
      if (!selectedProfile && profiles.length > 0) {
        selectedProfile = profiles[0].id;
        await loadGraph();
      }
    } catch (e) {
      err = e?.message ?? String(e);
    }
  }

  async function loadGraph() {
    if (!selectedProfile) {
      nodes = [];
      edges = [];
      return;
    }
    busy = true;
    err = null;
    try {
      const g = await graph({ profile_id: selectedProfile });
      nodes = g.nodes || [];
      edges = g.edges || [];
    } catch (e) {
      err = e?.message ?? String(e);
    } finally {
      busy = false;
    }
  }

  onMount(refreshProfiles);
  $effect(() => {
    // selectedProfile 변경 감시
    if (selectedProfile) loadGraph();
  });
  onDestroy(() => {});
</script>

<svelte:head>
  <title>지식 그래프 · Maya</title>
</svelte:head>

<main class="page">
  <header class="head">
    <div>
      <a class="back" href={`${base}/knowledge`}>← 지식</a>
      <h1>엔티티 그래프</h1>
      <p class="sub">
        선택한 프로필의 엔티티와 관계를 force layout 으로 시각화합니다.
      </p>
    </div>
    <div class="ctrl">
      <select bind:value={selectedProfile} aria-label="프로필 선택">
        <option value={null}>— 프로필 선택 —</option>
        {#each profiles as p (p.id)}
          <option value={p.id}>{p.name} ({p.id})</option>
        {/each}
      </select>
      <button onclick={refreshProfiles} disabled={busy}>새로고침</button>
    </div>
  </header>

  {#if err}
    <p class="err">⚠ {err}</p>
  {/if}

  <EntityGraph {nodes} {edges} />

  <p class="meta">
    노드 <strong>{nodes.length}</strong> · 엣지 <strong>{edges.length}</strong>
    {#if busy}· 불러오는 중…{/if}
  </p>
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
  }
  .sub {
    color: var(--fg-2);
    font-size: 12px;
    margin: 0;
  }
  .ctrl {
    display: flex;
    gap: 8px;
  }
  .ctrl select {
    background: rgb(255 255 255 / 0.04);
    border: 1px solid rgb(255 255 255 / 0.12);
    border-radius: var(--radius-s);
    color: var(--fg-0);
    padding: 6px 10px;
    font-size: 12px;
  }
  .ctrl button {
    padding: 6px 14px;
    border-radius: var(--radius-s);
    border: 1px solid rgb(255 255 255 / 0.12);
    background: rgb(255 255 255 / 0.04);
    color: var(--fg-0);
    font-size: 12px;
    cursor: pointer;
  }
  .ctrl button:hover:not(:disabled) {
    background: rgb(124 92 255 / 0.18);
    border-color: rgb(124 92 255 / 0.5);
  }
  .err {
    color: var(--color-error);
    font-size: 13px;
  }
  .meta {
    margin-top: 12px;
    color: var(--fg-2);
    font-size: 12px;
  }
  .meta strong {
    color: var(--color-brand-soft);
    font-family: var(--font-mono);
  }
</style>
