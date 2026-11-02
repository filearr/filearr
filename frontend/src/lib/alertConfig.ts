// Alert channel config and rule timings, as the API wants them (B23, B42).
// Pure; tested on Node.
//
// The channel PATCH MERGES `config` into the stored config: an omitted key is
// kept, an explicit `null` removes it (the only way to clear a stored secret),
// and a secret equal to ""/__unchanged__/__redacted__ keeps the stored value.
// So an edit sends only the keys the operator changed; keys this form does not
// know (e.g. something set through the API) are never touched.

import type { ChannelType, WebhookFormat } from "./api";
import { intOrNull } from "./numInput.ts";

/** Stored secrets come back as this marker on read (never the value). */
const REDACTED = "__redacted__";

const SECRET_KEY: Record<ChannelType, string> = { webhook: "secret", email: "password", apprise: "url" };

export type EmailSecurity = "starttls" | "ssl" | "plain";

export interface ChannelForm {
  webhookUrl: string;
  webhookSecret: string;
  clearWebhookSecret: boolean;
  webhookFormat: WebhookFormat;
  emailHost: string;
  /** `bind:value` on a number input: a number, or null/undefined when empty. */
  emailPort: number | string | null | undefined;
  emailSecurity: EmailSecurity;
  /** "Allow insecure plaintext SMTP": required (and sent) when security is plain. */
  allowInsecure: boolean;
  emailFrom: string;
  emailTo: string;
  emailUser: string;
  emailPassword: string;
  clearEmailPassword: boolean;
  /** Apprise: the whole URL is the secret. */
  appriseUrl: string;
}

export function emptyChannelForm(): ChannelForm {
  return {
    webhookUrl: "",
    webhookSecret: "",
    clearWebhookSecret: false,
    webhookFormat: "generic",
    emailHost: "",
    emailPort: null,
    emailSecurity: "starttls",
    allowInsecure: false,
    emailFrom: "",
    emailTo: "",
    emailUser: "",
    emailPassword: "",
    clearEmailPassword: false,
    appriseUrl: "",
  };
}

/** Auto-detect a webhook format from its URL (new-channel UI default; the
 *  select stays editable). Mirrors the backend `webhook_formats.detect_format`. */
export function detectWebhookFormat(url: string): WebhookFormat {
  try {
    const u = new URL(url);
    const host = u.hostname.toLowerCase();
    const discordHosts = ["discord.com", "discordapp.com"];
    const isDiscordHost =
      discordHosts.includes(host) ||
      discordHosts.some((h) => host.endsWith("." + h));
    if (isDiscordHost && u.pathname.includes("/api/webhooks")) return "discord";
    if (host === "hooks.slack.com") return "slack";
  } catch {
    // not a parseable URL yet — fall through to generic
  }
  return "generic";
}

/** Whether the channel has a stored secret (shown redacted on read). */
export function hasStoredSecret(kind: ChannelType, config: Record<string, unknown> | null | undefined): boolean {
  return !!config && config[SECRET_KEY[kind]] === REDACTED;
}

const str = (v: unknown): string => (v == null ? "" : String(v));

/** Seed the form from a stored (redacted) config. Secrets start blank (= keep). */
export function channelFormFromConfig(kind: ChannelType, config: Record<string, unknown> | null | undefined): ChannelForm {
  const cfg = config ?? {};
  const f = emptyChannelForm();
  if (kind === "webhook") {
    f.webhookUrl = str(cfg.url);
    const fmt = str(cfg.webhook_format);
    f.webhookFormat = (fmt || "generic") as WebhookFormat;
  } else if (kind === "email") {
    f.emailHost = str(cfg.host);
    f.emailPort = typeof cfg.port === "number" ? cfg.port : cfg.port == null ? null : str(cfg.port);
    f.emailSecurity = (str(cfg.security) || "starttls") as EmailSecurity;
    f.allowInsecure = cfg.allow_insecure === true;
    f.emailFrom = str(cfg.from_addr ?? cfg.from);
    const to = cfg.to ?? cfg.to_addrs ?? [];
    f.emailTo = Array.isArray(to) ? to.join(", ") : str(to);
    f.emailUser = str(cfg.username);
  }
  return f;
}

const isBlank = (v: unknown): boolean => v == null || (typeof v === "string" && v.trim() === "");

const splitRecipients = (s: string): string[] =>
  s.split(/[\n,]/).map((x) => x.trim()).filter(Boolean);

/** The non-secret keys the form owns, as they would be stored. An absent key
 *  means "not set". No validation here. */
function ownedKeys(kind: ChannelType, f: ChannelForm): Record<string, unknown> {
  if (kind === "webhook") return { url: f.webhookUrl.trim(), webhook_format: f.webhookFormat };
  if (kind === "email") {
    const out: Record<string, unknown> = {
      host: f.emailHost.trim(),
      security: f.emailSecurity,
      from_addr: f.emailFrom.trim(),
      to: splitRecipients(f.emailTo),
    };
    const port = intOrNull(f.emailPort);
    if (!isBlank(f.emailPort) && port !== null) out.port = port;
    if (f.emailUser.trim()) out.username = f.emailUser.trim();
    // The opt-in is only meaningful (and only owned by the form) with plain;
    // under STARTTLS/SSL it is inert, so a stored value is left alone.
    if (f.emailSecurity === "plain") out.allow_insecure = f.allowInsecure;
    return out;
  }
  return {};
}

function validate(kind: ChannelType, f: ChannelForm, editing: boolean): Record<string, string> {
  const errs: Record<string, string> = {};
  if (kind === "webhook") {
    if (!f.webhookUrl.trim()) errs.url = "URL is required.";
  } else if (kind === "email") {
    if (!f.emailHost.trim()) errs.host = "SMTP host is required.";
    if (!f.emailFrom.trim()) errs.from = "From is required.";
    if (splitRecipients(f.emailTo).length === 0) errs.to = "To is required.";
    if (!isBlank(f.emailPort) && intOrNull(f.emailPort, { min: 1, max: 65535 }) === null) {
      errs.port = "Port must be a whole number from 1 to 65535 (blank = 587).";
    }
    if (f.emailSecurity === "plain" && !f.allowInsecure) {
      errs.allow_insecure =
        "Plain SMTP sends the password and mail unencrypted. Tick \"Allow insecure plaintext SMTP\" to use it, or pick STARTTLS/SSL.";
    }
  } else if (!editing && !f.appriseUrl.trim()) {
    errs.apprise = "Apprise URL is required.";
  }
  return errs;
}

const same = (a: unknown, b: unknown): boolean => JSON.stringify(a) === JSON.stringify(b);

/** Where a stored secret is used, normalised like the server's
 *  `_check_secret_destination`: email = SMTP host (trimmed, lower-case) +
 *  port (blank = 587) + security; webhook = URL scheme + host + port. */
function destination(kind: ChannelType, f: ChannelForm): string | null {
  if (kind === "email") {
    const port = isBlank(f.emailPort) ? 587 : intOrNull(f.emailPort) ?? -1;
    return JSON.stringify([f.emailHost.trim().toLowerCase(), port, (f.emailSecurity || "starttls").toLowerCase()]);
  }
  if (kind === "webhook") {
    try {
      const u = new URL(f.webhookUrl.trim());
      return JSON.stringify([u.protocol, u.hostname.toLowerCase(), u.port]);
    } catch {
      return JSON.stringify(["", f.webhookUrl.trim(), ""]);
    }
  }
  return null; // apprise: the whole URL is the secret
}

/** True when an edit moves a channel that has a stored secret to a new
 *  destination (another SMTP server / webhook origin). The server refuses to
 *  carry the stored secret there, so the operator must type it again (or
 *  clear it). */
export function secretReentryRequired(
  kind: ChannelType,
  form: ChannelForm,
  original: Record<string, unknown> | null | undefined,
): boolean {
  if (!original || !hasStoredSecret(kind, original)) return false;
  const now = destination(kind, form);
  return now !== null && now !== destination(kind, channelFormFromConfig(kind, original));
}

export type ChannelConfigResult =
  | { ok: true; config: Record<string, unknown> }
  | { ok: false; errors: Record<string, string> };

/** The `config` to send. Create (`original` absent): every set key, secrets
 *  only when typed. Edit: only changed keys — a key the operator emptied is
 *  sent as `null` (removed; a cleared port falls back to 587 at delivery), a
 *  blank secret is omitted (kept), and the clear action sends `null`. */
export function buildChannelConfig(
  kind: ChannelType,
  form: ChannelForm,
  original?: Record<string, unknown> | null,
): ChannelConfigResult {
  const editing = original != null;
  const errors = validate(kind, form, editing);
  if (editing && secretReentryRequired(kind, form, original)) {
    const typedSecret = kind === "webhook" ? form.webhookSecret : form.emailPassword;
    const clearing = kind === "webhook" ? form.clearWebhookSecret : form.clearEmailPassword;
    if (!typedSecret && !clearing) {
      if (kind === "email") {
        errors.password =
          "You changed the mail server: re-enter the password (the stored one is never sent to a new server), or tick \"Clear stored password\".";
      } else {
        errors.secret =
          "You changed the webhook's host: re-enter the secret (the stored one is never used for a new destination), or tick \"Clear stored secret\".";
      }
    }
  }
  if (Object.keys(errors).length) return { ok: false, errors };

  const want = ownedKeys(kind, form);
  const config: Record<string, unknown> = {};
  if (!editing) {
    Object.assign(config, want);
  } else {
    const had = ownedKeys(kind, channelFormFromConfig(kind, original));
    // Compare the opt-in with what is really stored, whatever the security.
    if (kind === "email") had.allow_insecure = original.allow_insecure === true;
    for (const k of new Set([...Object.keys(had), ...Object.keys(want)])) {
      if (!(k in want)) {
        // Leaving plain does not remove a stored opt-in (inert without plain).
        if (k !== "allow_insecure") config[k] = null;
      } else if (!same(want[k], had[k])) config[k] = want[k];
    }
  }

  const secretKey = SECRET_KEY[kind];
  const typed = kind === "webhook" ? form.webhookSecret : kind === "email" ? form.emailPassword : form.appriseUrl.trim();
  const clear = kind === "webhook" ? form.clearWebhookSecret : kind === "email" ? form.clearEmailPassword : false;
  if (editing && clear) {
    if (hasStoredSecret(kind, original)) config[secretKey] = null;
  } else if (typed) {
    config[secretKey] = typed;
  }
  return { ok: true, config };
}

// ---- rule timings --------------------------------------------------------- //

export const DEFAULT_GROUP_WAIT_S = 30;
export const DEFAULT_INHIBIT_WINDOW_S = 900;
export const MAX_INHIBIT_WINDOW_S = 86400;

export interface RuleTimingInput {
  /** Text field: seconds, blank = never re-notify. */
  repeat: string;
  groupWait: number | string | null | undefined;
  inhibitWindow: number | string | null | undefined;
}

export type RuleTimingResult =
  | { ok: true; values: { repeat_interval_s: number | null; group_wait_s: number; inhibit_window_s: number } }
  | { ok: false; errors: Record<string, string> };

/** Typed rule timings. A cleared group wait / inhibit window takes the stated
 *  default (the API rejects null for both); garbage is a field error, never
 *  NaN. Error keys: `repeat`, `groupWait`, `inhibitWindow`. */
export function parseRuleTimings(input: RuleTimingInput): RuleTimingResult {
  const errors: Record<string, string> = {};
  let repeat: number | null = null;
  if (!isBlank(input.repeat)) {
    repeat = intOrNull(input.repeat, { min: 0 });
    if (repeat === null) errors.repeat = "Repeat interval must be a whole number of seconds (0 or more), or blank.";
  }
  let groupWait = DEFAULT_GROUP_WAIT_S;
  if (!isBlank(input.groupWait)) {
    const n = intOrNull(input.groupWait, { min: 0 });
    if (n === null) errors.groupWait = "Group wait must be a whole number of seconds (0 or more).";
    else groupWait = n;
  }
  let inhibit = DEFAULT_INHIBIT_WINDOW_S;
  if (!isBlank(input.inhibitWindow)) {
    const n = intOrNull(input.inhibitWindow, { min: 0, max: MAX_INHIBIT_WINDOW_S });
    if (n === null) errors.inhibitWindow = `Inhibit window must be a whole number of seconds from 0 to ${MAX_INHIBIT_WINDOW_S}.`;
    else inhibit = n;
  }
  if (Object.keys(errors).length) return { ok: false, errors };
  return { ok: true, values: { repeat_interval_s: repeat, group_wait_s: groupWait, inhibit_window_s: inhibit } };
}
