// The API error type and the one function that turns any thrown value into a
// sentence a person can read. DOM-free (imports nothing from api.ts) so the
// rules are unit-tested on Node; api.ts re-exports everything here.

/** An HTTP error carrying the numeric status, so callers can branch on 401/403/
 *  404. ``message`` keeps the legacy ``"<status>: <body>"`` shape so existing
 *  ``String(e)`` paths are unchanged; ``detail`` is the parsed FastAPI
 *  ``detail`` field when the body is JSON, else undefined. */
export class ApiError extends Error {
  status: number;
  body: string;
  detail: unknown;
  constructor(status: number, body: string) {
    super(`${status}: ${body}`);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
    this.detail = parseDetail(body);
  }
}

/** ``fetch`` itself rejected (offline, DNS, refused, CORS): no HTTP status.
 *  ``send`` wraps every non-abort rejection in this so the message does not
 *  depend on each browser's wording (Safari says "Load failed"). */
export class NetworkError extends Error {
  constructor(cause: unknown) {
    super(cause instanceof Error ? cause.message : String(cause), { cause });
    this.name = "NetworkError";
  }
}

function parseDetail(body: string): unknown {
  try {
    const parsed: unknown = JSON.parse(body);
    if (parsed && typeof parsed === "object" && "detail" in parsed) {
      return (parsed as { detail: unknown }).detail;
    }
  } catch {
    /* not JSON (an HTML error page, plain text, empty) */
  }
  return undefined;
}

// Pydantic's loc starts with where the field came from; that head is noise.
const LOC_HEADS = new Set(["body", "query"]);

function pydanticItem(item: unknown): string {
  if (!item || typeof item !== "object") return String(item ?? "");
  const { loc, msg } = item as { loc?: unknown; msg?: unknown };
  const text = typeof msg === "string" ? msg : "";
  const parts = Array.isArray(loc) ? [...loc] : [];
  if (parts.length && LOC_HEADS.has(String(parts[0]))) parts.shift();
  if (!parts.length) return text;
  const last = parts[parts.length - 1];
  // A list element: "tags[3]" says which one, "3" alone does not.
  if (typeof last === "number") {
    const parent = parts.length > 1 ? String(parts[parts.length - 2]) : "";
    return `${parent}[${last}]: ${text}`;
  }
  return `${String(last)}: ${text}`;
}

function validationItem(item: unknown): string {
  if (typeof item === "string") return item;
  if (item && typeof item === "object") {
    const { msg, message } = item as { msg?: unknown; message?: unknown };
    if (typeof msg === "string") return msg;
    if (typeof message === "string") return message;
  }
  return "";
}

/** The ``detail`` rendered as text, or "" when there is nothing usable. */
function detailText(detail: unknown): string {
  if (typeof detail === "string") return detail.trim();
  if (Array.isArray(detail)) return detail.map(pydanticItem).filter(Boolean).join("; ");
  if (detail && typeof detail === "object") {
    const v = (detail as { validation?: unknown }).validation;
    if (Array.isArray(v)) return v.map(validationItem).filter(Boolean).join("; ");
  }
  return "";
}

function statusSentence(status: number, verb: string | undefined): string {
  switch (status) {
    case 400: return "The request was invalid.";
    case 401: return "Your session has expired. Sign in again.";
    case 403: return `You don't have permission to ${verb ?? "do that"}.`;
    case 404: return "Not found. It may be outside your access or removed.";
    case 409: return "That conflicts with the current state. Reload and try again.";
    case 413: return "That is too large.";
    case 422: return "Some of the input is invalid.";
    case 429: return "Too many requests. Try again shortly.";
  }
  if (status >= 200 && status < 300) return "The server returned an unexpected response.";
  return status >= 500 ? `The server returned an error (${status}).` : `Request failed (${status}).`;
}

/** Any thrown value → a user-facing sentence. Never the raw ``"ApiError: …"``. */
export function errorMessage(e: unknown, opts: { verb?: string } = {}): string {
  if (e instanceof ApiError) {
    // 401/403/404 always get the status sentence: the backend's detail there
    // ("Missing bearer token") is not user language.
    if (e.status !== 401 && e.status !== 403 && e.status !== 404) {
      const text = detailText(e.detail);
      if (text) return text;
    }
    return statusSentence(e.status, opts.verb);
  }
  if (e instanceof NetworkError) return "Can't reach the server. Check your connection and try again.";
  // Fallback for callers that call fetch directly, bypassing send().
  if (e instanceof TypeError && /fetch|network/i.test(e.message)) {
    return "Can't reach the server. Check your connection and try again.";
  }
  const name = (e instanceof Error || e instanceof DOMException) ? e.name : "";
  if (name === "TimeoutError") return "The request timed out.";
  if (name === "AbortError") return "The request was cancelled.";
  if (e instanceof Error) return e.message || e.name;
  return String(e);
}

/** The HTTP status of an ApiError, else null. */
export function errorStatus(e: unknown): number | null {
  return e instanceof ApiError ? e.status : null;
}

/** Legacy name kept for existing callers: ``errorMessage`` with a verb. */
export function friendlyError(e: unknown, verb = "view"): string {
  return errorMessage(e, { verb });
}
