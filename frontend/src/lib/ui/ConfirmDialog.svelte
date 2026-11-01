<!--
  Confirmation on bits-ui AlertDialog: no outside-click dismiss, Escape and
  Cancel both mean "no". Most callers want the promise helpers instead:
    if (!(await confirm({ title: "Delete agent?", tone: "danger", typedName: agent.name }))) return;
    const name = await promptText({ title: "Rename", label: "Name", initial: item.name });
    if (name === null) return;

  With `prompt` set it is a text prompt (a second mode of the typedName
  field): Enter or Confirm runs checkPromptValue and shows its error under the
  field instead of closing; on success onConfirm gets the trimmed value.
-->
<script lang="ts">
  import { untrack, type Snippet } from "svelte";
  import { AlertDialog } from "bits-ui";
  import Button from "./Button.svelte";
  import Field from "./Field.svelte";
  import Input from "./Input.svelte";
  import { checkPromptValue, type PromptRules } from "./promptRules.ts";

  let {
    open = $bindable(false),
    title,
    message,
    details,
    confirmLabel = "Confirm",
    cancelLabel = "Cancel",
    tone = "default",
    typedName,
    prompt,
    onConfirm,
    onCancel,
    children,
  }: {
    open?: boolean;
    title: string;
    message?: string;
    /** Consequence summary listed under the message (what will be lost). */
    details?: string[];
    confirmLabel?: string;
    cancelLabel?: string;
    tone?: "default" | "danger";
    /** When set, the user must type this exactly before Confirm enables. */
    typedName?: string;
    /** When set, asks for a line of text; onConfirm receives the trimmed value. */
    prompt?: PromptRules & { label: string; initial?: string; placeholder?: string };
    /** Gets the trimmed value in prompt mode, nothing otherwise. */
    onConfirm: (value?: string) => void;
    /** Called on Cancel or Escape. */
    onCancel?: () => void;
    /** Rich message; takes the place of `message`. */
    children?: Snippet;
  } = $props();

  const fieldId = $props.id();
  let typed = $state("");
  let promptError = $state<string | undefined>();
  let cancelEl = $state<HTMLElement | null>(null);
  let inputEl = $state<HTMLInputElement | null>(null);

  // Case-sensitive on purpose: the name is the thing being destroyed. Both
  // sides are trimmed, so a stray space from copy-paste still matches.
  const canConfirm = $derived(typedName === undefined || typed.trim() === typedName.trim());

  // A reopened dialog starts from a clean field: no stale typed-name match,
  // a prompt back at its initial value with no error.
  $effect(() => {
    if (open)
      untrack(() => {
        typed = prompt?.initial ?? "";
        promptError = undefined;
      });
  });

  function handleConfirm() {
    if (!canConfirm) return;
    let value: string | undefined;
    if (prompt) {
      const checked = checkPromptValue(typed, prompt);
      if (checked.error !== undefined) {
        promptError = checked.error;
        inputEl?.focus();
        return;
      }
      value = checked.value;
    }
    // Closing from here does not fire onOpenChange, so onCancel stays silent.
    open = false;
    onConfirm(value);
  }
</script>

<!-- onOpenChange only fires for closes bits-ui makes itself (Cancel, Escape);
     the confirm path closes via `open = false` above. -->
<AlertDialog.Root bind:open onOpenChange={(v) => { if (!v) onCancel?.(); }}>
  <AlertDialog.Portal>
    <!-- Same z-index and backdrop as Modal. -->
    <AlertDialog.Overlay class="fixed inset-0 z-50 bg-black/50" />
    <!-- Cancel is the safe default, so it takes the initial focus rather than
         the content box; a prompt focuses its field instead. Deferred a frame
         because the refs are attached after the focus scope mounts. The panel
         scrolls, not the wrapper, for the same reason as in Modal (the wrapper
         is pointer-events: none). -->
    <AlertDialog.Content
      class="relative max-h-[calc(100dvh-4rem)] w-full max-w-md overflow-y-auto rounded-panel bg-surface-2 p-5 shadow-xl"
      onOpenAutoFocus={(e) => {
        e.preventDefault();
        requestAnimationFrame(() => (prompt ? inputEl : cancelEl)?.focus());
      }}
    >
      {#snippet child({ props })}
        <div class="fixed inset-0 z-50 flex items-start justify-center px-4 py-8">
          <div {...props}>
            <AlertDialog.Title>
              {#snippet child({ props: titleProps })}
                <h2 {...titleProps} class="text-lg font-semibold">{title}</h2>
              {/snippet}
            </AlertDialog.Title>
            <!-- The details sit inside the description so aria-describedby
                 reads the consequences too, not just the message. -->
            {#if children || message || details?.length}
              <AlertDialog.Description class="mt-1 text-sm text-muted">
                {#if children}{@render children()}{:else}{message}{/if}
                {#if details?.length}
                  <ul class="mt-2 list-disc space-y-0.5 pl-5 text-sm text-muted">
                    {#each details as item, i (i)}<li>{item}</li>{/each}
                  </ul>
                {/if}
              </AlertDialog.Description>
            {/if}
            {#if prompt}
              <!-- A form so Enter in the field submits, like the typedName one. -->
              <form
                class="mt-4"
                onsubmit={(e) => {
                  e.preventDefault();
                  handleConfirm();
                }}
              >
                <Field label={prompt.label} error={promptError}>
                  <Input
                    bind:value={typed}
                    bind:ref={inputEl}
                    placeholder={prompt.placeholder}
                    autocomplete="off"
                    oninput={() => (promptError = undefined)}
                  />
                </Field>
              </form>
            {:else if typedName !== undefined}
              <!-- Not Field: its label is a plain string and this one needs the
                   name in bold. Same label styling and id wiring by hand. -->
              <!-- A form so Enter in the field confirms once the name matches. -->
              <form
                class="mt-4 flex flex-col gap-1"
                onsubmit={(e) => {
                  e.preventDefault();
                  handleConfirm();
                }}
              >
                <label for={fieldId} class="text-xs font-medium text-fg">
                  Type <strong>{typedName}</strong> to confirm
                </label>
                <Input id={fieldId} bind:value={typed} autocomplete="off" spellcheck={false} />
              </form>
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
