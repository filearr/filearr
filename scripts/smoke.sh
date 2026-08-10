#!/usr/bin/env bash
# smoke.sh — post-deploy sanity check for a running Filearr stack (OPS-T2).
#
# Hits the four load-bearing read endpoints and asserts a healthy shape. Prints a
# clear PASS/FAIL line per check and exits NON-ZERO if any check fails, so it
# doubles as a deploy gate (deploy-proxmox.sh runs it inside the CT after the
# build-stamp verification) and a manual "is it actually up?" tool.
#
# Usage:
#   scripts/smoke.sh [BASE_URL]
#     BASE_URL   default http://localhost:8000  (e.g. http://<ct-ip>:8484,
#                or an https URL — TLS cert is validated loosely, see -k below)
#
# Env:
#   FILEARR_SMOKE_TOKEN   optional Bearer token; sent when auth is enabled
#                         (/stats and /search require the `read` scope). Not
#                         needed when FILEARR_AUTH_ENABLED=false (deploy default).
#   SMOKE_TIMEOUT         per-request timeout seconds (default 15)
#   SMOKE_RETRIES         attempts per check before failing (default 4)
#   SMOKE_RETRY_DELAY     seconds before the first retry, doubling (default 2)
#   SMOKE_VERBOSE=1       print transport timing for every request, not just
#                         failures
#   SMOKE_NO_DIAGNOSTICS=1  skip the container/log dump after a final failure
#
# WHY THE RETRIES (2026-08-10): this runs the instant the deploy finishes, with
# no readiness wait in front of it, and `/stats` is the ONLY check that talks to
# Meilisearch. A stack that is still opening its index answers that one endpoint
# slowly or not at all for a few seconds, which showed up twice live as a bare
# "HTTP 000, meili not healthy" that cleared on the next run. Retrying makes the
# gate honest about readiness instead of racing it — but every retry is PRINTED,
# and the summary says how many happened, so a transient is never silently
# smoothed over. A stack that needs three attempts to answer is information.
#
# WHY THE CURL DIAGNOSTICS: "HTTP 000" only means "curl returned no status". The
# old version sent curl's stderr to /dev/null and dropped its exit code, so the
# one number that says WHICH failure it was — 7 refused, 28 timed out, 52 empty
# reply, 56 reset — was thrown away, and the failure was genuinely undiagnosable
# after the fact. Every failure now carries the decoded reason and the timing.
#
# HTTPS note: curl runs with -k (insecure) so the check works against Caddy's
# self-signed internal-CA cert without needing the LAN CA trusted on the box
# running the smoke test. It validates REACHABILITY + response shape, not the
# cert chain (trust verification is a separate manual step, see docs/ops).

set -uo pipefail

BASE_URL="${1:-http://localhost:8000}"
BASE_URL="${BASE_URL%/}"   # strip trailing slash
TIMEOUT="${SMOKE_TIMEOUT:-15}"
RETRIES="${SMOKE_RETRIES:-4}"
RETRY_DELAY="${SMOKE_RETRY_DELAY:-2}"
TOKEN="${FILEARR_SMOKE_TOKEN:-}"
VERBOSE="${SMOKE_VERBOSE:-0}"

pass=0 fail=0 retried=0
green()  { printf '  \033[32mPASS\033[0m %s\n' "$*"; }
red()    { printf '  \033[31mFAIL\033[0m %s\n' "$*"; }
yellow() { printf '  \033[33mRETRY\033[0m %s\n' "$*"; }
dim()    { printf '        \033[2m%s\033[0m\n' "$*"; }
ok()   { green "$*"; pass=$((pass+1)); }
bad()  { red   "$*"; fail=$((fail+1)); }

# curl_reason <exit-code> — the human meaning of curl's exit status. Only the
# codes this check can realistically produce are named; anything else is echoed
# as-is rather than guessed at.
curl_reason() {
  case "$1" in
    0)  echo "no transport error" ;;
    6)  echo "curl 6: could not resolve host" ;;
    7)  echo "curl 7: connection refused / no listener" ;;
    28) echo "curl 28: timed out after ${TIMEOUT}s" ;;
    35) echo "curl 35: TLS handshake failed" ;;
    52) echo "curl 52: empty reply (server closed without responding)" ;;
    56) echo "curl 56: connection reset while receiving" ;;
    60) echo "curl 60: TLS certificate not trusted (unexpected — -k is set)" ;;
    *)  echo "curl ${1}" ;;
  esac
}

# fetch <path> -> sets HTTP_CODE, BODY, CURL_RC, CURL_ERR, TIMING globals.
#
# The timing triple separates the two failure shapes that both print as 000:
# a connect that never completed (refused/unreachable) versus a connect that
# succeeded and then hung (the app blocked on a Meili call), which is the one
# actually seen live.
fetch() {
  local path="$1"; local hdr=()
  [[ -n "$TOKEN" ]] && hdr=(-H "Authorization: Bearer ${TOKEN}")
  local raw errfile
  errfile=$(mktemp)
  raw=$(curl -sk --max-time "$TIMEOUT" "${hdr[@]}" \
        -w $'\n%{http_code} %{time_connect} %{time_starttransfer} %{time_total}' \
        "${BASE_URL}${path}" 2>"$errfile")
  CURL_RC=$?
  CURL_ERR=$(tr -d '\r' <"$errfile" | tr '\n' ' ' | sed 's/  */ /g; s/ *$//')
  rm -f "$errfile"

  local tail="${raw##*$'\n'}"
  BODY="${raw%$'\n'*}"
  # A transport failure means the -w line may be absent or partial; fall back to
  # 000 rather than parsing garbage into HTTP_CODE.
  if [[ "$CURL_RC" -ne 0 ]]; then
    HTTP_CODE="000"
    TIMING="connect=$(awk '{print $2}' <<<"$tail" 2>/dev/null || echo '?')s total=$(awk '{print $4}' <<<"$tail" 2>/dev/null || echo '?')s"
    [[ "$raw" == *$'\n'* ]] || BODY=""
  else
    HTTP_CODE=$(awk '{print $1}' <<<"$tail")
    TIMING="connect=$(awk '{print $2}' <<<"$tail")s ttfb=$(awk '{print $3}' <<<"$tail")s total=$(awk '{print $4}' <<<"$tail")s"
  fi
  [[ "$VERBOSE" == "1" ]] && dim "${path} → ${HTTP_CODE} (${TIMING})"
  return 0
}

# failure_detail — the one-line "why" appended to a FAIL/RETRY message.
failure_detail() {
  if [[ "$HTTP_CODE" == "000" ]]; then
    local why; why=$(curl_reason "$CURL_RC")
    printf 'HTTP 000 — %s [%s]%s' "$why" "$TIMING" \
      "${CURL_ERR:+ — curl said: ${CURL_ERR}}"
  else
    printf 'HTTP %s [%s], body: %s' "$HTTP_CODE" "$TIMING" "${BODY:0:160}"
  fi
}

# check <label> <path> <validate-fn> [retry_on_bad_shape]
#
# Retries a TRANSPORT failure (000) or a 5xx unconditionally — those are the
# shapes a still-warming stack produces. A 200 whose BODY fails the assertion is
# retried only when the caller says the assertion is about readiness (Meili
# health) rather than about configuration (a null build stamp will still be null
# on the fourth attempt, and retrying it just makes a clear error take longer).
check() {
  local label="$1" path="$2" validate="$3" retry_bad_shape="${4:-yes}"
  local attempt=1 delay="$RETRY_DELAY"
  while :; do
    fetch "$path"
    if "$validate"; then
      if [[ "$attempt" -gt 1 ]]; then
        ok "${label} — ${DETAIL} (after ${attempt} attempts)"
      else
        ok "${label} — ${DETAIL}"
      fi
      return 0
    fi
    local transient=no
    if [[ "$HTTP_CODE" == "000" || "$HTTP_CODE" =~ ^5 ]]; then
      transient=yes
    elif [[ "$HTTP_CODE" == "200" && "$retry_bad_shape" == "yes" ]]; then
      transient=yes
    fi
    if [[ "$transient" == "yes" && "$attempt" -lt "$RETRIES" ]]; then
      yellow "${label} — attempt ${attempt}/${RETRIES}: $(failure_detail)"
      dim "retrying in ${delay}s (the stack may still be warming up)"
      retried=$((retried+1))
      sleep "$delay"
      delay=$((delay*2))
      attempt=$((attempt+1))
      continue
    fi
    bad "${label} — $(failure_detail)"
    return 1
  done
}

# --- per-check assertions (read HTTP_CODE/BODY, set DETAIL) -----------------
v_health() {
  if [[ "$HTTP_CODE" == "200" ]] && grep -Eq '"status"[[:space:]]*:[[:space:]]*"ok"' <<<"$BODY"; then
    DETAIL="200, status ok"; return 0
  fi
  return 1
}

v_version() {
  STAMP=$(sed -n 's/.*"build_stamp"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' <<<"$BODY")
  if [[ "$HTTP_CODE" == "200" && -n "$STAMP" ]]; then
    DETAIL="200, build_stamp=${STAMP}"; return 0
  fi
  if [[ "$HTTP_CODE" == "200" ]]; then
    # Deterministic, not transient: say so instead of retrying into the same answer.
    DETAIL="200 but build_stamp is null (dev checkout, not a deployed image?)"
  fi
  return 1
}

v_stats() {
  if [[ "$HTTP_CODE" == "200" ]] && grep -Eq '"healthy"[[:space:]]*:[[:space:]]*true' <<<"$BODY"; then
    DETAIL="200, meili.healthy true"; return 0
  fi
  return 1
}

v_search() {
  if [[ "$HTTP_CODE" == "200" ]]; then DETAIL="200"; return 0; fi
  return 1
}

# diagnostics — printed ONCE after a final failure, because the whole point of
# this pass is that the operator cannot reproduce a transient on demand. Every
# command is guarded and best-effort: this runs inside the CT during a deploy,
# and a missing docker or an unreadable compose file must never turn a useful
# report into a second error.
diagnostics() {
  [[ "${SMOKE_NO_DIAGNOSTICS:-0}" == "1" ]] && return 0
  command -v docker >/dev/null 2>&1 || return 0
  local app_dir
  app_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd) || return 0
  [[ -f "${app_dir}/docker-compose.yml" ]] || return 0

  echo ""
  echo "── diagnostics (a failed smoke is usually the stack, not the check) ──"
  echo "  container state:"
  (cd "$app_dir" && docker compose ps 2>&1 | sed 's/^/    /') || true
  echo "  meilisearch — last 30 log lines:"
  (cd "$app_dir" && docker compose logs --tail=30 --no-color meilisearch 2>&1 | sed 's/^/    /') || true
  echo "  app — last 30 log lines:"
  (cd "$app_dir" && docker compose logs --tail=30 --no-color app 2>&1 | sed 's/^/    /') || true
  echo "  host resources (a Meili stall is often disk or memory):"
  df -h "$app_dir" 2>&1 | sed 's/^/    /' || true
  free -m 2>&1 | sed 's/^/    /' || true
  echo "── end diagnostics ──"
}

echo "── Filearr smoke test → ${BASE_URL} ──"
[[ "$RETRIES" -gt 1 ]] && dim "up to ${RETRIES} attempts per check, ${TIMEOUT}s timeout each"

check "/api/v1/health"            "/api/v1/health"            v_health
check "/api/v1/version"           "/api/v1/version"           v_version no
check "/api/v1/stats"             "/api/v1/stats"             v_stats
check "/api/v1/search?q=smoke"    "/api/v1/search?q=smoke&limit=1" v_search

summary="── ${pass} passed, ${fail} failed"
[[ "$retried" -gt 0 ]] && summary="${summary}, ${retried} retried"
echo "${summary} ──"

if [[ "$retried" -gt 0 && "$fail" -eq 0 ]]; then
  # Worth saying out loud: the deploy is green, but the stack was not ready when
  # asked. Repeated occurrences point at a slow Meili open or an under-resourced
  # box, and this line is the only record that it happened.
  echo "NOTE: every check eventually passed, but ${retried} attempt(s) failed first —" >&2
  echo "      the stack was still warming. Rerun with SMOKE_VERBOSE=1 for per-request timing." >&2
fi

if [[ "$fail" -ne 0 ]]; then
  diagnostics
  echo "SMOKE FAILED" >&2
  exit 1
fi
echo "SMOKE PASSED"
