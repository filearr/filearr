// Screen-reader announcements: App.svelte renders `announcement.text` in its
// single polite live region; anything (route changes now, toasts later) calls
// `announce()` instead of owning a region of its own.
export const announcement = $state({ text: "" });

/** Announce `message` via the app's live region. Clears first and sets the
 *  text on a later task, so an identical repeated message is a real DOM change
 *  and gets re-read. A timeout (not a microtask) so the empty state is flushed
 *  to the DOM before the new text lands. */
export function announce(message: string): void {
  announcement.text = "";
  setTimeout(() => {
    announcement.text = message;
  }, 0);
}
