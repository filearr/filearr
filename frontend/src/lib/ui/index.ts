// Public surface of the UI kit. Three rules for callers:
// 1. `class` is appended last but does not override a utility for the same
//    property the component already sets (Tailwind order, not class order,
//    decides); use it for layout (margins, width), not to restyle.
// 2. Importing from this barrel in eager shell code can pull bits-ui into the
//    entry chunk. App.svelte imports ToastHost/confirm/toast by direct path
//    for that reason. ConfirmHost and ToastHost are not exported: App mounts
//    the only instances, and a second host would double every dialog/toast.
// 3. Controls inside `<Field>` wire themselves through context (`id`,
//    `aria-describedby`, `aria-invalid`, `required`); pass those explicitly
//    only for custom controls, and keep one control per Field. Wrap a
//    Checkbox/Switch in a Field only for hint/error wiring — the Field label
//    then names the group.
export { default as Button } from "./Button.svelte";
export { default as Badge } from "./Badge.svelte";
export { default as Alert } from "./Alert.svelte";
export { default as StateBlock } from "./StateBlock.svelte";
export { default as Field } from "./Field.svelte";
export { default as Input } from "./Input.svelte";
export { default as Select } from "./Select.svelte";
export { default as Textarea } from "./Textarea.svelte";
export { default as Checkbox } from "./Checkbox.svelte";
export { default as Switch } from "./Switch.svelte";
export { default as Modal } from "./Modal.svelte";
export { default as ConfirmDialog } from "./ConfirmDialog.svelte";
export { default as Toast } from "./Toast.svelte";
export { default as Tabs } from "./Tabs.svelte";
export { default as Popover } from "./Popover.svelte";
export { default as DataTable } from "./DataTable.svelte";
export { default as ScrollRegion } from "./ScrollRegion.svelte";
export { confirm, promptText, type ConfirmOptions, type PromptOptions } from "./confirm.svelte.ts";
export { toast } from "./toast.svelte.ts";
export { controlClass, type ControlSize } from "./control.ts";
export { FIELD_CTX, controlAttrs, type FieldCtx } from "./fieldContext.ts";
export { nextSort, toggleDir, compareValues, sortRows, type SortDir, type SortState } from "./sort.ts";
export { toneClasses, toneForStatus, type Tone } from "./tone.ts";
