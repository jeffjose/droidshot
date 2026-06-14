<script lang="ts">
  import { openLongshot, fileUrl, type Longshot } from '$lib/longshot';

  let ls = $state<Longshot | null>(null);
  let error = $state<string | null>(null);
  let mode = $state<'compare' | 'frames' | 'stitched'>('compare');
  let sel = $state(0); // selected frame index
  let showSeams = $state(true);
  let zoom = $state(1); // 1 = fit the window height; >1 magnifies (then it scrolls)

  function zoomBy(f: number) {
    zoom = Math.min(8, Math.max(1, +(zoom * f).toFixed(3)));
  }
  // plain wheel = scroll/pan; Ctrl/⌘+wheel = zoom (like a map / image editor)
  function onZoomWheel(ev: WheelEvent) {
    if (!(ev.ctrlKey || ev.metaKey)) return;
    ev.preventDefault();
    zoomBy(ev.deltaY < 0 ? 1.15 : 1 / 1.15);
  }

  let frameUrls: string[] = $state([]);
  let stitchedUrl = $state('');
  let frameW = $state(0); // rendered width of the individual frame (for scale)
  let stitchW = $state(0); // rendered width of the stitched image (for scale)
  let stitchedScrollEl = $state<HTMLDivElement>();

  const man = $derived(ls?.manifest ?? null);
  const devW = $derived(man?.device.w ?? 1080);
  const devH = $derived(man?.device.h ?? 2400);
  const frames = $derived(man?.frames ?? []);
  const seamByFrame = $derived.by(() => {
    const m = new Map<number, { y: number; h: number }>();
    for (const s of man?.seams ?? []) m.set(s.frame, { y: s.y, h: s.h });
    return m;
  });

  // scale factors: rendered px per device px
  const fscale = $derived(frameW > 0 ? frameW / devW : 0);
  const sscale = $derived(stitchW > 0 ? stitchW / devW : 0);

  // The slice of the SELECTED frame that became new content in the stitch.
  // frame 0 contributes its whole band; later frames contribute the bottom
  // `delta` rows (everything revealed below what the prior frame already showed).
  const srcRect = $derived.by(() => {
    if (!man || !frames[sel]) return null;
    const top = man.topChrome;
    const bot = man.bottomChrome;
    if (sel === 0) return { top, h: devH - top - bot };
    const d = frames[sel].delta;
    return { top: devH - bot - d, h: d };
  });
  const dstRect = $derived(seamByFrame.get(sel) ?? null);

  // auto-scroll the stitched panel so the selected frame's slice is in view
  $effect(() => {
    const r = dstRect;
    if (r && stitchedScrollEl && sscale > 0) {
      stitchedScrollEl.scrollTo({ top: Math.max(0, r.y * sscale - 48), behavior: 'smooth' });
    }
  });

  function revoke() {
    for (const u of frameUrls) URL.revokeObjectURL(u);
    if (stitchedUrl) URL.revokeObjectURL(stitchedUrl);
    frameUrls = [];
    stitchedUrl = '';
  }
  async function load(file: File) {
    error = null;
    try {
      revoke();
      const next = openLongshot(new Uint8Array(await file.arrayBuffer()));
      ls = next;
      frameUrls = next.manifest.frames.map((f) => fileUrl(next, f.file));
      stitchedUrl = fileUrl(next, next.manifest.stitched);
      sel = 0;
      zoom = 1;
    } catch (e) {
      ls = null;
      error = e instanceof Error ? e.message : String(e);
    }
  }
  const onPick = (ev: Event) => {
    const f = (ev.target as HTMLInputElement).files?.[0];
    if (f) load(f);
  };
  const onDrop = (ev: DragEvent) => {
    ev.preventDefault();
    const f = ev.dataTransfer?.files?.[0];
    if (f) load(f);
  };
</script>

<svelte:head><title>longshot viewer</title></svelte:head>

<main ondragover={(e) => e.preventDefault()} ondrop={onDrop} role="application" aria-label="longshot viewer">
  <header>
    <h1>longshot</h1>
    <a class="back" href="/">‹ droidshot</a>
    <label class="pick">
      open .longshot
      <input type="file" onchange={onPick} hidden />
    </label>
    {#if ls}
      <div class="modes">
        <button class:on={mode === 'compare'} onclick={() => (mode = 'compare')}>compare</button>
        <button class:on={mode === 'frames'} onclick={() => (mode = 'frames')}>frames</button>
        <button class:on={mode === 'stitched'} onclick={() => (mode = 'stitched')}>stitched</button>
      </div>
      {#if mode !== 'frames'}
        <div class="zoom">
          <button onclick={() => zoomBy(1 / 1.25)} title="zoom out" aria-label="zoom out">−</button>
          <button class="z" onclick={() => (zoom = 1)} title="fit to window">{Math.round(zoom * 100)}%</button>
          <button onclick={() => zoomBy(1.25)} title="zoom in" aria-label="zoom in">+</button>
        </div>
      {/if}
      <label class="toggle"><input type="checkbox" bind:checked={showSeams} /> seams</label>
    {/if}
  </header>

  {#if error}<p class="error">⚠ {error}</p>{/if}

  {#if !ls || !man}
    <div class="dropzone">
      <p>Drag a <code>.longshot</code> here, or use “open”.</p>
      <p class="hint">Produced by <code>./longshot.py capture</code> or <code>stitch</code>.</p>
    </div>
  {:else}
    <div class="summary">
      {devW}×{devH} viewport · {frames.length} frames · stitched {man.stitchedSize[0]}×{man.stitchedSize[1]}
      ({(man.stitchedSize[1] / devH).toFixed(1)}× tall) · top chrome {man.topChrome}px · bottom chrome {man.bottomChrome}px
    </div>

    {#if mode === 'frames'}
      <div class="grid">
        {#each frames as f, i}
          <figure class:sel={i === sel} onclick={() => (sel = i)} role="presentation">
            <img src={frameUrls[i]} alt="frame {i}" />
            <figcaption>frame {i}<span class="d">+{f.delta}px</span></figcaption>
          </figure>
        {/each}
      </div>

    {:else if mode === 'stitched'}
      <div class="stitched-only">
        <div class="stitch-scroll" bind:this={stitchedScrollEl} onwheel={onZoomWheel}>
          <div class="stitch-wrap">
            <img src={stitchedUrl} alt="stitched" bind:clientWidth={stitchW}
              style="height: calc((100vh - 162px) * {zoom})" />
            {#if showSeams && sscale > 0}
              {#each man.seams as s}
                <div class="seamline" style="top:{s.y * sscale}px" title="frame {s.frame} joins here"></div>
                <div class="seamtag" style="top:{s.y * sscale}px">f{s.frame}</div>
              {/each}
            {/if}
          </div>
        </div>
      </div>

    {:else}
      <!-- compare: filmstrip · source frame (slice taken) · stitched (slice landed) -->
      <div class="compare">
        <aside class="strip">
          {#each frames as f, i}
            <button class="thumb" class:sel={i === sel} onclick={() => (sel = i)}>
              <img src={frameUrls[i]} alt="frame {i}" />
              <span class="lbl">f{i}<br /><em>+{f.delta}</em></span>
            </button>
          {/each}
        </aside>

        <figure class="src">
          <div class="cap">frame {sel} — slice taken {srcRect ? `(${srcRect.h}px)` : ''}</div>
          <div class="imgbox">
            <img src={frameUrls[sel]} alt="frame {sel}" bind:clientWidth={frameW} />
            {#if fscale > 0 && man.topChrome}
              <div class="chrome" style="height:{man.topChrome * fscale}px" title="top chrome (kept once)"></div>
            {/if}
            {#if fscale > 0 && man.bottomChrome}
              <div class="chrome bot" style="height:{man.bottomChrome * fscale}px" title="bottom chrome"></div>
            {/if}
            {#if srcRect && fscale > 0}
              <div class="slice" style="top:{srcRect.top * fscale}px; height:{srcRect.h * fscale}px"></div>
            {/if}
          </div>
        </figure>

        <figure class="dst">
          <div class="cap">where it landed in the stitch</div>
          <div class="stitch-scroll tall" bind:this={stitchedScrollEl} onwheel={onZoomWheel}>
            <div class="stitch-wrap">
              <img src={stitchedUrl} alt="stitched" bind:clientWidth={stitchW}
                style="height: calc((100vh - 182px) * {zoom})" />
              {#if showSeams && sscale > 0}
                {#each man.seams as s}
                  <div class="seamline" style="top:{s.y * sscale}px"></div>
                {/each}
              {/if}
              {#if dstRect && sscale > 0}
                <div class="slice" style="top:{dstRect.y * sscale}px; height:{dstRect.h * sscale}px"></div>
              {/if}
            </div>
          </div>
        </figure>
      </div>
    {/if}
  {/if}
</main>

<style>
  :global(body) { margin: 0; background: #0e0f12; color: #e7e9ee; font: 14px/1.5 system-ui, sans-serif; }
  main { min-height: 100vh; padding: 16px 20px; }
  header { display: flex; align-items: center; gap: 14px; }
  h1 { font-size: 16px; margin: 0; color: #9aa4b2; letter-spacing: .04em; }
  .back { color: #6b7280; text-decoration: none; font-size: 13px; }
  .back:hover { color: #cbd2dc; }
  .pick { background: #2a6ef0; color: #fff; padding: 6px 12px; border-radius: 8px; cursor: pointer; }
  .modes { display: flex; gap: 2px; background: #14171d; border: 1px solid #20242c; border-radius: 8px; padding: 2px; }
  .modes button { background: none; border: 0; color: #9aa4b2; padding: 5px 12px; border-radius: 6px; cursor: pointer; font-size: 13px; }
  .modes button.on { background: #2a6ef0; color: #fff; }
  .zoom { display: flex; align-items: center; gap: 2px; background: #14171d; border: 1px solid #20242c; border-radius: 8px; padding: 2px; }
  .zoom button { background: none; border: 0; color: #cbd2dc; padding: 4px 9px; border-radius: 6px; cursor: pointer; font-size: 14px; line-height: 1; }
  .zoom button:hover { background: #1a1d23; }
  .zoom .z { min-width: 46px; font-variant-numeric: tabular-nums; font-size: 12px; color: #9aa4b2; }
  .toggle { color: #9aa4b2; user-select: none; margin-left: auto; }
  .error { color: #ff7a7a; }
  .dropzone { margin-top: 16vh; text-align: center; color: #6b7280; border: 1.5px dashed #2a2e37; border-radius: 14px; padding: 60px; }
  .dropzone .hint { font-size: 12px; margin-top: 8px; }
  code { background: #1a1d23; padding: 1px 6px; border-radius: 5px; }
  .summary { color: #9aa4b2; font-size: 12.5px; margin: 14px 0 10px; font-family: ui-monospace, monospace; }

  /* frames grid */
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 14px; }
  figure { margin: 0; }
  .grid figure { background: #121419; border: 1px solid #20242c; border-radius: 10px; padding: 8px; cursor: pointer; }
  .grid figure.sel { border-color: #2a6ef0; }
  .grid img { width: 100%; border-radius: 5px; display: block; }
  figcaption { font-size: 12px; color: #9aa4b2; margin-top: 6px; display: flex; justify-content: space-between; }
  .d { color: #ffcc33; font-family: ui-monospace, monospace; }

  /* compare */
  .compare { display: grid; grid-template-columns: 92px minmax(220px, 360px) 1fr; gap: 18px; align-items: start; }
  .strip { display: flex; flex-direction: column; gap: 8px; max-height: 84vh; overflow: auto; }
  .thumb { position: relative; background: #121419; border: 1px solid #20242c; border-radius: 8px; padding: 0; cursor: pointer; overflow: hidden; }
  .thumb.sel { border-color: #2a6ef0; box-shadow: 0 0 0 1px #2a6ef0; }
  .thumb img { width: 100%; display: block; opacity: .85; }
  .thumb.sel img { opacity: 1; }
  .thumb .lbl { position: absolute; top: 3px; left: 4px; font: 10px ui-monospace, monospace; color: #cbd2dc;
    background: rgba(0,0,0,.55); padding: 1px 4px; border-radius: 4px; line-height: 1.2; }
  .thumb .lbl em { color: #ffcc33; font-style: normal; }

  .cap { font-size: 12px; color: #9aa4b2; margin-bottom: 6px; }
  .imgbox { position: relative; border-radius: 14px; overflow: hidden; line-height: 0; box-shadow: 0 10px 30px rgba(0,0,0,.5); }
  .imgbox img { width: 100%; display: block; }
  .chrome { position: absolute; left: 0; right: 0; top: 0; background: rgba(255,80,80,.16);
    border-bottom: 1px dashed rgba(255,120,120,.7); }
  .chrome.bot { top: auto; bottom: 0; border-bottom: 0; border-top: 1px dashed rgba(255,120,120,.7); }
  .slice { position: absolute; left: 0; right: 0; background: rgba(42,200,120,.20);
    border-top: 2px solid #2ac878; border-bottom: 2px solid #2ac878; }

  /* fit-to-window by default: the image is sized by HEIGHT (inline style), so at
     100% the whole long shot is visible with no scrollbar; zooming past 100%
     overflows and the container scrolls. */
  .stitch-scroll { max-height: calc(100vh - 150px); overflow: auto; border-radius: 14px;
    background: #000; border: 1px solid #20242c; text-align: center; }
  .stitch-scroll.tall { max-height: calc(100vh - 170px); }
  .stitched-only .stitch-scroll { margin: 0 auto; }
  .stitch-wrap { position: relative; display: inline-block; vertical-align: top; line-height: 0; }
  .stitch-wrap img { display: block; width: auto; max-width: none; }
  .seamline { position: absolute; left: 0; right: 0; height: 0; border-top: 1px solid rgba(255,204,51,.8); pointer-events: none; }
  .seamtag { position: absolute; left: 4px; transform: translateY(-100%); font: 10px ui-monospace, monospace;
    color: #ffcc33; background: rgba(0,0,0,.6); padding: 0 4px; border-radius: 3px; }
</style>
