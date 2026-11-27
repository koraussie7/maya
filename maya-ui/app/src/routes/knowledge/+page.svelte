<script>
  import { onMount, onDestroy } from 'svelte';
  import { health, ingest, search, listProfiles, deleteProfile, graph, profile, fileToBase64 } from '$lib/api/knowledge.js';
  import { knowledgeState, setProfiles, setHits, setGraph, setBusy, setError, clearError, clearHits } from '$lib/stores/knowledge.svelte.js';
  import CitationCard from '$lib/components/knowledge/CitationCard.svelte';

  /** /knowledge/health 결과 — laya 로드 여부 + 청크/엔티티 카운트 */
  /** @type {{laya_loaded: boolean, doc_count: number, graph_entity_count: number}} */
  let layaStatus = $state({ laya_loaded: false, doc_count: 0, graph_entity_count: 0 });

  onMount(async () => {
    setBusy('list', true);
    try {
      const proflist = await listProfiles();
      setProfiles(proflist.profiles || []);
    } catch (e) {
      setError(e.message || '프로필 목록 로드 실패');
    } finally {
      setBusy('list', false);
    }
    clearHits();
    try {
      layaStatus = await health();
    } catch {
      layaStatus = { laya_loaded: false, doc_count: 0, graph_entity_count: 0 };
    }
  });

  onDestroy(() => {
    clearError();
  });

  // ---- actions ----
  /** 프로필 목록 새로고침 */
  async function refreshProfiles() {
    setBusy('list', true);
    try {
      const proflist = await listProfiles();
      setProfiles(proflist.profiles || []);
    } catch (e) {
      setError(e.message || '프로필 목록 로드 실패');
    } finally {
      setBusy('list', false);
    }
  }

  /** 검색 실행 */
  async function doSearch() {
    if (!knowledgeState.query.trim()) return;
    setBusy('search', true);
    setError(null);
    try {
      const result = await search({ query: knowledgeState.query.trim(), top_k: 5 });
      setHits(result.hits || []);
    } catch (e) {
      setError(e.message || '검색 중 오류가 발생했습니다');
    } finally {
      setBusy('search', false);
    }
  }

  /** 프로필 선택 */
  function selectProfile(id) {
    knowledgeState.profile_id = id || null;
  }

  /** 프로필 삭제 확인 후 삭제 */
  async function doDeleteProfile(id) {
    if (!confirm('정말로 이 프로필을 삭제하시겠습니까?')) return;
    setBusy('delete', true);
    setError(null);
    try {
      await deleteProfile({ profile_id: id });
      // 상태에서 제거
      knowledgeState.profiles = knowledgeState.profiles.filter((p) => p.id !== id);
      knowledgeState.profile_id = knowledgeState.profile_id === id ? null : knowledgeState.profile_id;
      setError('프로필이 삭제되었습니다');
      refreshProfiles();
    } catch (e) {
      setError(e.message || '프로필 삭제 실패');
    } finally {
      setBusy('delete', false);
    }
  }

  /** 그래프 조회 */
  async function doGraph() {
    if (!knowledgeState.profile_id) {
      setError('그래프를 보려면 프로필을 선택해주세요');
      return;
    }
    setBusy('graph', true);
    setError(null);
    try {
      const g = await graph({ profile_id: knowledgeState.profile_id });
      setGraph(g, knowledgeState.profile_id);
    } catch (e) {
      setError(e.message || '그래프 로드 실패');
    } finally {
      setBusy('graph', false);
    }
  }

  /** 파일 업로드 핸들러 */
  async function handleFileUpload(event) {
    const file = event.target.files?.[0];
    if (!file) return;

    setBusy('ingest', true);
    setError(null);

    try {
      const { content_base64, name, mime } = await fileToBase64(file);
      const payload = {
        profile_id: `profile-${Date.now()}`,
        files: [{ name, content_base64, mime }]
      };
      const result = await ingest(payload);
      // 프로필 목록에 추가 (서버가 반환한 청크 수)
      setProfiles([{ id: payload.profile_id, name: file.name, doc_count: result.chunks_added || 0, created_at: new Date().toISOString() }, ...knowledgeState.profiles]);
      setError(`"${file.name}" 업로드 완료 (청크 ${result.chunks_added || 0}개)`);
    } catch (e) {
      setError(e.message || '업로드 실패');
    } finally {
      setBusy('ingest', false);
      event.target.value = ''; // 파일 입력 초기화
    }
  }
</script>

<div class="knowledge-page">
  <header class="knowledge-header">
    <h1>지식</h1>
    <div class="header-actions">
      <label class="upload-btn" class:busy={knowledgeState.busy.ingest}>
        <input
          type="file"
          accept=".txt,.md,.markdown,.pdf,.json"
          onchange={handleFileUpload}
          disabled={knowledgeState.busy.ingest}
          aria-label="문서 업로드"
        />
        {#if knowledgeState.busy.ingest}업로드 중…{:else}문서 업로드{/if}
      </label>
      <div class="search-bar">
      <input
        type="text"
        placeholder="검색어를 입력하세요..."
        bind:value={knowledgeState.query}
        disabled={knowledgeState.busy.search}
        onkeydown={(e) => e.key === 'Enter' && doSearch()}
        aria-label="지식 검색"
        class="search-input"
      />
      <button class="search-btn" disabled={knowledgeState.busy.search} onclick={doSearch}>
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <circle cx="11" cy="11" r="8"></circle>
          <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
        </svg>
      </button>
      </div>
    </div>
  </header>

  <main class="knowledge-container">
    <section class="profiles-section">
      <h2>프로필</h2>
      <div class="profiles-grid">
        {#each knowledgeState.profiles as profile (profile.id)}
          <div class="profile-card" onclick={() => selectProfile(profile.id)}>
            <span class="profile-name">{profile.name}</span>
            <span class="profile-docs">{profile.doc_count || 0} 문서</span>
          </div>
        {/each}
        {#if knowledgeState.profiles.length === 0}
          <p class="empty-state">업로드된 프로필이 없습니다.</p>
        {/if}
      </div>
    </section>

    <section class="search-results">
      <h2>검색 결과</h2>
      {#if knowledgeState.hits.length > 0}
        <div class="results-info">
          <span>{knowledgeState.hits.length}개 결과</span>
        </div>
        <div class="results-cards">
          {#each knowledgeState.hits as hit (hit.chunk_id)}
            <CitationCard {hit} />
          {/each}
        </div>
      {:else}
        <p class="no-results">검색어와 일치하는 결과가 없습니다.</p>
      {/if}
    </section>
  </main>
</div>
<style>
  .knowledge-page {
    max-width: 980px;
    margin: 0 auto;
    padding: 24px 20px 80px;
    color: var(--fg-0);
    font-family: var(--font-display);
  }
  .knowledge-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    flex-wrap: wrap;
    margin-bottom: 20px;
  }
  .knowledge-header h1 {
    margin: 0;
    font-size: 22px;
    font-weight: 600;
    letter-spacing: -0.01em;
  }
  .header-actions {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }
  .upload-btn {
    display: inline-flex;
    align-items: center;
    padding: 8px 14px;
    border: 1px solid rgb(124 92 255 / 0.4);
    border-radius: var(--radius-pill);
    background: rgb(124 92 255 / 0.14);
    color: var(--brand-soft);
    font-size: 12px;
    font-weight: 500;
    cursor: pointer;
    transition: background 0.15s ease;
    white-space: nowrap;
  }
  .upload-btn:hover:not(.busy) {
    background: rgb(124 92 255 / 0.26);
  }
  .upload-btn.busy {
    opacity: 0.6;
    cursor: progress;
  }
  /* 실제 input 은 숨기고 label 을 버튼으로 쓴다 */
  .upload-btn input {
    position: absolute;
    width: 1px;
    height: 1px;
    opacity: 0;
    pointer-events: none;
  }
  .search-bar {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 4px 6px 4px 12px;
    border: 1px solid rgb(255 255 255 / 0.1);
    border-radius: var(--radius-pill);
    background: var(--bg-1);
  }
  .search-bar:focus-within {
    border-color: rgb(124 92 255 / 0.55);
    box-shadow: 0 0 0 3px rgb(124 92 255 / 0.15);
  }
  .search-input {
    width: 240px;
    padding: 6px 0;
    border: none;
    outline: none;
    background: transparent;
    color: var(--fg-0);
    font-size: 13px;
    font-family: inherit;
  }
  .search-input::placeholder {
    color: var(--fg-2);
  }
  .search-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 30px;
    height: 30px;
    border: none;
    border-radius: 50%;
    background: rgb(124 92 255 / 0.18);
    color: var(--brand-soft);
    cursor: pointer;
  }
  .search-btn:hover:not(:disabled) {
    background: rgb(124 92 255 / 0.3);
  }
  .search-btn:disabled {
    opacity: 0.45;
    cursor: not-allowed;
  }
  .knowledge-container {
    display: grid;
    gap: 22px;
  }
  section h2 {
    margin: 0 0 10px;
    font-size: 12px;
    font-weight: 500;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: var(--fg-2);
  }
  .profiles-grid,
  .results-cards {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
    gap: 10px;
  }
  .profile-card {
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding: 12px 14px;
    border: 1px solid rgb(255 255 255 / 0.08);
    border-radius: var(--radius-m);
    background: var(--bg-1);
    cursor: pointer;
    transition:
      border-color 0.15s ease,
      background 0.15s ease;
  }
  .profile-card:hover {
    border-color: rgb(124 92 255 / 0.4);
    background: var(--bg-2);
  }
  .profile-name {
    font-size: 13px;
    font-weight: 500;
  }
  .profile-docs {
    font-size: 11px;
    color: var(--fg-2);
    font-family: var(--font-mono);
  }
  .empty-state,
  .no-results {
    margin: 0;
    padding: 20px 0;
    font-size: 13px;
    color: var(--fg-2);
  }
  .results-info {
    margin-bottom: 8px;
    font-size: 11px;
    color: var(--fg-2);
    font-family: var(--font-mono);
  }
  @media (max-width: 560px) {
    .search-input {
      width: 140px;
    }
  }
</style>
