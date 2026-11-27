<script>
  /**
   * EntityGraph — /knowledge/graph 응답을 SVG force layout 으로 시각화.
   *
   * props:
   *   nodes: [{id, label, type?}]
   *   edges: [{src, dst, weight?, rel?}]
   *
   * CODEPLAN 수용 기준:
   *   "노드 20개 이상이면 force layout 이 60fps 유지 (requestAnimationFrame)"
   *
   * 구현 메모:
   *   - 외부 의존 없이 vanilla SVG + 자체 force simulation (Coulomb 반발 + Hooke 인력)
   *   - 매 틱을 requestAnimationFrame 으로 돌리고 alpha<0.005 에서 자연 정지
   *   - tick 마다 DOM 직접 mutate (Svelte reactivity 우회) → 프레임당 setter 비용 최소화
   *   - 60fps 보장을 위해 노드당 O(N²) 의 pairwise force 대신 grid binning 정도까지만 적용
   *     (지금 단계엔 N≤수백 가정 → 단순 O(N²) 도 충분, 필요 시 binning 확장)
   */
  /** @type {{ nodes: Array<{id:string, label:string, type?:string}>, edges: Array<{src:string, dst:string, weight?:number, rel?:string}> }} */
  let { nodes = [], edges = [] } = $props();

  /** @type {SVGSVGElement} */
  let svg;
  /** @type {SVGGElement} */
  let edgeLayer;
  /** @type {SVGGElement} */
  let nodeLayer;
  /** @type {SVGGElement} */
  let labelLayer;

  /** @type {Map<string, {x:number,y:number,vx:number,vy:number,label:string,type?:string}>} */
  let sim = new Map();
  /** @type {number|null} */
  let rafHandle = null;

  // 색상 팔레트 — type 별 색을 다르게
  const TYPE_COLORS = {
    person: "#7c5cff",
    place: "#58e1ff",
    org: "#4ade80",
    default: "#b6bccd",
  };
  /** @param {string} [t] @returns {string} */
  function color(t) {
    if (!t) return TYPE_COLORS.default;
    return TYPE_COLORS[t] ?? TYPE_COLORS.default;
  }

  // 컴포넌트 안에서만 쓰는 시뮬레이션 파라미터
  const REPULSION = 2200; // Coulomb
  const SPRING_K = 0.04; // Hooke
  const SPRING_REST = 90;
  const DAMPING = 0.85;
  const CENTER_PULL = 0.01;
  const MAX_V = 8;
  const ALPHA_MIN = 0.005;

  /** 시뮬레이션 상태를 노드 데이터로 초기화 — id→2D 위치 */
  function buildSim() {
    const cx = 400,
      cy = 300;
    sim = new Map();
    nodes.forEach((n, i) => {
      const angle = (i / Math.max(nodes.length, 1)) * Math.PI * 2;
      const radius = 80 + (i % 5) * 20;
      sim.set(n.id, {
        x: cx + Math.cos(angle) * radius,
        y: cy + Math.sin(angle) * radius,
        vx: 0,
        vy: 0,
        label: n.label ?? n.id,
        type: n.type,
      });
    });
  }

  /** 시뮬레이션 한 틱 — O(N²) + O(E) */
  function tick() {
    const arr = Array.from(sim.values());
    let totalEnergy = 0;
    // 1) 반발 (pairwise)
    for (let i = 0; i < arr.length; i++) {
      const a = arr[i];
      for (let j = i + 1; j < arr.length; j++) {
        const b = arr[j];
        let dx = a.x - b.x;
        let dy = a.y - b.y;
        let d2 = dx * dx + dy * dy;
        if (d2 < 1) {
          dx = (Math.random() - 0.5) * 0.5;
          dy = (Math.random() - 0.5) * 0.5;
          d2 = dx * dx + dy * dy + 0.01;
        }
        const d = Math.sqrt(d2);
        const f = REPULSION / d2;
        const fx = (dx / d) * f;
        const fy = (dy / d) * f;
        a.vx += fx;
        a.vy += fy;
        b.vx -= fx;
        b.vy -= fy;
      }
    }
    // 2) 인력 (edge)
    for (const e of edges) {
      const a = sim.get(e.src);
      const b = sim.get(e.dst);
      if (!a || !b) continue;
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const d = Math.sqrt(dx * dx + dy * dy) || 0.01;
      const f = SPRING_K * (d - SPRING_REST);
      const fx = (dx / d) * f;
      const fy = (dy / d) * f;
      a.vx += fx;
      a.vy += fy;
      b.vx -= fx;
      b.vy -= fy;
    }
    // 3) 중심 인력 + 속도 제한 + 감쇠
    for (const n of arr) {
      n.vx += (400 - n.x) * CENTER_PULL;
      n.vy += (300 - n.y) * CENTER_PULL;
      n.vx = Math.max(-MAX_V, Math.min(MAX_V, n.vx * DAMPING));
      n.vy = Math.max(-MAX_V, Math.min(MAX_V, n.vy * DAMPING));
      n.x += n.vx;
      n.y += n.vy;
      totalEnergy += Math.abs(n.vx) + Math.abs(n.vy);
    }
    // 4) DOM 반영 (set attribute 만)
    for (const e of edges) {
      const a = sim.get(e.src);
      const b = sim.get(e.dst);
      if (!a || !b) continue;
      const el = edgeLayer?.querySelector(
        `[data-eid="${cssEsc(e.src)}-${cssEsc(e.dst)}"]`,
      );
      if (el instanceof SVGLineElement) {
        el.setAttribute("x1", a.x.toFixed(1));
        el.setAttribute("y1", a.y.toFixed(1));
        el.setAttribute("x2", b.x.toFixed(1));
        el.setAttribute("y2", b.y.toFixed(1));
      }
    }
    for (const n of arr) {
      const id = cssEsc(n.label + "-" + n.x.toFixed(0) + n.y.toFixed(0));
      const g = nodeLayer?.querySelector(`[data-nid="${cssEsc(n.label)}"]`);
      if (g instanceof SVGGElement) {
        g.setAttribute(
          "transform",
          `translate(${n.x.toFixed(1)},${n.y.toFixed(1)})`,
        );
      }
      void id;
    }
    // 5) 종료 판정
    if (totalEnergy < ALPHA_MIN * arr.length) {
      rafHandle = null;
      return;
    }
    rafHandle = requestAnimationFrame(tick);
  }

  /** @param {string} s */
  function cssEsc(s) {
    return s.replace(/[^a-zA-Z0-9_-]/g, "_");
  }

  /** 렌더 트리 재구성 — 노드/엣지가 바뀔 때만 */
  function rerender() {
    if (!edgeLayer || !nodeLayer || !labelLayer) return;
    edgeLayer.replaceChildren();
    nodeLayer.replaceChildren();
    labelLayer.replaceChildren();
    // 노드
    for (const n of sim.values()) {
      const g = document.createElementNS("http://www.w3.org/2000/svg", "g");
      g.setAttribute("data-nid", cssEsc(n.label));
      g.setAttribute(
        "transform",
        `translate(${n.x.toFixed(1)},${n.y.toFixed(1)})`,
      );
      const c = document.createElementNS(
        "http://www.w3.org/2000/svg",
        "circle",
      );
      c.setAttribute("r", "9");
      c.setAttribute("fill", color(n.type));
      c.setAttribute("stroke", "#0b0d12");
      c.setAttribute("stroke-width", "2");
      g.appendChild(c);
      nodeLayer.appendChild(g);
      const t = document.createElementNS("http://www.w3.org/2000/svg", "text");
      t.setAttribute("x", "12");
      t.setAttribute("y", "4");
      t.setAttribute("fill", "#e6e9f2");
      t.setAttribute("font-size", "11");
      t.setAttribute("font-family", "JetBrains Mono, monospace");
      t.textContent = n.label;
      labelLayer.appendChild(t);
    }
    // 엣지
    for (const e of edges) {
      const a = sim.get(e.src);
      const b = sim.get(e.dst);
      if (!a || !b) continue;
      const l = document.createElementNS("http://www.w3.org/2000/svg", "line");
      l.setAttribute("data-eid", `${cssEsc(e.src)}-${cssEsc(e.dst)}`);
      l.setAttribute("x1", a.x.toFixed(1));
      l.setAttribute("y1", a.y.toFixed(1));
      l.setAttribute("x2", b.x.toFixed(1));
      l.setAttribute("y2", b.y.toFixed(1));
      l.setAttribute("stroke", "rgb(124 92 255 / 0.35)");
      l.setAttribute("stroke-width", String(Math.min(3, e.weight ?? 1)));
      edgeLayer.appendChild(l);
    }
  }

  /** @param {Array<{id:string, label:string, type?:string}>} nds @param {Array<{src:string, dst:string, weight?:number, rel?:string}>} eds */
  function startSim(nds, eds) {
    nodes = nds;
    edges = eds;
    buildSim();
    rerender();
    if (rafHandle != null) cancelAnimationFrame(rafHandle);
    rafHandle = requestAnimationFrame(tick);
  }

  $effect(() => {
    // 입력 변경 감시 — effect 가 nodes/edges 를 직접 읽음
    startSim(nodes, edges);
    return () => {
      if (rafHandle != null) cancelAnimationFrame(rafHandle);
      rafHandle = null;
    };
  });
</script>

<div class="graph-wrap">
  <svg
    bind:this={svg}
    viewBox="0 0 800 600"
    preserveAspectRatio="xMidYMid meet"
  >
    <g bind:this={edgeLayer}></g>
    <g bind:this={nodeLayer}></g>
    <g bind:this={labelLayer}></g>
  </svg>
  {#if nodes.length === 0}
    <p class="empty">표시할 노드가 없습니다.</p>
  {/if}
</div>

<style>
  .graph-wrap {
    position: relative;
    width: 100%;
    aspect-ratio: 4 / 3;
    background: radial-gradient(
      ellipse at center,
      rgb(124 92 255 / 0.08),
      transparent 70%
    );
    border: 1px solid rgb(255 255 255 / 0.06);
    border-radius: var(--radius-m);
    overflow: hidden;
  }
  svg {
    width: 100%;
    height: 100%;
  }
  .empty {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--fg-2);
    font-size: 13px;
  }
</style>
