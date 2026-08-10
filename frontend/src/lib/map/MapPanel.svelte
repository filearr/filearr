<script lang="ts">
  // R8-UI — the photo GPS map. A LENS on the current search, not a second search
  // box: it plots the geo-bearing hits of whatever query the page is already
  // running, and a box drawn on it becomes `geo_top_lat`/`geo_right_lng`/
  // `geo_bottom_lat`/`geo_left_lng` on that same query (SearchPage owns the
  // state; this component only reports the box it drew).
  //
  // WHY NO TILES BY DEFAULT: Filearr is built to run with no outbound internet —
  // the manual is baked into the image, Swagger is vendored, nothing phones home.
  // A tile layer would quietly undo that, and worse: the tile server would learn
  // the viewer's IP and, tile by tile, which places appear in their private photo
  // library. So the basemap is BUNDLED (Natural Earth 1:110m, public domain, ~46
  // KB encoded — see basemapData.ts) and tiles are an explicit, clearly-labelled
  // opt-in. See mapSettings.svelte.ts.
  //
  // All the arithmetic lives in ./geo (pure, unit-tested); this file is the DOM.
  import { onMount } from "svelte";
  import { loadBasemap, type Basemap, type Shape } from "./basemap";
  import {
    mapSettings,
    saveMapSettings,
    tileHost,
    DEFAULT_TILE_URL,
  } from "./mapSettings.svelte";
  import {
    CLUSTER_CELL_PX,
    MAP_POINT_CAP,
    MAX_LAT,
    TILE_SIZE,
    boundsOf,
    boxError,
    clusterPoints,
    formatBox,
    makeViewport,
    normalizeBox,
    project,
    unproject,
    worldSize,
    zoomForBox,
    type Cluster,
    type GeoBox,
    type LatLng,
    type MapPoint,
    type ScreenPoint,
  } from "./geo";

  interface Props {
    /** Geo-bearing hits for the CURRENT query (already capped — see geoTotal). */
    points: MapPoint[];
    /** How many geo-bearing hits the query actually has (the M in "N of M"). */
    geoTotal: number;
    /** How many hits the query has in total, geo-bearing or not. */
    queryTotal: number;
    loading: boolean;
    /** The active area filter, or null. Owned by SearchPage. */
    box: GeoBox | null;
    /** True when NO library has `expose_gps` on — the privacy gate, not a fault. */
    gpsGated: boolean;
    /** Base URL of the bundled manual, for the gate explanation's deep link. */
    docsUrl: string;
    onBoxChange: (box: GeoBox | null) => void;
    onOpenItem: (id: string) => void;
  }

  let {
    points,
    geoTotal,
    queryTotal,
    loading,
    box,
    gpsGated,
    docsUrl,
    onBoxChange,
    onOpenItem,
  }: Props = $props();

  // Zoom is an INTEGER. Fractional zoom would mean scaling tiles, and the whole
  // value of the optional tile layer is that it lines up exactly with the vector
  // outline; at 1:110m source detail nobody gains anything from half-steps.
  const MIN_ZOOM = 0;
  const MAX_ZOOM = 18;

  let width = $state(0);
  let height = $state(0);
  let zoom = $state(1);
  let center = $state<LatLng>({ lat: 20, lng: 0 });
  // Once the user pans/zooms we stop auto-fitting to each new result set —
  // re-framing the map under someone who just navigated somewhere is the single
  // most annoying thing a map can do.
  let userMoved = $state(false);
  let basemap = $state<Basemap | null>(null);
  let svgEl = $state<SVGSVGElement | undefined>();
  let openCluster = $state<Cluster | null>(null);
  let offline = $state(false);

  // Drag state: "select" draws a filter box (the default — the map is an input),
  // "pan" moves the view. Shift+drag pans without leaving select mode.
  let dragMode = $state<"select" | "pan">("select");
  let dragging = $state<
    | { kind: "select"; from: { x: number; y: number }; to: { x: number; y: number } }
    | { kind: "pan"; from: { x: number; y: number }; origin: LatLng }
    | null
  >(null);

  const vp = $derived(
    makeViewport({ center, zoom, width: Math.max(1, width), height: Math.max(1, height) }),
  );

  /** Keep the centre over the world. The bundled outline is drawn ONCE (plus the
   *  ±360 edge copies the generator emits for shapes that straddle the 180th
   *  meridian), so letting the view drift past ±180 would just scroll onto
   *  nothing. Latitude stops at the Mercator clip for the same reason. */
  function setCenter(next: LatLng) {
    center = {
      lat: Math.min(MAX_LAT, Math.max(-MAX_LAT, next.lat)),
      lng: Math.min(180, Math.max(-180, next.lng)),
    };
  }

  onMount(() => {
    loadBasemap().then((b) => (basemap = b));
    // "Tiles on but no network" is a state the map should name, not a silent
    // grid of broken images.
    offline = typeof navigator !== "undefined" && navigator.onLine === false;
    const on = () => (offline = false);
    const off = () => (offline = true);
    window.addEventListener("online", on);
    window.addEventListener("offline", off);
    return () => {
      window.removeEventListener("online", on);
      window.removeEventListener("offline", off);
    };
  });

  // Auto-frame the results until the user takes the wheel. Reads `points`,
  // `width` and `height`, so a first paint (width 0 -> measured) re-frames once.
  $effect(() => {
    if (userMoved || !points.length || width < 50 || height < 50) return;
    fitTo(box ?? boundsOf(points));
  });

  function fitTo(target: GeoBox | null) {
    if (!target) return;
    setCenter({
      lat: (target.top + target.bottom) / 2,
      lng: (target.left + target.right) / 2,
    });
    zoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, Math.floor(zoomForBox(target, width, height))));
  }

  function setZoom(next: number, anchor?: { x: number; y: number }) {
    const z = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, Math.round(next)));
    if (z === zoom) return;
    openCluster = null;
    // Zoom about the cursor: whatever place is under `anchor` must still be
    // under it afterwards. Solve for the new origin directly — the point's world
    // pixel at the new zoom, minus where it has to land on screen — then read the
    // centre back out of it. (Adjusting the centre by a delta instead
    // accumulates error over a wheel-spin's worth of steps.)
    if (anchor && width && height) {
      const held = vp.fromScreen(anchor.x, anchor.y);
      const p = project(held.lat, held.lng, z);
      setCenter(unproject(p.x - anchor.x + width / 2, p.y - anchor.y + height / 2, z));
    }
    zoom = z;
    userMoved = true;
  }

  // ---- pointer interaction -------------------------------------------------
  function localXY(e: PointerEvent | MouseEvent): { x: number; y: number } {
    const r = svgEl?.getBoundingClientRect();
    return { x: e.clientX - (r?.left ?? 0), y: e.clientY - (r?.top ?? 0) };
  }

  function onPointerDown(e: PointerEvent) {
    if (e.button !== 0) return;
    openCluster = null;
    const at = localXY(e);
    svgEl?.setPointerCapture(e.pointerId);
    if (dragMode === "pan" || e.shiftKey) {
      dragging = { kind: "pan", from: at, origin: center };
    } else {
      dragging = { kind: "select", from: at, to: at };
    }
  }

  function onPointerMove(e: PointerEvent) {
    if (!dragging) return;
    const at = localXY(e);
    if (dragging.kind === "select") {
      dragging = { ...dragging, to: at };
    } else {
      // Pan by re-centring on the geographic point that was under the cursor
      // when the drag started — no accumulated rounding drift.
      const base = makeViewport({ center: dragging.origin, zoom, width, height });
      setCenter(
        base.fromScreen(
          width / 2 - (at.x - dragging.from.x),
          height / 2 - (at.y - dragging.from.y),
        ),
      );
      userMoved = true;
    }
  }

  function onPointerUp(e: PointerEvent) {
    const d = dragging;
    dragging = null;
    if (svgEl?.hasPointerCapture(e.pointerId)) svgEl.releasePointerCapture(e.pointerId);
    if (!d || d.kind !== "select") return;
    const dx = Math.abs(d.to.x - d.from.x);
    const dy = Math.abs(d.to.y - d.from.y);
    // A click, not a drag: clear the area filter rather than sending a
    // zero-area box the API would (rightly) refuse.
    if (dx < 4 || dy < 4) {
      if (box) onBoxChange(null);
      return;
    }
    onBoxChange(normalizeBox(vp.fromScreen(d.from.x, d.from.y), vp.fromScreen(d.to.x, d.to.y)));
  }

  function onWheel(e: WheelEvent) {
    e.preventDefault();
    setZoom(zoom + (e.deltaY < 0 ? 1 : -1), localXY(e));
  }

  // Svelte 5 registers `onwheel={}` as a PASSIVE listener, so preventDefault()
  // inside one is ignored and the page scrolls out from under the map while it
  // zooms. Attaching the listener ourselves is the documented way to get a
  // cancelable one.
  $effect(() => {
    const el = svgEl;
    if (!el) return;
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  });

  // Keyboard equivalents for every pointer gesture except drawing a box, which
  // has its own numeric form below (also the paste-coordinates path).
  function onKeydown(e: KeyboardEvent) {
    const step = 60; // px
    const pan = (dxPx: number, dyPx: number) => {
      setCenter(vp.fromScreen(width / 2 + dxPx, height / 2 + dyPx));
      userMoved = true;
      openCluster = null; // its screen position no longer means anything
    };
    if (e.key === "ArrowLeft") pan(-step, 0);
    else if (e.key === "ArrowRight") pan(step, 0);
    else if (e.key === "ArrowUp") pan(0, -step);
    else if (e.key === "ArrowDown") pan(0, step);
    else if (e.key === "+" || e.key === "=") setZoom(zoom + 1);
    else if (e.key === "-" || e.key === "_") setZoom(zoom - 1);
    else if (e.key === "Escape" && openCluster) openCluster = null;
    else return;
    e.preventDefault();
    e.stopPropagation();
  }

  // ---- rendering -----------------------------------------------------------
  /** Visible geographic window, used to cull basemap shapes and off-screen
   *  points before any per-vertex work happens. */
  const visible = $derived.by(() => {
    const nw = vp.fromScreen(0, 0);
    const se = vp.fromScreen(width, height);
    return { top: nw.lat, left: nw.lng, bottom: se.lat, right: se.lng };
  });

  /** Build one SVG path for a set of shapes.
   *
   *  Two cheap bounds keep this linear in what is actually on screen: whole
   *  shapes outside the window are skipped by their precomputed bbox, and within
   *  a shape a vertex closer than half a pixel to the previous one is dropped —
   *  at world zoom that discards most of the 11 600 source vertices, which is why
   *  panning stays smooth without a tiling/quadtree scheme. */
  function pathFor(shapes: Shape[], close: boolean): string {
    let d = "";
    for (const s of shapes) {
      if (
        s.maxLng < visible.left ||
        s.minLng > visible.right ||
        s.maxLat < visible.bottom ||
        s.minLat > visible.top
      )
        continue;
      let started = false;
      let px = NaN;
      let py = NaN;
      for (let i = 0; i < s.pts.length; i += 2) {
        const p = vp.toScreen(s.pts[i + 1], s.pts[i]);
        const x = Math.round(p.x * 2) / 2;
        const y = Math.round(p.y * 2) / 2;
        if (started && x === px && y === py) continue;
        d += `${started ? "L" : "M"}${x} ${y}`;
        started = true;
        px = x;
        py = y;
      }
      if (started && close) d += "Z";
    }
    return d;
  }

  const landPath = $derived(basemap && width ? pathFor(basemap.land, true) : "");
  const borderPath = $derived(basemap && width ? pathFor(basemap.borders, false) : "");

  /** Graticule step in degrees: the FINEST step from the ladder that still keeps
   *  lines ~90 px apart, so the grid never turns into a hatch and its labels
   *  never collide. Falls back to the coarsest rung when even that is too dense
   *  (zoom 0, where the whole world is 256 px wide). */
  const GRID_STEPS = [0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 45];
  const gridStep = $derived.by(() => {
    const pxPerDeg = worldSize(zoom) / 360;
    return GRID_STEPS.find((s) => s * pxPerDeg >= 90) ?? GRID_STEPS[GRID_STEPS.length - 1];
  });

  const graticule = $derived.by(() => {
    if (!width || !height) return { lines: [] as { d: string; label: string; x: number; y: number }[] };
    const step = gridStep;
    const lines: { d: string; label: string; x: number; y: number }[] = [];
    const lngStart = Math.ceil(Math.max(-180, visible.left) / step) * step;
    for (let lng = lngStart; lng <= Math.min(180, visible.right); lng += step) {
      const x = Math.round(vp.toScreen(0, lng).x);
      lines.push({
        d: `M${x} 0L${x} ${height}`,
        label: `${Number(lng.toFixed(4))}°`,
        x: x + 3,
        y: 12,
      });
    }
    const latStart = Math.ceil(Math.max(-85, visible.bottom) / step) * step;
    for (let lat = latStart; lat <= Math.min(85, visible.top); lat += step) {
      const y = Math.round(vp.toScreen(lat, 0).y);
      lines.push({
        d: `M0 ${y}L${width} ${y}`,
        label: `${Number(lat.toFixed(4))}°`,
        x: 3,
        y: y - 3,
      });
    }
    return { lines };
  });

  /** Screen-space clusters for the visible points. The grid bounds the number of
   *  drawn marks to (width/cell)·(height/cell) regardless of input size. */
  const clusters = $derived.by<Cluster[]>(() => {
    if (!width || !height) return [];
    const on: ScreenPoint[] = [];
    for (const p of points) {
      const s = vp.toScreen(p.lat, p.lng);
      if (s.x < -40 || s.y < -40 || s.x > width + 40 || s.y > height + 40) continue;
      on.push({ ...p, x: s.x, y: s.y });
    }
    return clusterPoints(on, CLUSTER_CELL_PX);
  });

  const hiddenByViewport = $derived(
    points.length - clusters.reduce((n, c) => n + c.count, 0),
  );

  const radiusOf = (count: number): number =>
    count === 1 ? 4.5 : Math.min(20, 8 + Math.log2(count) * 3);

  function activate(c: Cluster) {
    if (c.count === 1) {
      onOpenItem(c.points[0].id);
      return;
    }
    openCluster = openCluster?.key === c.key ? null : c;
  }

  function zoomIntoCluster(c: Cluster) {
    openCluster = null;
    const b = boundsOf(c.points, 0.002);
    if (!b) return;
    userMoved = true;
    setCenter({ lat: (b.top + b.bottom) / 2, lng: (b.left + b.right) / 2 });
    // At least one step in, and never past the ceiling.
    zoom = Math.min(MAX_ZOOM, Math.max(zoom + 1, Math.floor(zoomForBox(b, width, height))));
  }

  // ---- selection rectangle + numeric (keyboard) box form -------------------
  const dragRect = $derived.by(() => {
    const d = dragging;
    if (!d || d.kind !== "select") return null;
    const { from, to } = d;
    return {
      x: Math.min(from.x, to.x),
      y: Math.min(from.y, to.y),
      w: Math.abs(to.x - from.x),
      h: Math.abs(to.y - from.y),
    };
  });

  const boxRect = $derived.by(() => {
    if (!box || !width) return null;
    const a = vp.toScreen(box.top, box.left);
    const b = vp.toScreen(box.bottom, box.right);
    return { x: a.x, y: a.y, w: b.x - a.x, h: b.y - a.y };
  });

  // The numeric form is the non-drag path to a box — and how coordinates get
  // pasted in. Drafts are strings so a half-typed "-" or "" is representable
  // without becoming NaN mid-keystroke.
  let draft = $state({ top: "", right: "", bottom: "", left: "" });
  let draftError = $state("");

  function fillDraftFromView() {
    const src = box ?? visible;
    // Clamped: zoomed out far enough, the visible window runs past ±180 / ±90,
    // and pre-filling the form with a coordinate it would then reject is a
    // pointless error to hand someone.
    const f = (v: number, limit: number) =>
      String(Number(Math.min(limit, Math.max(-limit, v)).toFixed(5)));
    draft = {
      top: f(src.top, 90),
      right: f(src.right, 180),
      bottom: f(src.bottom, 90),
      left: f(src.left, 180),
    };
    draftError = "";
  }

  // Keep the numeric fields showing whatever area is actually in force, so the
  // keyboard path starts from something real instead of four blanks. Primed once
  // from the first measured viewport; after that only a box change refills it
  // (retyping under someone mid-edit would be worse than a stale value).
  let draftPrimed = false;
  $effect(() => {
    if (box) {
      fillDraftFromView();
      return;
    }
    if (!draftPrimed && width > 50) {
      draftPrimed = true;
      fillDraftFromView();
    }
  });

  function applyDraft() {
    const candidate: GeoBox = {
      top: Number(draft.top),
      right: Number(draft.right),
      bottom: Number(draft.bottom),
      left: Number(draft.left),
    };
    const err = boxError(candidate);
    draftError = err ?? "";
    // Refusing here is the point: these are exactly the shapes the API answers
    // 422 to (inverted edges, a box crossing the antimeridian, out-of-range
    // coordinates), and a search should not have to bounce off the server to
    // learn that the user typed south into the north field.
    if (err) return;
    onBoxChange(candidate);
  }

  // ---- optional tiles ------------------------------------------------------
  const tiles = $derived.by(() => {
    if (!mapSettings.tiles || !width || !height || offline) return [];
    const n = 2 ** zoom;
    const out: { key: string; url: string; x: number; y: number }[] = [];
    const x0 = Math.floor(vp.originX / TILE_SIZE);
    const x1 = Math.floor((vp.originX + width) / TILE_SIZE);
    const y0 = Math.floor(vp.originY / TILE_SIZE);
    const y1 = Math.floor((vp.originY + height) / TILE_SIZE);
    for (let ty = y0; ty <= y1; ty++) {
      if (ty < 0 || ty >= n) continue; // above the north pole / below the south
      for (let tx = x0; tx <= x1; tx++) {
        const wrapped = ((tx % n) + n) % n; // the world repeats east-west
        out.push({
          key: `${zoom}/${tx}/${ty}`,
          url: mapSettings.tileUrl
            .replace("{z}", String(zoom))
            .replace("{x}", String(wrapped))
            .replace("{y}", String(ty)),
          x: tx * TILE_SIZE - vp.originX,
          y: ty * TILE_SIZE - vp.originY,
        });
      }
    }
    return out;
  });

  function toggleTiles() {
    mapSettings.tiles = !mapSettings.tiles;
    saveMapSettings();
  }

  const host = $derived(tileHost(mapSettings.tileUrl) || "an external server");

  /** The numeric box fields, in the reading order N/S/W/E. */
  const BOX_FIELDS = [
    { k: "top" as const, label: "North lat" },
    { k: "bottom" as const, label: "South lat" },
    { k: "left" as const, label: "West lng" },
    { k: "right" as const, label: "East lng" },
  ];
</script>

<div class="mt-2 rounded-lg border border-slate-200 dark:border-slate-800">
  <!-- Toolbar. Wraps on narrow viewports; every control has a keyboard path. -->
  <div class="flex flex-wrap items-center gap-2 border-b border-slate-200 p-2 text-xs dark:border-slate-800">
    <div class="flex items-center overflow-hidden rounded-full border border-slate-300 dark:border-slate-700" role="group" aria-label="Drag behaviour">
      <button
        type="button"
        class="px-3 py-1 {dragMode === 'select' ? 'bg-[var(--accent)] text-white' : ''}"
        aria-pressed={dragMode === "select"}
        title="Drag on the map to select an area (filters the search)"
        onclick={() => (dragMode = "select")}>Select area</button>
      <button
        type="button"
        class="px-3 py-1 {dragMode === 'pan' ? 'bg-[var(--accent)] text-white' : ''}"
        aria-pressed={dragMode === "pan"}
        title="Drag on the map to pan (Shift+drag always pans)"
        onclick={() => (dragMode = "pan")}>Pan</button>
    </div>
    <button
      type="button"
      class="rounded-full border border-slate-300 px-2 py-1 dark:border-slate-700"
      title="Zoom out (−)"
      onclick={() => setZoom(zoom - 1)}>−</button>
    <span class="tabular-nums text-slate-500">z{zoom}</span>
    <button
      type="button"
      class="rounded-full border border-slate-300 px-2 py-1 dark:border-slate-700"
      title="Zoom in (+)"
      onclick={() => setZoom(zoom + 1)}>+</button>
    <button
      type="button"
      class="rounded-full border border-slate-300 px-3 py-1 dark:border-slate-700"
      title="Frame every point in this result set"
      onclick={() => { userMoved = false; fitTo(boundsOf(points)); }}>Fit results</button>
    {#if box}
      <button
        type="button"
        class="rounded-full border border-slate-300 px-3 py-1 dark:border-slate-700"
        title="Remove the area filter from this search"
        onclick={() => onBoxChange(null)}>Clear area</button>
    {/if}
    <span class="grow"></span>
    <!-- The tile state is spelled out, never a bare unchecked box: "off" here is
         a privacy property, so it gets words. -->
    <label class="flex items-center gap-1" title="Optional background tiles from a third-party server">
      <input type="checkbox" checked={mapSettings.tiles} onchange={toggleTiles} />
      Background tiles
      <span class={mapSettings.tiles ? "text-amber-600 dark:text-amber-400" : "text-slate-500"}>
        {mapSettings.tiles ? `on — requests ${host}` : "off — bundled outline only"}
      </span>
    </label>
    {#if mapSettings.tiles}
      <!-- Only shown once tiles are on: an operator running their own tile
           server should not have to leave the page, and seeing the template
           makes it obvious where the requests go. -->
      <input
        class="w-72 rounded border border-slate-300 bg-transparent px-2 py-1 outline-none
               focus:border-[var(--accent)] dark:border-slate-700"
        aria-label="Tile URL template"
        title="XYZ tile URL template, with the z / x / y placeholders in braces"
        bind:value={mapSettings.tileUrl}
        onchange={saveMapSettings}
      />
    {/if}
  </div>

  <div
    class="relative w-full"
    style="height: 65vh; min-height: 320px;"
    bind:clientWidth={width}
    bind:clientHeight={height}
  >
    <!-- The <svg> IS the control here (role="application"): it takes pointer
         drags for pan/select and arrow keys for panning, and it is the focus
         target that makes the map keyboard-operable at all. The a11y heuristics
         for a decorative <svg> do not apply. -->
    <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
    <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
    <svg
      bind:this={svgEl}
      width={width || 1}
      height={height || 1}
      viewBox={`0 0 ${width || 1} ${height || 1}`}
      class="block h-full w-full touch-none bg-slate-50 dark:bg-slate-950
             {dragMode === 'select' ? 'cursor-crosshair' : 'cursor-grab'}"
      role="application"
      tabindex="0"
      aria-label="Map of results with coordinates. Arrow keys pan, plus and minus zoom; use the coordinate fields below to select an area."
      onpointerdown={onPointerDown}
      onpointermove={onPointerMove}
      onpointerup={onPointerUp}
      onpointercancel={() => (dragging = null)}
      onkeydown={onKeydown}
    >
      {#if tiles.length}
        <g>
          {#each tiles as t (t.key)}
            <image href={t.url} x={t.x} y={t.y} width={TILE_SIZE} height={TILE_SIZE} />
          {/each}
        </g>
      {/if}

      <!-- Graticule under the land so the grid reads as a background. -->
      <g class="stroke-slate-300/60 dark:stroke-slate-700/60" fill="none" stroke-width="1">
        {#each graticule.lines as l (l.d)}<path d={l.d} />{/each}
      </g>

      {#if landPath}
        <path
          d={landPath}
          fill-rule="evenodd"
          class="fill-slate-200/90 stroke-slate-400 dark:fill-slate-800/80 dark:stroke-slate-600"
          stroke-width="0.75"
          opacity={mapSettings.tiles && tiles.length ? 0.25 : 1}
        />
        <path
          d={borderPath}
          fill="none"
          class="stroke-slate-400/80 dark:stroke-slate-600/80"
          stroke-width="0.5"
          opacity={mapSettings.tiles && tiles.length ? 0.25 : 1}
        />
      {/if}

      <!-- Degree labels above the outline so they stay legible over land. -->
      <g class="fill-slate-500 dark:fill-slate-400" font-size="9">
        {#each graticule.lines as l (l.label + l.d)}
          <text x={l.x} y={l.y}>{l.label}</text>
        {/each}
      </g>

      {#if boxRect}
        <rect
          x={boxRect.x}
          y={boxRect.y}
          width={Math.max(0, boxRect.w)}
          height={Math.max(0, boxRect.h)}
          class="fill-[var(--accent)]/10 stroke-[var(--accent)]"
          stroke-width="1.5"
          stroke-dasharray="4 3"
        />
      {/if}

      {#each clusters as c (c.key)}
        <!-- svelte-ignore a11y_click_events_have_key_events -->
        <g
          role="button"
          tabindex="0"
          aria-label={c.count === 1
            ? `Open ${c.points[0].label}`
            : `${c.count} items near ${c.lat.toFixed(3)}, ${c.lng.toFixed(3)}`}
          class="cursor-pointer"
          onclick={(e) => { e.stopPropagation(); activate(c); }}
          onkeydown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); e.stopPropagation(); activate(c); } }}
        >
          <circle
            cx={c.x}
            cy={c.y}
            r={radiusOf(c.count)}
            class="fill-[var(--accent)]/70 stroke-white dark:stroke-slate-900"
            stroke-width="1.5"
          />
          {#if c.count > 1}
            <text
              x={c.x}
              y={c.y + 3}
              text-anchor="middle"
              font-size="10"
              class="pointer-events-none fill-white font-medium"
            >{c.count > 999 ? "999+" : c.count}</text>
          {/if}
        </g>
      {/each}

      {#if dragRect}
        <rect
          x={dragRect.x}
          y={dragRect.y}
          width={dragRect.w}
          height={dragRect.h}
          class="fill-[var(--accent)]/15 stroke-[var(--accent)]"
          stroke-width="1"
        />
      {/if}
    </svg>

    <!-- Cluster contents. Not a second detail view: each row hands the id to the
         page's existing ItemDetail, the same as clicking a result row. -->
    {#if openCluster}
      <div
        class="absolute max-h-64 w-64 overflow-auto rounded-lg border border-slate-300 bg-white p-2 text-xs shadow-lg dark:border-slate-700 dark:bg-slate-900"
        style={`left: ${Math.min(Math.max(8, openCluster.x + 12), Math.max(8, width - 272))}px; top: ${Math.min(Math.max(8, openCluster.y + 12), Math.max(8, height - 200))}px;`}
      >
        <div class="mb-1 flex items-center gap-2">
          <span class="font-medium">{openCluster.count} items here</span>
          <button
            type="button"
            class="ml-auto rounded border border-slate-300 px-2 py-0.5 dark:border-slate-700"
            onclick={() => openCluster && zoomIntoCluster(openCluster)}>Zoom in</button>
          <button type="button" class="rounded px-1" aria-label="Close" onclick={() => (openCluster = null)}>×</button>
        </div>
        <ul>
          {#each openCluster.points.slice(0, 50) as p (p.id)}
            <li>
              <button
                type="button"
                class="w-full truncate rounded px-1 py-0.5 text-left hover:bg-slate-100 dark:hover:bg-slate-800"
                title={p.label}
                onclick={() => onOpenItem(p.id)}>{p.label}</button>
            </li>
          {/each}
        </ul>
        {#if openCluster.count > 50}
          <p class="mt-1 text-slate-500">…and {openCluster.count - 50} more — zoom in to separate them.</p>
        {/if}
      </div>
    {/if}

    <!-- Explanations, not errors. Each one names the reason the map is empty and
         what would change it. -->
    {#if gpsGated}
      <div class="pointer-events-auto absolute inset-0 flex items-center justify-center bg-slate-50/85 p-6 text-center dark:bg-slate-950/85">
        <div class="max-w-md text-sm text-slate-600 dark:text-slate-300">
          <p class="text-base font-medium">No library is publishing coordinates</p>
          <p class="mt-2">
            GPS is read from your photos and stored, but the search index leaves it
            out until a library opts in. Turn on <strong>Expose GPS</strong> in
            Admin → the library's settings; Filearr then re-projects that library's
            documents (turning it back off removes the points again).
          </p>
          <p class="mt-2">
            <a
              class="underline decoration-dotted underline-offset-2 hover:text-[var(--accent)]"
              href={`${docsUrl}reference/api/#geo-search-radius-and-bounding-box`}
              target="_blank"
              rel="noreferrer">How the GPS gate works</a>
          </p>
        </div>
      </div>
    {:else if !loading && !points.length}
      <div class="pointer-events-none absolute inset-0 flex items-center justify-center p-6 text-center">
        <div class="max-w-md text-sm text-slate-500">
          {#if box}
            <p>No results with coordinates inside this area.</p>
            <p class="mt-1">Clear the area, or widen it, to see the rest of this search.</p>
          {:else if queryTotal > 0}
            <p>None of the {queryTotal} results for this search carry coordinates.</p>
            <p class="mt-1">
              Only image and video files with GPS metadata, in a library with
              Expose GPS on, appear here.
            </p>
          {:else}
            <p>Run a search to plot its results.</p>
          {/if}
        </div>
      </div>
    {/if}

    {#if mapSettings.tiles && offline}
      <div class="absolute inset-x-2 top-2 rounded border border-amber-500/50 bg-amber-500/10 p-2 text-xs text-amber-700 dark:text-amber-300" role="status">
        Background tiles are on, but this browser is offline — no tiles will load.
        The outline below is bundled with Filearr and draws either way.
      </div>
    {/if}
  </div>

  <!-- Numeric area form: the keyboard path to a selection, and where you paste
       coordinates from somewhere else. -->
  <div class="flex flex-wrap items-end gap-2 border-t border-slate-200 p-2 text-xs dark:border-slate-800">
    <span class="mr-1 font-medium text-slate-500">Area</span>
    {#each BOX_FIELDS as f (f.k)}
      <label class="flex flex-col gap-0.5 text-slate-500">
        {f.label}
        <input
          class="w-24 rounded border border-slate-300 bg-transparent px-2 py-1 tabular-nums outline-none
                 focus:border-[var(--accent)] dark:border-slate-700"
          type="text"
          inputmode="decimal"
          bind:value={draft[f.k]}
          onkeydown={(e: KeyboardEvent) => { if (e.key === "Enter") applyDraft(); e.stopPropagation(); }}
        />
      </label>
    {/each}
    <button
      type="button"
      class="rounded-full bg-[var(--accent)] px-3 py-1 text-white"
      onclick={applyDraft}>Apply area</button>
    <button
      type="button"
      class="rounded-full border border-slate-300 px-3 py-1 dark:border-slate-700"
      title="Copy the current view (or the active area) into the fields"
      onclick={fillDraftFromView}>Use current view</button>
    {#if draftError}<span class="text-red-500">{draftError}</span>{/if}
  </div>

  <!-- Caption: what is drawn, out of what. The cap is stated, never silent. -->
  <p class="border-t border-slate-200 p-2 text-xs text-slate-500 dark:border-slate-800">
    {#if loading}
      Loading points…
    {:else if geoTotal > points.length}
      Showing {points.length} of {geoTotal} results with coordinates (capped at
      {MAP_POINT_CAP} per search to keep the map responsive — narrow the search or
      draw a smaller area to see the rest).
    {:else}
      Showing {points.length} of {queryTotal} results with coordinates.
    {/if}
    {#if hiddenByViewport > 0}
      {hiddenByViewport} are outside the current view.
    {/if}
    {#if box}
      Area filter: {formatBox(box)}.
    {/if}
    {#if !mapSettings.tiles}
      Outline: Natural Earth 1:110m (public domain), bundled — this map makes no
      network requests.
    {/if}
    {#if mapSettings.tiles && mapSettings.tileUrl !== DEFAULT_TILE_URL}
      Tiles: {mapSettings.tileUrl}.
    {/if}
  </p>
</div>
