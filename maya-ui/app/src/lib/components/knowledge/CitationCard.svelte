<script>
  /**
   * CitationCard — /knowledge 검색 한 건을 카드 형태로 보여준다.
   *
   * props (CODEPLAN §0.3 search.hits 스키마):
   *   - chunk_id, text, score
   *   - doc_name, profile_id (optional)
   *   - entity_ids, laya_score (optional)
   *
   * 디자인 토큰: app.css 의 --bg-*, --fg-*, --brand, --accent 사용.
   */
  /** @type {{ chunk_id?: string, text?: string, score?: number, doc_name?: string, profile_id?: string, entity_ids?: string[], laya_score?: number, rank?: number }} */
  let { hit } = $props();

  /** @param {number|undefined} p @returns {string} */
  function fmtProb(p) {
    if (p == null || Number.isNaN(p)) return "—";
    return (p * 100).toFixed(1) + "%";
  }

  /** @param {string|undefined} s @returns {string} 미리보기 (200자 절단) */
  function preview(s) {
    if (!s) return "";
    return s.length > 200 ? s.slice(0, 200) + "…" : s;
  }
</script>

<article class="citation">
  <header>
    <div class="rank">#{hit.rank ?? "?"}</div>
    <div class="score">
      <span class="k">score</span>
      <span class="v">{fmtProb(hit.score)}</span>
    </div>
    {#if hit.laya_score != null}
      <div class="score laya">
        <span class="k">laya</span>
        <span class="v">{fmtProb(hit.laya_score)}</span>
      </div>
    {/if}
  </header>

  <p class="text">{preview(hit.text)}</p>

  <footer>
    {#if hit.doc_name}
      <span class="chip">📄 {hit.doc_name}</span>
    {/if}
    {#if hit.profile_id}
      <span class="chip subtle">👤 {hit.profile_id}</span>
    {/if}
    {#if hit.chunk_id}
      <span class="chip mono">{hit.chunk_id.slice(0, 8)}</span>
    {/if}
  </footer>
</article>

<style>
  .citation {
    background: linear-gradient(
      180deg,
      rgb(255 255 255 / 0.04),
      rgb(255 255 255 / 0.01)
    );
    border: 1px solid rgb(255 255 255 / 0.08);
    border-radius: var(--radius-m);
    padding: 14px 16px;
    display: flex;
    flex-direction: column;
    gap: 8px;
    transition: border-color 0.15s ease;
  }
  .citation:hover {
    border-color: rgb(124 92 255 / 0.5);
  }
  header {
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .rank {
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--fg-2);
    background: rgb(124 92 255 / 0.18);
    border: 1px solid rgb(124 92 255 / 0.4);
    color: rgb(190 173 255);
    padding: 2px 8px;
    border-radius: var(--radius-pill);
  }
  .score {
    display: flex;
    align-items: center;
    gap: 6px;
    font-family: var(--font-mono);
    font-size: 12px;
  }
  .score .k {
    color: var(--fg-2);
  }
  .score .v {
    color: var(--fg-0);
    font-weight: 600;
  }
  .score.laya .v {
    color: var(--color-accent);
  }
  .text {
    margin: 0;
    font-size: 13px;
    line-height: 1.55;
    color: var(--fg-1);
    word-break: break-word;
    white-space: pre-wrap;
  }
  footer {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  .chip {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    padding: 0.2rem 0.55rem;
    background: rgb(255 255 255 / 0.05);
    border: 1px solid rgb(255 255 255 / 0.1);
    border-radius: var(--radius-pill);
    color: var(--fg-1);
    font-size: 11px;
  }
  .chip.subtle {
    background: rgb(255 255 255 / 0.02);
    color: var(--fg-2);
  }
  .chip.mono {
    font-family: var(--font-mono);
    letter-spacing: 0.04em;
  }
</style>
