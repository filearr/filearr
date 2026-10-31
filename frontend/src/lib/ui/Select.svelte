<script lang="ts">
  import type { Snippet } from "svelte";
  import type { HTMLSelectAttributes } from "svelte/elements";
  import { controlClass, type ControlSize } from "./control.ts";

  // The native numeric `size` attribute is replaced by the kit's size scale.
  type Props = Omit<HTMLSelectAttributes, "size" | "value" | "class" | "children"> & {
    value?: HTMLSelectAttributes["value"];
    invalid?: boolean;
    size?: ControlSize;
    class?: string;
    /** The `<option>` / `<optgroup>` elements. */
    children?: Snippet;
  };

  let {
    value = $bindable(),
    invalid = false,
    size = "md",
    class: className = "",
    children,
    ...rest
  }: Props = $props();
</script>

<select
  {...rest}
  bind:value
  aria-invalid={invalid ? "true" : rest["aria-invalid"]}
  class="{controlClass(size, invalid)} {className}"
>
  {@render children?.()}
</select>
