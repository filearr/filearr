<script lang="ts">
  import { getContext } from "svelte";
  import type { HTMLTextareaAttributes } from "svelte/elements";
  import { controlClass, type ControlSize } from "./control.ts";
  import { controlAttrs, FIELD_CTX, type FieldCtx } from "./fieldContext.ts";

  type Props = Omit<HTMLTextareaAttributes, "value" | "class"> & {
    value?: HTMLTextareaAttributes["value"];
    invalid?: boolean;
    size?: ControlSize;
    class?: string;
  };

  let {
    value = $bindable(),
    invalid,
    size = "md",
    rows = 3,
    class: className = "",
    ...rest
  }: Props = $props();

  // Inside a <Field> the context supplies id, aria-describedby, invalid and
  // required; outside one getContext returns undefined and only own props
  // apply. `invalid` has no default so an absent prop lets the Field decide.
  // `invalid` alone drives aria-invalid (see controlAttrs); attrs spread after
  // rest so the merged values are the ones that land on the element.
  const field = getContext<FieldCtx | undefined>(FIELD_CTX);
  const { invalid: isInvalid, ...attrs } = $derived(
    controlAttrs(field, {
      id: rest.id ?? undefined,
      "aria-describedby": rest["aria-describedby"] ?? undefined,
      "aria-invalid": rest["aria-invalid"] ?? undefined,
      required: rest.required ?? undefined,
      invalid,
    }),
  );
</script>

<!-- Vertical-only resize keeps the field inside its column. -->
<textarea
  {...rest}
  {rows}
  bind:value
  {...attrs}
  class="{controlClass(size, isInvalid)} resize-y {className}"
></textarea>
