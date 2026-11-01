// Page-load caches for configuration that does not change while a page is open
// (A44): custom-field definitions and per-category metadata profiles. Opening
// one item detail used to GET /custom-fields three times (ItemDetail, every
// KeyFactsCard mount, BulkEditBar) and the profile once per card.
//
// One shared promise per key. A rejection is not cached, so a later caller
// retries. Writers of a cached resource call its invalidate*() after a
// successful change (CustomFieldsPanel does for custom fields), so the next
// reader fetches fresh definitions. A caller's `signal` ends only that caller's wait (rejecting with an
// AbortError); the shared fetch carries on for everyone else.

import {
  errorStatus,
  getMetadataProfile,
  listCustomFields,
  type CustomField,
  type MetadataProfile,
} from "./api.ts";

function abortError(): DOMException {
  return new DOMException("The request was cancelled.", "AbortError");
}

/** `p`, or an AbortError as soon as `signal` aborts (`p` keeps running). */
export function waitWithSignal<T>(p: Promise<T>, signal?: AbortSignal): Promise<T> {
  if (!signal) return p;
  if (signal.aborted) return Promise.reject(abortError());
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => reject(abortError());
    signal.addEventListener("abort", onAbort, { once: true });
    p.then(
      (v) => {
        signal.removeEventListener("abort", onAbort);
        resolve(v);
      },
      (e) => {
        signal.removeEventListener("abort", onAbort);
        reject(e);
      },
    );
  });
}

/** A shared getter plus `reset()`, which drops the cached promise so the next
 *  call fetches again (callers already waiting keep their answer). */
export type Shared<T> = ((signal?: AbortSignal) => Promise<T>) & { reset(): void };
export type SharedByKey<T> = ((key: string, signal?: AbortSignal) => Promise<T>) & { reset(): void };

/** Memoise `load` for the page load: every caller shares one promise; a
 *  rejection clears it so the next call fetches again. */
export function sharedOnce<T>(load: () => Promise<T>): Shared<T> {
  let shared: Promise<T> | null = null;
  const get = (signal?: AbortSignal) => {
    if (!shared) {
      const mine = load();
      shared = mine;
      mine.catch(() => {
        if (shared === mine) shared = null;
      });
    }
    return waitWithSignal(shared, signal);
  };
  return Object.assign(get, {
    reset() {
      shared = null;
    },
  });
}

/** sharedOnce per key; `reset()` drops every key. */
export function sharedByKey<T>(load: (key: string) => Promise<T>): SharedByKey<T> {
  const byKey = new Map<string, Shared<T>>();
  const get = (key: string, signal?: AbortSignal) => {
    let one = byKey.get(key);
    if (!one) {
      one = sharedOnce(() => load(key));
      byKey.set(key, one);
    }
    return one(signal);
  };
  return Object.assign(get, {
    reset() {
      byKey.clear();
    },
  });
}

/** GET /custom-fields, once per page load. */
export const cachedCustomFields: Shared<CustomField[]> = sharedOnce(() => listCustomFields());

/** After a custom field is created, updated or deleted. */
export function invalidateCustomFields(): void {
  cachedCustomFields.reset();
}

/** GET /metadata-profiles/{category}, once per category per page load. A 404
 *  (no profile seeded for that category) is an answer, cached as `null`. */
export const cachedProfile: SharedByKey<MetadataProfile | null> = sharedByKey((category) =>
    getMetadataProfile(category).catch((e: unknown) => {
      if (errorStatus(e) === 404) return null;
      throw e;
    }),
  );

/** After a metadata profile changes (no in-app editor calls it yet). */
export function invalidateProfiles(): void {
  cachedProfile.reset();
}
