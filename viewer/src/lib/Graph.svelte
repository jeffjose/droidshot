<script lang="ts">
  import { assetUrl, type Droidshot } from '$lib/droidshot';

  let { ds, currentId, onpick }: { ds: Droidshot; currentId: string; onpick: (id: string) => void } =
    $props();

  // card + grid geometry (world px)
  const CW = 150;
  const IMGH = 150;
  const LABELH = 28;
  const CH = IMGH + LABELH;
  const COLX = 250; // horizontal step per depth
  const ROWY = 210; // vertical step per leaf slot
  const MARGIN = 40;

  // --- layout: tidy tree, columns by depth, parents centred over children ---
  const layout = $derived.by(() => {
    const nodes = ds.manifest.nodes;
    const edges = ds.manifest.edges;
    const parent = new Map<string, string>(); // child -> first (tree) parent
    for (const e of edges) if (e.to !== 'n0' && !parent.has(e.to)) parent.set(e.to, e.from);
    const kids = new Map<string, string[]>();
    for (const n of nodes) {
      const p = parent.get(n.id);
      if (p) (kids.get(p) ?? kids.set(p, []).get(p)!).push(n.id);
    }
    const pos = new Map<string, { x: number; y: number; depth: number }>();
    let slot = 0;
    const place = (id: string, depth: number): number => {
      const cs = kids.get(id) ?? [];
      let y: number;
      if (cs.length === 0) y = slot++;
      else {
        const ys = cs.map((c) => place(c, depth + 1));
        y = (ys[0] + ys[ys.length - 1]) / 2;
      }
      pos.set(id, { x: MARGIN + depth * COLX, y: MARGIN + y * ROWY, depth });
      return y;
    };
    for (const n of nodes) if (!parent.has(n.id)) place(n.id, 0); // roots (n0)
    // any node not reached (shouldn't happen) gets parked in a trailing column
    for (const n of nodes) if (!pos.has(n.id)) place(n.id, 0);

    const ex = [...pos.values()];
    const W = Math.max(...ex.map((p) => p.x), 0) + CW + MARGIN;
    const H = Math.max(...ex.map((p) => p.y), 0) + CH + MARGIN;

    const links = edges.map((e) => {
      const a = pos.get(e.from);
      const b = pos.get(e.to);
      const tree = parent.get(e.to) === e.from;
      return a && b ? { from: e.from, to: e.to, a, b, tree, label: (e as any).label as string } : null;
    });
    return { pos, links: links.filter(Boolean) as NonNullable<(typeof links)[number]>[], W, H };
  });

  // thumbnails (top of each screenshot). Built once per file; revoked on change.
  let urls = $state<Record<string, string>>({});
  $effect(() => {
    const map: Record<string, string> = {};
    for (const n of ds.manifest.nodes) map[n.id] = assetUrl(ds, n.screenshot);
    urls = map;
    return () => {
      for (const u of Object.values(map)) URL.revokeObjectURL(u);
    };
  });

  const title = (id: string) => {
    const n = ds.manifest.nodes.find((x) => x.id === id);
    return (n as any)?.title || n?.activity?.split('/').pop() || id;
  };

  // --- pan / zoom ---
  let tx = $state(MARGIN);
  let ty = $state(MARGIN);
  let s = $state(0.7);
  let canvasEl = $state<HTMLDivElement>();
  let panning = false;
  let sx = 0, sy = 0, stx = 0, sty = 0;

  function fit() {
    if (!canvasEl) return;
    const r = canvasEl.getBoundingClientRect();
    s = Math.min(1, Math.min(r.width / layout.W, r.height / layout.H)) || 0.7;
    tx = (r.width - layout.W * s) / 2;
    ty = 20;
  }
  function zoomAt(cx: number, cy: number, factor: number) {
    const ns = Math.min(3, Math.max(0.1, s * factor));
    tx = cx - ((cx - tx) * ns) / s;
    ty = cy - ((cy - ty) * ns) / s;
    s = ns;
  }
  function onwheel(e: WheelEvent) {
    e.preventDefault();
    const r = canvasEl!.getBoundingClientRect();
    zoomAt(e.clientX - r.left, e.clientY - r.top, e.deltaY < 0 ? 1.12 : 1 / 1.12);
  }
  function down(e: PointerEvent) {
    if ((e.target as HTMLElement).closest('.card')) return; // let card clicks through
    panning = true;
    sx = e.clientX; sy = e.clientY; stx = tx; sty = ty;
  }
  function move(e: PointerEvent) {
    if (!panning) return;
    tx = stx + (e.clientX - sx);
    ty = sty + (e.clientY - sy);
  }
  function up() {
    panning = false;
  }

  // an edge path: from the right edge of `a` to the left edge of `b`, bezier
  function edgePath(a: { x: number; y: number }, b: { x: number; y: number }) {
    const x1 = a.x + CW, y1 = a.y + IMGH / 2;
    const x2 = b.x, y2 = b.y + IMGH / 2;
    const dx = Math.max(40, (x2 - x1) / 2);
    return `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
  }
</script>

<svelte:window onpointermove={move} onpointerup={up} />

<div class="wrap">
  <div class="tools">
    <button onclick={() => zoomAt(200, 200, 1 / 1.2)} aria-label="zoom out">−</button>
    <button class="pct" onclick={fit} title="fit to window">{Math.round(s * 100)}%</button>
    <button onclick={() => zoomAt(200, 200, 1.2)} aria-label="zoom in">+</button>
    <button class="fit" onclick={fit} title="fit">⊡</button>
  </div>

  <div class="canvas" bind:this={canvasEl} onpointerdown={down} onwheel={onwheel} role="presentation">
    <div class="world" style="transform: translate({tx}px, {ty}px) scale({s})">
      <svg class="edges" width={layout.W} height={layout.H}>
        <defs>
          <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7"
            orient="auto-start-reverse">
            <path d="M0,0 L10,5 L0,10 z" fill="#4a5570" />
          </marker>
          <marker id="arrow-rev" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7"
            orient="auto-start-reverse">
            <path d="M0,0 L10,5 L0,10 z" fill="#6b5570" />
          </marker>
        </defs>
        {#each layout.links as l}
          <path d={edgePath(l.a, l.b)} fill="none"
            stroke={l.tree ? '#4a5570' : '#6b5570'} stroke-width={l.tree ? 1.5 : 1.2}
            stroke-dasharray={l.tree ? 'none' : '5 4'}
            marker-end={l.tree ? 'url(#arrow)' : 'url(#arrow-rev)'} />
        {/each}
      </svg>

      {#each ds.manifest.nodes as n}
        {@const p = layout.pos.get(n.id)}
        {#if p}
          <button class="card" class:active={n.id === currentId}
            style="left:{p.x}px; top:{p.y}px; width:{CW}px"
            onclick={() => onpick(n.id)} title={title(n.id)}>
            <span class="shot" style="height:{IMGH}px">
              {#if urls[n.id]}<img src={urls[n.id]} alt="" />{/if}
            </span>
            <span class="lbl">{title(n.id)}</span>
          </button>
        {/if}
      {/each}
    </div>
  </div>
</div>

<style>
  .wrap { position: relative; width: 100%; }
  .tools { position: absolute; top: 8px; right: 8px; z-index: 4; display: flex; gap: 2px;
    background: #14171d; border: 1px solid #20242c; border-radius: 8px; padding: 2px; }
  .tools button { background: none; border: 0; color: #cbd2dc; padding: 4px 9px; border-radius: 6px;
    cursor: pointer; font-size: 14px; line-height: 1; }
  .tools button:hover { background: #1a1d23; }
  .tools .pct { min-width: 46px; font-variant-numeric: tabular-nums; font-size: 12px; color: #9aa4b2; }

  .canvas { width: 100%; height: calc(100vh - 120px); overflow: hidden; cursor: grab;
    background: #0b0c0f radial-gradient(#1a1d24 1px, transparent 1px) 0 0 / 22px 22px;
    border: 1px solid #20242c; border-radius: 12px; touch-action: none; }
  .canvas:active { cursor: grabbing; }
  .world { position: absolute; top: 0; left: 0; transform-origin: 0 0; }
  .edges { position: absolute; top: 0; left: 0; overflow: visible; pointer-events: none; }

  .card { position: absolute; display: flex; flex-direction: column; padding: 0; cursor: pointer;
    background: #161a21; border: 1px solid #2a2e37; border-radius: 10px; overflow: hidden;
    box-shadow: 0 6px 18px rgba(0,0,0,.45); }
  .card:hover { border-color: #3a72f0; }
  .card.active { border-color: #2a6ef0; box-shadow: 0 0 0 2px #2a6ef0, 0 8px 22px rgba(0,0,0,.5); }
  .shot { display: block; overflow: hidden; background: #000; line-height: 0; }
  .shot img { width: 100%; display: block; object-fit: cover; object-position: top; }
  .lbl { padding: 6px 8px; font-size: 12px; color: #cbd2dc; white-space: nowrap; overflow: hidden;
    text-overflow: ellipsis; text-align: left; }
</style>
