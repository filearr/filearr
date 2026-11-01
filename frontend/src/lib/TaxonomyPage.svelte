<script lang="ts">
  // W8 — the file-extension similarity taxonomy EDITOR. A dedicated #/taxonomy
  // route reachable from the Libraries page. Shows the category -> group -> extension
  // tree and supports full CRUD: create/rename/delete categories (with an
  // extractor), create/rename/delete/REPARENT groups, and add/remove/MOVE
  // extensions (the add-to-group endpoint upserts, so adding an ext that already
  // lives elsewhere reparents it and reports where it moved from).
  //
  // Mutations are admin-scoped server-side; non-admins get a read-only view. Every
  // mutation echoes the bumped schema ``version`` (shown in the header). 409
  // (delete category with groups) and 422 (bad ext charset) surface inline.
  import { onMount } from "svelte";
  import {
    isAdminPrincipal,
    getTaxonomy,
    createTaxonomyCategory,
    updateTaxonomyCategory,
    deleteTaxonomyCategory,
    createTaxonomyGroup,
    updateTaxonomyGroup,
    deleteTaxonomyGroup,
    addTaxonomyExtension,
    deleteTaxonomyExtension,
    syncTaxonomySeed,
    reclassifyExtensions,
    TAXONOMY_EXTRACTORS,
    type TaxonomyNode,
    type AuthPrincipal,
    errorMessage,
  } from "./api";
  import { intOrNull } from "./numInput";
  import { Alert, Badge, Button, Field, Input, Modal, Select, StateBlock, confirm } from "./ui";

  let { me = null, authDisabled = false }: { me?: AuthPrincipal | null; authDisabled?: boolean } =
    $props();
  // Auth off (dev) => unrestricted API, so treat as admin; else require the role.
  const isAdmin = $derived(authDisabled || isAdminPrincipal(me));

  let tree = $state<TaxonomyNode[]>([]);
  let version = $state<number | null>(null);
  let loadError = $state(""); // replaces the tree with a retry block
  let loading = $state(true);
  // Per-origin action errors, each shown under the control that raised it.
  let seedError = $state("");
  let reclassError = $state("");
  let categoryError = $state<{ key: string; message: string } | null>(null);
  let groupError = $state<{ key: string; message: string } | null>(null);

  // Per-group extension add/remove: input text + inline error / success notice.
  let extInput = $state<Record<string, string>>({});
  let extError = $state<Record<string, string>>({});
  let extNotice = $state<Record<string, string>>({});

  // Maintenance actions: adopt a widened built-in seed / apply the current
  // taxonomy to already-catalogued items (no rescan). Result shown inline.
  let seedBusy = $state(false);
  let reclassBusy = $state(false);
  let actionNotice = $state("");

  async function syncSeed() {
    if (seedBusy) return;
    seedBusy = true; seedError = ""; actionNotice = "";
    try {
      const r = await syncTaxonomySeed();
      actionNotice = r.added_count
        ? `Seed sync adopted ${r.added_count} new extension(s)` +
          (r.skipped?.length ? ` (${r.skipped.length} left where you placed them)` : "") +
          " — run “Reclassify items” to apply to the existing catalogue."
        : "Seed sync: nothing new — this taxonomy already covers the shipped defaults.";
      await refresh();
    } catch (e) {
      seedError = errorMessage(e, { verb: "edit the taxonomy" });
    } finally {
      seedBusy = false;
    }
  }

  async function reclassify() {
    if (reclassBusy) return;
    reclassBusy = true; reclassError = ""; actionNotice = "";
    try {
      const r = await reclassifyExtensions();
      actionNotice = r.changed
        ? `Reclassified ${r.changed.toLocaleString()} item(s); their search docs are re-syncing.`
        : "Reclassify: every item already matches the current taxonomy.";
    } catch (e) {
      reclassError = errorMessage(e, { verb: "edit the taxonomy" });
    } finally {
      reclassBusy = false;
    }
  }

  async function refresh() {
    loadError = "";
    try {
      const t = await getTaxonomy();
      tree = t.tree;
      version = t.version;
    } catch (e) {
      loadError = errorMessage(e);
    } finally {
      loading = false;
    }
  }

  onMount(refresh);

  const allCategoryKeys = $derived(tree.map((n) => n.category.key));

  // ---- category / group dialog ---------------------------------------------
  type Dialog =
    | { kind: "category"; mode: "create" | "edit"; key: string; label: string; description: string; extractor: string; sortOrder: number | null }
    | { kind: "group"; mode: "create" | "edit"; key: string; label: string; description: string; categoryKey: string; sortOrder: number | null };
  let dialog = $state<Dialog | null>(null);
  let dialogError = $state("");
  let dialogBusy = $state(false);
  // The dialog's values as opened, for its "Discard changes?" check.
  let dialogInitial = "";
  const dialogDirty = $derived(!!dialog && JSON.stringify(dialog) !== dialogInitial);

  function openDialog(d: Dialog) {
    dialogError = "";
    dialogInitial = JSON.stringify(d);
    dialog = d;
  }
  function newCategory() {
    openDialog({ kind: "category", mode: "create", key: "", label: "", description: "", extractor: "none", sortOrder: null });
  }
  function editCategory(n: TaxonomyNode) {
    openDialog({
      kind: "category", mode: "edit",
      key: n.category.key, label: n.category.label, description: n.category.description ?? "",
      extractor: n.category.extractor || "none", sortOrder: n.category.sort_order ?? null,
    });
  }
  function newGroup(categoryKey: string) {
    openDialog({ kind: "group", mode: "create", key: "", label: "", description: "", categoryKey, sortOrder: null });
  }
  function editGroup(categoryKey: string, g: TaxonomyNode["groups"][number]) {
    openDialog({
      kind: "group", mode: "edit",
      key: g.key, label: g.label, description: g.description ?? "",
      categoryKey, sortOrder: g.sort_order ?? null,
    });
  }

  // bind:value on the type="number" input writes a number, or null when
  // emptied. Blank = auto (undefined); the backend wants an integer.
  function sortNum(v: number | null): number | undefined {
    return intOrNull(v) ?? undefined;
  }

  async function saveDialog() {
    if (!dialog) return;
    if (dialog.mode === "create" && !dialog.key.trim()) {
      dialogError = "Key is required.";
      return;
    }
    if (!dialog.label.trim()) {
      dialogError = "Label is required.";
      return;
    }
    if (dialog.sortOrder !== null && intOrNull(dialog.sortOrder) === null) {
      dialogError = "Sort order must be a whole number, or blank for auto.";
      return;
    }
    dialogBusy = true;
    dialogError = "";
    try {
      if (dialog.kind === "category") {
        if (dialog.mode === "create") {
          await createTaxonomyCategory({
            key: dialog.key.trim(),
            label: dialog.label.trim(),
            description: dialog.description.trim() || undefined,
            extractor: dialog.extractor,
            sort_order: sortNum(dialog.sortOrder),
          });
        } else {
          await updateTaxonomyCategory(dialog.key, {
            label: dialog.label.trim(),
            description: dialog.description.trim(),
            extractor: dialog.extractor,
            ...(sortNum(dialog.sortOrder) != null ? { sort_order: sortNum(dialog.sortOrder) } : {}),
          });
        }
      } else {
        if (dialog.mode === "create") {
          await createTaxonomyGroup({
            key: dialog.key.trim(),
            label: dialog.label.trim(),
            description: dialog.description.trim() || undefined,
            category_key: dialog.categoryKey,
            sort_order: sortNum(dialog.sortOrder),
          });
        } else {
          // A changed category_key REPARENTS the group under a new category.
          await updateTaxonomyGroup(dialog.key, {
            label: dialog.label.trim(),
            description: dialog.description.trim(),
            category_key: dialog.categoryKey,
            ...(sortNum(dialog.sortOrder) != null ? { sort_order: sortNum(dialog.sortOrder) } : {}),
          });
        }
      }
      dialog = null;
      await refresh();
    } catch (e) {
      // 409 (dup key / delete-blocked) or 422 (bad key/extractor) inline.
      dialogError = errorMessage(e, { verb: "edit the taxonomy" });
    } finally {
      dialogBusy = false;
    }
  }

  async function removeCategory(n: TaxonomyNode) {
    const ok = await confirm({
      title: `Delete category "${n.category.label}"?`,
      confirmLabel: "Delete",
      tone: "danger",
    });
    if (!ok) return;
    categoryError = null;
    try {
      await deleteTaxonomyCategory(n.category.key);
      await refresh();
    } catch (e) {
      // 409 when the category still has groups — surface the server's message.
      categoryError = { key: n.category.key, message: errorMessage(e, { verb: "edit the taxonomy" }) };
    }
  }

  async function removeGroup(g: TaxonomyNode["groups"][number]) {
    const ok = await confirm({
      title: `Delete group "${g.label}"?`,
      message: `Its ${g.extensions.length} extension(s) will be removed from the taxonomy.`,
      confirmLabel: "Delete",
      tone: "danger",
    });
    if (!ok) return;
    groupError = null;
    try {
      await deleteTaxonomyGroup(g.key);
      await refresh();
    } catch (e) {
      groupError = { key: g.key, message: errorMessage(e, { verb: "edit the taxonomy" }) };
    }
  }

  // ---- extensions -----------------------------------------------------------
  async function addExt(groupKey: string) {
    const raw = (extInput[groupKey] ?? "").trim().toLowerCase().replace(/^\./, "");
    extError = { ...extError, [groupKey]: "" };
    extNotice = { ...extNotice, [groupKey]: "" };
    if (!raw) return;
    try {
      const r = await addTaxonomyExtension(groupKey, raw);
      extInput = { ...extInput, [groupKey]: "" };
      // Upsert semantics: report a reparent so the move is visible.
      if (r.previous_group && r.previous_group !== groupKey) {
        extNotice = { ...extNotice, [groupKey]: `Moved "${raw}" from ${r.previous_group}` };
      }
      await refresh();
    } catch (e) {
      // 422 on a bad ext (must match ^[a-z0-9_+-]{1,32}$) surfaces here.
      extError = { ...extError, [groupKey]: errorMessage(e, { verb: "edit the taxonomy" }) };
    }
  }

  async function removeExt(groupKey: string, ext: string) {
    extError = { ...extError, [groupKey]: "" };
    extNotice = { ...extNotice, [groupKey]: "" };
    try {
      await deleteTaxonomyExtension(ext);
      await refresh();
    } catch (e) {
      extError = { ...extError, [groupKey]: errorMessage(e, { verb: "edit the taxonomy" }) };
    }
  }

  function goBack() {
    location.hash = "#/libraries";
  }
</script>

<div class="mt-4">
  <div class="flex flex-wrap items-center gap-3">
    <Button size="sm" onclick={goBack}>← Admin</Button>
    <h2 class="text-lg font-semibold">File taxonomy</h2>
    {#if version != null}
      <span title="Schema version (bumps on every change)"><Badge>v{version}</Badge></span>
    {/if}
    <span class="text-xs text-muted">category → group → extensions</span>
    <div class="grow"></div>
    <Button size="sm" onclick={refresh}>Refresh</Button>
    {#if isAdmin}
      <Button size="sm"
        title="Adopt extensions the shipped built-in seed has gained since this database was set up. Add-only: anything you moved or removed stays exactly where you put it."
        disabled={seedBusy} onclick={syncSeed}>{seedBusy ? "syncing…" : "Sync built-in seed"}</Button>
      <Button size="sm"
        title="Apply the current taxonomy to items already in the catalogue (updates file category/group + search docs in place — no rescan)."
        disabled={reclassBusy} onclick={reclassify}>{reclassBusy ? "reclassifying…" : "Reclassify items"}</Button>
      <Button size="sm" variant="primary" onclick={newCategory}>New category</Button>
    {/if}
  </div>
  {#if seedError}
    <Alert tone="danger" class="mt-2" onDismiss={() => (seedError = "")}>{seedError}</Alert>
  {/if}
  {#if reclassError}
    <Alert tone="danger" class="mt-2" onDismiss={() => (reclassError = "")}>{reclassError}</Alert>
  {/if}
  {#if actionNotice}
    <Alert tone="ok" class="mt-2" onDismiss={() => (actionNotice = "")}>{actionNotice}</Alert>
  {/if}

  {#if !isAdmin}
    <Alert tone="warn" live={false} class="mt-2">
      Read-only — editing the taxonomy requires an admin account.
    </Alert>
  {/if}

  {#if loadError}
    <StateBlock state="error" message={loadError} onRetry={refresh} />
  {:else if loading}
    <StateBlock state="loading" />
  {:else if tree.length === 0}
    <StateBlock state="empty"
      message={isAdmin
        ? "No categories yet. Use “New category” to start building the taxonomy."
        : "No categories yet. An admin can build the taxonomy here."} />
  {:else}
    <div class="mt-4 flex flex-col gap-4">
      {#each tree as node (node.category.key)}
        {@const cat = node.category}
        <div class="rounded-panel border border-border">
          <!-- Category header -->
          <div class="border-b border-border px-4 py-3">
            <div class="flex flex-wrap items-center gap-2">
              <span class="text-base font-semibold">{cat.label}</span>
              <Badge class="font-mono">{cat.key}</Badge>
              <span title="extractor family"><Badge tone="accent">{cat.extractor || "none"}</Badge></span>
              {#if cat.is_builtin}<Badge>built-in</Badge>{/if}
              <span class="text-2xs text-muted">sort {cat.sort_order}</span>
              {#if cat.description}<span class="hidden text-xs text-muted sm:inline">— {cat.description}</span>{/if}
              <div class="grow"></div>
              {#if isAdmin}
                <Button size="sm" variant="ghost" onclick={() => newGroup(cat.key)}>+ group</Button>
                <Button size="sm" variant="ghost" onclick={() => editCategory(node)}>edit</Button>
                <Button size="sm" variant="danger" onclick={() => removeCategory(node)}>delete</Button>
              {/if}
            </div>
            {#if categoryError?.key === cat.key}
              <Alert tone="danger" class="mt-2" onDismiss={() => (categoryError = null)}>{categoryError.message}</Alert>
            {/if}
          </div>

          <!-- Groups -->
          {#if node.groups.length === 0}
            <p class="px-4 py-3 text-xs text-muted">No groups in this category.{#if isAdmin} Use “+ group” to add one.{/if}</p>
          {:else}
            <div class="divide-y divide-border">
              {#each node.groups as g (g.key)}
                <div class="px-4 py-3">
                  <div class="flex flex-wrap items-center gap-2">
                    <span class="font-medium">{g.label}</span>
                    <Badge class="font-mono">{g.key}</Badge>
                    {#if g.is_builtin}<Badge>built-in</Badge>{/if}
                    <span class="text-2xs text-muted">sort {g.sort_order}</span>
                    {#if g.description}<span class="hidden text-xs text-muted sm:inline">— {g.description}</span>{/if}
                    <div class="grow"></div>
                    {#if isAdmin}
                      <Button size="sm" variant="ghost" onclick={() => editGroup(cat.key, g)} title="Rename or reparent (change category)">edit / reparent</Button>
                      <Button size="sm" variant="danger" onclick={() => removeGroup(g)}>delete</Button>
                    {/if}
                  </div>
                  {#if groupError?.key === g.key}
                    <Alert tone="danger" class="mt-2" onDismiss={() => (groupError = null)}>{groupError.message}</Alert>
                  {/if}

                  <!-- Extensions. The chip's × is icon-only chrome: a raw
                       button with token classes. -->
                  <div class="mt-2 flex flex-wrap items-center gap-1.5">
                    {#if g.extensions.length === 0}
                      <span class="text-xs text-muted">no extensions</span>
                    {:else}
                      {#each g.extensions as ext (ext)}
                        <span class="inline-flex items-center gap-1 rounded-full bg-surface-3 px-2 py-0.5 font-mono text-xs text-fg">
                          .{ext}
                          {#if isAdmin}
                            <button type="button" class="rounded-full px-0.5 leading-none text-muted hover:text-danger"
                              aria-label={`Remove .${ext}`} title="Remove extension" onclick={() => removeExt(g.key, ext)}>×</button>
                          {/if}
                        </span>
                      {/each}
                    {/if}
                  </div>

                  {#if isAdmin}
                    <!-- Add / move an extension. Adding one that lives in another
                         group reparents it here (upsert) and reports the move. -->
                    <div class="mt-2 flex flex-wrap items-center gap-2">
                      <!-- Sized by the wrapper: the kit control is w-full. -->
                      <div class="w-40">
                        <Input size="sm" class="font-mono"
                          aria-label={`Add extension to ${g.label}`}
                          placeholder="add ext (e.g. mkv)"
                          value={extInput[g.key] ?? ""}
                          oninput={(e) => (extInput = { ...extInput, [g.key]: e.currentTarget.value })}
                          onkeydown={(e) => { if (e.key === "Enter") { e.preventDefault(); addExt(g.key); } }} />
                      </div>
                      <Button size="sm" onclick={() => addExt(g.key)}>add</Button>
                      {#if extNotice[g.key]}<span class="text-xs text-ok" role="status">{extNotice[g.key]}</span>{/if}
                    </div>
                  {/if}
                  {#if extError[g.key]}
                    <Alert tone="danger" class="mt-2" onDismiss={() => (extError = { ...extError, [g.key]: "" })}>{extError[g.key]}</Alert>
                  {/if}
                </div>
              {/each}
            </div>
          {/if}
        </div>
      {/each}
    </div>
  {/if}
</div>

<!-- Create / edit dialog (category or group) -->
<Modal
  open={!!dialog}
  onClose={() => (dialog = null)}
  title={dialog ? `${dialog.mode === "create" ? "New" : "Edit"} ${dialog.kind}` : ""}
  size="md"
  dirty={() => dialogDirty}
>
  {#if dialog}
    <div class="flex flex-col gap-3">
      <Field label={dialog.mode === "edit" ? "Key (immutable)" : "Key"}>
        <Input class="font-mono" placeholder="lowercase-key" disabled={dialog.mode === "edit"} bind:value={dialog.key} />
      </Field>
      <Field label="Label">
        <Input bind:value={dialog.label} />
      </Field>
      <Field label="Description">
        <Input bind:value={dialog.description} />
      </Field>

      {#if dialog.kind === "category"}
        <Field label="Extractor" class="w-56">
          <Select bind:value={dialog.extractor}>
            {#each TAXONOMY_EXTRACTORS as ex}
              <option value={ex}>{ex}</option>
            {/each}
          </Select>
        </Field>
      {:else}
        <Field label={dialog.mode === "edit" ? "Category (change to reparent)" : "Category"} class="w-56">
          <Select bind:value={dialog.categoryKey}>
            {#each allCategoryKeys as ck}
              <option value={ck}>{ck}</option>
            {/each}
          </Select>
        </Field>
      {/if}

      <Field label="Sort order" class="w-32">
        <Input type="number" step="1" placeholder="(auto)" bind:value={dialog.sortOrder} />
      </Field>

      {#if dialogError}
        <Alert tone="danger" onDismiss={() => (dialogError = "")}>{dialogError}</Alert>
      {/if}
    </div>
  {/if}
  {#snippet footer({ close })}
    <Button onclick={close}>Cancel</Button>
    <Button variant="primary" disabled={dialogBusy} onclick={saveDialog}>
      {dialogBusy ? "Saving…" : dialog?.mode === "create" ? "Create" : "Save"}
    </Button>
  {/snippet}
</Modal>
