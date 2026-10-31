<!--
  Confirmation on bits-ui AlertDialog: no outside-click dismiss, Escape and
  Cancel both mean "no". Most callers want the promise helper instead:
    if (!(await confirm({ title: "Delete agent?", tone: "danger", typedName: agent.name }))) return;
-->
<script lang="ts">
  import { untrack, type Snippet } from "svelte";
  import { AlertDialog } from "bits-ui";
  import Button from "./Button.svelte";
  import Input from "./Input.svelte";

  let {
    open = $bindable(false),
    title,
    message,
    confirmLabel = "Confirm",
    cancelLabel = "Cancel",
    tone = "default",
    typedName,
    onConfirm,
    onCancel,
    children,
  }: {
    open?: boolean;
    title: string;
    message?: string;
    confirmLabel?: string;
    cancelLabel?: string;
    tone?: "default" | "danger";
    /** When set, the user must type this exactly before Confirm enables. */
    typedName?: string;
    onConfirm: () => void;
    /** Called on Cancel or Escape. */
    onCancel?: () => void;
    /** Rich message; takes the place of `message`. */
    children?: Snippet;
  } = $props();

  const fieldId = $props.id();
  let typed = $state("");
  let cancelEl = $state<HTMLElement | null>(null);

  // Case-sensitive on purpose: the name is the thing being destroyed. Only the
  // input is trimmed, so a stray space from copy-paste still matches.
  const canConfirm = $derived(typedName === undefined || typed.trim() === typedName);

  // A reopened dialog starts with an empty field, never a stale match.
  $effect(() => {
    if (open) untrack(() => (typed = ""));
  });

  function handleConfirm() {
    if (!canConfirm) return;
    // Closing from here does not fire onOpenChange, so onCancel stays silent.
    open = false;
    onConfirm();
  }
</script>

<!-- onOpenChange only fires for closes bits-ui makes itself (Cancel, Escape);
     the confirm path closes via `open = false` above. -->
<AlertDialog.Root bind:open onOpenChange={(v) => { if (!v) onCancel?.(); }}>
  <AlertDialog.Portal>
    <!-- Same z-index and backdrop as Modal. -->
    <AlertDialog.Overlay class="fixed inset-0 z-50 bg-black/50" />
    <!-- Cancel is the safe default, so it takes the initial focus rather than
         the content box. Deferred a frame because the button's ref is attached
         after the focus scope mounts. -->
    <AlertDialog.Content
      class="relative mx-auto my-8 w-full max-w-md rounded-panel bg-surface-2 p-5 shadow-xl"
      onOpenAutoFocus={(e) => {
        e.preventDefault();
        requestAnimationFrame(() => cancelEl?.focus());
      }}
    >
      {#snippet child({ props })}
        <div class="fixed inset-0 z-50 overflow-y-auto px-4">
          <div {...props}>
            <AlertDialog.Title>
              {#snippet child({ props: titleProps })}
                <h2 {...titleProps} class="text-lg font-semibold">{title}</h2>
              {/snippet}
            </AlertDialog.Title>
            {#if children || message}
              <AlertDialog.Description class="mt-1 text-sm text-muted">
                {#if children}{@render children()}{:else}{message}{/if}
              </AlertDialog.Description>
            {/if}
            {#if typedName !== undefined}
              <!-- Not Field: its label is a plain string and this one needs the
                   name in bold. Same label styling and id wiring by hand. -->
              <div class="mt-4 flex flex-col gap-1">
                <label for={fieldId} class="text-xs font-medium text-fg">
                  Type <strong>{typedName}</strong> to confirm
                </label>
                <Input id={fieldId} bind:value={typed} autocomplete="off" spellcheck={false} />
              </div>
            {/if}
            <div class="mt-5 flex flex-wrap justify-end gap-2">
              <AlertDialog.Cancel bind:ref={cancelEl}>
                {#snippet child({ props: cancelProps })}
                  <Button {...cancelProps} variant="secondary">{cancelLabel}</Button>
                {/snippet}
              </AlertDialog.Cancel>
              <AlertDialog.Action>
                {#snippet child({ props: actionProps })}
                  <Button
                    {...actionProps}
                    variant={tone === "danger" ? "danger" : "primary"}
                    disabled={!canConfirm}
                    onclick={handleConfirm}>{confirmLabel}</Button
                  >
                {/snippet}
              </AlertDialog.Action>
            </div>
          </div>
        </div>
      {/snippet}
    </AlertDialog.Content>
  </AlertDialog.Portal>
</AlertDialog.Root>
