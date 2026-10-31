<script lang="ts">
  import type { HTMLTextareaAttributes } from "svelte/elements";
  import { controlClass, type ControlSize } from "./control.ts";

  type Props = Omit<HTMLTextareaAttributes, "value" | "class"> & {
    value?: HTMLTextareaAttributes["value"];
    invalid?: boolean;
    size?: ControlSize;
    class?: string;
  };

  let {
    value = $bindable(),
    invalid = false,
    size = "md",
    rows = 3,
    class: className = "",
    ...rest
  }: Props = $props();
</script>

<!-- Vertical-only resize keeps the field inside its column. -->
<textarea
  {...rest}
  {rows}
  bind:value
  aria-invalid={invalid ? "true" : rest["aria-invalid"]}
  class="{controlClass(size, invalid)} resize-y {className}"
></textarea>
