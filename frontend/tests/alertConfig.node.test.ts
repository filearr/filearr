// alertConfig: the alert-channel config the API receives (B23) and the rule
// timing fields (B42). Channel PATCH merges `config` server-side: omitted keys
// are kept, explicit null removes a key, ""/__unchanged__/__redacted__ keep a
// stored secret. So an edit sends ONLY the keys the operator changed.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  buildChannelConfig,
  channelFormFromConfig,
  detectWebhookFormat,
  emptyChannelForm,
  hasStoredSecret,
  parseRuleTimings,
  secretReentryRequired,
  type ChannelForm,
} from "../src/lib/alertConfig.ts";

const form = (o: Partial<ChannelForm>): ChannelForm => ({ ...emptyChannelForm(), ...o });
const ok = (r: ReturnType<typeof buildChannelConfig>) => {
  assert.equal(r.ok, true, JSON.stringify(r));
  return (r as { ok: true; config: Record<string, unknown> }).config;
};
const errs = (r: ReturnType<typeof buildChannelConfig>) => {
  assert.equal(r.ok, false, JSON.stringify(r));
  return (r as { ok: false; errors: Record<string, string> }).errors;
};

// ---- create --------------------------------------------------------------- //

test("webhook create: url trimmed, format, secret only when typed", () => {
  assert.deepEqual(ok(buildChannelConfig("webhook", form({ webhookUrl: " https://x/h ", webhookFormat: "generic" }))), {
    url: "https://x/h",
    webhook_format: "generic",
  });
  assert.deepEqual(
    ok(buildChannelConfig("webhook", form({ webhookUrl: "https://x/h", webhookSecret: "s3" }))),
    { url: "https://x/h", webhook_format: "generic", secret: "s3" },
  );
});

test("webhook create: url required", () => {
  assert.ok(errs(buildChannelConfig("webhook", form({ webhookUrl: "  " }))).url);
});

const emailBase = { emailHost: "smtp.x", emailFrom: "a@x", emailTo: "b@x, c@x\nd@x" };

test("email create: empty port omitted (server default 587), recipients split", () => {
  const c = ok(buildChannelConfig("email", form({ ...emailBase, emailPort: null })));
  assert.deepEqual(c, {
    host: "smtp.x",
    security: "starttls",
    from_addr: "a@x",
    to: ["b@x", "c@x", "d@x"],
  });
  assert.equal("port" in c, false);
  for (const blank of [undefined, "", "  "]) {
    assert.equal("port" in ok(buildChannelConfig("email", form({ ...emailBase, emailPort: blank }))), false);
  }
});

test("email create: port typed as int; out of range / fractional → field error", () => {
  assert.equal(ok(buildChannelConfig("email", form({ ...emailBase, emailPort: 2525 }))).port, 2525);
  assert.equal(ok(buildChannelConfig("email", form({ ...emailBase, emailPort: "465" }))).port, 465);
  for (const bad of [0, 65536, 25.5, -1, "abc"]) {
    assert.match(errs(buildChannelConfig("email", form({ ...emailBase, emailPort: bad }))).port, /1.*65535/);
  }
});

test("email create: required host/from/to", () => {
  const e = errs(buildChannelConfig("email", form({})));
  assert.ok(e.host && e.from && e.to);
});

test("email create: username + password only when typed", () => {
  const c = ok(buildChannelConfig("email", form({ ...emailBase, emailUser: " u ", emailPassword: "pw" })));
  assert.equal(c.username, "u");
  assert.equal(c.password, "pw");
});

test("plain SMTP requires the explicit opt-in; ticked sends allow_insecure: true", () => {
  const e = errs(buildChannelConfig("email", form({ ...emailBase, emailSecurity: "plain" })));
  assert.match(e.allow_insecure, /insecure/i);
  const c = ok(buildChannelConfig("email", form({ ...emailBase, emailSecurity: "plain", allowInsecure: true })));
  assert.equal(c.security, "plain");
  assert.equal(c.allow_insecure, true);
});

test("allow_insecure is not sent when plain is not selected (it is inert there)", () => {
  const c = ok(buildChannelConfig("email", form({ ...emailBase, emailSecurity: "ssl", allowInsecure: true })));
  assert.equal("allow_insecure" in c, false);
});

test("apprise create: url required, sent as is", () => {
  assert.ok(errs(buildChannelConfig("apprise", form({}))).apprise);
  assert.deepEqual(ok(buildChannelConfig("apprise", form({ appriseUrl: "tgram://t/c" }))), { url: "tgram://t/c" });
});

// ---- edit (changed keys only) -------------------------------------------- //

const storedEmail = {
  host: "smtp.x",
  port: 2525,
  security: "starttls",
  from_addr: "a@x",
  to: ["b@x"],
  username: "u",
  password: "__redacted__",
  allow_insecure: true, // set via the API; the UI must not drop it
  x_custom: 1,
};

test("edit with nothing changed sends an empty config", () => {
  const f = channelFormFromConfig("email", storedEmail);
  assert.deepEqual(ok(buildChannelConfig("email", f, storedEmail)), {});
});

test("edit sends only the changed keys; unknown keys are never sent", () => {
  const f = { ...channelFormFromConfig("email", storedEmail), emailFrom: "c@x", emailTo: "b@x, z@x" };
  assert.deepEqual(ok(buildChannelConfig("email", f, storedEmail)), { from_addr: "c@x", to: ["b@x", "z@x"] });
});

test("edit: moving an email channel to another server requires the password again", () => {
  const f = channelFormFromConfig("email", storedEmail);
  for (const change of [{ emailHost: "evil.x" }, { emailPort: 465 }, { emailPort: null }, { emailSecurity: "ssl" as const }]) {
    const moved = { ...f, ...change };
    assert.equal(secretReentryRequired("email", moved, storedEmail), true, JSON.stringify(change));
    assert.match(errs(buildChannelConfig("email", moved, storedEmail)).password, /re-enter the password/i);
    // A typed password or the clear action satisfies it.
    assert.equal(ok(buildChannelConfig("email", { ...moved, emailPassword: "pw2" }, storedEmail)).password, "pw2");
    assert.equal(ok(buildChannelConfig("email", { ...moved, clearEmailPassword: true }, storedEmail)).password, null);
  }
});

test("edit: same server spelled differently is not a move; no stored password = no requirement", () => {
  const f = { ...channelFormFromConfig("email", storedEmail), emailHost: " SMTP.X " };
  assert.equal(secretReentryRequired("email", { ...f, emailHost: "SMTP.x" }, storedEmail), false);
  const noPort = { ...storedEmail, port: undefined };
  assert.equal(secretReentryRequired("email", { ...channelFormFromConfig("email", noPort), emailPort: 587 }, noPort), false);
  const noPw = { ...storedEmail, password: undefined };
  assert.equal(secretReentryRequired("email", { ...f, emailHost: "evil.x" }, noPw), false);
  assert.equal(secretReentryRequired("email", { ...f, emailHost: "evil.x" }, null), false);
});

test("edit: a webhook moving to another origin with a stored secret requires it again", () => {
  const stored = { url: "https://hook.x/a", secret: "__redacted__" };
  const f = channelFormFromConfig("webhook", stored);
  assert.equal(secretReentryRequired("webhook", { ...f, webhookUrl: "https://hook.x/b" }, stored), false);
  for (const url of ["https://evil.x/a", "http://hook.x/a", "https://hook.x:8443/a"]) {
    const moved = { ...f, webhookUrl: url };
    assert.equal(secretReentryRequired("webhook", moved, stored), true, url);
    assert.match(errs(buildChannelConfig("webhook", moved, stored)).secret, /re-enter the secret/i);
    assert.equal(ok(buildChannelConfig("webhook", { ...moved, webhookSecret: "s2" }, stored)).secret, "s2");
  }
});

test("edit: blank secret = keep (omitted); typed = set; clear = null", () => {
  const f = channelFormFromConfig("email", storedEmail);
  assert.equal(f.emailPassword, "");
  assert.equal("password" in ok(buildChannelConfig("email", f, storedEmail)), false);
  assert.deepEqual(ok(buildChannelConfig("email", { ...f, emailPassword: "new" }, storedEmail)), { password: "new" });
  assert.deepEqual(ok(buildChannelConfig("email", { ...f, clearEmailPassword: true }, storedEmail)), { password: null });
  // clear wins over a typed value (the input is disabled while clearing)
  assert.deepEqual(
    ok(buildChannelConfig("email", { ...f, emailPassword: "x", clearEmailPassword: true }, storedEmail)),
    { password: null },
  );
});

test("clear is a no-op when nothing is stored, and ignored on create", () => {
  const stored = { ...storedEmail, password: undefined };
  const f = { ...channelFormFromConfig("email", stored), clearEmailPassword: true };
  assert.deepEqual(ok(buildChannelConfig("email", f, stored)), {});
  const c = ok(buildChannelConfig("email", form({ ...emailBase, clearEmailPassword: true })));
  assert.equal("password" in c, false);
});

test("edit: clearing a stored port removes it (null → server default 587)", () => {
  // 2525 → 587 is a new destination, so the password is re-entered too.
  const f = { ...channelFormFromConfig("email", storedEmail), emailPort: null, emailPassword: "pw" };
  assert.deepEqual(ok(buildChannelConfig("email", f, storedEmail)), { port: null, password: "pw" });
  // no stored port + blank → nothing
  const noPort = { ...storedEmail, port: undefined };
  assert.deepEqual(ok(buildChannelConfig("email", channelFormFromConfig("email", noPort), noPort)), {});
});

test("edit: clearing username removes it", () => {
  const f = { ...channelFormFromConfig("email", storedEmail), emailUser: "" };
  assert.deepEqual(ok(buildChannelConfig("email", f, storedEmail)), { username: null });
});

test("edit: switching to plain with the box ticked sends security (+ allow_insecure only if changed)", () => {
  // No stored password here: a security change with one would also require
  // re-entering it (covered above).
  const storedEmail_ = { ...storedEmail, password: undefined };
  const f = channelFormFromConfig("email", storedEmail_);
  assert.equal(f.allowInsecure, true); // seeded from the stored opt-in
  assert.deepEqual(ok(buildChannelConfig("email", { ...f, emailSecurity: "plain" }, storedEmail_)), {
    security: "plain",
  });
  const noOpt = { ...storedEmail_, allow_insecure: undefined };
  const g = channelFormFromConfig("email", noOpt);
  assert.equal(g.allowInsecure, false);
  assert.deepEqual(ok(buildChannelConfig("email", { ...g, emailSecurity: "plain", allowInsecure: true }, noOpt)), {
    security: "plain",
    allow_insecure: true,
  });
});

test("edit: unticking the opt-in on a plain channel is refused (would never deliver)", () => {
  const stored = { ...storedEmail, security: "plain" };
  const f = { ...channelFormFromConfig("email", stored), allowInsecure: false };
  assert.ok(errs(buildChannelConfig("email", f, stored)).allow_insecure);
});

test("edit: legacy from/to_addrs keys read back and are not re-sent unchanged", () => {
  const legacy = { host: "h", from: "a@x", to_addrs: ["b@x"] };
  const f = channelFormFromConfig("email", legacy);
  assert.equal(f.emailFrom, "a@x");
  assert.equal(f.emailTo, "b@x");
  assert.deepEqual(ok(buildChannelConfig("email", f, legacy)), {});
});

test("webhook edit: stored format kept; a missing format reads as generic", () => {
  const stored = { url: "https://x", secret: "__redacted__" };
  const f = channelFormFromConfig("webhook", stored);
  assert.equal(f.webhookFormat, "generic");
  assert.deepEqual(ok(buildChannelConfig("webhook", f, stored)), {});
  assert.deepEqual(ok(buildChannelConfig("webhook", { ...f, webhookFormat: "slack" }, stored)), {
    webhook_format: "slack",
  });
  assert.deepEqual(ok(buildChannelConfig("webhook", { ...f, clearWebhookSecret: true }, stored)), { secret: null });
});

test("apprise edit: blank = keep; typed replaces", () => {
  const stored = { url: "__redacted__" };
  const f = channelFormFromConfig("apprise", stored);
  assert.equal(f.appriseUrl, "");
  assert.deepEqual(ok(buildChannelConfig("apprise", f, stored)), {});
  assert.deepEqual(ok(buildChannelConfig("apprise", { ...f, appriseUrl: "ntfy://t" }, stored)), { url: "ntfy://t" });
});

test("hasStoredSecret reads the redacted marker per channel type", () => {
  assert.equal(hasStoredSecret("webhook", { secret: "__redacted__" }), true);
  assert.equal(hasStoredSecret("webhook", { url: "x" }), false);
  assert.equal(hasStoredSecret("email", { password: "__redacted__" }), true);
  assert.equal(hasStoredSecret("apprise", { url: "__redacted__" }), true);
  assert.equal(hasStoredSecret("email", undefined), false);
});

// ---- detectWebhookFormat (behaviour preserved) ---------------------------- //

test("detectWebhookFormat", () => {
  assert.equal(detectWebhookFormat("https://discord.com/api/webhooks/1/abc"), "discord");
  assert.equal(detectWebhookFormat("https://ptb.discord.com/api/webhooks/1/abc"), "discord");
  assert.equal(detectWebhookFormat("https://discordapp.com/api/webhooks/1/abc"), "discord");
  assert.equal(detectWebhookFormat("https://discord.com/channels/1"), "generic");
  assert.equal(detectWebhookFormat("https://evil-discord.com/api/webhooks/1"), "generic");
  assert.equal(detectWebhookFormat("https://hooks.slack.com/services/T/B/x"), "slack");
  assert.equal(detectWebhookFormat("https://x.hooks.slack.com/services"), "generic");
  assert.equal(detectWebhookFormat("not a url"), "generic");
  assert.equal(detectWebhookFormat(""), "generic");
});

// ---- rule timings (B42) --------------------------------------------------- //

const timings = (o: Partial<Parameters<typeof parseRuleTimings>[0]>) =>
  parseRuleTimings({ repeat: "", groupWait: 30, inhibitWindow: 900, ...o });

test("rule timings: valid values typed; blank repeat = null (never re-notify)", () => {
  assert.deepEqual(timings({ repeat: " 3600 " }), {
    ok: true,
    values: { repeat_interval_s: 3600, group_wait_s: 30, inhibit_window_s: 900 },
  });
  assert.deepEqual(timings({}), {
    ok: true,
    values: { repeat_interval_s: null, group_wait_s: 30, inhibit_window_s: 900 },
  });
});

test("rule timings: garbage repeat is a field error, never NaN/null", () => {
  for (const bad of ["abc", "1.5", "-1", "1e999"]) {
    const r = timings({ repeat: bad });
    assert.equal(r.ok, false);
    assert.ok((r as { ok: false; errors: Record<string, string> }).errors.repeat);
  }
});

test("rule timings: cleared group wait / inhibit window → explicit defaults (30 / 900), never null", () => {
  for (const blank of [null, undefined, ""]) {
    const r = timings({ groupWait: blank, inhibitWindow: blank });
    assert.deepEqual(r, { ok: true, values: { repeat_interval_s: null, group_wait_s: 30, inhibit_window_s: 900 } });
  }
});

test("rule timings: negative / fractional / over-cap → field errors", () => {
  const r = timings({ groupWait: -1, inhibitWindow: 86401 }) as { ok: false; errors: Record<string, string> };
  assert.equal(r.ok, false);
  assert.ok(r.errors.groupWait);
  assert.ok(r.errors.inhibitWindow);
  assert.equal(timings({ groupWait: 2.5 }).ok, false);
  assert.equal(timings({ inhibitWindow: 86400 }).ok, true);
});
