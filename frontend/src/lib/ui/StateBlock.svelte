<script lang="ts">
  import Button from "./Button.svelte";

  type State = "loading" | "empty" | "error";

  let {
    state,
    message,
    onRetry,
    compact = false,
    class: className = "",
  }: {
    state: State;
    message?: string;
    onRetry?: () => void;
    compact?: boolean;
    class?: string;
  } = $props();

  const defaults: Record<State, string> = {
    loading: "Loading…",
    empty: "Nothing here yet.",
    error: "Something went wrong.",
  };

  // Loading announces politely, errors assertively; an empty list is not news,
  // so it carries no live role.
  const role = $derived(state === "loading" ? "status" : state === "error" ? "alert" : undefined);
</script>

<div
  {role}
  class="flex flex-col items-center gap-2 text-center text-sm text-muted {compact ? 'py-3' : 'py-8'} {className}"
>
  <p>{message ?? defaults[state]}</p>
  {#if state === "error" && onRetry}
    <Button variant="secondary" size="sm" onclick={onRetry}>Retry</Button>
  {/if}
</div>
