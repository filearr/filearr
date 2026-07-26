<script lang="ts">
  import { copyText } from "./clipboard";
  // P4-T11 — generic "Raw" view: dumps EVERY field of the item record as a flat
  // key/value table. It iterates the response keys (no hardcoded allowlist) so
  // future backend columns (e.g. P4-T7 provenance) appear automatically. The two
  // metadata columns arrive as separate keys, so they render separately / unmerged
  // here by construction. All values render as TEXT — Svelte escapes interpolations
  // and we never use {@html}; filenames and metadata are untrusted.
  let { item }: { item: Record<string, unknown> } = $props();

  const isComplex = (v: unknown): boolean => v !== null && typeof v === "object";

  function pretty(v: unknown): string {
    if (v === null || v === undefined) return "";
    if (isComplex(v)) return JSON.stringify(v, null, 2);
    return String(v);
  }

  // Roadmap §20: some values are ENORMOUS (metadata with OCR body text, archive
  // member lists, ffprobe dumps) and made the Raw view extremely long. Values
  // over the clamp render collapsed behind a per-row "Show all" toggle; the cut
  // lands on a line boundary so the preview never ends mid-token. Expansion
  // always reveals the complete value — nothing is truncated for good.
  const CLAMP_CHARS = 1500;
  let expanded = $state<Record<string, boolean>>({});

  function clampAt(text: string): number {
    const nl = text.lastIndexOf("\n", CLAMP_CHARS);
    return nl > CLAMP_CHARS / 2 ? nl : CLAMP_CHARS;
  }
  const lineCount = (s: string): number => s.split("\n").length;
  function fmtSize(chars: number): string {
    return chars >= 4096 ? `${(chars / 1024).toFixed(0)} KB` : `${chars} chars`;
  }

  let copied = $state<string | null>(null);
  async function copy(key: string, v: unknown) {
    try {
      await copyText(pretty(v));
      copied = key;
      setTimeout(() => { if (copied === key) copied = null; }, 1200);
    } catch {
      // Clipboard unavailable (insecure context) — ignore silently.
    }
  }

  // Alphabetize for scanability; iteration order is otherwise payload order.
  let entries = $derived(
    Object.entries(item).sort(([a], [b]) => a.localeCompare(b)),
  );
</script>

<table class="w-full border-collapse text-sm">
  <tbody>
    {#each entries as [key, value] (key)}
      {@const text = pretty(value)}
      {@const long = text.length > CLAMP_CHARS}
      {@const open = !long || !!expanded[key]}
      <tr class="border-b border-slate-200 align-top dark:border-slate-800">
        <th class="w-56 py-2 pr-4 text-left align-top font-mono text-xs font-medium text-slate-500">
          {key}
        </th>
        <td class="py-2">
          {#if value === null || value === undefined || value === ""}
            <span class="text-slate-400">—</span>
          {:else if isComplex(value) || long}
            <pre class="overflow-x-auto whitespace-pre-wrap break-words font-mono text-xs">{open ? text : text.slice(0, clampAt(text))}{#if !open}…{/if}</pre>
            {#if long}
              <button
                type="button"
                class="mt-1 rounded border border-slate-300 px-2 py-0.5 text-xs text-slate-500 hover:border-[var(--accent)] hover:text-[var(--accent)] dark:border-slate-700"
                aria-expanded={open}
                onclick={() => (expanded[key] = !expanded[key])}
              >{open ? "Show less" : `Show all (${lineCount(text)} lines, ${fmtSize(text.length)})`}</button>
            {/if}
          {:else}
            <span class="break-words font-mono text-xs">{String(value)}</span>
          {/if}
        </td>
        <td class="w-10 py-2 pl-2 text-right align-top">
          <button
            type="button"
            class="text-xs text-slate-400 hover:text-[var(--accent)]"
            title="Copy value (full value, even when collapsed)"
            onclick={() => copy(key, value)}>{copied === key ? "✓" : "⧉"}</button>
        </td>
      </tr>
    {/each}
  </tbody>
</table>
