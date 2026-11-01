// Whether hybrid semantic search is enabled server-side (ruling 4, A8/A44).
// No cheap feature-flag endpoint exists yet (backend ticket:
// `GET /system/features`), so the flag is read off the heavy /stats payload —
// ONCE per page load, shared by SearchPage and every ItemDetail. A failed probe
// reads as "off" and is not cached, so a later caller tries again.

import { stats } from "./api.ts";
import { sharedOnce } from "./configCache.ts";

const probe = sharedOnce(async () => {
  const r = (await stats()) as { semantic?: { enabled?: unknown } };
  return !!r.semantic?.enabled;
});

export function semanticEnabled(): Promise<boolean> {
  return probe().catch(() => false);
}
