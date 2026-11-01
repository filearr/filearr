<script lang="ts">
  import { fmtBytes, fmtDate, fmtDateTime, fmtUtc } from "./format";
  import { COPY_FAILED, copyText } from "./clipboard";
  import { toast } from "./ui/toast.svelte";
  import * as safeStorage from "./safeStorage";
  import { safeHref } from "./pathlinks";
  import { semanticEnabled } from "./semanticProbe";
  import { cachedCustomFields } from "./configCache";
  import {
    type ItemPermissions,
    type PermissionAce,
    type PermissionPrincipal,
    errorMessage,
    friendlyError,
    getItem,
    getItemPermissions,
    itemCopies,
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
  import { cardComponent, cardLabel } from "./cards/registry";
  import ArchiveSection from "./ArchiveSection.svelte";
  import Thumb from "./Thumb.svelte";
  import { thumbable } from "./thumbPolicy";
  import RetrievePanel from "./RetrievePanel.svelte";
  import AgentStatusPanel from "./AgentStatusPanel.svelte";
  import { Alert, Badge, Button, Field, Input, Modal, ScrollRegion, StateBlock, confirm, toneForStatus } from "./ui";

  let {
    id,
    onClose,
    onOpen,
    onChanged,
  }: {
    id: string;
    onClose: () => void;
    // Optional: navigate this panel to another item (similar-items tiles).
    // Parents pass their selected-id setter; without it tiles are inert labels.
    onOpen?: (id: string) => void;
    // A17: called once per successful save with the PATCH response, so the
    // parent's row (search hit, browse entry) shows the new title/year/tags.
    onChanged?: (item: ItemRecord) => void;
  } = $props();

  let item = $state<ItemRecord | null>(null);
  let error = $state("");
  // Mount-style: the parent mounts this per item and unmounts it from onClose.
  let open = $state(true);
  // The breadcrumb header: focused on open, so a screen reader starts at the
  // item's location and Tab continues into the header's links.
  let headerEl = $state<HTMLElement | null>(null);

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
  // D9: only an allowed scheme / absolute path becomes a link; else plain text.
  const itemShareHref = $derived(safeHref(itemShareUrl));
  // Roadmap §5 P3 provenance: extracted download-source URLs (never user-editable).
  const extractedMeta = $derived(
    (item?.metadata as Record<string, unknown> | undefined) ?? {},
  );
  const WEB = { schemes: ["http", "https"] };
  const originUrl = $derived(safeHref(extractedMeta["origin_url"], WEB));
  const referrerUrl = $derived(safeHref(extractedMeta["referrer_url"], WEB));
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
    // FIX-5 plain-http-safe helper (the async clipboard API is unavailable over
    // http://<lan-ip>); it falls back to a textarea + execCommand copy.
    if (!(await copyText(text))) return toast(COPY_FAILED, { tone: "warn" });
    shareCopied = true;
    clearTimeout(shareCopiedTimer);
    shareCopiedTimer = setTimeout(() => (shareCopied = false), 1500);
  }

  // P4-T10: the typed per-media_type card is the FIRST tab; "Raw" is ALWAYS last.
  // The card component is resolved from the registry (a future/unregistered type
  // falls back to the generic key-facts card). Rendered via native Svelte-5
  // dynamic-component syntax below — no <svelte:component>.
  const mediaType = $derived(str("file_category") ?? "");
  const fileGroup = $derived(str("file_group"));
  // Ruling 5: keyed by backend category; spreadsheets are a document group.
  const CardComponent = $derived(cardComponent(mediaType, fileGroup));
  const cardTabLabel = $derived(cardLabel(mediaType, fileGroup));

  // A15 facts header. Reads `item`, which saveEdit replaces with the PATCH
  // response, so a saved title/year/tags change shows here at once.
  const factFilename = $derived(str("filename") ?? relPath.split("/").pop() ?? "");
  const factTitle = $derived.by(() => {
    const t = str("title")?.trim();
    return t && t !== factFilename ? t : "";
  });
  const factYear = $derived(typeof item?.year === "number" ? item.year : null);
  const factSize = $derived(typeof item?.size === "number" ? item.size : null);
  // GET /items/{id} sends `mtime` as an ISO string (search hits: epoch
  // seconds); format.ts reads both.
  const factMtime = $derived(
    typeof item?.mtime === "number" || (typeof item?.mtime === "string" && item.mtime)
      ? (item.mtime as number | string)
      : null,
  );
  const factTags = $derived(
    Array.isArray(item?.tags)
      ? (item.tags as unknown[]).filter((t): t is string => typeof t === "string" && t !== "")
      : [],
  );
  const factStatus = $derived(str("status"));
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
  // The form as seeded by startEdit(); the Modal's dirty check compares the
  // live values against it, so reverting an edit by hand reads as clean.
  let seeded = "";
  const editSnapshot = () => JSON.stringify([eTitle, eYear, eTags, eTagDraft, eFields]);
  const isDirty = () => active === "edit" && editSnapshot() !== seeded;
  // Leaving the edit form by any route other than the Modal's own dismissal
  // (breadcrumb, principal search, a Similar item, the Card/Raw tabs) asks too.
  async function guardDirty(): Promise<boolean> {
    return (
      !isDirty() ||
      confirm({
        title: "Discard changes?",
        message: "Your edits will be lost.",
        confirmLabel: "Discard",
        tone: "danger",
      })
    );
  }

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
    const fields: Record<string, string | boolean> = {};
    for (const d of editableFields) fields[d.name] = toRaw(userMeta[d.name]);
    eFields = fields;
    seeded = editSnapshot();
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
    // A16: another item may open mid-save; the PATCH still lands server-side,
    // but its response must not overwrite the newer item's panel.
    const mine = itemToken;
    let saved: ItemRecord | null = null;
    try {
      // The PATCH response IS the updated projection, so the panel refreshes
      // without a second GET (and without waiting on the async index sync).
      const r = await patchItem(id, patch);
      saved = r;
      if (mine === itemToken) {
        item = r;
        active = "card";
      }
    } catch (e) {
      if (mine === itemToken) editError = errorMessage(e, { verb: "edit this item" });
    } finally {
      if (mine === itemToken) saving = false;
    }
    // Outside the try: a throwing parent callback cannot turn a save that
    // landed into "edit failed". Told even if another item opened meanwhile —
    // the parent matches its row by the response's id.
    if (saved) onChanged?.(saved);
  }

  // P10-T3/T10: agent-hosted items (library owned by an agent) surface the
  // hosting agent's identity, online status, verify freshness, and an inline
  // Verify action via <AgentStatusPanel>. ``source_agent_id`` on the item record
  // is the cheap ownership gate; the panel fetches the live agent-status detail.
  const agentOwned = $derived(!!str("source_agent_id"));
  // P10-T6 retrieve: the download filename for the staged file.
  const fileName = $derived(str("filename") ?? relPath.split("/").pop() ?? "download");

  // Navigating a breadcrumb folder closes the modal and switches to browse.
  async function navigate(folderRelPath: string) {
    if (!(await guardDirty())) return;
    onClose();
    if (libId) gotoBrowse(libId, folderRelPath);
  }

  // P3-T10: the OTHER copies of this item. Always fetched on open; the Copies
  // section renders whenever the group has more than one member (count > 1).
  let copies = $state<CopiesResponse | null>(null);
  let perms = $state<ItemPermissions | null>(null);
  let permsShowInherited = $state(false);
  function principalLabel(p: PermissionPrincipal | null | undefined): string {
    if (!p) return "—";
    const id = p.canonical_id || p.id || p.source_identifier || "";
    const alias = id && perms?.aliases ? perms.aliases[id] : undefined;
    const name = p.display || p.name || alias;
    if (name && id && name !== id) return `${name}`;
    return name || id || "—";
  }
  function principalId(p: PermissionPrincipal | null | undefined): string {
    return p?.canonical_id || p?.id || p?.source_identifier || "";
  }
  const visibleAces = $derived(
    (perms?.aces ?? []).filter((a) => permsShowInherited || !a.inherited),
  );
  const inheritedCount = $derived((perms?.aces ?? []).filter((a) => a.inherited).length);
  function verbsLabel(a: PermissionAce): string {
    const v = a.verbs ?? [];
    if (!v.length) return a.raw_mask || "—";
    if (v.includes("full")) return "full control";
    return v.join(", ");
  }
  /** Deep link to the Search page scoped to one principal. */
  async function searchByPrincipal(value: string) {
    if (!(await guardDirty())) return;
    const qs = new URLSearchParams({ principal: value });
    window.location.hash = `#/search?${qs}`;
    onClose?.();
  }
  async function openSimilar(id: string) {
    if (!onOpen || !(await guardDirty())) return;
    onOpen(id);
  }
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
    (safeStorage.get("filearr.similarView") as "grid" | "table") || "grid",
  );
  function setSimilarView(v: "grid" | "table") {
    similarView = v;
    safeStorage.set("filearr.similarView", v);
  }
  const simPct = (h: Record<string, unknown>): string =>
    typeof h.similarity === "number" ? `${(h.similarity * 100).toFixed(1)}%` : "—";
  async function toggleSimilar() {
    similarOpen = !similarOpen;
    if (!similarOpen || similarLoaded || similarLoading) return;
    await loadSimilar();
  }

  async function loadSimilar() {
    similarLoading = true;
    similarError = "";
    const mine = itemToken;
    try {
      const r = await similarItems(id, 10);
      if (mine !== itemToken) return; // another item opened; its effect reset us
      similar = r;
    } catch (e) {
      if (mine !== itemToken) return;
      similar = null;
      similarError = "Similar items are unavailable for this item.";
    }
    similarLoading = false;
    similarLoaded = true;
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
    if (!(await copyText(path))) return toast(COPY_FAILED, { tone: "warn" });
    copiedPath = path;
    clearTimeout(copiedTimer);
    copiedTimer = setTimeout(() => (copiedPath = null), 2000);
  }

  // A16: bumped per opened item; every fetch the item-change effect starts (and
  // the lazy Similar fetch) checks it, so a fast second open cannot interleave
  // the first item's responses into the second item's panel.
  let itemToken = 0;
  // Bumped by the load error's Retry; read inside the effect so it re-runs.
  let reloadKey = $state(0);
  let touchedId: typeof id | null = null;

  $effect(() => {
    void reloadKey;
    const mine = ++itemToken;
    const live = () => mine === itemToken;
    const ac = new AbortController();
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
    saving = false; // a save for the previous item must not leave the spinner stuck
    // Custom-field DEFINITIONS for the edit form. Same best-effort fetch pattern
    // as KeyFactsCard; a failure just means no custom-field rows to edit.
    cachedCustomFields(ac.signal)
      .then((d) => live() && (defs = d))
      .catch(() => live() && (defs = []));
    getItem(id, ac.signal)
      .then((r) => live() && (item = r))
      // RBAC (P6-T4): a 403/404 shows a friendly line, never a blank/raw dump.
      .catch((e) => live() && (error = friendlyError(e)));
    // Permissions (2026-08-23): newest agent-collected ACL; non-blocking, a
    // failure just hides the section.
    perms = null;
    getItemPermissions(id, ac.signal)
      .then((r) => live() && (perms = r))
      .catch(() => live() && (perms = null));
    // Copies are a separate, non-blocking fetch — a failure just hides the section.
    itemCopies(id, ac.signal)
      .then((r) => live() && (copies = r))
      .catch(() => live() && (copies = null));
    // Frecency (roadmap §5 P3): opening the detail view counts as one use for
    // the caller's personal ranking profile. Fire-and-forget: errors (older
    // backend, feature off, rate budget) are deliberately invisible. Not
    // aborted: the open already happened, and it writes no panel state.
    // A Retry re-runs this effect for the same id; that is not another open.
    if (touchedId !== id) {
      touchedId = id;
      touchItem(id).catch(() => {});
    }
    // Runs before the next item's effect and on destroy.
    return () => {
      itemToken++;
      ac.abort();
    };
  });

  // Segment toggles (card/raw/edit tabs, similar grid/table): pressed = accent.
  const TAB_ON = "bg-accent text-accent-fg";
  const TAB_OFF = "text-muted";
</script>

<!-- Escape, outside click, ×, focus trap and focus return are the Modal's. A
     dirty Edit tab asks "Discard changes?" before any of them closes it. -->
<Modal bind:open title="Item details" size="xl" {onClose} dirty={isDirty} initialFocus={() => headerEl}>
  {#snippet children({ close })}
    <!-- Rendered (empty) while the item loads, so the open-time focus can land
         here before the fetch returns. -->
    <div bind:this={headerEl} tabindex="-1" class={item ? "mb-3 border-b border-border pb-3" : ""}>
      {#if item}
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
            {#if itemShareHref}
              <a
                class="min-w-0 max-w-full truncate rounded-control border border-border-strong px-2 py-1 font-mono text-muted hover:border-accent hover:text-accent-text"
                href={itemShareHref}
                target="_blank"
                rel="noopener noreferrer"
                title="Open the file at its network location. Browsers may block smb:// / file:// links — use Copy if nothing happens."
                >{itemShareUrl}</a>
            {:else}
              <span
                class="min-w-0 max-w-full truncate rounded-control border border-border-strong px-2 py-1 font-mono text-muted"
                title={itemShareUrl}>{itemShareUrl}</span>
            {/if}
            <Button size="sm" onclick={() => copyShare(itemShareUrl)}>{shareCopied ? "Copied!" : "Copy"}</Button>
            {#if shareSourceLabel}
              <span
                class="rounded-full bg-surface-3 px-2 py-0.5 font-medium text-muted"
                title="Source of this network location">{shareSourceLabel}</span>
            {/if}
          </div>
        {/if}
      {/if}
    </div>

    {#if item}
      <!-- A15 facts header: what the file is, before the per-type card. -->
      <div class="mb-3 space-y-1.5">
        <p class="min-w-0 break-words font-medium">{factFilename}
          {#if factYear != null}<span class="font-normal text-muted">({factYear})</span>{/if}</p>
        {#if factTitle}<p class="min-w-0 break-words text-sm">{factTitle}</p>{/if}
        <p class="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
          {#if factSize != null}<span class="tabular-nums">{fmtBytes(factSize)}</span>{/if}
          {#if factMtime != null}
            <span class="tabular-nums" title={fmtUtc(factMtime)}>Modified {fmtDateTime(factMtime)}</span>
          {/if}
          {#if factStatus && factStatus !== "active"}<Badge tone={toneForStatus(factStatus)}>{factStatus}</Badge>{/if}
          {#if mediaType}<Badge>{mediaType}</Badge>{/if}
          {#if fileGroup}<Badge>{fileGroup}</Badge>{/if}
        </p>
        {#if factTags.length}
          <ul class="flex flex-wrap gap-1" aria-label="Tags">
            {#each factTags as t (t)}<li><Badge>{t}</Badge></li>{/each}
          </ul>
        {/if}
      </div>
      <!-- S12/P12 slice 1: the larger preview-tier thumbnail (lazy-generated on
           first request), only for items the server can render (thumbPolicy);
           anything else has no preview box at all. -->
      {#if thumbable(mediaType, factFilename)}
        <div class="mb-3 flex justify-center">
          <Thumb id={String(item.id)} tier="preview" category={mediaType} filename={factFilename}
            size="h-64 w-full max-w-md" rounded="rounded-lg" fit="contain" />
        </div>
      {/if}
    {/if}

    <!-- P10-T10 agent identity + online status + inline Verify (freshness updates
         in place, no full transfer). -->
    {#if item && agentOwned}
      <div class="mb-3">
        <AgentStatusPanel itemId={id} />
      </div>
      <!-- P10-T6/T7: pull the file from the hosting agent to the server, then
           download it. Offline agents show a clear waiting state, never a spinner. -->
      <div class="mb-3 border-b border-border pb-3">
        <RetrievePanel itemId={id} filename={fileName} />
      </div>
    {/if}

    <div class="flex items-center gap-2 border-b border-border pb-3">
      <!-- Typed card tab first, Raw last. -->
      <button
        class="rounded-control px-3 py-1 text-sm {active === 'card' ? TAB_ON : TAB_OFF}"
        aria-pressed={active === "card"}
        onclick={async () => { if (await guardDirty()) active = "card"; }}>{cardTabLabel}</button>
      <button
        class="rounded-control px-3 py-1 text-sm {active === 'raw' ? TAB_ON : TAB_OFF}"
        aria-pressed={active === "raw"}
        onclick={async () => { if (await guardDirty()) active = "raw"; }}>Raw</button>
      <!-- IN-T4: the ONLY metadata-writing surface for a single item. Only ever
           writes `user_metadata` + the editable columns — invariant 2 (extracted
           `metadata` belongs to scans) is a backend rule this UI never asks to
           break. -->
      <button
        class="rounded-control px-3 py-1 text-sm disabled:opacity-50 {active === 'edit' ? TAB_ON : TAB_OFF}"
        aria-pressed={active === "edit"}
        disabled={!item}
        onclick={startEdit}>Edit</button>
      <span class="grow"></span>
      <Button size="sm" onclick={close}>Close</Button>
    </div>

    <div class="mt-4">
      {#if error}
        <StateBlock state="error" message={error} onRetry={() => reloadKey++} />
      {:else if !item}
        <StateBlock state="loading" />
      {:else if active === "raw"}
        <RawView {item} />
      {:else if active === "edit"}
        <!-- Same typed controls the bulk bar uses (CustomFieldInput), same
             coercion rules (bulkEdit.coerceCustomValue) — one implementation, so
             the two surfaces cannot disagree about what a field accepts. -->
        <div class="flex flex-col gap-3">
          <Field label="Title">
            <Input placeholder="(empty clears the title)" bind:value={eTitle} />
          </Field>

          <!-- value/oninput, NOT bind:value: Svelte's number binding would
               rewrite `eYear` to a `number | null` behind its declared string
               type, breaking the empty-means-clear check in saveEdit(). -->
          <Field label="Year" class="w-32">
            <Input
              type="number"
              min="1"
              max="9999"
              step="1"
              placeholder="(empty)"
              value={eYear}
              oninput={(e) => (eYear = e.currentTarget.value)} />
          </Field>

          <div class="flex flex-col gap-1">
            <Field label="Tags" class="w-44">
              <Input
                size="sm"
                placeholder="add tag…"
                bind:value={eTagDraft}
                onkeydown={onTagKey}
                onblur={commitTagDraft} />
            </Field>
            {#if eTags.length}
              <div class="flex flex-wrap items-center gap-1">
                {#each eTags as t (t)}
                  <span class="inline-flex items-center gap-1 rounded-full bg-accent/15 px-2 py-1 text-xs text-accent-text">
                    {t}
                    <button
                      type="button"
                      class="rounded-full px-1 leading-none"
                      aria-label={`Remove tag ${t}`}
                      onclick={() => (eTags = eTags.filter((x) => x !== t))}>×</button>
                  </span>
                {/each}
              </div>
            {/if}
          </div>

          {#if editableFields.length}
            <div class="mt-1 border-t border-border pt-3">
              <p class="mb-2 text-xs text-muted">
                Custom fields — leaving one empty removes it from this item.
              </p>
              <div class="flex flex-col gap-2">
                {#each editableFields as d (d.id)}
                  <!-- CustomFieldInput's kit controls read this Field's context. -->
                  <Field label={d.label}>
                    <CustomFieldInput def={d} bind:value={eFields[d.name]} />
                  </Field>
                {/each}
              </div>
            </div>
          {/if}

          <div class="mt-2 flex items-center gap-2">
            <Button variant="primary" disabled={saving} onclick={saveEdit}>{saving ? "Saving…" : "Save changes"}</Button>
            <Button
              disabled={saving}
              onclick={() => { editError = ""; active = "card"; }}>Cancel</Button>
            <span class="text-xs text-muted">Search results update shortly after saving.</span>
          </div>
          {#if editError}
            <Alert tone="danger" onDismiss={() => (editError = "")}>{editError}</Alert>
          {/if}
        </div>
      {:else}
        {@const Card = CardComponent}
        <Card {item} />
      {/if}
    </div>

    <!-- Permissions (2026-08-23): the newest agent-collected ACL snapshot (W7).
         Central-scanned items explain why there is none instead of hiding. -->
    {#if item && perms}
      <div class="mt-5 border-t border-border pt-4">
        <h3 class="mb-2 text-sm font-semibold">Permissions</h3>
        {#if !perms.available}
          <p class="text-xs text-muted">{perms.reason}</p>
        {:else}
          <dl class="grid grid-cols-[6rem_1fr] gap-x-3 gap-y-1 text-xs">
            <dt class="text-muted">Owner</dt>
            <dd class="min-w-0 truncate" title={principalId(perms.owner)}>
              {principalLabel(perms.owner)}
              {#if principalId(perms.owner)}
                <button type="button" class="ml-1 underline opacity-70 hover:opacity-100"
                  title="search everything this principal can read"
                  onclick={() => searchByPrincipal(principalId(perms!.owner))}>find all</button>
              {/if}
            </dd>
            {#if perms.group}
              <dt class="text-muted">Group</dt>
              <dd class="min-w-0 truncate" title={principalId(perms.group)}>{principalLabel(perms.group)}</dd>
            {/if}
            <dt class="text-muted">Readable by</dt>
            <dd>
              {#if perms.summary?.perm_exposure === "anonymous"}
                anyone — no sign-in needed (Everyone / POSIX other)
              {:else if perms.summary?.perm_exposure === "authenticated"}
                any signed-in account (Authenticated Users) — anonymous is denied
              {:else}
                named principals only
              {/if}
              {#if perms.summary?.layers?.share}
                <span class="opacity-70"> · effective through share {(perms.summary.share_names ?? []).join(", ") || "(SMB)"} = share AND file permissions</span>
              {/if}
            </dd>
            {#if perms.summary?.layers?.share}
              {@const ly = perms.summary.layers}
              <dt class="text-muted">Share vs file</dt>
              <dd class={ly.share && (ly.share.anonymous !== ly.local.anonymous || ly.share.authenticated !== ly.local.authenticated) ? "text-warn" : ""}>
                {#if ly.share && (ly.share.anonymous !== ly.local.anonymous || ly.share.authenticated !== ly.local.authenticated)}
                  they disagree —
                  share: {ly.share.anonymous ? "anyone" : ly.share.authenticated ? "signed-in accounts" : "named principals"};
                  file: {ly.local.anonymous ? "anyone" : ly.local.authenticated ? "signed-in accounts" : "named principals"}.
                  The more restrictive layer wins for access through the share.
                {:else}
                  they agree
                {/if}
              </dd>
            {/if}
            <dt class="text-muted">Collected</dt>
            <dd class="min-w-0 truncate">
              {perms.collected_at ? new Date(perms.collected_at).toLocaleString() : "—"}
              {#if perms.agent_name} · by agent {perms.agent_name}{/if}
              {#if perms.fidelity} · {perms.fidelity}{/if}
            </dd>
          </dl>
          {#if (perms.aces ?? []).length}
            <div class="mt-2 overflow-x-auto">
              <table class="w-full text-xs">
                <caption class="sr-only">Access control entries</caption>
                <thead class="text-left text-muted">
                  <tr>
                    <th scope="col" class="py-1 pr-2">Principal</th>
                    <th scope="col" class="py-1 pr-2">Access</th>
                    <th scope="col" class="py-1 pr-2">Rights</th>
                    <th scope="col" class="py-1 pr-2">Layer · scope</th>
                  </tr>
                </thead>
                <tbody class="divide-y divide-border">
                  {#each visibleAces as a, i (i)}
                    <tr class={a.type === "deny" ? "text-danger" : ""}>
                      <td class="py-1 pr-2">
                        <button type="button" class="text-left hover:underline" title={principalId(a.principal)}
                          onclick={() => searchByPrincipal(principalId(a.principal) || principalLabel(a.principal))}>
                          {principalLabel(a.principal)}
                        </button>
                      </td>
                      <td class="py-1 pr-2">{a.type}{#if a.inherited} <span class="opacity-60">(inherited)</span>{/if}</td>
                      <td class="py-1 pr-2">{verbsLabel(a)}</td>
                      <td class="py-1 pr-2 opacity-70">{a.source === "share" ? `share ${a.share ?? ""}`.trim() : "file"} · {a.scope ?? "this"}</td>
                    </tr>
                  {/each}
                </tbody>
              </table>
            </div>
            {#if inheritedCount}
              <button type="button" class="mt-1 text-xs underline opacity-70 hover:opacity-100"
                onclick={() => (permsShowInherited = !permsShowInherited)}>
                {permsShowInherited ? "hide" : "show"} {inheritedCount} inherited entr{inheritedCount === 1 ? "y" : "ies"}
              </button>
            {/if}
          {/if}
        {/if}
      </div>
    {/if}

    <!-- Roadmap §5 P3 provenance: where the file was downloaded from (xattrs /
         Zone.Identifier read at extract time). Rendered only when present; the
         link is rel=noopener and never auto-fetched. -->
    {#if item && (originUrl || referrerUrl)}
      <div class="mt-5 border-t border-border pt-4">
        <h3 class="mb-2 text-sm font-semibold">Origin</h3>
        <dl class="grid grid-cols-[6rem_1fr] gap-x-3 gap-y-1 text-xs">
          {#if originUrl}
            <dt class="text-muted">Downloaded from</dt>
            <dd class="min-w-0 truncate">
              <a class="font-mono underline hover:text-accent-text" href={originUrl}
                target="_blank" rel="noopener noreferrer" title={originUrl}>{originUrl}</a>
            </dd>
          {/if}
          {#if referrerUrl}
            <dt class="text-muted">Referrer</dt>
            <dd class="min-w-0 truncate">
              <a class="font-mono underline hover:text-accent-text" href={referrerUrl}
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
      <div class="mt-5 border-t border-border pt-4">
        <h3 class="mb-2 text-sm font-semibold">
          {copies.count} copies
          <span class="ml-1 font-normal text-muted">
            ({copies.copies.length} other{copies.copies.length === 1 ? "" : "s"}
            {copies.capped ? ", showing first 50" : ""})
          </span>
        </h3>
        <ul class="flex flex-col gap-1">
          {#each copies.copies as c (c.id)}
            {@const path = c.native_path ?? c.path}
            <li class="flex items-center gap-2 text-xs">
              <span class="shrink-0 rounded-control bg-surface-3 px-2 py-0.5"
                >{c.library_name ?? "?"}</span>
              <span class="min-w-0 flex-1 truncate font-mono" title={path}>{path}</span>
              <Button size="sm" class="shrink-0" onclick={() => copyCopyPath(path)}>Copy path</Button>
            </li>
          {/each}
        </ul>
        {#if copiedPath}
          <p class="mt-2 text-xs text-accent-text" role="status">Copied {copiedPath}</p>
        {/if}
      </div>
    {/if}

    <!-- P3-T9 Similar section (grid upgrade 2026-08-06): rendered only when
         semantic search is enabled; hits lazy-load on expand as a clickable
         thumbnail grid — a tile navigates this panel to that item. A 409
         (this item not embedded yet) degrades to an "unavailable" note. -->
    {#if item && semanticOn}
      <div class="mt-5 border-t border-border pt-4">
        <button
          type="button"
          class="text-sm font-semibold"
          aria-expanded={similarOpen}
          onclick={toggleSimilar}>Similar items {similarOpen ? "▲" : "▾"}</button>
        {#if similarOpen}
          <span class="ml-2 inline-flex overflow-hidden rounded-control border border-border-strong text-2xs">
            <button type="button" class="px-1.5 py-0.5 {similarView === 'grid' ? TAB_ON : TAB_OFF}"
              aria-pressed={similarView === "grid"}
              onclick={() => setSimilarView("grid")}>grid</button>
            <button type="button" class="px-1.5 py-0.5 {similarView === 'table' ? TAB_ON : TAB_OFF}"
              aria-pressed={similarView === "table"}
              onclick={() => setSimilarView("table")}>table</button>
          </span>
          <span class="ml-1 cursor-help text-2xs text-muted"
            title="How this list is built: each embedded item carries a vector computed locally from its text (title/filename, tags, extracted body/OCR text). These are the nearest items by cosine similarity of those vectors — similar DESCRIBED CONTENT, not similar bytes (byte-identical copies are the Copies section). The % is the normalised similarity score the ranking sorted by: ~100% = near-duplicate text signal, ~80%+ = same topic/series.">ⓘ why these?</span>
        {/if}
        {#if similarOpen}
          <div class="mt-2">
            {#if similarLoading}
              <StateBlock state="loading" compact />
            {:else if similarError}
              <StateBlock state="error" compact message={similarError} onRetry={loadSimilar} />
            {:else if similar && similar.hits.length && similarView === "table"}
              <!-- Wide and free of focusable cells, so the scroll container takes
                   focus itself (ScrollRegion) to stay keyboard-scrollable. -->
              <ScrollRegion label="Similar items table">
                <table class="w-full text-xs">
                  <caption class="sr-only">Similar items</caption>
                  <thead class="text-left text-muted">
                    <tr>
                      <th scope="col" class="py-1 pr-2">Name</th>
                      <th scope="col" class="pr-2">Similarity</th>
                      <th scope="col" class="pr-2">Type</th>
                      <th scope="col" class="pr-2">Size</th>
                      <th scope="col" class="pr-2">Modified</th>
                      <th scope="col">Path</th>
                    </tr>
                  </thead>
                  <tbody class="divide-y divide-border">
                    {#each similar.hits as h (h.id)}
                      <tr class={onOpen ? "cursor-pointer hover:bg-surface-3" : ""}
                        onclick={() => void openSimilar(String(h.id))}>
                        <td class="max-w-[16rem] truncate py-1 pr-2" title={hitLabel(h)}>{hitLabel(h)}</td>
                        <td class="pr-2 font-mono">{simPct(h)}</td>
                        <td class="pr-2 text-muted">{h.file_group ?? h.file_category ?? "—"}</td>
                        <td class="pr-2 text-muted">{typeof h.size === "number" ? fmtBytes(h.size) : "—"}</td>
                        <td class="pr-2 text-muted">{fmtDate(typeof h.mtime === "number" ? h.mtime : null)}</td>
                        <td class="max-w-[18rem] truncate font-mono text-2xs text-muted" title={hitPath(h)}>{hitPath(h)}</td>
                      </tr>
                    {/each}
                  </tbody>
                </table>
              </ScrollRegion>
            {:else if similar && similar.hits.length}
              <ul class="grid grid-cols-2 gap-2 sm:grid-cols-3">
                {#each similar.hits as h (h.id)}
                  <li>
                    <button
                      type="button"
                      class="w-full rounded-panel border border-border p-2 text-left hover:border-accent disabled:cursor-default disabled:hover:border-border"
                      disabled={!onOpen}
                      title={hitPath(h)}
                      onclick={() => void openSimilar(String(h.id))}>
                      <div class="flex h-20 items-center justify-center overflow-hidden">
                        <Thumb id={String(h.id)} tier="grid" category={typeof h.file_category === "string" ? h.file_category : null}
                          filename={typeof h.filename === "string" ? h.filename : null}
                          size="h-20 w-full" rounded="rounded" fit="contain" />
                      </div>
                      <p class="mt-1 truncate text-xs">{hitLabel(h)}
                        {#if typeof h.similarity === "number"}<span class="ml-1 text-2xs text-muted">{simPct(h)}</span>{/if}</p>
                      <p class="truncate font-mono text-2xs text-muted">{hitPath(h)}</p>
                    </button>
                  </li>
                {/each}
              </ul>
            {:else}
              <StateBlock state="empty" compact message="No similar items found." />
            {/if}
          </div>
        {/if}
      </div>
    {/if}
  {/snippet}
</Modal>
