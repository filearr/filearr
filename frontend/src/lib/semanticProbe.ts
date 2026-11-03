// Whether hybrid semantic search is enabled server-side (ruling 4, A8/A44).
// Read off the cheap `GET /system/features` gate list (one tiny libraries
// count) — ONCE per page load, shared by SearchPage and every ItemDetail. It
// used to be read off the heavy /stats aggregate, which made every Search page
// load cost a full dashboard fan-out (2026-11-02). A failed probe reads as
// "off" and is not cached, so a later caller tries again.

import { systemFeatures } from "./api.ts";
import { sharedOnce } from "./configCache.ts";

const probe = sharedOnce(async () => {
  const r = await systemFeatures();
  return !!r.features?.find((f) => f.key === "semantic")?.enabled;
});

export function semanticEnabled(): Promise<boolean> {
  return probe().catch(() => false);
}
