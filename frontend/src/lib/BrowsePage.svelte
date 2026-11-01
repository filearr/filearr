<script module lang="ts">
  // Heavy panels shown only on demand are split into their own chunks so the
  // page chunk stays small. Promises are cached at module level so `{#await}`
  // sees the same settled promise on every reopen (no re-fetch, no flash).
  let itemDetailModule: Promise<typeof import("./ItemDetail.svelte")> | undefined;
  function loadItemDetail() {
    return (itemDetailModule ??= import("./ItemDetail.svelte"));
  }
</script>

<script lang="ts">
  import { fmtBytes } from "./format";
  import { onDestroy } from "svelte";
  import { COPY_FAILED, copyText } from "./clipboard";
  import { latest } from "./latest";
  // UI-T12 Part 3 — in-page folder navigation. Renders the breadcrumb header
  // (shared Breadcrumbs component), a folder list (click → deeper), and the file
  // list (click → ItemDetail modal). Back/forward work because navigation is
  // pure hash mutation (App owns the router; this component reads props and emits
  // hash changes via gotoBrowse). All names render as text (untrusted).
  import {
    libraryTree,
    listLibraries,
    targetedScan,
    type ItemRecord,
    type Library,
    type TreeResponse,
    errorMessage,
  } from "./api";
  import { buildDisplayPath, buildOpenUrl } from "./pathlinks";
  import { formatShare, shareLocation } from "./osFormat";
  import { shareFormat, detectedPlatform } from "./osFormat.svelte";
  import { gotoBrowse } from "./routes";
  import Breadcrumbs from "./Breadcrumbs.svelte";
  import Thumb from "./Thumb.svelte";
  import { Alert, Button, Checkbox, Field, Input, StateBlock } from "./ui";
  import { toast } from "./ui/toast.svelte";

  let { libraryId, path }: { libraryId: string; path: string } = $props();

  const LIMIT = 100;

  let tree = $state<TreeResponse | null>(null);
  let error = $state("");
  let selected = $state<string | null>(null);
  let offset = $state(0);
  // UI-T15: raw url-ish + UNC spellings from the library; the effective one
  // (per the viewer's OS / override) is derived below and drives every affordance.
  let shareUrlRaw = $state<string | null>(null);
  let shareUncRaw = $state<string | null>(null);
  const sharePrefix = $derived(
    formatShare(shareLocation(shareUrlRaw, shareUncRaw), shareFormat.pref, detectedPlatform),
  );
  // D9: buildOpenUrl is "" for a prefix it will not link (unsafe scheme,
  // relative path) — no "Open" link then, only Copy path.
  const canOpen = $derived(!!buildOpenUrl(sharePrefix));
  let libName = $state<string>("");
  let copied = $state<string | null>(null);
  let copyTimer: ReturnType<typeof setTimeout>;
  let openFolderMenu = $state<string | null>(null);
  // P12 slice 2: list vs. thumbnail-grid layout for the Files section. Ephemeral
  // (component-local) -- a cheap presentational toggle, no deep-link needed here.
  let filesView = $state<"list" | "grid">("list");

  // Resolve the library's EFFECTIVE share prefix + name once (the tree endpoint
  // returns the name but not the share prefix). OPS-T7: share_prefix_effective
  // folds in the deploy mount map, so open/copy work even with no manual prefix.
  // Refreshed if the library id changes.
  let resolvedFor = "";

  // Last-request-wins over the whole assembly (library lookup + tree + the
  // follow-up folder pages): a fast second navigation aborts the first, and
  // the stale route's result is dropped rather than rendered under the new one.
  // `undefined` lib = not looked up (cached, or the lookup failed).
  const treeLoader = latest(async (signal, lib: string, p: string, off: number) => {
    let libInfo: Library | null | undefined;
    if (resolvedFor !== lib) {
      try {
        const libs: Library[] = await listLibraries(signal);
        libInfo = libs.find((l) => l.id === lib) ?? null;
      } catch {
        /* name falls back to the tree payload below */
      }
    }
    const res = await libraryTree(lib, p, LIMIT, off, 0, signal);
    // Folders are server-paginated (cap 500/page). Auto-fetch the remaining
    // pages so huge directories (e.g. >500 shows) always render completely —
    // folder rows are tiny, so this stays cheap even at a few thousand.
    let guard = 0;
    while (res.folders.length < res.folders_total && guard < 20) {
      const more = await libraryTree(lib, p, LIMIT, off, res.folders.length, signal);
      if (more.folders.length === 0) break;
      res.folders = [...res.folders, ...more.folders];
      guard += 1;
    }
    return { res, libInfo };
  });
  onDestroy(() => treeLoader.cancel());

  async function load() {
    // A18: snapshot the route once; every request in this run uses it, never
    // the live props, which may already point at the next folder.
    const lib = libraryId;
    const p = path;
    const off = offset;
    error = "";
    try {
      const r = await treeLoader.run(lib, p, off);
      if (r === undefined) return;
      if (r.libInfo !== undefined) {
        shareUrlRaw = r.libInfo?.share_prefix_effective ?? r.libInfo?.share_prefix ?? null;
        shareUncRaw = r.libInfo?.share_unc_effective ?? null;
        libName = r.libInfo?.name ?? "";
        resolvedFor = lib;
      }
      tree = r.res;
      if (!libName) libName = r.res.library_name;
    } catch (e) {
      tree = null;
      error = errorMessage(e);
    }
  }

  // A17: a save in ItemDetail mirrors the PATCH response's title/year into the
  // matching row (TreeItem carries no tags).
  function onItemChanged(rec: ItemRecord) {
    if (!tree) return;
    const id = typeof rec.id === "string" ? rec.id : "";
    tree.items = tree.items.map((it) =>
      it.id === id
        ? {
            ...it,
            title: typeof rec.title === "string" ? rec.title : null,
            year: typeof rec.year === "number" ? rec.year : null,
          }
        : it,
    );
  }

  // Reset pagination whenever the target folder changes, then (re)load. Reading
  // offset inside the effect makes it re-run on Prev/Next too.
  let lastKey = "";
  $effect(() => {
    const key = `${libraryId} ${path}`;
    if (key !== lastKey) {
      lastKey = key;
      offset = 0;
    }
    void offset;
    load();
  });

  async function copy(label: string, textToCopy: string) {
    // copyText already carries the insecure-context fallback.
    if (!(await copyText(textToCopy))) return toast(COPY_FAILED, { tone: "warn" });
    copied = label;
    clearTimeout(copyTimer);
    copyTimer = setTimeout(() => (copied = null), 1200);
    openFolderMenu = null;
  }

  function folderRel(name: string): string {
    return path ? `${path}/${name}` : name;
  }

  // W9 targeted rescan (file or folder, optionally recursive). A transient toast
  // confirms the enqueue; the run then appears in the Admin scan list / SSE.
  // A failure from a file row's Rescan stays under that row (keyed by its
  // rel_path); the folder menu has closed by then, so its failures are toasts.
  let rowErrors = $state<Record<string, string>>({});
  function setRowError(rel: string, msg: string) {
    const next = { ...rowErrors };
    if (msg) next[rel] = msg;
    else delete next[rel];
    rowErrors = next;
  }

  async function scanTarget(rel: string, recursive: boolean, label: string, fromRow = false) {
    openFolderMenu = null;
    if (fromRow) setRowError(rel, "");
    try {
      const r = await targetedScan(libraryId, { path: rel, recursive });
      toast(
        r.coalesced
          ? `Scan of ${label} already in progress — joined it.`
          : `Scan of ${label} queued.`,
        { tone: "ok" },
      );
    } catch (e) {
      const msg = `Could not scan ${label}: ${errorMessage(e, { verb: "scan this folder" })}`;
      if (fromRow) setRowError(rel, msg);
      else toast(msg, { tone: "danger" });
    }
  }

  // Free-text targeted scan (automation-style: scan a path not necessarily shown
  // in the current listing, e.g. a freshly laid-down folder). 404 = not on disk.
  let showScanForm = $state(false);
  let scanPath = $state("");
  let scanRecursive = $state(true);
  let scanBusy = $state(false);
  let scanError = $state("");
  async function submitScanForm(e: Event) {
    e.preventDefault();
    if (scanBusy) return;
    scanBusy = true;
    scanError = "";
    const target = scanPath.trim();
    try {
      const r = await targetedScan(libraryId, { path: target, recursive: scanRecursive });
      toast(
        r.coalesced
          ? `Scan of "${target || "whole library"}" already in progress — joined it.`
          : `Scan of "${target || "whole library"}" queued${r.is_file ? " (file)" : ""}.`,
        { tone: "ok" },
      );
      showScanForm = false;
      scanPath = "";
    } catch (e2) {
      scanError = `Could not scan "${target}": ${errorMessage(e2, { verb: "scan this folder" })}`;
    } finally {
      scanBusy = false;
    }
  }

  const shownFrom = $derived(tree && tree.total_items > 0 ? offset + 1 : 0);
  const shownTo = $derived(tree ? Math.min(offset + LIMIT, tree.total_items) : 0);
</script>

<div class="mx-auto max-w-5xl">
  <div class="mt-2 rounded-panel border border-border p-3">
    <div class="flex items-center gap-2">
      <div class="min-w-0 grow">
        <Breadcrumbs
          libraryName={libName || tree?.library_name || "Library"}
          shareUrl={shareUrlRaw}
          shareUnc={shareUncRaw}
          relPath={path}
          isFile={false}
          onNavigate={(p) => gotoBrowse(libraryId, p)}
        />
      </div>
      <Button
        size="sm"
        class="shrink-0"
        aria-expanded={showScanForm}
        title="Rescan this folder, or a specific path under this library, without a full library scan"
        onclick={() => (showScanForm = !showScanForm)}>Targeted scan</Button>
    </div>

    {#if showScanForm}
      <form class="mt-3 border-t border-border pt-3" onsubmit={submitScanForm}>
        <div class="flex flex-wrap items-end gap-2">
          <Field label="Path under the library root (blank = whole library; a file or folder)" class="min-w-[16rem] grow">
            <Input placeholder={path || "e.g. Movies/Incoming"} bind:value={scanPath} />
          </Field>
          <span class="pb-1.5" title="Ignored when the path is a file">
            <Checkbox size="sm" label="Include subfolders" bind:checked={scanRecursive} />
          </span>
          <Button
            size="sm"
            title="Fill in the folder you are currently viewing"
            onclick={() => (scanPath = path)}>Use current folder</Button>
          <Button type="submit" variant="primary" size="sm" disabled={scanBusy}>
            {scanBusy ? "Queueing…" : "Scan"}
          </Button>
        </div>
        {#if scanError}
          <Alert tone="danger" class="mt-2" onDismiss={() => (scanError = "")}>{scanError}</Alert>
        {/if}
      </form>
    {/if}
  </div>

  {#if error}
    <StateBlock state="error" class="mt-6" message={error} onRetry={load} />
  {:else if !tree}
    <StateBlock state="loading" class="mt-6" />
  {:else}
    {#if tree.folders.length === 0 && tree.items.length === 0}
      <StateBlock
        state="empty"
        class="mt-6"
        message="This folder is empty. Add files to it on disk, then run a targeted scan to index them." />
    {/if}

    {#if tree.folders.length > 0}
      <h2 class="mt-6 text-sm font-semibold text-muted">Folders</h2>
      <ul class="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
        {#each tree.folders as f (f.name)}
          <li class="relative flex items-center gap-2 rounded-panel border border-border px-3 py-2">
            <button
              type="button"
              class="flex min-w-0 grow items-center gap-2 text-left"
              onclick={() => gotoBrowse(libraryId, folderRel(f.name))}>
              <span class="text-muted">📁</span>
              <span class="truncate font-medium" title={f.name}>{f.name}</span>
              <span class="ml-auto shrink-0 text-xs text-muted">{f.item_count}</span>
            </button>
            <button
              type="button"
              class="rounded-control px-1 text-xs text-muted hover:bg-surface-3 hover:text-fg"
              aria-label="Folder actions for {f.name}"
              aria-expanded={openFolderMenu === f.name}
              title="Folder actions"
              onclick={() => (openFolderMenu = openFolderMenu === f.name ? null : f.name)}>⋯</button>
            {#if openFolderMenu === f.name}
              <div
                class="absolute right-2 top-full z-20 mt-1 w-max min-w-[10rem] rounded-control border border-border-strong bg-surface-2 p-1 text-xs shadow-lg"
                role="menu">
                {#if canOpen}
                  <a
                    class="block rounded-control px-2 py-1 hover:bg-surface-3"
                    href={buildOpenUrl(sharePrefix, folderRel(f.name))}
                    target="_blank"
                    rel="noopener noreferrer"
                    title="Open the folder at its network location. Browsers may block file:// — use Copy path if nothing happens."
                    onclick={() => (openFolderMenu = null)}>Open via network</a>
                {/if}
                <button
                  type="button"
                  class="block w-full rounded-control px-2 py-1 text-left hover:bg-surface-3"
                  onclick={() =>
                    copy(
                      `folder-${f.name}`,
                      sharePrefix ? buildDisplayPath(sharePrefix, folderRel(f.name)) : folderRel(f.name),
                    )}>
                  {copied === `folder-${f.name}` ? "Copied!" : "Copy path"}
                </button>
                <button
                  type="button"
                  class="block w-full rounded-control px-2 py-1 text-left hover:bg-surface-3"
                  onclick={() => scanTarget(folderRel(f.name), true, f.name)}>
                  Scan folder + subfolders
                </button>
                <button
                  type="button"
                  class="block w-full rounded-control px-2 py-1 text-left hover:bg-surface-3"
                  onclick={() => scanTarget(folderRel(f.name), false, f.name)}>
                  Scan this folder only
                </button>
              </div>
            {/if}
          </li>
        {/each}
      </ul>
    {/if}

    {#if tree.items.length > 0}
      <div class="mt-6 flex items-baseline gap-3">
        <h2 class="text-sm font-semibold text-muted">Files</h2>
        <span class="text-xs text-muted">showing {shownFrom}–{shownTo} of {tree.total_items}</span>
        <span class="grow"></span>
        <div class="flex items-center overflow-hidden rounded-full border border-border-strong text-xs" role="group" aria-label="File layout">
          <button
            type="button"
            class="px-2 py-0.5 {filesView === 'list' ? 'bg-accent text-accent-fg' : ''}"
            aria-pressed={filesView === 'list'}
            onclick={() => (filesView = 'list')}>List</button>
          <button
            type="button"
            class="px-2 py-0.5 {filesView === 'grid' ? 'bg-accent text-accent-fg' : ''}"
            aria-pressed={filesView === 'grid'}
            onclick={() => (filesView = 'grid')}>Grid</button>
        </div>
      </div>
      {#if filesView === "grid"}
        <div class="mt-2 grid gap-3 grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5">
          {#each tree.items as it (it.id)}
            <button
              type="button"
              class="flex flex-col overflow-hidden rounded-panel border border-border text-left hover:border-border-strong"
              onclick={() => (selected = it.id)}>
              <Thumb id={it.id} size="aspect-square w-full h-auto" rounded="rounded-none" />
              <div class="flex flex-col gap-1 p-2">
                <span class="truncate text-sm font-medium" title={it.filename}>{it.title ?? it.filename}</span>
                <span class="flex items-center gap-1">
                  <span class="shrink-0 rounded-control bg-surface-3 px-1.5 py-0.5 text-2xs">{it.file_category}</span>
                  <span class="ml-auto shrink-0 text-2xs text-muted">{fmtBytes(it.size)}</span>
                </span>
              </div>
            </button>
          {/each}
        </div>
      {:else}
      <ul class="mt-2 divide-y divide-border">
        {#each tree.items as it (it.id)}
          <li class="flex flex-col">
            <div class="flex items-center gap-1">
              <button
                type="button"
                class="flex w-full items-center gap-3 py-3 text-left hover:bg-surface-3"
                onclick={() => (selected = it.id)}>
                <Thumb id={it.id} size="h-9 w-9" />
                <span class="rounded-control bg-surface-3 px-2 py-0.5 text-xs">{it.file_category}</span>
                <span class="truncate font-medium" title={it.filename}>{it.title ?? it.filename}</span>
                {#if it.year}<span class="text-sm text-muted">({it.year})</span>{/if}
                <span class="grow"></span>
                <span class="shrink-0 text-xs text-muted">{fmtBytes(it.size)}</span>
              </button>
              <Button
                size="sm"
                variant="ghost"
                class="shrink-0"
                title="Rescan just this file"
                aria-label="Rescan {it.filename}"
                onclick={() => scanTarget(it.rel_path, false, it.filename, true)}>Rescan</Button>
            </div>
            {#if rowErrors[it.rel_path]}
              <Alert tone="danger" class="mb-2" onDismiss={() => setRowError(it.rel_path, "")}>{rowErrors[it.rel_path]}</Alert>
            {/if}
          </li>
        {/each}
      </ul>
      {/if}

      {#if tree.total_items > LIMIT}
        <div class="mt-3 flex items-center gap-2">
          <Button
            disabled={offset === 0}
            onclick={() => (offset = Math.max(0, offset - LIMIT))}>Prev</Button>
          <Button
            disabled={offset + LIMIT >= tree.total_items}
            onclick={() => (offset = offset + LIMIT)}>Next</Button>
        </div>
      {/if}
    {/if}
  {/if}

  {#if selected}
    <!-- No pending markup: a "Loading…" line at the foot of the page would go
         unseen, and the modal appears as soon as its chunk lands. A failure
         (stale chunk after a deploy) is pinned to the viewport instead, so it
         is seen wherever the user has scrolled to. -->
    {#await loadItemDetail() then m}
      <m.default
        id={selected}
        onClose={() => (selected = null)}
        onOpen={(nid) => (selected = nid)}
        onChanged={onItemChanged} />
    {:catch}
      <div class="fixed inset-x-0 bottom-4 z-50 mx-auto w-fit shadow">
        <Alert tone="danger">
          Item details failed to load — Filearr may have been updated.
          <Button size="sm" class="ml-2" onclick={() => location.reload()}>Reload</Button>
        </Alert>
      </div>
    {/await}
  {/if}
</div>
