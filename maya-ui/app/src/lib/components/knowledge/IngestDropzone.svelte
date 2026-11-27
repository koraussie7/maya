<script>
  /**
   * IngestDropzone — 드래그&드롭 파일 업로드.
   *
   * 부모가 `onFile` 콜백을 주면 단일 파일을 base64 payload 로 변환해 호출.
   * CODEPLAN §0.3 ingest.files[] 스키마 (name, content_base64, mime) 와 1:1.
   *
   * 사용 예:
   *   <IngestDropzone onFile={(f) => handleUpload(f)} />
   */
  import { fileToBase64 } from "$lib/api/knowledge.js";

  /** @type {{ onFile?: (payload: {name:string, content_base64:string, mime:string}) => void | Promise<void>, disabled?: boolean }} */
  let { onFile, disabled = false } = $props();

  /** @type {boolean} */
  let dragOver = $state(false);
  /** @type {boolean} */
  let busy = $state(false);
  /** @type {string|null} */
  let err = $state(null);

  /** @param {DragEvent} e */
  function onDragEnter(e) {
    e.preventDefault();
    if (!disabled && !busy) dragOver = true;
  }
  /** @param {DragEvent} e */
  function onDragLeave(e) {
    e.preventDefault();
    dragOver = false;
  }
  /** @param {DragEvent} e */
  function onDragOver(e) {
    e.preventDefault();
  }
  /** @param {DragEvent} e */
  async function onDrop(e) {
    e.preventDefault();
    dragOver = false;
    if (disabled || busy) return;
    const file = e.dataTransfer?.files?.[0];
    if (!file) return;
    await handle(file);
  }
  /** @param {Event} e */
  async function onPick(e) {
    const t = /** @type {HTMLInputElement} */ (e.target);
    const file = t.files?.[0];
    if (!file) return;
    await handle(file);
    t.value = "";
  }
  /** @param {File} file */
  async function handle(file) {
    busy = true;
    err = null;
    try {
      const payload = await fileToBase64(file);
      await onFile?.(payload);
    } catch (e) {
      err = e?.message ?? String(e);
    } finally {
      busy = false;
    }
  }
</script>

<div
  class="drop"
  class:over={dragOver}
  class:disabled
  ondragenter={onDragEnter}
  ondragleave={onDragLeave}
  ondragover={onDragOver}
  ondrop={onDrop}
  role="button"
  aria-label="파일 드래그 앤 드롭"
  tabindex="0"
>
  <input
    type="file"
    accept=".md,.txt,.pdf,text/markdown,text/plain,application/pdf"
    onchange={onPick}
    disabled={disabled || busy}
    aria-hidden="true"
  />
  <div class="content">
    {#if busy}
      <p class="lead">업로드 중…</p>
    {:else}
      <p class="lead">📁 파일을 여기로 끌어다 놓으세요</p>
      <p class="sub">또는 클릭해서 선택 (.md, .txt, .pdf)</p>
    {/if}
    {#if err}
      <p class="err">{err}</p>
    {/if}
  </div>
</div>

<style>
  .drop {
    border: 1.5px dashed rgb(255 255 255 / 0.18);
    border-radius: var(--radius-m);
    padding: 24px;
    text-align: center;
    background: rgb(255 255 255 / 0.02);
    transition:
      border-color 0.15s ease,
      background 0.15s ease;
    position: relative;
  }
  .drop.over {
    border-color: var(--color-brand);
    background: rgb(124 92 255 / 0.08);
  }
  .drop.disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .drop input[type="file"] {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    opacity: 0;
    cursor: pointer;
  }
  .content .lead {
    margin: 0 0 6px 0;
    font-size: 14px;
    color: var(--fg-0);
  }
  .content .sub {
    margin: 0;
    font-size: 12px;
    color: var(--fg-2);
  }
  .content .err {
    margin: 8px 0 0 0;
    font-size: 12px;
    color: var(--color-error);
  }
</style>
