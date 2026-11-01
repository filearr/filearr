// localStorage that never throws. Private mode, blocked site data and Node all
// make it missing or throwing; the in-memory Map keeps the value for the rest
// of the session instead. DOM-free so it loads on the Node test runner.

const memory = new Map<string, string>();

function storage(): Storage | null {
  // Touching the global can itself throw (blocked storage → SecurityError).
  return (globalThis as { localStorage?: Storage }).localStorage ?? null;
}

export function get(key: string): string | null {
  // A key in memory means a write failed; it is newer than anything stored.
  if (memory.has(key)) return memory.get(key)!;
  try {
    return storage()?.getItem(key) ?? null;
  } catch {
    return null;
  }
}

export function set(key: string, value: string): void {
  try {
    const s = storage();
    if (!s) throw new Error("no localStorage");
    s.setItem(key, value);
    memory.delete(key);
  } catch {
    memory.set(key, value);
  }
}

export function remove(key: string): void {
  memory.delete(key);
  try {
    storage()?.removeItem(key);
  } catch {
    /* nothing stored that we could reach */
  }
}
