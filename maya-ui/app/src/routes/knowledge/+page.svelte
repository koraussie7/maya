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