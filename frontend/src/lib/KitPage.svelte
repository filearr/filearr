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
    Checkbox,
    DataTable,
    Field,
    Input,
    Modal,
    Popover,
    Select,
    StateBlock,
    Switch,
    Tabs,
    Textarea,
    confirm,
    promptText,
    toast,
    toneForStatus,
    type SortState,
    type Tone,
  } from "./ui";
  import { contrastRatio } from "./color";
  import { theme } from "./theme.svelte";

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

  // accent-text contrast, read from the live computed tokens so the numbers
  // follow the theme toggle and the account accent. Keyed on theme.mode/accent:
  // App sets those and calls applyTheme() synchronously, so by the time this
  // effect runs the new custom properties are in place.
  type Contrast = { token: string; cls: string; ratio: number | null };
  let accentText = $state("");
  let contrasts = $state<Contrast[]>([]);
  $effect(() => {
    void theme.mode;
    void theme.accent;
    const css = getComputedStyle(document.documentElement);
    const read = (name: string) => css.getPropertyValue(name).trim().toLowerCase();
    const hex = (v: string) => /^#[0-9a-f]{6}$/.test(v);
    // A local, not the state: reading `accentText` back here would make this
    // effect depend on what it just wrote.
    const fg = read("--accent-text");
    accentText = fg;
    contrasts = [
      { token: "surface", cls: "bg-surface" },
      { token: "surface-2", cls: "bg-surface-2" },
      { token: "surface-3", cls: "bg-surface-3" },
    ].map(({ token, cls }) => {
      const bg = read(`--${token}`);
      // Without applyTheme (no JS ran yet) the token is a color-mix() fallback.
      return { token, cls, ratio: hex(fg) && hex(bg) ? contrastRatio(fg, bg) : null };
    });
  });

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
  let sizeLimit = $state("500");

  // Checkbox and Switch
  let notifyFailures = $state(true);
  let notifyDigest = $state(false);
  let selectAll = $state(false);
  let selectSome = $state(true);
  let watchFolders = $state(true);
  let readOnly = $state(false);
  let scanFailEmail = $state(true);

  // Modal: one dialog whose width follows the button that opened it.
  let modalOpen = $state(false);
  let modalSize = $state<"sm" | "md" | "lg" | "xl">("md");
  function openModal(size: typeof modalSize) {
    modalSize = size;
    modalOpen = true;
  }

  // Modal with the built-in dirty check: every user dismissal (Escape, outside
  // click, ×, Cancel via close()) asks before discarding edits; Save sets
  // `open = false` and is not asked. $state, so `dirty` re-derives when Save
  // moves the baseline.
  const savedEdit = $state({ name: "Movies", path: "/mnt/media/movies" });
  let editOpen = $state(false);
  // Off: dismissible={false} — Escape and outside clicks do nothing; × and
  // Cancel still close (through the dirty check).
  let editDismissible = $state(true);
  let editName = $state(savedEdit.name);
  let editPath = $state(savedEdit.path);
  const dirty = $derived(editName !== savedEdit.name || editPath !== savedEdit.path);
  function openEdit() {
    editName = savedEdit.name;
    editPath = savedEdit.path;
    editOpen = true;
  }
  // Focus target for initialFocus, via the Input's bindable element ref.
  let editPathInput = $state<HTMLInputElement | null>(null);
  function saveEdit() {
    savedEdit.name = editName;
    savedEdit.path = editPath;
    editOpen = false;
    // A "caller" close is never vetoed, so the save is final here.
    toast("Library saved", { tone: "ok" });
  }

  // Nested modals: a click inside the inner one is not an outside click on
  // the outer one.
  let outerOpen = $state(false);
  let innerOpen = $state(false);

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

  async function askDetails() {
    const ok = await confirm({
      title: "Remove library?",
      message: "Movies will be removed from Filearr.",
      details: ["Removes 1,204 indexed items", "Cancels 2 queued jobs", "Keeps the files on disk"],
      confirmLabel: "Remove library",
      tone: "danger",
    });
    toast(ok ? "Confirmed" : "Cancelled", { tone: ok ? "ok" : "info" });
  }

  async function askName() {
    const name = await promptText({
      title: "Name this search",
      label: "Name",
      validate: (v) => (v.length > 40 ? "Keep it under 40 characters." : undefined),
    });
    if (name === null) toast("Cancelled", { tone: "info" });
    else toast(`Saved as “${name}”`, { tone: "ok" });
  }

  // Tabs
  let activeTab = $state("files");

  // Popover
  let popoverOpen = $state(false);
  let onlyFailed = $state(false);
  let includeArchived = $state(false);

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
  let tableLoading = $state(false);
  let tableError = $state(false);
  let tableEmpty = $state(false);

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
    <h3 class="mt-4 mb-2 text-sm font-medium">accent-text contrast (current theme)</h3>
    <p class="mb-2 text-2xs text-muted">
      Computed per theme by <code>applyTheme</code> for 4.5:1 on the strictest surface; toggle the theme to compare.
      <code>--accent-text</code> is <code>{accentText || "unset"}</code>.
    </p>
    <ul class="grid gap-3 sm:grid-cols-3">
      {#each contrasts as c (c.token)}
        <li class="rounded-control border border-border p-3 {c.cls}">
          <span class="block text-sm font-medium text-accent-text">Accent text on {c.token}</span>
          <span class="text-2xs text-muted tabular-nums">
            {#if c.ratio === null}ratio unavailable (fallback token){:else}{c.ratio.toFixed(2)}:1 · {c.ratio >= 4.5
                ? "meets"
                : "fails"} AA{/if}
          </span>
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
    <p class="mb-3 text-sm text-muted">
      Short form: the kit controls wire themselves to the Field (id, hint/error, required). "Size limit" uses the
      explicit snippet form, as a custom control would.
    </p>
    <div class="grid gap-4 md:grid-cols-2">
      <Field label="Library name" required><Input bind:value={libraryName} /></Field>
      <Field label="Scan schedule" hint="Nightly scans start at 02:00 server time.">
        <Select bind:value={schedule}>
          <option value="hourly">Hourly</option>
          <option value="nightly">Nightly</option>
          <option value="weekly">Weekly</option>
          <option value="manual">Manual only</option>
        </Select>
      </Field>
      <Field label="Share path" error="Path must be absolute, e.g. /mnt/media/movies."><Input bind:value={scanPath} /></Field>
      <Field label="Size limit" hint="Custom control: Input plus a unit. Larger files are skipped during scans.">
        {#snippet children({ id, describedBy, invalid, required })}
          <div class="flex items-center gap-2">
            <Input {id} aria-describedby={describedBy} {invalid} {required} type="number" min="1" class="w-32" bind:value={sizeLimit} />
            <span class="text-sm text-muted">GB per file</span>
          </div>
        {/snippet}
      </Field>
      <Field label="Notes" hint="Optional. Markdown is not rendered.">
        <Textarea rows={3} placeholder="Who owns this share…" bind:value={notes} />
      </Field>
      <div class="flex flex-col gap-4">
        <Field label="Small input"><Input size="sm" placeholder="size=&quot;sm&quot;" /></Field>
        <Field label="Disabled input"><Input disabled value="Read-only on this deployment" /></Field>
      </div>
    </div>

    <h3 class="mt-6 mb-2 text-sm font-medium">Checkbox</h3>
    <div class="grid gap-3 md:grid-cols-2">
      <div class="flex flex-col gap-2">
        <Checkbox label="Email me when a scan fails" bind:checked={notifyFailures} />
        <Checkbox label="Weekly digest" bind:checked={notifyDigest} />
        <Checkbox
          label="Select all libraries"
          bind:checked={selectAll}
          bind:indeterminate={selectSome}
          description="Indeterminate: some libraries are selected."
        />
        <Checkbox label="Sync to cloud (not available on this plan)" disabled />
        <Checkbox label="Follow symlinks" description="Scans the target of each link. Can loop on misconfigured shares." />
      </div>
      <Field label="Notifications" hint="Sent to the address on your account.">
        <Checkbox label="Email me when a scheduled scan fails" bind:checked={scanFailEmail} />
      </Field>
    </div>

    <h3 class="mt-6 mb-2 text-sm font-medium">Switch</h3>
    <p class="mb-2 text-2xs text-muted">Space or Enter toggles a focused switch.</p>
    <div class="flex flex-col gap-2">
      <Switch label="Watch folders for changes" description="Picks up new files without waiting for a scan." bind:checked={watchFolders} />
      <Switch label="Read-only mode" bind:checked={readOnly} />
      <Switch label="Hardware transcoding (no GPU detected)" disabled />
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
      <Field label="Library name"><Input bind:value={libraryName} /></Field>
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

    <h3 class="mt-4 mb-2 text-sm font-medium">Unsaved changes</h3>
    <p class="mb-3 text-sm text-muted">
      <code>dirty=&#123;() =&gt; dirty&#125;</code>: edit a field, then press Escape, click outside, Close or Cancel to
      get the discard prompt; Save closes without asking. Focus starts on the second input. Switch off
      <code>dismissible</code> and Escape and outside clicks do nothing at all.
    </p>
    <div class="flex flex-wrap items-center gap-4">
      <Button variant="secondary" onclick={openEdit}>Edit with unsaved changes</Button>
      <Switch label="Dismissible (Escape, outside click)" bind:checked={editDismissible} />
    </div>
    <Modal
      bind:open={editOpen}
      title="Edit library"
      description="Leaving with unsaved edits asks first."
      dirty={() => dirty}
      dismissible={editDismissible}
      initialFocus={() => editPathInput}
    >
      <div class="flex flex-col gap-3">
        <Field label="Library name"><Input bind:value={editName} /></Field>
        <Field label="Share path" hint="Absolute path on the agent host.">
          <Input bind:value={editPath} bind:ref={editPathInput} />
        </Field>
      </div>
      <p class="mt-3 text-2xs text-muted">{dirty ? "Unsaved changes" : "No changes"}</p>
      {#snippet footer({ close })}
        <Button variant="secondary" onclick={close}>Cancel</Button>
        <Button onclick={saveEdit}>Save</Button>
      {/snippet}
    </Modal>

    <h3 class="mt-4 mb-2 text-sm font-medium">Nested</h3>
    <p class="mb-3 text-sm text-muted">
      Open the inner dialog and click inside it: the outer one stays open. A click on the backdrop closes the inner one.
    </p>
    <Button variant="secondary" onclick={() => (outerOpen = true)}>Open outer</Button>
    <Modal bind:open={outerOpen} title="Choose a folder">
      <p class="text-sm text-muted">The picker opens in a second dialog on top of this one.</p>
      <Button class="mt-3" variant="secondary" onclick={() => (innerOpen = true)}>Browse…</Button>
      {#snippet footer({ close })}
        <Button variant="secondary" onclick={close}>Close</Button>
      {/snippet}
    </Modal>
    <Modal bind:open={innerOpen} size="sm" title="Browse">
      <Field label="Folder"><Input value="/mnt/media" /></Field>
      {#snippet footer({ close })}
        <Button variant="secondary" onclick={close}>Cancel</Button>
        <Button onclick={() => (innerOpen = false)}>Select</Button>
      {/snippet}
    </Modal>
  </section>

  <section class="rounded-panel border border-border bg-surface-2 p-4" aria-labelledby="kit-confirm">
    <h2 id="kit-confirm" class="mb-3 text-lg font-semibold">Confirm dialog</h2>
    <p class="mb-3 text-sm text-muted">The danger dialog asks you to type <code>Movies</code>. The result shows as a toast.</p>
    <div class="flex flex-wrap gap-2">
      <Button variant="secondary" onclick={askDefault}>Rescan library…</Button>
      <Button variant="danger" onclick={askDanger}>Delete library…</Button>
      <Button variant="danger" onclick={askDetails}>Remove library (with details)…</Button>
    </div>

    <h3 class="mt-4 mb-2 text-sm font-medium">Prompt</h3>
    <p class="mb-3 text-sm text-muted">
      <code>promptText()</code> replaces <code>window.prompt</code>: Enter submits, an empty or over-long name shows its
      error under the field, Escape cancels.
    </p>
    <Button variant="secondary" onclick={askName}>Prompt…</Button>
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
    <p class="mb-3 text-sm text-muted">
      Left: the <code>label</code> shorthand (a kit Button with ▾). Right: a custom trigger snippet.
    </p>
    <div class="flex flex-wrap gap-2">
      <Popover label="Filters">
        <div class="flex w-64 max-w-full flex-col gap-2">
          <Checkbox label="Only failed scans" bind:checked={onlyFailed} />
          <Checkbox label="Include archived libraries" bind:checked={includeArchived} />
        </div>
      </Popover>
      <Popover bind:open={popoverOpen} ariaLabel="Filters">
        {#snippet trigger({ props })}<Button variant="secondary" {...props}>Filters</Button>{/snippet}
        <div class="flex w-64 max-w-full flex-col gap-3">
          <Field label="Type">
            <Select size="sm">
              <option>Any</option>
              <option>Video</option>
              <option>Audio</option>
              <option>Image</option>
            </Select>
          </Field>
          <div class="flex justify-end">
            <Button size="sm" onclick={() => (popoverOpen = false)}>Apply</Button>
          </div>
        </div>
      </Popover>
    </div>
  </section>

  <section class="rounded-panel border border-border bg-surface-2 p-4" aria-labelledby="kit-table">
    <h2 id="kit-table" class="mb-3 text-lg font-semibold">Data table</h2>
    <p class="mb-3 text-sm text-muted">
      Narrow the window below the <code>md</code> breakpoint to see the card fallback; the "Sort by" row above the
      cards appears there too. Error wins over loading, loading over empty; Loading keeps existing rows dimmed, so
      add Empty to see the loading block.
    </p>
    <div class="mb-3 flex flex-wrap gap-4">
      <Switch label="Loading" bind:checked={tableLoading} />
      <Switch label="Error" bind:checked={tableError} />
      <Switch label="Empty" bind:checked={tableEmpty} />
    </div>
    <DataTable
      caption="Recently scanned files"
      columns={[
        { key: "name", label: "Name", sortable: true },
        { key: "type", label: "Type" },
        { key: "size", label: "Size", align: "right", sortable: true, cell: sizeCell },
        { key: "status", label: "Status", cell: statusCell },
      ]}
      rows={tableEmpty ? [] : files}
      loading={tableLoading}
      error={tableError ? "Could not load files from nas-01." : null}
      onRetry={() => toast("Retried", { tone: "ok" })}
      emptyMessage="No files scanned yet."
      rowKey={(f) => f.id}
      bind:sort
      onRowClick={(f) => toast(f.name)}
    />
  </section>
</div>
