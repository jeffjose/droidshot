<script lang="ts">
  import {
    openDroidshot,
    assetUrl,
    assetText,
    parseHierarchy,
    type Droidshot,
    type HierNode,
    type Edge
  } from '$lib/droidshot';

  let ds = $state<Droidshot | null>(null);
  let error = $state<string | null>(null);
  let currentId = $state('n0');
  let xray = $state(false);
  let hovered = $state<HierNode | null>(null);
  let imgW = $state(0); // displayed screenshot width, for scaling overlays

  const node = $derived(ds?.manifest.nodes.find((n) => n.id === currentId) ?? null);
  const outEdges = $derived(ds?.manifest.edges.filter((e) => e.from === currentId) ?? []);
  // "back" = up a level: the screen we drilled in FROM (parent view), not a
  // scroll-up. Follow the non-swipe edge that enters this node's view.
  const parentId = $derived.by(() => {
    if (!ds) return null;
    const vof = (id: string) => ds!.manifest.nodes.find((n) => n.id === id)?.scroll?.chain ?? id;
    const cv = vof(currentId);
    const e = ds.manifest.edges.find(
      (e) => e.action.type === 'tap' && vof(e.to) === cv && vof(e.from) !== cv
    );
    return e?.from ?? null;
  });
  // scroll chain: follow swipe edges forward/back
  const scrollDown = $derived(outEdges.find((e) => e.action.type === 'swipe')?.to ?? null);
  const scrollUp = $derived(
    ds?.manifest.edges.find((e) => e.to === currentId && e.action.type === 'swipe')?.from ?? null
  );
  // non-swipe/non-tap edges shown as buttons (key, captured back); taps are hotspots
  const barEdges = $derived(outEdges.filter((e) => e.action.type === 'key'));

  // scrollbar: current node's position within its scroll chain
  const sc = $derived(node?.scroll ?? null);
  const chainNodes = $derived(
    sc && ds
      ? ds.manifest.nodes
          .filter((n) => n.scroll?.chain === sc.chain)
          .sort((a, b) => (a.scroll!.y ?? 0) - (b.scroll!.y ?? 0))
      : []
  );
  const chainIdx = $derived(chainNodes.findIndex((n) => n.id === currentId));

  const deviceW = $derived(ds?.manifest.device.display.w ?? 1080);
  const aspect = $derived(deviceW / (ds?.manifest.device.display.h ?? 2400)); // w/h
  const scale = $derived(imgW > 0 ? imgW / deviceW : 0);
  const shotUrl = $derived(ds && node ? assetUrl(ds, node.screenshot) : '');
  const hier = $derived.by(() => {
    if (!ds || !node?.hierarchy) return [] as HierNode[];
    return parseHierarchy(assetText(ds, node.hierarchy));
  });

  function actionLabel(e: Edge): string {
    const a = e.action;
    if (a.type === 'tap') return a.elementId ? `tap ${a.elementId.split('/').pop()}` : `tap ${a.x},${a.y}`;
    if (a.type === 'swipe') return `scroll ${(a as any).dir ?? ''}`.trim();
    if (a.type === 'key') return `key ${(a as any).key}`;
    return a.type; // back
  }

  const area = (b: number[]) => (b[2] - b[0]) * (b[3] - b[1]);
  // a tap's hotspot should cover the whole clickable row/container, not just the
  // text element that was matched — find the smallest clickable node under the tap
  function hotspotRect(e: Edge): [number, number, number, number] | null {
    const a = e.action as any;
    const cx = a.bounds ? (a.bounds[0] + a.bounds[2]) / 2 : a.x;
    const cy = a.bounds ? (a.bounds[1] + a.bounds[3]) / 2 : a.y;
    if (cx == null || cy == null) return a.bounds ?? null;
    const hits = hier.filter(
      (n) => n.clickable && area(n.bounds) > 0 &&
        n.bounds[0] <= cx && cx <= n.bounds[2] && n.bounds[1] <= cy && cy <= n.bounds[3]
    );
    hits.sort((p, q) => area(p.bounds) - area(q.bounds));
    return hits[0]?.bounds ?? a.bounds ?? null;
  }

  // --- views: a scroll chain is ONE view with N viewports; only drilling (a
  // non-swipe edge to another view) creates a new level in the flow tree ---
  const nodeById = (id: string) => ds?.manifest.nodes.find((n) => n.id === id) ?? null;
  const viewOf = (id: string) => nodeById(id)?.scroll?.chain ?? id;

  function viewMembers(view: string): string[] {
    if (!ds) return [];
    return ds.manifest.nodes
      .filter((n) => (n.scroll?.chain ?? n.id) === view)
      .sort((a, b) => (a.scroll?.y ?? 0) - (b.scroll?.y ?? 0))
      .map((n) => n.id);
  }
  function screenName(view: string): string {
    const act = nodeById(view)?.activity ?? '';
    return act.split('/').pop()?.split('.').pop()?.replace(/Activity$/, '') || 'screen';
  }
  function drillLabel(e: Edge): string {
    // human name for a drill-in = the text at the tap location in the source screen.
    // prefer the actual tapped point (x,y) — bounds is the whole row, whose centre
    // can miss the title text.
    const src = nodeById(e.from);
    const a = e.action as any;
    const b = e.action.bounds;
    if (ds && src?.hierarchy && (a.x != null || b)) {
      const cx = a.x ?? (b[0] + b[2]) / 2,
        cy = a.y ?? (b[1] + b[3]) / 2;
      const hits = parseHierarchy(assetText(ds, src.hierarchy)).filter(
        (n) => n.text && n.bounds[0] <= cx && cx <= n.bounds[2] && n.bounds[1] <= cy && cy <= n.bounds[3]
      );
      hits.sort(
        (a, z) =>
          (a.bounds[2] - a.bounds[0]) * (a.bounds[3] - a.bounds[1]) -
          (z.bounds[2] - z.bounds[0]) * (z.bounds[3] - z.bounds[1])
      );
      if (hits.length) return hits[0].text;
    }
    return actionLabel(e);
  }

  const viewTree = $derived.by(() => {
    if (!ds) return [] as { view: string; depth: number; edge: Edge | null }[];
    const vof = (id: string) => ds!.manifest.nodes.find((n) => n.id === id)?.scroll?.chain ?? id;
    // only taps drill into a new screen; back/key are navigation, not structure
    const drills = ds.manifest.edges.filter((e) => e.action.type === 'tap' && vof(e.from) !== vof(e.to));
    const incoming = new Set(drills.map((e) => vof(e.to)));
    const allViews: string[] = [];
    for (const n of ds.manifest.nodes) {
      const v = n.scroll?.chain ?? n.id;
      if (!allViews.includes(v)) allViews.push(v);
    }
    const seen = new Set<string>();
    const out: { view: string; depth: number; edge: Edge | null }[] = [];
    const walk = (v: string, depth: number, edge: Edge | null) => {
      if (seen.has(v)) return;
      seen.add(v);
      out.push({ view: v, depth, edge });
      for (const e of drills.filter((e) => vof(e.from) === v)) walk(vof(e.to), depth + 1, e);
    };
    for (const r of allViews.filter((v) => !incoming.has(v))) walk(r, 0, null);
    return out;
  });

  function navigate(to: string) {
    currentId = to;
    hovered = null;
  }
  function flowBack() {
    if (parentId) navigate(parentId);
  }

  // the toolbar "up/back" arrow drawn in the screenshot → wire it to flow-back
  const upRect = $derived.by(() => {
    if (!parentId) return null; // nothing to go back to
    const byDesc = hier.find((n) => /navigate up|^back$|go back/i.test(n.contentDesc));
    if (byDesc) return byDesc.bounds;
    // view-tree screens have no content-desc: a clickable ImageButton in the top-left toolbar
    const dispH = ds?.manifest.device.display.h ?? 2400;
    const btn = hier.find(
      (n) => n.clickable && /ImageButton$/.test(n.cls) &&
        n.bounds[0] < 90 && n.bounds[1] < dispH * 0.3 &&
        n.bounds[2] - n.bounds[0] > 0 && n.bounds[2] - n.bounds[0] <= 230
    );
    return btn?.bounds ?? null;
  });

  // --- scrollbar interaction: drag, gutter-paging, wheel ---
  let trackEl = $state<HTMLDivElement>();
  let dragging = $state(false);

  function nearestInChain(clientY: number): string | null {
    if (!sc || !trackEl || chainNodes.length === 0) return null;
    const r = trackEl.getBoundingClientRect();
    const targetY = Math.min(1, Math.max(0, (clientY - r.top) / r.height)) * sc.content;
    let best = chainNodes[0];
    for (const n of chainNodes)
      if (Math.abs((n.scroll!.y ?? 0) - targetY) < Math.abs((best.scroll!.y ?? 0) - targetY)) best = n;
    return best.id;
  }
  function startDrag(ev: PointerEvent) {
    ev.stopPropagation();
    ev.preventDefault();
    dragging = true;
    (ev.target as HTMLElement).setPointerCapture?.(ev.pointerId);
  }
  function onDragMove(ev: PointerEvent) {
    if (!dragging) return;
    const id = nearestInChain(ev.clientY);
    if (id) currentId = id;
  }
  function endDrag() {
    dragging = false;
  }
  function gutterClick(ev: PointerEvent) {
    if (!sc || !trackEl) return;
    const r = trackEl.getBoundingClientRect();
    const frac = (ev.clientY - r.top) / r.height;
    if (frac < sc.y / sc.content) {
      const prev = chainNodes[chainIdx - 1];
      if (prev) navigate(prev.id);
    } else if (frac > (sc.y + sc.viewport) / sc.content) {
      const next = chainNodes[chainIdx + 1];
      if (next) navigate(next.id);
    }
  }
  let lastWheel = 0;
  function onWheel(ev: WheelEvent) {
    if (!scrollDown && !scrollUp) return; // not scrollable here — let the page scroll
    ev.preventDefault();
    const now = performance.now();
    if (now - lastWheel < 200) return; // one step per notch
    if (ev.deltaY > 4 && scrollDown) (navigate(scrollDown), (lastWheel = now));
    else if (ev.deltaY < -4 && scrollUp) (navigate(scrollUp), (lastWheel = now));
  }

  async function load(file: File) {
    error = null;
    try {
      ds = openDroidshot(new Uint8Array(await file.arrayBuffer()));
      currentId = ds.manifest.nodes[0]?.id ?? 'n0';
    } catch (e) {
      ds = null;
      error = e instanceof Error ? e.message : String(e);
    }
  }
  const onDrop = (ev: DragEvent) => {
    ev.preventDefault();
    const f = ev.dataTransfer?.files?.[0];
    if (f) load(f);
  };
  const onPick = (ev: Event) => {
    const f = (ev.target as HTMLInputElement).files?.[0];
    if (f) load(f);
  };
</script>

<svelte:head><title>droidshot viewer</title></svelte:head>
<svelte:window onpointermove={onDragMove} onpointerup={endDrag} />

<main ondragover={(e) => e.preventDefault()} ondrop={onDrop} role="application" aria-label="droidshot viewer">
  <header>
    <h1>droidshot</h1>
    <label class="pick">
      open .droidshot
      <input type="file" accept=".droidshot,application/zip" onchange={onPick} hidden />
    </label>
    {#if ds}
      <label class="toggle"><input type="checkbox" bind:checked={xray} /> x-ray</label>
    {/if}
  </header>

  {#if error}<p class="error">⚠ {error}</p>{/if}

  {#if !ds}
    <div class="dropzone"><p>Drag a <code>.droidshot</code> here, or use “open”.</p></div>
  {:else if node}
    <div class="layout">
      <!-- left: flow tree (file-explorer style) -->
      <aside class="tree">
        <div class="tree-h">flow</div>
        {#each viewTree as v}
          {@const mem = viewMembers(v.view)}
          <button class="trow" class:active={viewOf(currentId) === v.view}
            style="padding-left:{8 + v.depth * 16}px" onclick={() => navigate(v.view)}>
            <span class="tlabel">{v.edge ? drillLabel(v.edge) : screenName(v.view)}</span>
            {#if mem.length > 1}<span class="vcount" title="{mem.length} viewports">⊞ {mem.length}</span>{/if}
            <span class="dbg">{mem.length > 1 ? `${mem[0]}–${mem[mem.length - 1]}` : mem[0]}</span>
          </button>
        {/each}
      </aside>

      <section class="stage">
        <div class="phone-row">
          <div class="leftctrl">
            <button class="scroll-btn" disabled={!parentId} onclick={flowBack}
              title="back (up the captured flow)" aria-label="back">←</button>
          </div>
          <div class="phone"
            style="--ar:{aspect}; width: min(400px, 84vw, calc((100vh - 150px) * var(--ar)))"
            class:xray onwheel={onWheel}>
            <img src={shotUrl} alt="screen {node.id}" bind:clientWidth={imgW} />

            {#if scale > 0}
              <div class="overlay">
                {#if xray}
                  {#each hier as h}
                    <div class="box" class:clickable={h.clickable}
                      style="left:{h.bounds[0] * scale}px; top:{h.bounds[1] * scale}px;
                             width:{(h.bounds[2] - h.bounds[0]) * scale}px;
                             height:{(h.bounds[3] - h.bounds[1]) * scale}px"
                      role="presentation"
                      onmouseenter={() => (hovered = h)}
                      onmouseleave={() => (hovered === h ? (hovered = null) : null)}></div>
                  {/each}
                  {#if hovered}
                    {@const top = hovered.bounds[3] * scale + 6}
                    {@const left = Math.min(hovered.bounds[0] * scale, imgW - 180)}
                    <div class="tip" style="left:{Math.max(0, left)}px; top:{top}px">
                      <b>{hovered.cls.split('.').pop()}</b>
                      {#if hovered.text}<div class="t">“{hovered.text}”</div>{/if}
                      {#if hovered.resourceId}<div class="i">{hovered.resourceId}</div>{/if}
                      {#if hovered.contentDesc}<div class="i">desc: {hovered.contentDesc}</div>{/if}
                    </div>
                  {/if}
                {/if}

                <!-- the screenshot's own back/up arrow → flow-back -->
                {#if upRect}
                  <button class="hotspot" title="back → {parentId}"
                    style="left:{upRect[0] * scale}px; top:{upRect[1] * scale}px;
                           width:{(upRect[2] - upRect[0]) * scale}px; height:{(upRect[3] - upRect[1]) * scale}px"
                    onclick={flowBack} aria-label="back"></button>
                {/if}

                <!-- tap edges → clickable hotspots covering the whole clickable row -->
                {#each outEdges as e}
                  {#if e.action.type === 'tap'}
                    {@const r = hotspotRect(e)}
                    {#if r}
                      <button class="hotspot" title="{actionLabel(e)} → {e.to}"
                        style="left:{r[0] * scale}px; top:{r[1] * scale}px;
                               width:{(r[2] - r[0]) * scale}px; height:{(r[3] - r[1]) * scale}px"
                        onclick={() => navigate(e.to)} aria-label="go to {e.to}"></button>
                    {:else if e.action.x != null}
                      <button class="hotspot dot" title="{actionLabel(e)} → {e.to}"
                        style="left:{e.action.x * scale - 18}px; top:{e.action.y! * scale - 18}px"
                        onclick={() => navigate(e.to)} aria-label="go to {e.to}"></button>
                    {/if}
                  {/if}
                {/each}
              </div>
            {/if}
          </div>

          <div class="scrollers">
            <button class="scroll-btn" disabled={!scrollUp}
              onclick={() => scrollUp && navigate(scrollUp)} title="scroll up" aria-label="scroll up">▲</button>
            <div class="track" class:on={!!sc} bind:this={trackEl} onpointerdown={gutterClick}
              role="presentation" title={sc ? `y ${sc.y} / ${sc.content}px` : 'no scroll data'}>
              {#if sc}
                {#each chainNodes as n}
                  <div class="tick" class:cur={n.id === currentId}
                    style="top:{(100 * (n.scroll!.y ?? 0)) / sc.content}%"></div>
                {/each}
                <div class="thumb" class:dragging
                  style="top:{(100 * sc.y) / sc.content}%; height:{Math.max(7, (100 * sc.viewport) / sc.content)}%"
                  onpointerdown={startDrag} role="slider" tabindex="0"
                  aria-valuenow={sc.y} aria-valuemax={sc.content} aria-label="scroll position"></div>
              {/if}
            </div>
            <button class="scroll-btn" disabled={!scrollDown}
              onclick={() => scrollDown && navigate(scrollDown)} title="scroll down" aria-label="scroll down">▼</button>
          </div>
        </div><!-- /phone-row -->

        {#if barEdges.length}
          <div class="edges">
            {#each barEdges as e}
              <button class="edge" onclick={() => navigate(e.to)}>
                <span class="kind">{e.action.type}</span>{actionLabel(e).replace(e.action.type, '').trim()}
                <span class="arrow">→ {e.to}</span>
              </button>
            {/each}
          </div>
        {/if}
      </section>

      <aside class="meta">
        <h2>{ds.manifest.device.oem} {ds.manifest.device.model}</h2>
        <dl>
          <dt>Android</dt><dd>{ds.manifest.device.android} (sdk {ds.manifest.device.sdk})</dd>
          <dt>Display</dt><dd>{deviceW}×{ds.manifest.device.display.h} @ {ds.manifest.device.display.density}dpi</dd>
          <dt>Screen</dt><dd>{node.id} of {ds.manifest.nodes.length}</dd>
          <dt>Activity</dt><dd class="mono">{node.activity ?? '—'}</dd>
          <dt>Hierarchy</dt><dd>{node.hierarchy
            ? `${hier.length} nodes · ${node.hierarchySource === 'viewtree' ? 'view-tree (no text)' : 'uiautomator'}`
            : 'unavailable'}</dd>
        </dl>
        {#if xray && hovered}
          <div class="inspect">
            <div class="mono">{hovered.cls.split('.').pop()}</div>
            {#if hovered.resourceId}<div class="mono small">{hovered.resourceId}</div>{/if}
            {#if hovered.text}<div>“{hovered.text}”</div>{/if}
          </div>
        {/if}
      </aside>
    </div>
  {/if}
</main>

<style>
  :global(body) { margin: 0; background: #0e0f12; color: #e7e9ee; font: 14px/1.5 system-ui, sans-serif; }
  main { min-height: 100vh; padding: 16px 20px; }
  header { display: flex; align-items: center; gap: 16px; }
  h1 { font-size: 16px; margin: 0; color: #9aa4b2; letter-spacing: .04em; }
  .pick { background: #2a6ef0; color: #fff; padding: 6px 12px; border-radius: 8px; cursor: pointer; }
  .toggle { color: #9aa4b2; margin-left: auto; user-select: none; }
  .error { color: #ff7a7a; }
  .dropzone { margin-top: 18vh; text-align: center; color: #6b7280; border: 1.5px dashed #2a2e37; border-radius: 14px; padding: 60px; }
  code { background: #1a1d23; padding: 1px 6px; border-radius: 5px; }
  .layout { display: grid; grid-template-columns: 210px 1fr 300px; gap: 22px; margin-top: 18px; align-items: start; }

  /* left flow tree */
  .tree { background: #121419; border: 1px solid #20242c; border-radius: 10px; padding: 6px; max-height: 86vh; overflow: auto; }
  .tree-h { color: #6b7280; font-size: 11px; text-transform: uppercase; letter-spacing: .08em; padding: 4px 6px 8px; }
  .trow { display: flex; gap: 7px; align-items: center; width: 100%; text-align: left;
    background: none; border: 0; color: #cbd2dc; padding: 5px 8px; border-radius: 6px; cursor: pointer; font-size: 13px; }
  .trow:hover { background: #1a1d23; }
  .trow.active { background: #2a6ef0; color: #fff; }
  .tlabel { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .vcount { flex: none; font-size: 10px; background: #2a2e37; color: #9aa4b2; border-radius: 8px; padding: 0 6px; }
  .trow.active .vcount { background: rgba(255,255,255,.22); color: #fff; }
  .dbg { margin-left: auto; flex: none; font: 10px ui-monospace, monospace; color: #4a5160; }
  .trow.active .dbg { color: #b9c6ee; }

  .stage { display: flex; flex-direction: column; align-items: center; gap: 14px; }
  .phone-row { display: flex; align-items: center; gap: 14px; }
  .leftctrl { display: flex; align-items: flex-start; align-self: stretch; }
  .scrollers { display: flex; flex-direction: column; gap: 10px; align-self: stretch; align-items: center; }
  .scroll-btn { width: 40px; height: 40px; border-radius: 50%; font-size: 16px; flex: none;
    background: #1a1d23; color: #cbd2dc; border: 1px solid #2a2e37; cursor: pointer; line-height: 1; }
  .scroll-btn:hover:not(:disabled) { background: #2a6ef0; color: #fff; border-color: #2a6ef0; }
  .scroll-btn:disabled { opacity: .25; cursor: default; }
  .track { flex: 1; width: 14px; background: #14171d; border-radius: 7px; position: relative; border: 1px solid #20242c; }
  .track.on { cursor: pointer; }
  .thumb { position: absolute; left: 1px; right: 1px; background: #2a6ef0; border-radius: 6px;
    min-height: 16px; opacity: .85; cursor: grab; touch-action: none; transition: top .1s ease, height .1s ease; }
  .thumb:hover { opacity: 1; }
  .thumb.dragging { cursor: grabbing; transition: none; opacity: 1; }
  .tick { position: absolute; left: 50%; transform: translateX(-50%); width: 4px; height: 4px; border-radius: 50%; background: #4a5160; }
  .tick.cur { background: #fff; }

  .phone { position: relative; border-radius: 22px; overflow: hidden; box-shadow: 0 12px 40px rgba(0,0,0,.5); line-height: 0; }
  .phone img { width: 100%; display: block; }
  .overlay { position: absolute; inset: 0; font-size: 0; }
  .box { position: absolute; outline: 1px solid rgba(120,180,255,.18); }
  .box.clickable { outline-color: rgba(120,180,255,.4); }
  .box:hover { outline: 1.5px solid #2a6ef0; background: rgba(42,110,240,.14); z-index: 2; }
  .tip { position: absolute; z-index: 9; background: #0b1830; border: 1px solid #2a6ef0; border-radius: 7px;
    padding: 6px 8px; max-width: 200px; line-height: 1.35; pointer-events: none; }
  .tip b { font-size: 12px; } .tip .t { font-size: 12px; color: #e7e9ee; }
  .tip .i { font: 11px ui-monospace, monospace; color: #8fb0ee; word-break: break-all; }
  /* invisible until hovered — only lights up when the mouse is over it */
  .hotspot { position: absolute; border: 1.5px solid transparent; background: transparent;
    border-radius: 8px; cursor: pointer; padding: 0; z-index: 3;
    transition: background .12s, border-color .12s, box-shadow .12s; }
  .hotspot:hover { border-color: #ffcc33; background: rgba(255,204,51,.20);
    box-shadow: 0 0 14px rgba(255,204,51,.35); }
  .hotspot.dot { width: 36px; height: 36px; border-radius: 50%; }
  .edges { display: flex; flex-wrap: wrap; gap: 8px; justify-content: center; max-width: 440px; }
  .navbtn { background: #1a1d23; color: #cbd2dc; border: 1px solid #2a2e37; border-radius: 8px; padding: 6px 12px; cursor: pointer; font-size: 13px; }
  .navbtn:hover:not(:disabled) { border-color: #2a6ef0; color: #fff; }
  .navbtn:disabled { opacity: .35; cursor: default; }
  .edge { background: #161a21; color: #cbd2dc; border: 1px solid #2a2e37; border-radius: 8px; padding: 6px 10px; cursor: pointer; font-size: 13px; }
  .edge:hover { border-color: #2a6ef0; }
  .edge .kind { color: #ffcc33; font-weight: 600; margin-right: 5px; }
  .edge .arrow { color: #6b7280; margin-left: 5px; }
  .meta h2 { font-size: 15px; margin: 0 0 10px; }
  dl { display: grid; grid-template-columns: auto 1fr; gap: 4px 12px; margin: 0; }
  dt { color: #6b7280; } dd { margin: 0; word-break: break-word; }
  .mono { font-family: ui-monospace, monospace; font-size: 12px; }
  .small { font-size: 11px; color: #9aa4b2; }
  .inspect { margin-top: 14px; padding: 10px; background: #14171d; border-radius: 8px; }
</style>
