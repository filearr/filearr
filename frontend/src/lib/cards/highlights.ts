// P6 (A13/A14, ruling 5) — which detail card an item gets and which metadata
// keys each card renders. DOM-free so it is Node-tested; registry.ts maps the
// card names to their Svelte components.
//
// Cards are keyed by backend category (backend/filearr/file_groups.py). The
// spreadsheet card is a `document` group, chosen by `file_group`; the audio
// card covers audiobooks (chapters) the same way. development / archive /
// system / other have no hand-written card and fall back to KeyFactsCard.
//
// HIGHLIGHT_KEYS: each card imports its list and uses it twice: it renders
// every key (as a stat chip, a table, a banner or a plain row) and passes the
// same list as KeyFactsCard's `exclude`, so a fact is never hidden from
// "More details" without being shown above it.

export const BACKEND_CATEGORIES = [
  "image",
  "audio",
  "video",
  "document",
  "three-d-cad",
  "development",
  "archive",
  "system",
  "other",
] as const;
export type BackendCategory = (typeof BACKEND_CATEGORIES)[number];

export type CardName =
  | "VideoCard"
  | "AudioCard"
  | "ImageCard"
  | "DocumentCard"
  | "SpreadsheetCard"
  | "Model3dCard"
  | "KeyFactsCard";

export type HighlightCard = "video" | "audio" | "image" | "document" | "spreadsheet" | "three-d-cad";

export const HIGHLIGHT_KEYS: Record<HighlightCard, readonly string[]> = {
  video: [
    "resolution", "width", "height", "video_codec", "audio_codec", "duration",
    "frame_rate", "hdr", "hdr_format", "container", "bitrate",
    "color_primaries", "color_transfer", "audio_tracks", "subtitle_tracks",
    "title", "year", "season", "episode",
  ],
  audio: [
    "artist", "album", "genre", "year", "duration", "bitrate", "samplerate",
    "channels", "title", "chapters", "chapter_count",
  ],
  image: ["width", "height", "format", "mode", "camera", "taken_at"],
  document: [
    "pages", "author", "created", "modified", "encrypted", "paragraphs",
    "title", "subject", "keywords", "creator", "producer", "revision",
    "unsupported", "body_text",
  ],
  spreadsheet: [
    "sheet_count", "author", "created", "modified", "title", "subject",
    "sheets", "unsupported",
  ],
  "three-d-cad": [
    "triangles", "vertices", "mesh_count", "file_format", "watertight",
    "bbox_volume", "bbox", "unsupported",
  ],
};

/** Category -> card. Every key is a BACKEND_CATEGORIES value (tested). */
export const CARD_REGISTRY: Partial<Record<BackendCategory, CardName>> = {
  video: "VideoCard",
  audio: "AudioCard",
  image: "ImageCard",
  document: "DocumentCard",
  "three-d-cad": "Model3dCard",
};

const CARD_LABELS: Record<CardName, string> = {
  VideoCard: "Video",
  AudioCard: "Audio",
  ImageCard: "Image",
  DocumentCard: "Document",
  SpreadsheetCard: "Spreadsheet",
  Model3dCard: "3D model",
  KeyFactsCard: "Details",
};

const own = (o: object, k: string) => Object.prototype.hasOwnProperty.call(o, k);

/** The card for an item; KeyFactsCard for a category without one (never throws). */
export function cardFor(category: string | null | undefined, fileGroup?: string | null): CardName {
  if (!category || !own(CARD_REGISTRY, category)) return "KeyFactsCard";
  if (category === "document" && fileGroup === "spreadsheet") return "SpreadsheetCard";
  return CARD_REGISTRY[category as BackendCategory] ?? "KeyFactsCard";
}

/** The card tab's label ("Details" for the generic fallback). */
export function cardLabel(category: string | null | undefined, fileGroup?: string | null): string {
  if (category === "audio" && fileGroup === "audiobook") return "Audiobook";
  return CARD_LABELS[cardFor(category, fileGroup)];
}
