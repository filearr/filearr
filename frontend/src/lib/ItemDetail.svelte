<script module lang="ts">
  import { semanticStats } from "./api";

  // One semantic-enabled probe per page load, shared by every ItemDetail
  // instance: the flag can't change without a server restart, so a cached
  // promise avoids a /stats round-trip on every detail open.
  let semanticProbe: Promise<boolean> | null = null;
  function semanticEnabled(): Promise<boolean> {
    semanticProbe ??= semanticStats()
      .then((s) => !!s?.enabled)
      .catch(() => false);
    return semanticProbe;
  }
</script>

<script lang="ts">
  import { copyText } from "./clipboard";
  import {
    ApiError,
    friendlyError,
    getItem,
    itemCopies,
    listCustomFields,
    patchItem,
    touchItem,
    similarItems,
    type CustomField,
    type ItemPatchBody,
    type ItemRecord,
    type CopiesResponse,
    type SimilarResponse,
  } from "./api";
  import CustomFieldInput from "./CustomFieldInput.svelte";
  import { applicableFields, coerceCustomValue } from "./bulkEdit";
  import RawView from "./RawView.svelte";
  import Breadcrumbs from "./Breadcrumbs.svelte";
  import { gotoBrowse } from "./routes";
  import { cardFor, cardLabel } from "./cards/registry";
  import ArchiveSection from "./ArchiveSection.svelte";
  import Thumb from "./Thumb.svelte";
  import RetrievePanel from "./RetrievePanel.svelte";
  import AgentStatusPanel from "./AgentStatusPanel.svelte";

  let {
    id,
    onClose,
    onOpen,
  }: {
    id: string;
    onClose: () => void;
    // Optional: navigate this panel to another item (similar-items tiles).
    // Parents pass their selected-id setter; without it tiles are inert labels.
    onOpen?: (id: string) => void;
  } = $props();

  let item = $state<ItemRecord | null>(null);
  let error = $state("");

  // Narrow the untyped record to the breadcrumb fields (all optional).
  const str = (k: string): string | null => {
    const v = item?.[k];
    return typeof v === "string" ? v : null;
  };
  const relPath = $derived(str("rel_path") ?? "");
  const libId = $derived(str("library_id") ?? "");
  const libName = $derived(str("library_name") ?? "Library");
  // UI-T15: url-ish + UNC spellings of the library share; Breadcrumbs picks the
  // OS-appropriate one.
  const shareUrl = $derived(str("library_share_prefix"));
  const shareUnc = $derived(str("library_share_unc"));
  const nativePath = $derived(str("native_path"));
  const containerPath = $derived(str("path"));

  // P10-T11/T12: the item's own RESOLVED network location + which tier produced
  // it (agent hint > admin mapping > library share_prefix). This is the unified,
  // authoritative file-open affordance: when present it supersedes the
  // library-share_prefix "Open file" row in <Breadcrumbs> (suppressed below).
  // Null => render nothing (no fabricated location, no empty state).
  const itemShareUrl = $derived(str("share_url"));
  // Roadmap §5 P3 provenance: extracted download-source URLs (never user-editable).
  const extractedMeta = $derived(
    (item?.metadata as Record<string, unknown> | undefined) ?? {},
  );
  const safeHttpUrl = (v: unknown): string | null =>
    typeof v === "string" && /^(https?|s?ftp):\/\//i.test(v) ? v : null;
  const originUrl = $derived(safeHttpUrl(extractedMeta["origin_url"]));
  const referrerUrl = $derived(safeHttpUrl(extractedMeta["referrer_url"]));
  const shareSource = $derived(str("share_source"));
  const shareSourceLabel = $derived(
    shareSource === "agent_hint"
      ? "from agent"
      : shareSource === "mapping"
        ? "admin mapping"
        : shareSource === "library"
          ? "library share"
          : "",
  );
  let shareCopied = $state(false);
  let shareCopiedTimer: ReturnType<typeof setTimeout>;
  async function copyShare(text: string) {
    // FIX-5 plain-http-safe helper (navigator.clipboard is unavailable over
    // http://<lan-ip>); it falls back to a textarea + execCommand copy.
    await copyText(text);
    shareCopied = true;
    clearTimeout(shareCopiedTimer);
    shareCopiedTimer = setTimeout(() => (shareCopied = false), 1500);
  }

  // P4-T10: the typed per-media_type card is the FIRST tab; "Raw" is ALWAYS last.
  // The card component is resolved from the registry (a future/unregistered type
  // falls back to the generic key-facts card). Rendered via native Svelte-5
  // dynamic-component syntax below — no <svelte:component>.
  const mediaType = $derived(str("file_category") ?? "");
  const CardComponent = $derived(cardFor(mediaType));
  const cardTabLabel = $derived(cardLabel(mediaType));
  // IN-T4: editing is a THIRD tab rather than an inline-per-field affordance.
  // The card and Raw views are dense read-only layouts; sprinkling pencil icons
  // through them would mean redesigning both, whereas a tab reuses the switcher
  // that is already there and keeps "looking" and "changing" visibly distinct.
  type TabId = "card" | "raw" | "edit";
  let active = $state<TabId>("card");

  // ------------------------------------------------------------------------ //
  // IN-T4 single-item edit. Companion to the bulk bar and deliberately the      //
  // ONLY place `title` can be changed: one title applied to N files is nearly   //
  // always a mistake, so the bulk bar omits it by design.                       //
  //                                                                            //
  // Empty input = CLEAR (explicit null on the wire, which pops the key /        //
  // nulls the column) — `patchItem` has had those semantics all along; it       //
  // simply had zero callers until now.                                          //
  // ------------------------------------------------------------------------ //
  let eTitle = $state("");
  let eYear = $state("");
  let eTags = $state<string[]>([]);
  let eTagDraft = $state("");
  // Raw control values per custom-field NAME (see CustomFieldInput for why the
  // binding stays a raw string/boolean rather than the coerced JSON type).
  let eFields = $state<Record<string, string | boolean>>({});
  let defs = $state<CustomField[]>([]);
  let saving = $state(false);
  let editError = $state("");

  // Only fields whose applies_to / library_ids cover THIS item (same rule the
  // key-facts card uses to decide what to display).
  const editableFields = $derived(
    applicableFields(defs, [{ file_category: mediaType, library_id: libId }]),
  );

  const userMeta = $derived(
    (item?.user_metadata as Record<string, unknown> | undefined) ?? {},
  );

  /** Render a stored user_metadata value back into a raw control value. */
  function toRaw(v: unknown): string | boolean {
    if (v == null) return "";
    if (typeof v === "boolean") return v;
    return String(v);
  }

  /** Seed the form from the item currently loaded, then switch to the tab. A
   *  fresh seed on every entry means a cancelled edit leaves nothing behind. */
  function startEdit() {
    if (!item) return;
    editError = "";
    eTitle = typeof item.title === "string" ? item.title : "";
    eYear = typeof item.year === "number" ? String(item.year) : "";
    // Deduped on seed: the chip list is a keyed {#each}, and a stored duplicate
    // (possible — nothing enforces uniqueness on the column) would crash it.
    eTags = [
      ...new Set(
        Array.isArray(item.tags)
          ? (item.tags as unknown[]).filter((t): t is string => typeof t === "string")
          : [],
      ),
    ];
    eTagDraft = "";
    const seeded: Record<string, string | boolean> = {};
    for (const d of editableFields) seeded[d.name] = toRaw(userMeta[d.name]);
    eFields = seeded;
    active = "edit";
  }

  function commitTagDraft() {
    const parts = eTagDraft.split(",").map((s) => s.trim()).filter(Boolean);
    const out = [...eTags];
    for (const p of parts) {
      if (!out.some((t) => t.toLowerCase() === p.toLowerCase())) out.push(p);
    }
    eTags = out;
    eTagDraft = "";
  }

  function onTagKey(e: KeyboardEvent) {
    if (e.key !== "Enter" && e.key !== ",") return;
    // Enter must not bubble to the dialog / window handlers.
    e.preventDefault();
    e.stopPropagation();
    commitTagDraft();
  }

  function editErrDetail(e: unknown): string {
    if (e instanceof ApiError) {
      try {
        const j = JSON.parse(e.body);
        if (j?.detail) return typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
      } catch {
        /* body not JSON */
      }
      return e.body || String(e);
    }
    return String(e);
  }

  async function saveEdit() {
    if (!item) return;
    if (eTagDraft.trim()) commitTagDraft();
    editError = "";

    const patch: ItemPatchBody = {};
    const curTitle = typeof item.title === "string" ? item.title : "";
    if (eTitle.trim() !== curTitle) patch.title = eTitle.trim() || null;

    const curYear = typeof item.year === "number" ? item.year : null;
    if (!eYear.trim()) {
      if (curYear != null) patch.year = null;
    } else {
      const y = Number(eYear.trim());
      if (!Number.isInteger(y) || y < 1 || y > 9999) {
        editError = "Year must be a whole number between 1 and 9999.";
        return;
      }
      if (y !== curYear) patch.year = y;
    }

    const curTags = Array.isArray(item.tags)
      ? (item.tags as unknown[]).filter((t): t is string => typeof t === "string")
      : [];
    if (eTags.length !== curTags.length || eTags.some((t, i) => t !== curTags[i])) {
      patch.tags = eTags; // arrays REPLACE (schema contract)
    }

    // Only CHANGED custom fields ride along: an untouched key must stay absent
    // so it is left alone, and a cleared one must be an explicit null.
    const meta: Record<string, unknown> = {};
    for (const d of editableFields) {
      // Only fields whose control was actually SEEDED (i.e. rendered) count. If a
      // definition arrived after the form opened, its control was never shown —
      // treating its absent raw value as "blank" would send an explicit null and
      // silently delete a value the user never laid eyes on.
      if (!(d.name in eFields)) continue;
      const raw = eFields[d.name] ?? "";
      const blank = raw === "" || raw == null;
      const had = userMeta[d.name] != null;
      if (blank) {
        if (had) meta[d.name] = null;
        continue;
      }
      const res = coerceCustomValue(d, raw);
      if (!res.ok) {
        editError = res.error;
        return;
      }
      if (res.value !== userMeta[d.name]) meta[d.name] = res.value;
    }
    if (Object.keys(meta).length) patch.user_metadata = meta;

    if (!Object.keys(patch).length) {
      active = "card";
      return;
    }

    saving = true;
    try {
      // The PATCH response IS the updated projection, so the panel refreshes
      // without a second GET (and without waiting on the async index sync).
      item = await patchItem(id, patch);
      active = "card";
    } catch (e) {
      editError = editErrDetail(e);
    } finally {
      saving = false;
    }
  }

  // P10-T3/T10: agent-hosted items (library owned by an agent) surface the
  // hosting agent's identity, online status, verify freshness, and an inline
  // Verify action via <AgentStatusPanel>. ``source_agent_id`` on the item record
  // is the cheap ownership gate; the panel fetches the live agent-status detail.
  const agentOwned = $derived(!!str("source_agent_id"));
  // P10-T6 retrieve: the download filename for the staged file.
  const fileName = $derived(str("filename") ?? relPath.split("/").pop() ?? "download");

  // Navigating a breadcrumb folder closes the modal and switches to browse.
  function navigate(folderRelPath: string) {
    onClose();
    if (libId) gotoBrowse(libId, folderRelPath);
  }

  // P3-T10: the OTHER copies of this item. Always fetched on open; the Copies
  // section renders whenever the group has more than one member (count > 1).
  let copies = $state<CopiesResponse | null>(null);
  let copiedPath = $state<string | null>(null);
  let copiedTimer: ReturnType<typeof setTimeout>;

  // P3-T9: related / near-duplicate items via the semantic vector. The whole
  // section renders ONLY when semantic search is enabled server-side (cached
  // module-level probe) — an always-off install no longer shows a clickable
  // affordance that can only fail. Hits stay lazy-fetched on expand; a 409
  // (this item unembedded yet) degrades to an "unavailable" note.
  let semanticOn = $state(false);
  $effect(() => {
    semanticEnabled().then((on) => (semanticOn = on));
  });
  let similar = $state<SimilarResponse | null>(null);
  let similarOpen = $state(false);
  let similarLoaded = $state(false);
  let similarLoading = $state(false);
  let similarError = $state("");
  // 2026-08-20: grid (thumbnails) or table (name/similarity/type/size/modified)
  // view for the similar list. Sticky per browser.
  let similarView = $state<"grid" | "table">(
    (localStorage.getItem("filearr.similarView") as "grid" | "table") || "grid",
  );
  function setSimilarView(v: "grid" | "table") {
    similarView = v;
    localStorage.setItem("filearr.similarView", v);
  }
  const simPct = (h: Record<string, unknown>): string =>
    typeof h.similarity === "number" ? `${(h.similarity * 100).toFixed(1)}%` : "—";
  const simSize = (h: Record<string, unknown>): string => {
    const n = h.size;
    if (typeof n !== "number") return "—";
    if (n >= 1 << 30) return `${(n / (1 << 30)).toFixed(1)} GiB`;
    if (n >= 1 << 20) return `${(n / (1 << 20)).toFixed(1)} MiB`;
    if (n >= 1 << 10) return `${(n / (1 << 10)).toFixed(1)} KiB`;
    return `${n} B`;
  };
  const simWhen = (h: Record<string, unknown>): string => {
    const t = h.mtime;
    return typeof t === "number" ? new Date(t * 1000).toLocaleDateString() : "—";
  };

  async function toggleSimilar() {
    similarOpen = !similarOpen;
    if (!similarOpen || similarLoaded || similarLoading) return;
    similarLoading = true;
    similarError = "";
    try {
      similar = await similarItems(id, 10);
    } catch (e) {
      similar = null;
      similarError = "Similar items are unavailable for this item.";
    } finally {
      similarLoading = false;
      similarLoaded = true;
    }
  }

  const hitLabel = (h: Record<string, unknown>): string => {
    const t = h.title ?? h.filename ?? h.rel_path ?? h.id;
    return typeof t === "string" ? t : String(t ?? "");
  };
  const hitPath = (h: Record<string, unknown>): string => {
    const v = h.path ?? h.rel_path;
    return typeof v === "string" ? v : "";
  };

  async function copyCopyPath(path: string) {
    try {
      await copyText(path);
      copiedPath = path;
      clearTimeout(copiedTimer);
      copiedTimer = setTimeout(() => (copiedPath = null), 2000);
    } catch {
      copiedPath = "(clipboard blocked)";
      clearTimeout(copiedTimer);
      copiedTimer = setTimeout(() => (copiedPath = null), 2000);
    }
  }

  $effect(() => {
    error = "";
    item = null;
    copies = null;
    // P3-T9: reset the lazy Similar section for the newly-opened item.
    similar = null;
    similarOpen = false;
    similarLoaded = false;
    similarLoading = false;
    similarError = "";
    active = "card"; // reset to the typed card whenever a different item opens
    editError = "";
    // Custom-field DEFINITIONS for the edit form. Same best-effort fetch pattern
    // as KeyFactsCard; a failure just means no custom-field rows to edit.
    listCustomFields()
      .then((d) => (defs = d))
      .catch(() => (defs = []));
    getItem(id)
      .then((r) => (item = r))
      // RBAC (P6-T4): a 403/404 shows a friendly line, never a blank/raw dump.
      .catch((e) => (error = friendlyError(e)));
    // Copies are a separate, non-blocking fetch — a failure just hides the section.
    itemCopies(id)
      .then((r) => (copies = r))
      .catch(() => (copies = null));
    // Frecency (roadmap §5 P3): opening the detail view counts as one use for
    // the caller's personal ranking profile. Fire-and-forget: errors (older
    // backend, feature off, rate budget) are deliberately invisible.
    touchItem(id).catch(() => {});
  });

  function onKey(e: KeyboardEvent) {
    if (e.key === "Escape") onClose();
  }
</script>

<svelte:window onkeydown={onKey} />

<div class="fixed inset-0 z-50 overflow-y-auto">
  <!-- Backdrop: a real <button> so click-to-close carries no a11y warnings.
       FIXED, not absolute (roadmap §20): an absolute backdrop scrolls away with
       the container, so on a long details page clicking beside the panel after
       scrolling missed it — "click outside doesn't close" (live report). A
       fixed backdrop covers the viewport at every scroll offset. Text-selection
       drags that end outside the panel are naturally safe: a cross-element
       mousedown/mouseup pair fires click on the common ancestor, never here. -->
  <button
    type="button"
    class="fixed inset-0 cursor-default bg-black/50"
    aria-label="Close details"
    onclick={onClose}
  ></button>

  <div
    class="relative z-10 mx-auto mt-16 mb-8 w-full max-w-3xl rounded-2xl bg-white p-5 shadow-xl dark:bg-slate-900"
    role="dialog"
    aria-modal="true"
    aria-label="Item details"
  >
    {#if item}
      <div class="mb-3 border-b border-slate-200 pb-3 dark:border-slate-800">
        <Breadcrumbs
          libraryName={libName}
          {shareUrl}
          {shareUnc}
          {relPath}
          isFile={true}
          {nativePath}
          {containerPath}
          hideFileActions={!!itemShareUrl}
          onNavigate={navigate}
        />
        <!-- P10-T11/T12 unified network location: the item's own resolved
             share_url (agent hint > admin mapping > library share). Rendered ONLY
             when present; replaces the library-share "Open file" row above. -->
        {#if itemShareUrl}
          <div class="mt-2 flex flex-wrap items-center gap-2 text-xs">
            <a
              class="min-w-0 max-w-full truncate rounded border border-slate-300 px-2 py-1 font-mono text-slate-600 hover:border-[var(--accent)] hover:text-[var(--accent)] dark:border-slate-700 dark:text-slate-300"
              href={itemShareUrl}
              target="_blank"
              rel="noopener noreferrer"
              title="Open the file at its network location. Browsers may block smb:// / file:// links — use Copy if nothing happens."
              >{itemShareUrl}</a>
            <button
              type="button"
              class="rounded border border-slate-300 px-2 py-1 text-slate-600 hover:border-[var(--accent)] hover:text-[var(--accent)] dark:border-slate-700 dark:text-slate-300"
              onclick={() => copyShare(itemShareUrl)}>{shareCopied ? "Copied!" : "Copy"}</button>
            {#if shareSourceLabel}
              <span
                class="rounded-full bg-slate-200 px-2 py-0.5 font-medium text-slate-600 dark:bg-slate-800 dark:text-slate-400"
                title="Source of this network location">{shareSourceLabel}</span>
            {/if}
          </div>
        {/if}
      </div>
    {/if}

    {#if item}
      <!-- S12/P12 slice 1: the larger preview-tier thumbnail (lazy-generated on
           first request). Hides itself on miss (skipped type / not-yet-generated). -->
      <div class="mb-3 flex justify-center">
        <Thumb id={String(item.id)} tier="preview" size="max-h-64 w-auto" rounded="rounded-lg" />
      </div>
    {/if}

    <!-- P10-T10 agent identity + online status + inline Verify (freshness updates
         in place, no full transfer). -->
    {#if item && agentOwned}
      <div class="mb-3">
        <AgentStatusPanel itemId={id} />
      </div>
      <!-- P10-T6/T7: pull the file from the hosting agent to the server, then
           download it. Offline agents show a clear waiting state, never a spinner. -->
      <div class="mb-3 border-b border-slate-200 pb-3 dark:border-slate-800">
        <RetrievePanel itemId={id} filename={fileName} />
      </div>
    {/if}

    <div class="flex items-center gap-2 border-b border-slate-200 pb-3 dark:border-slate-800">
      <!-- Typed card tab first, Raw last. -->
      <button
        class="rounded-lg px-3 py-1 text-sm {active === 'card'
          ? 'bg-[var(--accent)] text-white'
          : 'text-slate-500'}"
        onclick={() => (active = "card")}>{cardTabLabel}</button>
      <button
        class="rounded-lg px-3 py-1 text-sm {active === 'raw'
          ? 'bg-[var(--accent)] text-white'
          : 'text-slate-500'}"
        onclick={() => (active = "raw")}>Raw</button>
      <!-- IN-T4: the ONLY metadata-writing surface for a single item. Only ever
           writes `user_metadata` + the editable columns — invariant 2 (extracted
           `metadata` belongs to scans) is a backend rule this UI never asks to
           break. -->
      <button
        class="rounded-lg px-3 py-1 text-sm {active === 'edit'
          ? 'bg-[var(--accent)] text-white'
          : 'text-slate-500'}"
        disabled={!item}
        onclick={startEdit}>Edit</button>
      <span class="grow"></span>
      <button
        class="rounded-lg border border-slate-300 px-3 py-1 text-sm dark:border-slate-700"
        onclick={onClose}>Close</button>
    </div>

    <div class="mt-4">
      {#if error}
        <p class="text-red-500">{error}</p>
      {:else if !item}
        <p class="text-slate-500">Loading…</p>
      {:else if active === "raw"}
        <RawView {item} />
      {:else if active === "edit"}
        <!-- Same typed controls the bulk bar uses (CustomFieldInput), same
             coercion rules (bulkEdit.coerceCustomValue) — one implementation, so
             the two surfaces cannot disagree about what a field accepts. -->
        <div class="flex flex-col gap-3">
          {#if editError}
            <p class="rounded-lg bg-red-100 px-3 py-2 text-sm text-red-800 dark:bg-red-950 dark:text-red-200">
              {editError}
            </p>
          {/if}

          <label class="text-xs text-slate-500" for="edit-title">Title</label>
          <input
            id="edit-title"
            class="-mt-2 rounded-lg border border-slate-300 bg-transparent px-3 py-2 text-sm outline-none focus:border-[var(--accent)] dark:border-slate-700"
            placeholder="(empty clears the title)"
            bind:value={eTitle} />

          <label class="text-xs text-slate-500" for="edit-year">Year</label>
          <!-- value/oninput, NOT bind:value: Svelte's number binding would
               rewrite `eYear` to a `number | null` behind its declared string
               type, breaking the empty-means-clear check in saveEdit(). -->
          <input
            id="edit-year"
            type="number"
            min="1"
            max="9999"
            step="1"
            class="-mt-2 w-32 rounded-lg border border-slate-300 bg-transparent px-3 py-2 text-sm outline-none focus:border-[var(--accent)] dark:border-slate-700"
            placeholder="(empty)"
            value={eYear}
            oninput={(e) => (eYear = e.currentTarget.value)} />

          <label class="text-xs text-slate-500" for="edit-tags">Tags</label>
          <div class="-mt-2 flex flex-wrap items-center gap-1">
            <input
              id="edit-tags"
              class="w-44 rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-sm outline-none focus:border-[var(--accent)] dark:border-slate-700"
              placeholder="add tag…"
              bind:value={eTagDraft}
              onkeydown={onTagKey}
              onblur={commitTagDraft} />
            {#each eTags as t (t)}
              <span class="inline-flex items-center gap-1 rounded-full bg-[var(--accent)]/15 px-2 py-1 text-xs text-[var(--accent)]">
                {t}
                <button
                  type="button"
                  class="rounded-full px-1 leading-none"
                  aria-label={`Remove tag ${t}`}
                  onclick={() => (eTags = eTags.filter((x) => x !== t))}>×</button>
              </span>
            {/each}
          </div>

          {#if editableFields.length}
            <div class="mt-1 border-t border-slate-200 pt-3 dark:border-slate-800">
              <p class="mb-2 text-xs text-slate-500">
                Custom fields — leaving one empty removes it from this item.
              </p>
              <div class="flex flex-col gap-2">
                {#each editableFields as d (d.id)}
                  <div class="flex flex-wrap items-center gap-2">
                    <label class="w-40 shrink-0 text-xs text-slate-500" for={`edit-cf-${d.id}`}
                      >{d.label}</label>
                    <CustomFieldInput
                      def={d}
                      id={`edit-cf-${d.id}`}
                      bind:value={eFields[d.name]} />
                  </div>
                {/each}
              </div>
            </div>
          {/if}

          <div class="mt-2 flex items-center gap-2">
            <button
              class="rounded-lg bg-[var(--accent)] px-4 py-2 text-sm text-white disabled:opacity-50"
              disabled={saving}
              onclick={saveEdit}>{saving ? "Saving…" : "Save changes"}</button>
            <button
              class="rounded-lg border border-slate-300 px-3 py-2 text-sm dark:border-slate-700"
              disabled={saving}
              onclick={() => { editError = ""; active = "card"; }}>Cancel</button>
            <span class="text-xs text-slate-500">Search results update shortly after saving.</span>
          </div>
        </div>
      {:else}
        {@const Card = CardComponent}
        <Card {item} />
      {/if}
    </div>

    <!-- Roadmap §5 P3 provenance: where the file was downloaded from (xattrs /
         Zone.Identifier read at extract time). Rendered only when present; the
         link is rel=noopener and never auto-fetched. -->
    {#if item && (originUrl || referrerUrl)}
      <div class="mt-5 border-t border-slate-200 pt-4 dark:border-slate-800">
        <h3 class="mb-2 text-sm font-semibold">Origin</h3>
        <dl class="grid grid-cols-[6rem_1fr] gap-x-3 gap-y-1 text-xs">
          {#if originUrl}
            <dt class="text-slate-500">Downloaded from</dt>
            <dd class="min-w-0 truncate">
              <a class="font-mono underline hover:text-[var(--accent)]" href={originUrl}
                target="_blank" rel="noopener noreferrer" title={originUrl}>{originUrl}</a>
            </dd>
          {/if}
          {#if referrerUrl}
            <dt class="text-slate-500">Referrer</dt>
            <dd class="min-w-0 truncate">
              <a class="font-mono underline hover:text-[var(--accent)]" href={referrerUrl}
                target="_blank" rel="noopener noreferrer" title={referrerUrl}>{referrerUrl}</a>
            </dd>
          {/if}
        </dl>
      </div>
    {/if}

    <!-- P3-T13 Archive contents: shown whenever this item's extracted metadata
         carries an ``archive`` fact (zip/tar member listing, index-only). -->
    {#if item}
      <ArchiveSection {item} />
    {/if}

    <!-- P3-T10 Copies section: shown whenever this item has duplicates (the group
         has more than one active member). Each row lists the owning library +
         path with a copy-path action (native_prefix-resolved, invariant 3). -->
    {#if copies && copies.count > 1}
      <div class="mt-5 border-t border-slate-200 pt-4 dark:border-slate-800">
        <h3 class="mb-2 text-sm font-semibold">
          {copies.count} copies
          <span class="ml-1 font-normal text-slate-500">
            ({copies.copies.length} other{copies.copies.length === 1 ? "" : "s"}
            {copies.capped ? ", showing first 50" : ""})
          </span>
        </h3>
        <ul class="flex flex-col gap-1">
          {#each copies.copies as c (c.id)}
            {@const path = c.native_path ?? c.path}
            <li class="flex items-center gap-2 text-xs">
              <span class="shrink-0 rounded bg-slate-200 px-2 py-0.5 dark:bg-slate-800"
                >{c.library_name ?? "?"}</span>
              <span class="min-w-0 flex-1 truncate font-mono" title={path}>{path}</span>
              <button
                type="button"
                class="shrink-0 rounded border border-slate-300 px-2 py-0.5 dark:border-slate-700"
                onclick={() => copyCopyPath(path)}>Copy path</button>
            </li>
          {/each}
        </ul>
        {#if copiedPath}
          <p class="mt-2 text-xs text-[var(--accent)]" role="status">Copied {copiedPath}</p>
        {/if}
      </div>
    {/if}

    <!-- P3-T9 Similar section (grid upgrade 2026-08-06): rendered only when
         semantic search is enabled; hits lazy-load on expand as a clickable
         thumbnail grid — a tile navigates this panel to that item. A 409
         (this item not embedded yet) degrades to an "unavailable" note. -->
    {#if item && semanticOn}
      <div class="mt-5 border-t border-slate-200 pt-4 dark:border-slate-800">
        <button
          type="button"
          class="text-sm font-semibold"
          aria-expanded={similarOpen}
          onclick={toggleSimilar}>Similar items {similarOpen ? "▲" : "▾"}</button>
        {#if similarOpen}
          <span class="ml-2 inline-flex overflow-hidden rounded border border-slate-300 text-[10px] dark:border-slate-700">
            <button type="button" class="px-1.5 py-0.5 {similarView === 'grid' ? 'bg-[var(--accent)] text-white' : 'text-slate-500'}"
              onclick={() => setSimilarView("grid")}>grid</button>
            <button type="button" class="px-1.5 py-0.5 {similarView === 'table' ? 'bg-[var(--accent)] text-white' : 'text-slate-500'}"
              onclick={() => setSimilarView("table")}>table</button>
          </span>
          <span class="ml-1 cursor-help text-[10px] text-slate-400"
            title="How this list is built: each embedded item carries a vector computed locally from its text (title/filename, tags, extracted body/OCR text). These are the nearest items by cosine similarity of those vectors — similar DESCRIBED CONTENT, not similar bytes (byte-identical copies are the Copies section). The % is the normalised similarity score the ranking sorted by: ~100% = near-duplicate text signal, ~80%+ = same topic/series.">ⓘ why these?</span>
        {/if}
        {#if similarOpen}
          <div class="mt-2">
            {#if similarLoading}
              <p class="text-xs text-slate-500">Loading…</p>
            {:else if similarError}
              <p class="text-xs text-slate-500">{similarError}</p>
            {:else if similar && similar.hits.length && similarView === "table"}
              <div class="overflow-x-auto">
                <table class="w-full text-xs">
                  <thead class="text-left text-slate-500">
                    <tr><th class="py-1 pr-2">Name</th><th class="pr-2">Similarity</th><th class="pr-2">Type</th><th class="pr-2">Size</th><th class="pr-2">Modified</th><th>Path</th></tr>
                  </thead>
                  <tbody class="divide-y divide-slate-100 dark:divide-slate-800">
                    {#each similar.hits as h (h.id)}
                      <tr class={onOpen ? "cursor-pointer hover:bg-slate-50 dark:hover:bg-slate-900" : ""}
                        onclick={() => onOpen?.(String(h.id))}>
                        <td class="max-w-[16rem] truncate py-1 pr-2" title={hitLabel(h)}>{hitLabel(h)}</td>
                        <td class="pr-2 font-mono">{simPct(h)}</td>
                        <td class="pr-2 text-slate-500">{h.file_group ?? h.file_category ?? "—"}</td>
                        <td class="pr-2 text-slate-500">{simSize(h)}</td>
                        <td class="pr-2 text-slate-500">{simWhen(h)}</td>
                        <td class="max-w-[18rem] truncate font-mono text-[10px] text-slate-400" title={hitPath(h)}>{hitPath(h)}</td>
                      </tr>
                    {/each}
                  </tbody>
                </table>
              </div>
            {:else if similar && similar.hits.length}
              <ul class="grid grid-cols-2 gap-2 sm:grid-cols-3">
                {#each similar.hits as h (h.id)}
                  <li>
                    <button
                      type="button"
                      class="w-full rounded-lg border border-slate-200 p-2 text-left hover:border-[var(--accent)] disabled:cursor-default disabled:hover:border-slate-200 dark:border-slate-800 dark:disabled:hover:border-slate-800"
                      disabled={!onOpen}
                      title={hitPath(h)}
                      onclick={() => onOpen?.(String(h.id))}>
                      <div class="flex h-20 items-center justify-center overflow-hidden">
                        <Thumb id={String(h.id)} tier="grid" size="max-h-20 w-auto" rounded="rounded" />
                      </div>
                      <p class="mt-1 truncate text-xs">{hitLabel(h)}
                        {#if typeof h.similarity === "number"}<span class="ml-1 text-[10px] text-slate-400">{simPct(h)}</span>{/if}</p>
                      <p class="truncate font-mono text-[10px] text-slate-400">{hitPath(h)}</p>
                    </button>
                  </li>
                {/each}
              </ul>
            {:else}
              <p class="text-xs text-slate-500">No similar items found.</p>
            {/if}
          </div>
        {/if}
      </div>
    {/if}
  </div>
</div>
