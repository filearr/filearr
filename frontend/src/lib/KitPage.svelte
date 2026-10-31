<!--
  #/kit — development gallery for the UI kit (src/lib/ui). Every primitive and
  variant on one page so the kit can be checked in light and dark, by keyboard
  and at phone width. Reachable by URL only: neither the nav nor the footer
  links here.
-->
<script lang="ts">
  import {
    Alert,
    Badge,
    Button,
    DataTable,
    Field,
    Input,
    Modal,
    Popover,
    Select,
    StateBlock,
    Tabs,
    Textarea,
    confirm,
    toast,
    toneForStatus,
    type SortState,
    type Tone,
  } from "./ui";

  // Full literal class names (never assembled) so Tailwind's scanner sees them.
  const swatches: { token: string; cls: string }[] = [
    { token: "surface", cls: "bg-surface" },
    { token: "surface-2", cls: "bg-surface-2" },
    { token: "surface-3", cls: "bg-surface-3" },
    { token: "border", cls: "bg-border" },
    { token: "border-strong", cls: "bg-border-strong" },
    { token: "fg", cls: "bg-fg" },
    { token: "muted", cls: "bg-muted" },
    { token: "accent", cls: "bg-accent" },
    { token: "accent-fg", cls: "bg-accent-fg" },
    { token: "accent-text", cls: "bg-accent-text" },
    { token: "ok", cls: "bg-ok" },
    { token: "ok-bg", cls: "bg-ok-bg" },
    { token: "warn", cls: "bg-warn" },
    { token: "warn-bg", cls: "bg-warn-bg" },
    { token: "danger", cls: "bg-danger" },
    { token: "danger-bg", cls: "bg-danger-bg" },
    { token: "info", cls: "bg-info" },
    { token: "info-bg", cls: "bg-info-bg" },
  ];

  const variants = ["primary", "secondary", "danger", "ghost"] as const;
  const sizes = ["md", "sm"] as const;
  const tones: Tone[] = ["neutral", "ok", "warn", "danger", "info", "accent"];
  const statuses = ["completed", "running", "queued", "failed", "Healthy", "degraded", "disabled", "archived"];

  // Alerts
  let showDismissible = $state(true);

  // Form fields
  let libraryName = $state("Movies");
  let scanPath = $state("movies");
  let schedule = $state("nightly");
  let notes = $state("");

  // Modal: one dialog whose width follows the button that opened it.
  let modalOpen = $state(false);
  let modalSize = $state<"sm" | "md" | "lg" | "xl">("md");
  function openModal(size: typeof modalSize) {
    modalSize = size;
    modalOpen = true;
  }

  async function askDefault() {
    const ok = await confirm({
      title: "Rescan library?",
      message: "Movies will be re-indexed. Existing metadata is kept.",
      confirmLabel: "Rescan",
    });
    toast(ok ? "Confirmed" : "Cancelled", { tone: ok ? "ok" : "info" });
  }

  async function askDanger() {
    const ok = await confirm({
      title: "Delete library?",
      message: "This removes the library and all 12,408 catalogued items. Files on disk are not touched.",
      confirmLabel: "Delete library",
      tone: "danger",
      typedName: "Movies",
    });
    toast(ok ? "Confirmed" : "Cancelled", { tone: ok ? "ok" : "info" });
  }

  // Tabs
  let activeTab = $state("files");

  // Popover
  let popoverOpen = $state(false);

  // DataTable
  type MediaFile = { id: string; name: string; type: string; size: number; status: string };
  const files: MediaFile[] = [
    { id: "f1", name: "Blade Runner 2049 (2017).mkv", type: "Video", size: 41_873_920_512, status: "completed" },
    { id: "f2", name: "Arrival (2016).mp4", type: "Video", size: 8_204_115_968, status: "completed" },
    { id: "f3", name: "Kind of Blue — So What.flac", type: "Audio", size: 62_914_560, status: "queued" },
    { id: "f4", name: "IMG_2041.HEIC", type: "Image", size: 3_145_728, status: "running" },
    { id: "f5", name: "The Expanse S01E01.mkv", type: "Video", size: 2_791_728_742, status: "failed" },
    { id: "f6", name: "Dune — Frank Herbert.epub", type: "Book", size: 1_572_864, status: "completed" },
    { id: "f7", name: "holiday-2024.mov", type: "Video", size: 15_032_385_536, status: "degraded" },
    { id: "f8", name: "cover.jpg", type: "Image", size: 412_672, status: "pending" },
  ];
  let sort = $state<SortState>({ key: "name", dir: "asc" });

  function formatBytes(n: number): string {
    const units = ["B", "KB", "MB", "GB", "TB"];
    let v = n;
    let i = 0;
    while (v >= 1024 && i < units.length - 1) {
      v /= 1024;
      i++;
    }
    return `${v.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
  }
</script>

{#snippet sizeCell(f: MediaFile)}<span class="tabular-nums">{formatBytes(f.size)}</span>{/snippet}
{#snippet statusCell(f: MediaFile)}<Badge tone={toneForStatus(f.status)}>{f.status}</Badge>{/snippet}

<div class="flex flex-col gap-6 pb-8">
  <div>
    <h2 class="text-xl font-semibold">UI kit</h2>
    <p class="mt-1 max-w-prose text-sm text-muted">
      Development gallery for the shared components in <code>src/lib/ui</code>. This route is not linked from the
      navigation; use the header's Theme button to compare light and dark.
    </p>
  </div>

  <section class="rounded-panel border border-border bg-surface-2 p-4" aria-labelledby="kit-tokens">
    <h2 id="kit-tokens" class="mb-3 text-lg font-semibold">Colour tokens</h2>
    <ul class="grid grid-cols-3 gap-3 sm:grid-cols-6 lg:grid-cols-9">
      {#each swatches as s (s.token)}
        <li class="flex flex-col items-center gap-1">
          <span class="h-12 w-12 rounded-control border border-border {s.cls}"></span>
          <span class="text-2xs text-muted">{s.token}</span>
        </li>
      {/each}
    </ul>
  </section>

  <section class="rounded-panel border border-border bg-surface-2 p-4" aria-labelledby="kit-buttons">
    <h2 id="kit-buttons" class="mb-3 text-lg font-semibold">Buttons</h2>
    <div class="flex flex-col gap-3">
      {#each sizes as size (size)}
        <div class="flex flex-wrap items-center gap-2">
          <span class="w-8 text-2xs text-muted">{size}</span>
          {#each variants as variant (variant)}
            <Button {variant} {size}>{variant === "danger" ? "Delete" : variant === "primary" ? "Save changes" : variant === "ghost" ? "Skip" : "Cancel"}</Button>
          {/each}
        </div>
      {/each}
      <div class="flex flex-wrap items-center gap-2">
        <span class="w-8 text-2xs text-muted">off</span>
        {#each variants as variant (variant)}
          <Button {variant} disabled>Disabled {variant}</Button>
        {/each}
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <span class="w-8 text-2xs text-muted">link</span>
        <Button href="#/help">Open help</Button>
        <Button href="#/help" variant="secondary">Secondary link</Button>
        <Button href="#/help" variant="ghost" size="sm">Ghost link</Button>
        <Button href="#/help" variant="secondary" disabled>Disabled link</Button>
      </div>
    </div>
  </section>

  <section class="rounded-panel border border-border bg-surface-2 p-4" aria-labelledby="kit-badges">
    <h2 id="kit-badges" class="mb-3 text-lg font-semibold">Badges</h2>
    <div class="flex flex-wrap gap-2">
      {#each tones as tone (tone)}
        <Badge {tone}>{tone}</Badge>
      {/each}
    </div>
    <h3 class="mt-4 mb-2 text-sm font-medium">toneForStatus</h3>
    <ul class="flex flex-wrap gap-3">
      {#each statuses as status (status)}
        <li class="flex items-center gap-1 text-2xs text-muted">
          <code>"{status}"</code> → <Badge tone={toneForStatus(status)}>{status}</Badge>
        </li>
      {/each}
    </ul>
  </section>

  <section class="rounded-panel border border-border bg-surface-2 p-4" aria-labelledby="kit-alerts">
    <h2 id="kit-alerts" class="mb-3 text-lg font-semibold">Alerts</h2>
    <!-- live={false}: static gallery content, not news; on mount a stack of
         live regions would read every alert aloud. -->
    <div class="flex flex-col gap-3">
      <Alert tone="info" title="Scan scheduled" live={false}>The Movies library will rescan tonight at 02:00.</Alert>
      <Alert tone="ok" title="Library added" live={false}>Music is now being indexed — 4,210 files found so far.</Alert>
      <Alert tone="warn" title="Agent offline" live={false}>nas-02 has not checked in for 3 hours. Its libraries show stale data.</Alert>
      <Alert tone="danger" title="Scan failed" live={false}>Permission denied reading /mnt/media/tv. Check the share's credentials.</Alert>
      {#if showDismissible}
        <Alert tone="info" live={false} onDismiss={() => (showDismissible = false)}>
          Dismissible: a new Filearr release is available.
        </Alert>
      {:else}
        <div><Button variant="ghost" size="sm" onclick={() => (showDismissible = true)}>Show dismissed alert again</Button></div>
      {/if}
    </div>
  </section>

  <section class="rounded-panel border border-border bg-surface-2 p-4" aria-labelledby="kit-states">
    <h2 id="kit-states" class="mb-3 text-lg font-semibold">State blocks</h2>
    <div class="grid gap-3 md:grid-cols-3">
      <div class="rounded-control border border-border"><StateBlock state="loading" message="Loading libraries…" /></div>
      <div class="rounded-control border border-border"><StateBlock state="empty" message="No items match these filters." /></div>
      <div class="rounded-control border border-border">
        <StateBlock state="error" message="Could not reach the server." onRetry={() => toast("Retried", { tone: "ok" })} />
      </div>
    </div>
  </section>

  <section class="rounded-panel border border-border bg-surface-2 p-4" aria-labelledby="kit-forms">
    <h2 id="kit-forms" class="mb-3 text-lg font-semibold">Form fields</h2>
    <div class="grid gap-4 md:grid-cols-2">
      <Field label="Library name" hint="Shown in the sidebar and in search filters.">
        {#snippet children({ id, describedBy, invalid })}
          <Input {id} aria-describedby={describedBy} {invalid} bind:value={libraryName} />
        {/snippet}
      </Field>
      <Field label="Share path" error="Path must be absolute, e.g. /mnt/media/movies.">
        {#snippet children({ id, describedBy, invalid })}
          <Input {id} aria-describedby={describedBy} {invalid} bind:value={scanPath} />
        {/snippet}
      </Field>
      <Field label="Scan schedule" required>
        {#snippet children({ id, describedBy, invalid })}
          <Select {id} aria-describedby={describedBy} {invalid} required bind:value={schedule}>
            <option value="hourly">Hourly</option>
            <option value="nightly">Nightly</option>
            <option value="weekly">Weekly</option>
            <option value="manual">Manual only</option>
          </Select>
        {/snippet}
      </Field>
      <Field label="Notes" hint="Optional. Markdown is not rendered.">
        {#snippet children({ id, describedBy, invalid })}
          <Textarea {id} aria-describedby={describedBy} {invalid} rows={3} placeholder="Who owns this share…" bind:value={notes} />
        {/snippet}
      </Field>
      <Field label="Small input">
        {#snippet children({ id, describedBy, invalid })}
          <Input {id} aria-describedby={describedBy} {invalid} size="sm" placeholder="size=&quot;sm&quot;" />
        {/snippet}
      </Field>
      <Field label="Disabled input">
        {#snippet children({ id, describedBy, invalid })}
          <Input {id} aria-describedby={describedBy} {invalid} disabled value="Read-only on this deployment" />
        {/snippet}
      </Field>
    </div>
  </section>

  <section class="rounded-panel border border-border bg-surface-2 p-4" aria-labelledby="kit-modal">
    <h2 id="kit-modal" class="mb-3 text-lg font-semibold">Modal</h2>
    <p class="mb-3 text-sm text-muted">Check Escape, outside click and that focus returns to the button that opened it.</p>
    <div class="flex flex-wrap gap-2">
      <Button variant="secondary" onclick={() => openModal("sm")}>Open sm</Button>
      <Button variant="secondary" onclick={() => openModal("md")}>Open md</Button>
      <Button variant="secondary" onclick={() => openModal("lg")}>Open lg</Button>
      <Button variant="secondary" onclick={() => openModal("xl")}>Open xl</Button>
    </div>
    <Modal
      bind:open={modalOpen}
      size={modalSize}
      title="Edit library"
      description="Changes apply to the next scan."
      onClose={() => toast("Modal dismissed", { tone: "info" })}
    >
      <Field label="Library name">
        {#snippet children({ id, describedBy, invalid })}
          <Input {id} aria-describedby={describedBy} {invalid} bind:value={libraryName} />
        {/snippet}
      </Field>
      <p class="mt-3 text-sm text-muted">This dialog is size <code>{modalSize}</code>.</p>
      {#snippet footer()}
        <Button variant="secondary" onclick={() => (modalOpen = false)}>Cancel</Button>
        <Button
          onclick={() => {
            modalOpen = false;
            toast("Saved", { tone: "ok" });
          }}>Save</Button
        >
      {/snippet}
    </Modal>
  </section>

  <section class="rounded-panel border border-border bg-surface-2 p-4" aria-labelledby="kit-confirm">
    <h2 id="kit-confirm" class="mb-3 text-lg font-semibold">Confirm dialog</h2>
    <p class="mb-3 text-sm text-muted">The danger dialog asks you to type <code>Movies</code>. The result shows as a toast.</p>
    <div class="flex flex-wrap gap-2">
      <Button variant="secondary" onclick={askDefault}>Rescan library…</Button>
      <Button variant="danger" onclick={askDanger}>Delete library…</Button>
    </div>
  </section>

  <section class="rounded-panel border border-border bg-surface-2 p-4" aria-labelledby="kit-toasts">
    <h2 id="kit-toasts" class="mb-3 text-lg font-semibold">Toasts</h2>
    <p class="mb-3 text-sm text-muted">At most three show at once. Danger toasts persist until dismissed.</p>
    <div class="flex flex-wrap gap-2">
      <Button variant="secondary" onclick={() => toast("Export queued — you will be notified when it is ready.")}>Info</Button>
      <Button variant="secondary" onclick={() => toast("Library saved.", { tone: "ok" })}>Ok</Button>
      <Button variant="secondary" onclick={() => toast("3 files were skipped: unsupported format.", { tone: "warn" })}>Warn</Button>
      <Button
        variant="secondary"
        onclick={() =>
          toast("Tag removed from 24 items.", {
            tone: "ok",
            actionLabel: "Undo",
            onAction: () => toast("Undone", { tone: "info" }),
          })}>With action</Button
      >
      <Button variant="danger" onclick={() => toast("Scan failed: share unreachable.", { tone: "danger" })}>Danger (persists)</Button>
    </div>
  </section>

  <section class="rounded-panel border border-border bg-surface-2 p-4" aria-labelledby="kit-tabs">
    <h2 id="kit-tabs" class="mb-3 text-lg font-semibold">Tabs</h2>
    <Tabs
      bind:value={activeTab}
      ariaLabel="Item sections"
      tabs={[
        { value: "files", label: "Files", badge: 12 },
        { value: "metadata", label: "Metadata" },
        { value: "history", label: "History" },
      ]}
    >
      {#snippet children(active)}
        <p class="pt-3 text-sm text-muted">
          {#if active === "files"}12 files in this item. Arrow keys move between tabs.
          {:else if active === "metadata"}Title, year and tags pulled from the last scan.
          {:else}Scanned 3 times; last change 2 days ago.{/if}
        </p>
      {/snippet}
    </Tabs>
  </section>

  <section class="rounded-panel border border-border bg-surface-2 p-4" aria-labelledby="kit-popover">
    <h2 id="kit-popover" class="mb-3 text-lg font-semibold">Popover</h2>
    <Popover bind:open={popoverOpen} ariaLabel="Filters">
      {#snippet trigger({ props })}<Button variant="secondary" {...props}>Filters</Button>{/snippet}
      <div class="flex w-64 max-w-full flex-col gap-3">
        <Field label="Type">
          {#snippet children({ id, describedBy, invalid })}
            <Select {id} aria-describedby={describedBy} {invalid} size="sm">
              <option>Any</option>
              <option>Video</option>
              <option>Audio</option>
              <option>Image</option>
            </Select>
          {/snippet}
        </Field>
        <div class="flex justify-end">
          <Button size="sm" onclick={() => (popoverOpen = false)}>Apply</Button>
        </div>
      </div>
    </Popover>
  </section>

  <section class="rounded-panel border border-border bg-surface-2 p-4" aria-labelledby="kit-table">
    <h2 id="kit-table" class="mb-3 text-lg font-semibold">Data table</h2>
    <p class="mb-3 text-sm text-muted">Narrow the window below the <code>md</code> breakpoint to see the card fallback.</p>
    <DataTable
      caption="Recently scanned files"
      columns={[
        { key: "name", label: "Name", sortable: true },
        { key: "type", label: "Type" },
        { key: "size", label: "Size", align: "right", sortable: true, cell: sizeCell },
        { key: "status", label: "Status", cell: statusCell },
      ]}
      rows={files}
      rowKey={(f) => f.id}
      bind:sort
      onRowClick={(f) => toast(f.name)}
    />
  </section>
</div>
