<script lang="ts">
  import type { HTMLInputAttributes } from "svelte/elements";
  import { controlClass, type ControlSize } from "./control.ts";

  // The native numeric `size` attribute is replaced by the kit's size scale.
  type Props = Omit<HTMLInputAttributes, "size" | "value" | "class"> & {
    value?: HTMLInputAttributes["value"];
    invalid?: boolean;
    size?: ControlSize;
    class?: string;
  };

  let { value = $bindable(), invalid = false, size = "md", class: className = "", ...rest }: Props = $props();
</script>

<input
  {...rest}
  bind:value
  aria-invalid={invalid ? "true" : rest["aria-invalid"]}
  class="{controlClass(size, invalid)} {className}"
/>
