// Detail-card components (P4-T10), keyed by backend category since P6 (A13,
// ruling 5). The choice itself lives in the DOM-free highlights.ts (`cardFor`,
// Node-tested); this module only maps a card name to its component. A category
// without a hand-written card (development / archive / system / other, or one
// the backend adds before its card ships) gets the generic KeyFactsCard, so
// extractor work is never blocked by a missing card. ItemDetail renders the
// result with Svelte 5 dynamic-component syntax (`<CardComponent {item} />`).

import type { Component } from "svelte";
import VideoCard from "./VideoCard.svelte";
import AudioCard from "./AudioCard.svelte";
import ImageCard from "./ImageCard.svelte";
import Model3dCard from "./Model3dCard.svelte";
import DocumentCard from "./DocumentCard.svelte";
import SpreadsheetCard from "./SpreadsheetCard.svelte";
import KeyFactsCard from "./KeyFactsCard.svelte";
import { cardFor, type CardName } from "./highlights";

export { cardLabel } from "./highlights";

// A card component always takes at least `{ item }`; KeyFactsCard additionally
// accepts optional `exclude`/`heading`/`lead` (widened so all cards share a type).
type CardProps = { item: Record<string, unknown>; exclude?: readonly string[]; heading?: string };
export type CardComponent = Component<CardProps>;

const COMPONENTS: Record<CardName, CardComponent> = {
  VideoCard: VideoCard as CardComponent,
  AudioCard: AudioCard as CardComponent,
  ImageCard: ImageCard as CardComponent,
  DocumentCard: DocumentCard as CardComponent,
  SpreadsheetCard: SpreadsheetCard as CardComponent,
  Model3dCard: Model3dCard as CardComponent,
  KeyFactsCard: KeyFactsCard as CardComponent,
};

/** The detail component for an item's category (+ group, for spreadsheets). */
export function cardComponent(category: string | null | undefined, fileGroup?: string | null): CardComponent {
  return COMPONENTS[cardFor(category, fileGroup)];
}
