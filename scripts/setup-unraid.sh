#!/usr/bin/env bash
# setup-unraid.sh — guided, idempotent, resumable setup for the Filearr stack ON
# an Unraid server. Run it in the Unraid terminal (the web terminal icon, or SSH)
# as root; the Unraid shell IS root, so there is no sudo dance.
#
#   mkdir -p /boot/config/plugins/filearr
#   curl -fsSL https://raw.githubusercontent.com/pwsh/filearr/main/scripts/setup-unraid.sh \
#     -o /boot/config/plugins/filearr/setup-unraid.sh
#   bash /boot/config/plugins/filearr/setup-unraid.sh
#
# It lives on the FLASH deliberately: /boot survives reboots and array stops, so
# a resumed run, a --check months later and the saved answers are all the same
# copy. Invoke it as `bash <file>` rather than `./<file>`: the flash is vfat, its
# permission bits are a mount-time fiction, and `bash <file>` does not care.
#
# WHAT IT AUTOMATES — everything in docs-site/deployment/unraid.md EXCEPT the two
# things a shell genuinely cannot do:
#   * the per-container **Apply** click in the Docker tab (Unraid's container
#     creation is a webGUI action driven by dockerMan; there is no supported CLI),
#   * the **DNS records** on your LAN resolver (it is not this box).
# Everything else — the Docker setting, the network, the appdata directories and
# their ownership, the secrets, every template field, the CA harvest and the
# provisioner tuning — is done here.
#
# HOW IT RUNS
#   phase 0  preflight        read-only PASS/WARN/FAIL gate. No mutation happens
#                             before it passes. FAIL blocks; WARN needs a yes.
#   phase 1  prepare          answers wizard -> Docker setting -> network ->
#                             appdata dirs + ownership -> secrets -> the five
#                             filled-in templates -> the caddy re-attach helper.
#   phase 2  walkthrough      an interactive, one-container-at-a-time loop: it
#                             tells you which template to Apply, waits for you,
#                             then PROBES the container for real readiness. The
#                             step-ca harvest (fingerprint, admin password, the
#                             provisioner JWK, the certificate lifetimes) runs
#                             inline at the step-ca step.
#   phase 3  summary          the DNS records table for the addresses you chose,
#                             and the honest list of what is still yours to do.
#
# Re-running is always safe: finished phases and verified containers are recorded
# in the state file and skipped. Nothing that exists is ever regenerated —
# secrets least of all (see the FILEARR_SECRET_KEY discipline at gen_secret).
#
# FLAGS
#   (none)              run the next unfinished phase, then continue
#   --check             verify only: preflight + post-deploy assertions, PASS/FAIL
#                       per item, non-zero exit if anything fails. The post-deploy
#                       validator — safe to run any time, mutates nothing.
#   --summary           re-print the handoff summary (addresses, DNS table, paths,
#                       safeguarding, next steps) from saved state, with the same
#                       explicit prompt before any secret is shown. Closing the
#                       terminal therefore loses nothing.
#   --reconfigure       re-ask the wizard (keeps existing secrets)
#   --force             regenerate templates even for containers that exist
#   --local-dir DIR     take the six templates from a local checkout's unraid/
#                       folder instead of GitHub (air-gapped boxes)
#   --phase N           run exactly phase N (0|1|2|3) and stop
#   --yes               assume yes to WARN confirmations (not to the Docker
#                       service cycle — that one always asks)
#   --help
#
# THE SEVEN SHAKEOUT TRAPS THIS SCRIPT ABSORBS. Every one of them cost the first
# live Unraid deployment real time on 2026-08-14/15. They are documented as
# warnings in the guide (searchable by their verbatim errors, which is why the
# errors are quoted in the comments below) — but a warning you have to read is a
# trap you can still fall into, so each one is a STEP here instead:
#
#   TRAP 1  Preserve user defined networks OFF -> a CLI-created network is
#           invisible in the template dropdown and deleted on the next Docker
#           service restart.                             -> check_preserve_networks
#   TRAP 2  The network must be created AFTER that setting is on, or it is the
#           network that gets deleted.                   -> create_filearr_network
#   TRAP 3  Caddy on any bridge-type network can never bind host port 80 (the
#           Unraid webGUI owns it) -> topology prompt, Caddy on br0 with a fixed
#           IP, and an installed re-attach helper for the dual-homing.
#                                                        -> ask_topology / install_reattach_helper
#   TRAP 4  "/entrypoint.sh: line 56: /home/step/password: Permission denied" —
#           step-ca's appdata must be chown -R 1000:1000 BEFORE first start.
#                                                        -> create_appdata_dirs
#   TRAP 5  The root fingerprint and the CA ADMIN password are printed ONCE into
#           the container log and never again.           -> harvest_ca
#   TRAP 6  "No admin credentials found. You must login to execute admin
#           commands." — admin-API calls need the --admin-subject /
#           --admin-provisioner / --admin-password-file trio.
#                                                        -> set_provisioner_claims
#   TRAP 7  "open /home/step/adminpw failed: permission denied" — host-side
#           staging files are owned by root and the container runs as step
#           (1000). Eliminated rather than handled: the JWK extraction happens
#           ENTIRELY inside docker exec, JWE piped in on stdin, plaintext
#           captured on stdout. There are no host-side staging files at all.
#                                                        -> extract_provisioner_jwk
#
# TOOLING CONTRACT: bash, coreutils, sed/grep/awk, docker and curl only — the
# set that is guaranteed present on a stock Unraid box. In particular there is
# NO jq (it is not shipped, and a setup script that needs a package manager is
# not a setup script); JSON is pulled apart with grep/sed exactly the way the
# guide's manual commands do, or inside a container that has real tooling.
# openssl is used for secrets when present, with a /dev/urandom fallback.
#
# House style follows proxmox/deploy-proxmox.sh — numbered phases, ask/confirm
# with defaults, answers saved and re-used, every run idempotent.
#
# License: AGPL-3.0-or-later, same as the rest of Filearr.

set -euo pipefail
set -E  # ERR trap inherits into functions and subshells

# Under `set -e` a failed command used to kill a script silently, and a
# "successful-looking" no-op run is the worst failure mode a setup tool has
# (smoke.sh learned this the same way). Every unexpected exit names the phase,
# the step, the line and the command.
CURRENT_PHASE="startup"
CURRENT_STEP="argument parsing"
trap 'rc=$?; echo >&2; echo "✗ SETUP FAILED (exit $rc)" >&2;
      echo "  phase: ${CURRENT_PHASE}" >&2;
      echo "  step:  ${CURRENT_STEP}" >&2;
      echo "  line ${LINENO}: ${BASH_COMMAND}" >&2;
      echo "  Nothing is half-applied that a re-run cannot finish: re-run this" >&2;
      echo "  script to resume from the last completed phase, or trace it with" >&2;
      echo "     bash -x $0 ${ORIG_ARGS:-}" >&2' ERR

ORIG_ARGS="$*"

# --------------------------------------------------------------------------- #
# Constants                                                                    #
# --------------------------------------------------------------------------- #

VERSION="1.0.0 (2026-08-15)"

# Flash-backed state. /boot survives reboots, array stops and (unlike appdata) a
# pool that has not been mounted yet, which is exactly what a resumable setup
# tool needs. It is vfat, so see the mode warning in save_secrets.
STATE_DIR="/boot/config/plugins/filearr"
CONF="${STATE_DIR}/setup.conf"          # answers (no secrets)
SECRETS="${STATE_DIR}/secrets.env"      # generated secrets — your backup .env
STATE="${STATE_DIR}/setup.state"        # phase / per-container progress markers
HELPER="${STATE_DIR}/filearr-caddy-network.sh"

DOCKER_CFG="/boot/config/docker.cfg"
TEMPLATE_DIR="/boot/config/plugins/dockerMan/templates-user"
RC_DOCKER="/etc/rc.d/rc.docker"
USER_SCRIPTS_DIR="/boot/config/plugins/user.scripts/scripts"

TEMPLATE_BASE_URL="https://raw.githubusercontent.com/pwsh/filearr/main/unraid"
LOCAL_DIR=""            # --local-dir override

# The stack, in the order containers must be APPLIED. Note step-ca comes BEFORE
# filearr: the app template wants the CA fingerprint and the provisioner JWK,
# and those only exist once the CA has booted. Applying the CA first lets this
# script patch the values into my-filearr.xml before you ever open its Edit page,
# which removes a whole re-Apply round trip. Nothing in step-ca depends on the
# app (it is a standalone CA — it never calls out), so the reorder is free.
# The guide documents the same order.
APPLY_ORDER_SIMPLE="filearr-postgres filearr-meilisearch filearr"
APPLY_ORDER_FULL="filearr-postgres filearr-meilisearch filearr-stepca filearr filearr-caddy"

PROBE_TIMEOUT="${FILEARR_SETUP_PROBE_TIMEOUT:-180}"   # seconds per readiness probe

# Runtime flags
MODE="run"
PHASE_ONLY=""
FORCE=0
RECONFIGURE=0
ASSUME_YES=0

# Preflight tallies
PF_FAIL=0
PF_WARN=0

# --------------------------------------------------------------------------- #
# Output + interaction helpers (deliberately the deploy-proxmox.sh idiom)      #
# --------------------------------------------------------------------------- #

phase() { CURRENT_PHASE="$*"; echo; echo "═══════════════════════════════════════════════════════════════════════════"; echo "  PHASE $*"; echo "═══════════════════════════════════════════════════════════════════════════"; }
step()  { CURRENT_STEP="$*"; echo; echo "── $* ──"; }
info()  { echo "    $*"; }
warn()  { echo "    ! $*"; }
die()   { echo "ERROR: $*" >&2; exit 1; }

# ask PROMPT [DEFAULT] -> answer in $REPLY (same signature as deploy-proxmox.sh)
ask() {
  local p="$1" d="${2:-}"
  if [[ -n "$d" ]]; then read -r -p "  $p [$d]: " REPLY; REPLY="${REPLY:-$d}"
  else read -r -p "  $p: " REPLY; fi
}

# ask_bool PROMPT DEFAULT(true|false)  -> REPLY = true|false (re-asks on junk)
ask_bool() {
  local p="$1" d="$2"
  while :; do
    ask "$p (yes/no)" "$([[ "$d" == true ]] && echo yes || echo no)"
    case "${REPLY,,}" in
      y|yes|true|1)  REPLY=true;  return 0 ;;
      n|no|false|0)  REPLY=false; return 0 ;;
      *) echo "  answer yes or no" ;;
    esac
  done
}

# confirm PROMPT [default-yes]  -> 0 on yes
confirm() {
  local p="$1" dflt="${2:-n}" ans
  if [[ "$ASSUME_YES" == 1 && "${3:-}" != "always-ask" ]]; then echo "  $p [auto-yes]"; return 0; fi
  if [[ "$dflt" == "y" ]]; then read -r -p "  $p [Y/n] " ans; ans="${ans:-y}"
  else read -r -p "  $p [y/N] " ans; ans="${ans:-n}"; fi
  [[ "$ans" == y || "$ans" == Y || "$ans" == yes ]]
}

# PASS/WARN/FAIL rows for the preflight gate and --check. Fixed-width label so
# the block reads as a table without needing column(1).
pass() { printf '  [ PASS ] %-38s %s\n' "$1" "${2:-}"; }
warnr() { printf '  [ WARN ] %-38s %s\n' "$1" "${2:-}"; PF_WARN=$((PF_WARN+1)); }
failr() { printf '  [ FAIL ] %-38s %s\n' "$1" "${2:-}"; PF_FAIL=$((PF_FAIL+1)); }
note()  { printf '  [ info ] %-38s %s\n' "$1" "${2:-}"; }

# A secret must never reach the terminal, the log, or a screenshot — but the
# operator still needs to be able to tell two values apart and to check a
# restore. Fingerprint = first 16 hex of sha256, the same shape the About page
# shows for FILEARR_SECRET_KEY.
fingerprint() {
  local v="$1" h=""
  if command -v sha256sum >/dev/null 2>&1; then h="$(printf '%s' "$v" | sha256sum | cut -c1-16)"
  elif command -v openssl >/dev/null 2>&1; then h="$(printf '%s' "$v" | openssl dgst -sha256 | sed 's/.*= *//' | cut -c1-16)"
  else h="(no sha256 tool)"; fi
  printf '%s' "$h"
}

# --------------------------------------------------------------------------- #
# State + answers persistence                                                  #
# --------------------------------------------------------------------------- #

state_get() { grep -E "^$1=" "$STATE" 2>/dev/null | tail -1 | cut -d= -f2- || true; }
state_set() {
  mkdir -p "$STATE_DIR"; touch "$STATE"
  local tmp; tmp="$(mktemp)"
  grep -vE "^$1=" "$STATE" 2>/dev/null > "$tmp" || true
  printf '%s=%s\n' "$1" "$2" >> "$tmp"
  cat "$tmp" > "$STATE"; rm -f "$tmp"
}
state_done() { [[ "$(state_get "$1")" == "1" ]]; }

save_conf() {
  mkdir -p "$STATE_DIR"
  cat > "$CONF" <<EOF
# Filearr Unraid setup — saved answers. Written by scripts/setup-unraid.sh.
# Safe to edit by hand, then re-run the script. NO SECRETS LIVE HERE (see
# ${SECRETS}).
TIER=${TIER}
TOPOLOGY=${TOPOLOGY}
BRIDGE_IF=${BRIDGE_IF}
APPDATA_CACHE=${APPDATA_CACHE}
APPDATA_USER=${APPDATA_USER}
MEDIA_PATH=${MEDIA_PATH}
WEBUI_PORT=${WEBUI_PORT}
TZ_=${TZ_}
PG_USER=${PG_USER}
PG_DB=${PG_DB}
IP_POSTGRES=${IP_POSTGRES}
IP_MEILI=${IP_MEILI}
IP_APP=${IP_APP}
IP_STEPCA=${IP_STEPCA}
IP_CADDY=${IP_CADDY}
CADDY_PROFILE=${CADDY_PROFILE}
SEMANTIC=${SEMANTIC}
AGENTS=${AGENTS}
CONTENT_SNIFF=${CONTENT_SNIFF}
THUMB_BUDGET_GB=${THUMB_BUDGET_GB}
UPDATE_CHECK=${UPDATE_CHECK}
TLS_DOMAIN=${TLS_DOMAIN}
ACME_EMAIL=${ACME_EMAIL}
EOF
  chmod 600 "$CONF" 2>/dev/null || true
  info "saved answers -> $CONF"
}

load_conf() { [[ -f "$CONF" ]] && source "$CONF" || true; }

# --------------------------------------------------------------------------- #
# Secrets                                                                      #
# --------------------------------------------------------------------------- #

# Random hex. openssl is present on Unraid, but the /dev/urandom + od fallback
# costs two lines and removes the dependency entirely (deploy-proxmox.sh uses
# the same pair).
gen_hex() {
  local n="$1"
  if command -v openssl >/dev/null 2>&1; then openssl rand -hex "$n"
  else head -c "$n" /dev/urandom | od -An -tx1 | tr -d ' \n'; fi
}

secret_get() { grep -E "^$1=" "$SECRETS" 2>/dev/null | tail -1 | cut -d= -f2- || true; }

# secret_put KEY VALUE — upsert, keeping the file single-valued per key. Used
# for secrets that are DISCOVERED rather than generated (the Cloudflare token,
# the CA admin password, the provisioner JWK), so that ${SECRETS} really is the
# one canonical copy the handoff summary promises it is.
secret_put() {
  local key="$1" val="$2" tmp
  mkdir -p "$STATE_DIR"; touch "$SECRETS"
  tmp="$(mktemp)"
  grep -v "^${key}=" "$SECRETS" 2>/dev/null > "$tmp" || true
  printf '%s=%s\n' "$key" "$val" >> "$tmp"
  cat "$tmp" > "$SECRETS"; rm -f "$tmp"
  chmod 600 "$SECRETS" 2>/dev/null || true
}

# gen_secret KEY BYTES  — generate ONLY if absent. This is the whole discipline:
# FILEARR_SECRET_KEY is the envelope key for alert-channel credentials and it is
# NOT inside a database dump. Regenerating it on a re-run would leave every
# stored SMTP password and webhook secret permanently undecryptable while every
# API call still reported success. So: existing value wins, always, and the only
# way to change one is to edit ${SECRETS} deliberately.
gen_secret() {
  local key="$1" bytes="$2" cur
  cur="$(secret_get "$key")"
  if [[ -n "$cur" ]]; then
    info "$key: kept (fingerprint $(fingerprint "$cur")) — never regenerated"
    return 0
  fi
  local val; val="$(gen_hex "$bytes")"
  mkdir -p "$STATE_DIR"; touch "$SECRETS"
  printf '%s=%s\n' "$key" "$val" >> "$SECRETS"
  chmod 600 "$SECRETS" 2>/dev/null || true
  info "$key: generated (fingerprint $(fingerprint "$val"))"
}

save_secrets_header() {
  mkdir -p "$STATE_DIR"
  if [[ ! -f "$SECRETS" ]]; then
    cat > "$SECRETS" <<'EOF'
# Filearr — generated secrets. THIS FILE IS YOUR BACKUP .env.
#
# COPY IT OFF THIS BOX, TODAY, into your password manager or an encrypted
# archive. FILEARR_SECRET_KEY in particular is NOT inside a database dump:
# restore a dump under a different key and every stored alert-channel secret
# (SMTP passwords, webhook secrets, Apprise URLs) becomes permanently
# undecryptable while everything still reports success.
#
# Nothing here is ever regenerated by the setup script. Delete a line only if
# you understand what re-generating that value destroys.
EOF
  fi
  chmod 600 "$SECRETS" 2>/dev/null || true
  # The flash is vfat: chmod is accepted and then ignored, because DOS
  # permission bits cannot express a mode. Say so rather than implying a
  # protection that is not there.
  local mode; mode="$(stat -c '%a' "$SECRETS" 2>/dev/null || echo '?')"
  if [[ "$mode" != "600" ]]; then
    warn "the flash is vfat, so ${SECRETS} is mode ${mode}, not 600 —"
    warn "file permissions are not enforceable there. That is one more reason to"
    warn "copy this file off the box and treat the flash as recoverable, not secret."
  fi
}

# --------------------------------------------------------------------------- #
# Small XML / template surgery (no xmllint, no jq — sed/awk only)              #
# --------------------------------------------------------------------------- #

# XML-escape a value destined for element text. Unraid stores every Config value
# XML-escaped, and the provisioner JWK is a JSON object full of double quotes —
# pasting it raw produces a template Unraid cannot parse, which presents as a
# container whose every field is suddenly empty.
#
# Implemented with sed, NOT bash's ${var//x/y}: since bash 5.2 the shopt
# `patsub_replacement` is on by default and makes `&` in a substitution
# replacement expand to the matched text, so the obvious ${s//\"/&quot;} silently
# produces "quot; instead of &quot; — caught here by testing the real templates
# rather than reading the code. In a sed replacement `\&` is unambiguously a
# literal ampersand on every sed worth the name. Ampersand first, or the
# escapes escape each other.
xml_escape() {
  printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' -e 's/"/\&quot;/g'
}

# set_cfg FILE MATCH VALUE
#   MATCH is a literal attribute fragment identifying the Config line, e.g.
#   'Target="FILEARR_MEILI_URL"' or 'Name="Data"'. Handles both the
#   value-bearing form (<Config ...>old</Config>) and the empty self-closing
#   form (<Config ... />) that Unraid writes for blank fields.
#
#   awk rather than sed because the replacement text is arbitrary — a sed
#   replacement would have to escape & and the delimiter, and the JWK contains
#   &quot; after escaping. awk with -v and substr() interprets nothing.
set_cfg() {
  local file="$1" match="$2" value="$3" tmp
  value="$(xml_escape "$value")"
  tmp="$(mktemp)"
  awk -v m="$match" -v v="$value" '
    {
      if (index($0, "<Config") > 0 && index($0, m) > 0) {
        g = index($0, ">")                    # first > closes the opening tag:
                                              # attribute text is XML-escaped, so
                                              # a raw > cannot occur inside it
        if (g > 1 && substr($0, g-1, 1) == "/") open = substr($0, 1, g-2) ">"
        else                                   open = substr($0, 1, g)
        print open v "</Config>"
        next
      }
      print
    }' "$file" > "$tmp"
  cat "$tmp" > "$file"; rm -f "$tmp"
}

# set_elem FILE TAG VALUE — replace a simple top-level element's text.
set_elem() {
  local file="$1" tag="$2" value="$3" tmp
  value="$(xml_escape "$value")"
  tmp="$(mktemp)"
  awk -v t="$tag" -v v="$value" '
    {
      o = "<" t ">"; c = "</" t ">"
      if (index($0, o) > 0 && index($0, c) > 0) {
        i = index($0, o)
        print substr($0, 1, i - 1) o v c
        next
      }
      print
    }' "$file" > "$tmp"
  cat "$tmp" > "$file"; rm -f "$tmp"
}

# add_elem_before_close FILE XMLLINE — append an element just before </Container>
# (used for <MyIP> and for Caddy's <PostArgs>, neither of which exists in the
# pristine templates).
add_elem_before_close() {
  local file="$1" line="$2" tmp
  tmp="$(mktemp)"
  awk -v l="$line" '{ if ($0 ~ /<\/Container>/) print "  " l; print }' "$file" > "$tmp"
  cat "$tmp" > "$file"; rm -f "$tmp"
}

# get_cfg FILE MATCH — read a Config value back (used by --check).
get_cfg() {
  local file="$1" match="$2"
  awk -v m="$match" '
    index($0, "<Config") > 0 && index($0, m) > 0 {
      g = index($0, ">")
      if (g > 1 && substr($0, g-1, 1) == "/") { print ""; exit }
      rest = substr($0, g + 1)
      e = index(rest, "</Config>")
      if (e > 0) print substr(rest, 1, e - 1); else print ""
      exit
    }' "$file" 2>/dev/null || true
}

# --------------------------------------------------------------------------- #
# Docker helpers                                                               #
# --------------------------------------------------------------------------- #

container_exists() { docker container inspect "$1" >/dev/null 2>&1; }
container_running() { [[ "$(docker container inspect -f '{{.State.Running}}' "$1" 2>/dev/null || echo false)" == "true" ]]; }
network_exists() { docker network inspect "$1" >/dev/null 2>&1; }

# The container's address on a given network, for host-side probing.
container_ip() {
  docker container inspect -f \
    "{{with index .NetworkSettings.Networks \"$2\"}}{{.IPAddress}}{{end}}" "$1" 2>/dev/null || true
}

# --------------------------------------------------------------------------- #
# PHASE 0 — preflight. READ ONLY. Nothing below this line mutates anything.    #
# --------------------------------------------------------------------------- #

# TRAP 1 (live 2026-08-14) — "Preserve user defined networks".
#
# Unraid deletes user-defined Docker networks it did not create every time the
# Docker service starts, unless this setting is on. The failure is genuinely
# confusing because it is delayed: `docker network create filearr` succeeds,
# `docker network ls` lists it, the template dropdown never shows "Custom :
# filearr", and after the next Docker restart the network is simply gone.
#
# The key is DOCKER_USER_NETWORKS in /boot/config/docker.cfg and it is NOT a
# boolean — it takes the semantic strings "preserve" and "remove". Verified
# against the Unraid webgui sources rather than guessed: DockerSettings.page
# defines the field, and etc/rc.d/rc.docker consumes it as
#     [[ $DOCKER_USER_NETWORKS != preserve ]] && docker network rm $NETWORK
# (github.com/unraid/webgui). The cfg file uses shell-style KEY="value" quoting.
# Related keys, checked for context below: DOCKER_ALLOW_ACCESS ("yes"/empty) is
# "Host access to custom networks", DOCKER_NETWORK_TYPE ("1" = ipvlan, empty =
# macvlan).
preserve_networks_state() {
  # -> preserve | remove | unknown
  if [[ ! -r "$DOCKER_CFG" ]]; then echo unknown; return 0; fi
  local v
  v="$(grep -E '^DOCKER_USER_NETWORKS=' "$DOCKER_CFG" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"' || true)"
  case "$v" in
    preserve) echo preserve ;;
    remove)   echo remove ;;
    "")       echo remove ;;   # absent == the default, which is remove
    *)        echo unknown ;;
  esac
}

host_access_state() {
  local v
  v="$(grep -E '^DOCKER_ALLOW_ACCESS=' "$DOCKER_CFG" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"' || true)"
  [[ "$v" == "yes" ]] && echo yes || echo no
}

# Best-effort address collision check. Deliberately labelled best-effort: a
# device that is powered off answers nothing, and a firewall can swallow ICMP,
# so silence is NOT proof the address is free. It still catches the common case
# of typing the address of something already on the LAN.
ip_in_use() {
  local ip="$1"
  ping -c1 -W1 "$ip" >/dev/null 2>&1 && return 0
  # ARP cache second opinion — a host that ignores ping often still ARPs.
  if command -v arp >/dev/null 2>&1; then
    arp -n "$ip" 2>/dev/null | grep -qE '([0-9a-f]{2}:){5}' && return 0
  fi
  return 1
}

preflight() {
  phase "0 — preflight (read-only; nothing is changed yet)"
  PF_FAIL=0; PF_WARN=0

  step "environment"
  if [[ -d /boot/config ]]; then pass "Unraid flash (/boot/config)" "present"
  else failr "Unraid flash (/boot/config)" "missing — is this an Unraid box?"; fi

  if [[ "$(id -u)" == "0" ]]; then pass "running as root" ""
  else failr "running as root" "the Unraid terminal is root; re-run there"; fi

  if command -v docker >/dev/null 2>&1; then
    if docker info >/dev/null 2>&1; then
      pass "docker service" "running ($(docker version --format '{{.Server.Version}}' 2>/dev/null || echo 'version unknown'))"
    else
      failr "docker service" "installed but not answering — Settings -> Docker -> Enable Docker: Yes"
    fi
  else
    failr "docker" "not found"
  fi

  for t in curl awk sed grep; do
    command -v "$t" >/dev/null 2>&1 && pass "tool: $t" "" || failr "tool: $t" "required"
  done
  # jq is deliberately NOT required and deliberately NOT used anywhere in this
  # script; noted so a reader does not go looking for it.
  command -v openssl >/dev/null 2>&1 && pass "tool: openssl" "secrets" \
    || note "tool: openssl" "absent — falling back to /dev/urandom for secrets"

  step "Unraid Docker settings"
  local pn; pn="$(preserve_networks_state)"
  case "$pn" in
    preserve) pass "preserve user defined networks" 'DOCKER_USER_NETWORKS="preserve"' ;;
    remove)
      if [[ "${TOPOLOGY:-A}" == "A" ]]; then
        warnr "preserve user defined networks" 'currently "remove"'
        info   "  Phase 1 would set DOCKER_USER_NETWORKS=\"preserve\" in ${DOCKER_CFG}"
        info   "  and then CYCLE THE DOCKER SERVICE, which STOPS EVERY CONTAINER on"
        info   "  this box (not just Filearr's) for a few seconds. It asks first."
        info   "  Without it, the 'filearr' network is deleted on the next Docker"
        info   "  restart and never appears in the template Network Type dropdown."
      else
        note "preserve user defined networks" 'currently "remove" — not needed for topology B'
      fi
      ;;
    *) warnr "preserve user defined networks" "could not read ${DOCKER_CFG}" ;;
  esac

  local ha; ha="$(host_access_state)"
  if [[ "${TOPOLOGY:-A}" == "B" && "$ha" == "no" ]]; then
    warnr "host access to custom networks" 'DOCKER_ALLOW_ACCESS is not "yes"'
    info   "  Topology B puts every container on br0 (macvlan/ipvlan), and a"
    info   "  macvlan child and its parent are isolated BY DESIGN: this server"
    info   "  will not be able to reach its own containers, in either direction."
    info   "  Fix in the UI: Settings -> Docker -> Host access to custom networks:"
    info   "  Yes (needs the Docker service stopped). This script does NOT change"
    info   "  it for you — it also decides how the rest of your containers behave."
  else
    note "host access to custom networks" "$ha"
  fi

  local nt
  nt="$(grep -E '^DOCKER_NETWORK_TYPE=' "$DOCKER_CFG" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)"
  note "custom network type" "$([[ "$nt" == "1" ]] && echo ipvlan || echo macvlan)"

  step "storage"
  local cache_parent="${APPDATA_CACHE:-/mnt/cache/appdata}"
  case "$cache_parent" in
    /mnt/user/*|/mnt/user0/*)
      failr "cache pool path" "$cache_parent is on the shfs FUSE layer — Postgres/Meili/CA corrupt there; use the pool's direct path" ;;
  esac
  if [[ -d "$(dirname "$cache_parent")" ]]; then
    if [[ -w "$(dirname "$cache_parent")" ]]; then
      pass "cache pool path" "$(dirname "$cache_parent") writable"
    else
      failr "cache pool path" "$(dirname "$cache_parent") not writable"
    fi
  else
    failr "cache pool path" "$(dirname "$cache_parent") missing — is the array started, and is there a cache pool?"
    info  "  Postgres, Meilisearch and the CA must NOT live under /mnt/user:"
    info  "  the shfs FUSE layer's file locking and mmap are the classic Unraid"
    info  "  cause of 'database is locked' and index corruption."
  fi

  local user_parent="${APPDATA_USER:-/mnt/user/appdata}"
  [[ -d "$(dirname "$user_parent")" ]] && pass "user share path" "$(dirname "$user_parent")" \
    || warnr "user share path" "$(dirname "$user_parent") missing"

  if [[ -n "${MEDIA_PATH:-}" ]]; then
    # The wizard validates this at answer time (re-asks, or records an explicit
    # use-anyway decision) - so a missing dir here is a FAIL only once phase 1
    # is done: the share vanished, or the use-anyway one was never created and
    # a first scan would index nothing.
    if [[ -d "$MEDIA_PATH" ]]; then pass "media path" "$MEDIA_PATH"
    elif state_done PHASE1; then
      failr "media path" "$MEDIA_PATH no longer exists - a scan would find nothing"
    else
      note "media path" "$MEDIA_PATH (use-anyway choice - create it before the first scan)"
    fi
  fi

  [[ -d "$TEMPLATE_DIR" ]] || mkdir -p "$TEMPLATE_DIR" 2>/dev/null || true
  [[ -w "$TEMPLATE_DIR" ]] && pass "templates-user writable" "$TEMPLATE_DIR" \
    || failr "templates-user writable" "$TEMPLATE_DIR"

  step "networking"
  # The LAN network is CHOSEN in the wizard from the networks Docker actually
  # has, so the preflight's job is only to confirm the choice still exists
  # (it can vanish if the operator reworks Settings -> Docker between runs).
  # Before the wizard has run there is nothing to validate — that is not a
  # warning, it is simply the next step.
  if [[ -z "${BRIDGE_IF:-}" ]]; then
    note "LAN network" "not chosen yet — the wizard picks from this box's macvlan/ipvlan networks"
  elif network_exists "$BRIDGE_IF"; then
    pass "docker network '$BRIDGE_IF'" "present"
  else
    failr "docker network '$BRIDGE_IF'" "chosen earlier but no longer exists — re-run with --reconfigure"
    info  "  Available Docker networks:"
    docker network ls --format '    - {{.Name}} ({{.Driver}})' 2>/dev/null | sed 's/^/  /' || true
  fi

  if [[ "${TOPOLOGY:-A}" == "A" ]]; then
    if network_exists filearr; then pass "docker network 'filearr'" "present"
    else note "docker network 'filearr'" "absent — phase 1 creates it (after the setting above)"; fi
  fi

  # Host port 80. Informational only: the whole point of putting Caddy on br0 is
  # that it never competes for it. Saying who holds it stops the operator from
  # "fixing" a collision that the design already avoided.
  local p80=""
  if command -v ss >/dev/null 2>&1; then p80="$(ss -ltnp 2>/dev/null | awk '$4 ~ /:80$/ {print $NF; exit}')"; fi
  if [[ -n "$p80" ]]; then
    note "host port 80" "held by ${p80} (expected: the Unraid webGUI)"
  else
    note "host port 80" "no listener found (unusual — the webGUI normally holds it)"
  fi
  info "  Caddy never binds the host's 80/443: it gets its own LAN address on"
  info "  ${BRIDGE_IF:-the LAN network the wizard picks}, which is why this is information and not a problem."

  step "address plan"
  local ip
  for ip in "${IP_CADDY:-}" "${IP_POSTGRES:-}" "${IP_MEILI:-}" "${IP_APP:-}" "${IP_STEPCA:-}"; do
    [[ -n "$ip" ]] || continue
    if ip_in_use "$ip"; then
      warnr "fixed IP $ip" "something already answers at this address (best-effort check)"
    else
      pass "fixed IP $ip" "no reply (best-effort — silence is not proof it is free)"
    fi
  done
  [[ -n "${IP_CADDY:-}${IP_POSTGRES:-}" ]] || note "address plan" "no fixed IPs chosen yet"

  step "existing state (what a re-run would and would not touch)"
  local c found=0
  for c in filearr filearr-postgres filearr-meilisearch filearr-stepca filearr-caddy; do
    if container_exists "$c"; then
      found=1
      note "container $c" "EXISTS ($(docker container inspect -f '{{.State.Status}}' "$c" 2>/dev/null)) — its template will NOT be regenerated without --force"
    fi
  done
  for c in my-filearr.xml my-filearr-postgres.xml my-filearr-meilisearch.xml my-filearr-stepca.xml my-filearr-caddy.xml; do
    [[ -f "${TEMPLATE_DIR}/${c}" ]] && { found=1; note "template ${c}" "present"; }
  done
  # The duplicate-template trap the unraid/README documents: a pristine
  # <name>.xml sitting next to Unraid's my-<name>.xml means two templates claim
  # one container, the Edit page loads the PRISTINE DEFAULTS instead of the saved
  # settings, and re-applying overwrites the saved copy with them. This script
  # only ever writes the my- files, so a pristine one can only be a leftover from
  # a hand install.
  for c in filearr filearr-postgres filearr-meilisearch filearr-stepca filearr-caddy; do
    if [[ -f "${TEMPLATE_DIR}/${c}.xml" ]]; then
      warnr "pristine ${c}.xml present" "delete it — two templates claiming one container name"
      info  "  rm ${TEMPLATE_DIR}/${c}.xml"
    fi
  done
  [[ "$found" == 1 ]] || note "existing state" "clean box — nothing of Filearr's here yet"

  echo
  if [[ "$PF_FAIL" -gt 0 ]]; then
    echo "  preflight: ${PF_FAIL} FAIL, ${PF_WARN} WARN"
    return 1
  fi
  echo "  preflight: PASS with ${PF_WARN} warning(s)"
  return 0
}

# --------------------------------------------------------------------------- #
# PHASE 1 — prepare                                                            #
# --------------------------------------------------------------------------- #

wizard() {
  step "answers"
  echo "  Every answer is saved to ${CONF} and re-used on later runs."
  echo "  Re-run with --reconfigure to change them."
  echo

  echo "  TIER — how far this deployment goes."
  echo "    simple  filearr + postgres + meilisearch. Search and catalog on this"
  echo "            box only — NO remote agents (every agent needs a certificate"
  echo "            from step-ca, which the simple tier does not run)."
  echo "    full    the three above + step-ca + caddy: real TLS, a wildcard"
  echo "            certificate, and the agent plane (fingerprint or mTLS auth)."
  echo "            Needs a domain whose DNS is at Cloudflare, a spare LAN IP,"
  echo "            and a LAN resolver you can add records to."
  while :; do
    ask "tier (simple/full)" "${TIER:-simple}"
    case "$REPLY" in simple|full) TIER="$REPLY"; break ;; *) echo "  answer 'simple' or 'full'" ;; esac
  done

  echo
  echo "  TOPOLOGY — how the containers find each other. This decides both DSNs,"
  echo "  the Meilisearch URL, both Caddy upstreams and the CA's own certificate,"
  echo "  so changing it later is an edit to every container."
  echo "    A  one shared Docker network named 'filearr'; containers address each"
  echo "       other by NAME. One LAN address consumed (Caddy's). The default."
  echo "    B  every container on ${BRIDGE_IF:-br0} with its own fixed LAN IP;"
  echo "       every reference becomes an IP. No dual-homing, per-container"
  echo "       firewall rules — at the cost of IP bookkeeping and the macvlan"
  echo "       host-isolation gotcha (this server cannot reach its own containers"
  echo "       until Settings -> Docker -> Host access to custom networks: Yes)."
  while :; do
    ask "topology (A/B)" "${TOPOLOGY:-A}"
    case "$REPLY" in A|a) TOPOLOGY=A; break ;; B|b) TOPOLOGY=B; break ;; *) echo "  answer A or B" ;; esac
  done

  echo
  echo "  Caddy always gets its own LAN address, in BOTH topologies: on any"
  echo "  bridge-type network its 80/443 mappings publish to the HOST address,"
  echo "  and the Unraid webGUI already holds host port 80 — Apply then fails"
  echo "  with 'address already in use'. (live 2026-08-14)"
  # Enumerate the box's REAL candidates instead of assuming br0 exists. On a
  # box with bridging disabled the LAN-attached Docker networks are macvlan/
  # ipvlan networks named after the interface (eth1, eth1.42 for VLANs, ...);
  # the operator should pick from that list, not guess a name a warning later
  # rejects. (Live feedback 2026-08-15: br0-not-found landed as a WARN with the
  # valid choices printed underneath it — the choices belong in the prompt.)
  local _lan_nets
  _lan_nets=$(docker network ls --filter driver=macvlan --filter driver=ipvlan       --format '{{.Name}}' 2>/dev/null | sort)
  if [[ -n "$_lan_nets" ]]; then
    echo "  LAN-attached Docker networks on this box:"
    local _i=0 _names=()
    while IFS= read -r _n; do
      _i=$((_i+1)); _names+=("$_n")
      local _sub
      _sub=$(docker network inspect "$_n" --format         '{{range .IPAM.Config}}{{.Subnet}} {{end}}' 2>/dev/null || true)
      printf '    %d) %s  %s
' "$_i" "$_n" "${_sub:+(subnet ${_sub% })}"
    done <<<"$_lan_nets"
    local _def="${BRIDGE_IF:-${_names[0]}}"
    # A stored answer that no longer exists must not silently win the default.
    grep -qw -- "$_def" <<<"$_lan_nets" || _def="${_names[0]}"
    while :; do
      ask "LAN network for container IPs (number or name)" "$_def"
      if [[ "$REPLY" =~ ^[0-9]+$ ]] && (( REPLY >= 1 && REPLY <= _i )); then
        BRIDGE_IF="${_names[REPLY-1]}"; break
      elif grep -qw -- "$REPLY" <<<"$_lan_nets"; then
        BRIDGE_IF="$REPLY"; break
      else
        echo "  pick one of the listed networks (number or exact name)"
      fi
    done
  else
    # No macvlan/ipvlan network exists at all — nothing to pick from. This is
    # the one genuinely manual prerequisite: Unraid creates these from
    # Settings -> Docker when bridging/VLANs are configured.
    echo "  No LAN-attached (macvlan/ipvlan) Docker network exists on this box."
    echo "  Create one first: Settings -> Docker -> enable an interface for"
    echo "  custom networks (br0 with bridging, ethN without), then re-run."
    ask "or type a network name to use anyway (blank to abort)" ""
    [[ -z "$REPLY" ]] && die "no LAN network chosen"
    BRIDGE_IF="$REPLY"
  fi

  if [[ "$TIER" == "full" || "$TOPOLOGY" == "B" ]]; then
    echo
    echo "  Fixed addresses must be OUTSIDE your router's DHCP pool: these are"
    echo "  static assignments made in the container template, not leases, and an"
    echo "  address DHCP can also hand to a laptop is an outage waiting for the"
    echo "  next reboot."
  fi
  if [[ "$TIER" == "full" ]]; then
    ask "fixed IP for filearr-caddy (the proxy — all three DNS names point here)" "${IP_CADDY:-}"
    IP_CADDY="$REPLY"
  fi
  if [[ "$TOPOLOGY" == "B" ]]; then
    ask "fixed IP for filearr-postgres" "${IP_POSTGRES:-}"; IP_POSTGRES="$REPLY"
    ask "fixed IP for filearr-meilisearch" "${IP_MEILI:-}"; IP_MEILI="$REPLY"
    ask "fixed IP for filearr (the app)" "${IP_APP:-}"; IP_APP="$REPLY"
    if [[ "$TIER" == "full" ]]; then ask "fixed IP for filearr-stepca" "${IP_STEPCA:-}"; IP_STEPCA="$REPLY"; fi
  else
    IP_POSTGRES=""; IP_MEILI=""; IP_APP=""; IP_STEPCA=""
  fi

  echo
  echo "  APPDATA. Postgres, Meilisearch and the CA go on the DIRECT pool path"
  echo "  (/mnt/cache/...), never /mnt/user: the shfs FUSE layer's locking and"
  echo "  mmap behaviour is the classic Unraid cause of database corruption."
  echo "  The app's /config (thumbnails, caches, exports) is lock-insensitive and"
  echo "  stays on the user share."
  # A /mnt/user answer here would silently reintroduce the exact corruption
  # class the cache/user split exists to prevent: shfs FUSE locking + mmap are
  # the classic Unraid cause of "database is locked" and LMDB index damage.
  # Re-ask until the answer is a direct pool path (/mnt/<pool>/..., not user).
  while :; do
    ask "cache-pool appdata root (a DIRECT pool path, e.g. /mnt/cache/appdata or /mnt/nvme/appdata)" "${APPDATA_CACHE:-/mnt/cache/appdata}"
    case "$REPLY" in
      /mnt/user/*|/mnt/user0/*)
        echo "  Postgres, Meilisearch and the CA must NOT live under /mnt/user —"
        echo "  pick the pool's direct path (what /mnt/user maps to underneath)." ;;
      *) break ;;
    esac
  done
  APPDATA_CACHE="$REPLY"
  ask "user-share appdata root (for filearr /config)" "${APPDATA_USER:-/mnt/user/appdata}"; APPDATA_USER="$REPLY"
  # Offer the box's real shares instead of a guessed default the preflight
  # would only warn about later. A nonexistent answer re-asks; "use anyway"
  # stays possible for a share created later, but it is a decision, not a slip.
  if [[ -d /mnt/user ]]; then
    echo "  Shares on this box:  $(ls -d /mnt/user/*/ 2>/dev/null | sed 's|/mnt/user/||;s|/$||' | tr '
' ' ')"
  fi
  while :; do
    ask "media root to mount READ-ONLY into filearr" "${MEDIA_PATH:-/mnt/user/data/media}"
    if [[ -d "$REPLY" ]]; then MEDIA_PATH="$REPLY"; break; fi
    if confirm "  $REPLY does not exist — use it anyway (create the share before scanning)?" n; then
      MEDIA_PATH="$REPLY"; break
    fi
  done

  echo
  ask "host port for the filearr web UI (ignored under topology B)" "${WEBUI_PORT:-8484}"; WEBUI_PORT="$REPLY"
  ask "timezone" "${TZ_:-$(cat /etc/timezone 2>/dev/null || echo Etc/UTC)}"; TZ_="$REPLY"
  ask "postgres user" "${PG_USER:-filearr}"; PG_USER="$REPLY"
  ask "postgres database" "${PG_DB:-filearr}"; PG_DB="$REPLY"

  echo
  echo "  FEATURES. Every one of these is a plain environment variable on the"
  echo "  filearr container, so any answer here can be flipped later from its"
  echo "  Edit page. The defaults are the zero-cost ones."
  echo
  echo "  Semantic search: hybrid keyword + meaning search. The worker downloads"
  echo "  a small ONNX embedding model (bge-small, ~130 MB) on first use and"
  echo "  embeds every item in the background on CPU — a one-off pass that is"
  echo "  slow on a large catalog and adds a little to each later scan. Off = no"
  echo "  model, no embedding, zero cost."
  ask_bool "enable semantic search" "${SEMANTIC:-false}"; SEMANTIC="$REPLY"
  echo
  if [[ "$TIER" == "full" ]]; then
    echo "  Distributed agents: ON — the full tier exists for the agent fleet."
    AGENTS=true
  else
    echo "  Distributed agents: OFF — every agent enrols by fetching a client"
    echo "  certificate from step-ca, which the simple tier does not run; the"
    echo "  Agents page would only ever show failed enrolments. Choose the full"
    echo "  tier (--reconfigure) when you want agents on other machines."
    AGENTS=false
  fi
  echo
  echo "  Content sniffing: an ON-DEMAND maintenance action (nothing runs by"
  echo "  itself) that reads a 64 KiB prefix of extensionless files and"
  echo "  reclassifies them by libmagic MIME type. Reading thousands of files"
  echo "  over SMB is a deliberate act, so this only enables the button."
  ask_bool "enable content sniffing" "${CONTENT_SNIFF:-false}"; CONTENT_SNIFF="$REPLY"
  echo
  echo "  Thumbnail cache budget, in GiB, under ${APPDATA_USER}/filearr/thumbnails."
  echo "  ADVISORY: over budget you get a log line and an amber note on the Jobs"
  echo "  page — nothing is deleted, and per-file caps (20 KB grid / 60 KB"
  echo "  preview) are the hard guard. Rule of thumb, ~80% of items thumbnailable:"
  echo "    grid tier alone (always generated)     ~12 KB/item  ->  1 GB per 100k items"
  echo "    grid + preview (preview is lazy, on view) ~57 KB/item ->  5 GB per 100k items"
  echo "  So 5 GB fits ~100k items comfortably; a 1M-item catalog with heavy"
  echo "  browsing wants 40-50 GB. 0 disables the advisory."
  while :; do
    ask "thumbnail cache budget (GiB)" "${THUMB_BUDGET_GB:-5}"
    [[ "$REPLY" =~ ^[0-9]+([.][0-9]+)?$ ]] && { THUMB_BUDGET_GB="$REPLY"; break; }
    echo "  a number, e.g. 5 or 20"
  done
  echo
  echo "  Auto update check: lets the Jobs-page Updates card refresh its GitHub"
  echo "  release cache by itself. This is the ONLY automatic outbound call the"
  echo "  product ever makes; off = you press the check button yourself."
  ask_bool "enable automatic update check" "${UPDATE_CHECK:-false}"; UPDATE_CHECK="$REPLY"

  CADDY_PROFILE="${CADDY_PROFILE:-internal}"
  TLS_DOMAIN="${TLS_DOMAIN:-}"; ACME_EMAIL="${ACME_EMAIL:-}"
  if [[ "$TIER" == "full" ]]; then
    echo
    echo "  CADDY PROFILE. Start on 'internal' — Caddy's own self-signed CA, no"
    echo "  domain, no DNS credentials, no internet — and prove the proxy reaches"
    echo "  the app before any certificate machinery is involved. Switch to 'acme'"
    echo "  afterwards by editing the container."
    while :; do
      ask "caddy profile (internal/acme)" "${CADDY_PROFILE:-internal}"
      case "$REPLY" in internal|acme) CADDY_PROFILE="$REPLY"; break ;; *) echo "  answer 'internal' or 'acme'" ;; esac
    done
    echo
    echo "  The DOMAIN is used for the CA's own certificate (ca.<domain>), which"
    echo "  is FIRST BOOT ONLY on step-ca — so answer it now even on the internal"
    echo "  profile, or you will be re-initialising the CA and re-enrolling every"
    echo "  agent to change it later."
    ask "apex domain (example.com — blank to skip the public names entirely)" "${TLS_DOMAIN:-}"
    TLS_DOMAIN="$REPLY"
    if [[ "$CADDY_PROFILE" == "acme" ]]; then
      ask "Let's Encrypt account email" "${ACME_EMAIL:-}"; ACME_EMAIL="$REPLY"
      echo
      echo "  The Cloudflare API token (scope Zone:DNS:Edit on that zone only —"
      echo "  NOT a Global API Key) is a secret: it is written straight into the"
      echo "  caddy template and into ${SECRETS}, and never echoed."
      local cf
      read -r -s -p "  Cloudflare API token (blank = keep existing / fill in later): " cf; echo
      if [[ -n "$cf" ]]; then
        secret_put CLOUDFLARE_API_TOKEN "$cf"
        info "Cloudflare token stored (fingerprint $(fingerprint "$cf"))"
      fi
    fi
  fi

  save_conf
}

# TRAP 1 — the setting, and the Docker service cycle it needs.
check_preserve_networks() {
  [[ "$TOPOLOGY" == "A" ]] || { info "topology B uses no user-defined network — setting not required"; return 0; }
  step "Docker setting: preserve user defined networks (TRAP 1)"

  local st; st="$(preserve_networks_state)"
  if [[ "$st" == "preserve" ]]; then
    info 'DOCKER_USER_NETWORKS="preserve" already set — nothing to do'
    return 0
  fi

  echo "  Unraid currently deletes user-defined Docker networks on every Docker"
  echo "  service start. With that in force, 'docker network create filearr'"
  echo "  succeeds, 'docker network ls' shows it, the template dropdown never"
  echo "  lists 'Custom : filearr', and the network is gone after the next"
  echo "  restart. (live 2026-08-14)"
  echo
  echo "  The fix is one line in ${DOCKER_CFG}:"
  echo '      DOCKER_USER_NETWORKS="preserve"'
  echo "  which only takes effect when the Docker service is cycled — and cycling"
  echo "  it STOPS EVERY CONTAINER ON THIS SERVER, not just Filearr's."
  echo

  if ! confirm "Set it and cycle the Docker service now?" n always-ask; then
    echo
    echo "  Fine — do it in the UI instead, in this order, then re-run this script:"
    echo "    1. Settings -> Docker -> Enable Docker: No, Apply"
    echo "       (Docker settings are locked while the service is running)"
    echo "    2. Advanced View (top right) -> Preserve user defined networks: Yes, Apply"
    echo "    3. Enable Docker: Yes, Apply"
    echo
    echo "  Stopping here so nothing is created in the wrong order (TRAP 2: a"
    echo "  network created before the setting is the network that gets deleted)."
    exit 0
  fi

  local tmp; tmp="$(mktemp)"
  grep -vE '^DOCKER_USER_NETWORKS=' "$DOCKER_CFG" 2>/dev/null > "$tmp" || true
  printf 'DOCKER_USER_NETWORKS="preserve"\n' >> "$tmp"
  cat "$tmp" > "$DOCKER_CFG"; rm -f "$tmp"
  info "wrote DOCKER_USER_NETWORKS=\"preserve\" to ${DOCKER_CFG}"

  if [[ -x "$RC_DOCKER" ]]; then
    info "cycling the Docker service (every container stops and restarts)…"
    "$RC_DOCKER" stop  || true
    sleep 3
    "$RC_DOCKER" start || die "the Docker service did not come back — check Settings -> Docker"
    # Give the daemon a moment to accept connections again before phase 1
    # starts asking it about networks.
    local i
    for i in $(seq 1 30); do docker info >/dev/null 2>&1 && break; sleep 2; done
    docker info >/dev/null 2>&1 || die "docker is not answering after the restart"
    info "Docker service restarted"
  else
    warn "$RC_DOCKER not found — toggle Settings -> Docker -> Enable Docker off/on"
    warn "yourself, then re-run this script."
    exit 0
  fi

  [[ "$(preserve_networks_state)" == "preserve" ]] || die "the setting did not stick — set it in the UI"
  info "verified: user-defined networks are now preserved"
}

# TRAP 2 — create the network only AFTER the setting is on.
create_filearr_network() {
  [[ "$TOPOLOGY" == "A" ]] || return 0
  step "docker network 'filearr' (TRAP 2 — created only after the setting above)"
  if network_exists filearr; then
    info "network 'filearr' already exists"
  else
    docker network create filearr >/dev/null
    info "created network 'filearr'"
  fi
  # A name starting with a digit is not preserved even with the setting on
  # (long-standing Unraid bug). 'filearr' is safe; the check exists so a future
  # rename does not resurrect the bug silently.
  case "filearr" in [0-9]*) warn "a network name starting with a digit is not preserved — rename it" ;; esac
  info "open (or re-open) a container's Edit page: Network Type now lists 'Custom : filearr'"
}

# TRAP 4 — ownership, before anything starts.
create_appdata_dirs() {
  step "appdata directories and ownership (TRAP 4)"

  # Pool-backed, FUSE-free paths for everything that keeps a database.
  local d
  for d in "${APPDATA_CACHE}/filearr-postgres" "${APPDATA_CACHE}/filearr-meilisearch"; do
    mkdir -p "$d"; chown -R 99:100 "$d" 2>/dev/null || true
    info "$d  (99:100 nobody:users)"
  done
  mkdir -p "${APPDATA_USER}/filearr"; chown -R 99:100 "${APPDATA_USER}/filearr" 2>/dev/null || true
  info "${APPDATA_USER}/filearr  (99:100 — /config is lock-insensitive, FUSE is fine here)"

  if [[ "$TIER" == "full" ]]; then
    for d in "${APPDATA_CACHE}/filearr-caddy" "${APPDATA_CACHE}/filearr-caddy-config"; do
      mkdir -p "$d"; chown -R 99:100 "$d" 2>/dev/null || true
      info "$d  (99:100)"
    done

    # THE chown. The step-ca image runs as user `step`, UID 1000, and has NO
    # PUID/PGID support; Unraid creates appdata directories as nobody:users
    # (99:100); a bind mount keeps the host's ownership (compose users never hit
    # this — named volumes are chowned to the image user automatically). Without
    # it the container dies at boot with:
    #     /entrypoint.sh: line 56: /home/step/password: Permission denied
    # Do NOT "fix" it with --user 0:0: running a CA as root to dodge a chown is
    # the wrong trade. (live 2026-08-14)
    mkdir -p "${APPDATA_CACHE}/filearr-stepca"
    chown -R 1000:1000 "${APPDATA_CACHE}/filearr-stepca"
    info "${APPDATA_CACHE}/filearr-stepca  (1000:1000 step — REQUIRED before first start)"
    warn "Unraid's unsafe 'New Permissions' tool (not 'Docker Safe New Permissions')"
    warn "resets this and re-breaks the CA the same way. Re-run --check after using it."
  fi
}

make_secrets() {
  step "secrets"
  save_secrets_header
  gen_secret POSTGRES_PASSWORD 24
  gen_secret MEILI_MASTER_KEY 24
  gen_secret FILEARR_SECRET_KEY 32
  [[ "$TIER" == "full" ]] && gen_secret FILEARR_PROXY_SHARED_SECRET 32
  echo
  warn "${SECRETS} IS YOUR BACKUP .env — copy it off this box now."
  warn "FILEARR_SECRET_KEY is not inside a database dump; a restore under a"
  warn "different key silently orphans every stored alert-channel credential."
}

# TRAP 3 (second half) — the re-attach helper for Caddy's dual-homing.
#
# Under topology A, Caddy must be on br0 (for its own 80/443) AND on the
# 'filearr' network (to resolve filearr:8000 / filearr-stepca:9000 by name), and
# Unraid's Network Type is a single mutually-exclusive dropdown. A plain
# `docker network connect` works instantly and is NOT durable: Apply and every
# image update re-create the container from the saved template, which names one
# network, and Caddy starts 502-ing on an upstream it can no longer resolve —
# one edit AFTER the cause, which is what makes it worth automating.
#
# The generated caddy template carries the re-attach in Post Arguments (it fires
# exactly when the attachment is lost, with no polling gap). This helper is the
# safety net for the cases Post Arguments cannot cover — someone editing that
# field away, or a container recreated by a tool that ignores it.
install_reattach_helper() {
  [[ "$TIER" == "full" && "$TOPOLOGY" == "A" ]] || return 0
  step "caddy re-attach helper (TRAP 3)"
  mkdir -p "$STATE_DIR"
  cat > "$HELPER" <<'EOF'
#!/bin/bash
# Re-attach filearr-caddy to the 'filearr' Docker network after an Apply or an
# image update re-created the container without it. Idempotent: safe to run any
# number of times, does nothing when the attachment is already there.
#
# Installed by scripts/setup-unraid.sh. The container template's Post Arguments
# do the same thing at creation time with no gap; this is the safety net.
docker network inspect filearr -f '{{range .Containers}}{{.Name}} {{end}}' 2>/dev/null \
  | grep -qw filearr-caddy \
  || docker network connect filearr filearr-caddy
EOF
  chmod +x "$HELPER" 2>/dev/null || true
  info "installed $HELPER"

  # If the User Scripts plugin is present, install AND schedule it. The layout is
  # not guessed: it was read out of the plugin's own exec.php — a script is a
  # folder under scripts/ holding `script` (the body) and `name` (the display
  # name), with `description` optional; the UI builds its list with a live
  # scandir(), so nothing has to be registered. Custom cron lines live in
  # ../customSchedule.cron and are activated by Unraid's own
  # /usr/local/sbin/update_cron, which is exactly what the plugin calls after
  # saving a schedule.
  if [[ -d "$USER_SCRIPTS_DIR" ]]; then
    local sd="${USER_SCRIPTS_DIR}/filearr-caddy-network"
    mkdir -p "$sd"
    cp "$HELPER" "${sd}/script"
    chmod +x "${sd}/script" 2>/dev/null || true
    printf '%s\n' "filearr-caddy-network" > "${sd}/name"
    printf '%s\n' "Re-attach filearr-caddy to the 'filearr' Docker network after an Apply or image update re-created it. Installed by Filearr's setup-unraid.sh." \
      > "${sd}/description"
    info "installed into User Scripts as 'filearr-caddy-network'"

    # Every 10 minutes, and deliberately NOT 'At First Array Start Only' — that
    # misses the exact case this exists for, a single container recreated while
    # the array stays up. Be honest about the cost: between an Apply and the next
    # tick the proxy is down, which is why the template's Post Arguments are the
    # real mechanism and this is the net.
    local cron_file="/boot/config/plugins/user.scripts/customSchedule.cron"
    local cron_line="*/10 * * * * /usr/local/emhttp/plugins/user.scripts/startCustom.php filearr-caddy-network"
    if [[ -f "$cron_file" ]] && grep -qF "filearr-caddy-network" "$cron_file"; then
      info "custom cron entry already present"
    else
      local tmp; tmp="$(mktemp)"
      if [[ -f "$cron_file" ]]; then cat "$cron_file" > "$tmp"
      else printf '%s\n' "# Generated cron schedule for user.scripts" > "$tmp"; fi
      printf '%s\n' "$cron_line" >> "$tmp"
      cat "$tmp" > "$cron_file"; rm -f "$tmp"
      info "scheduled every 10 minutes in ${cron_file}"
    fi
    if [[ -x /usr/local/sbin/update_cron ]]; then
      /usr/local/sbin/update_cron || warn "update_cron reported an error — check Settings -> User Scripts"
      info "cron reloaded"
    else
      warn "/usr/local/sbin/update_cron not found — open Settings -> User Scripts once to activate the schedule"
    fi
  else
    info "User Scripts plugin not installed — the Post Arguments re-attach in the"
    info "caddy template covers the real failure events on its own. Install User"
    info "Scripts later and copy $HELPER in if you want the belt as well as braces."
  fi
}

# --------------------------------------------------------------------------- #
# Template generation                                                          #
# --------------------------------------------------------------------------- #

fetch_template() {
  local name="$1" dest="$2"
  if [[ -n "$LOCAL_DIR" ]]; then
    [[ -f "${LOCAL_DIR}/${name}.xml" ]] || die "--local-dir: ${LOCAL_DIR}/${name}.xml not found"
    cp "${LOCAL_DIR}/${name}.xml" "$dest"
  else
    curl -fsSL "${TEMPLATE_BASE_URL}/${name}.xml" -o "$dest" \
      || die "could not download ${name}.xml from ${TEMPLATE_BASE_URL} (air-gapped? use --local-dir <checkout>/unraid)"
  fi
  # A truncated download is worse than a failed one: Unraid would show a
  # template with half its fields missing.
  grep -q '</Container>' "$dest" || die "${name}.xml looks truncated"
}

# Should this container's template be (re)generated? No, if the container
# already exists — Unraid rewrote my-<name>.xml on Apply and it now holds the
# operator's own edits. --force overrides.
should_generate() {
  local name="$1"
  [[ "$FORCE" == 1 ]] && return 0
  container_exists "$name" && return 1
  return 0
}

# Network + fixed IP for a given container under the chosen topology.
apply_network() {
  local file="$1" name="$2"
  if [[ "$name" == "filearr-caddy" ]]; then
    # Always its own LAN address, in both topologies (trap 3).
    set_elem "$file" Network "$BRIDGE_IF"
    [[ -n "${IP_CADDY:-}" ]] && add_elem_before_close "$file" "<MyIP>${IP_CADDY}</MyIP>"
    return 0
  fi
  if [[ "$TOPOLOGY" == "A" ]]; then
    set_elem "$file" Network "filearr"
  else
    set_elem "$file" Network "$BRIDGE_IF"
    local ip=""
    case "$name" in
      filearr-postgres)    ip="${IP_POSTGRES:-}" ;;
      filearr-meilisearch) ip="${IP_MEILI:-}" ;;
      filearr)             ip="${IP_APP:-}" ;;
      filearr-stepca)      ip="${IP_STEPCA:-}" ;;
    esac
    [[ -n "$ip" ]] && add_elem_before_close "$file" "<MyIP>${ip}</MyIP>"
  fi
  # Explicit: a bare `[[ ]] && cmd` as a function's last statement returns 1 when
  # the test is false, and under `set -e` that would abort the caller.
  return 0
}

# Host:port for an inter-service reference, per topology. This ONE function is
# where option A's names and option B's IPs diverge; everything else just calls
# it, which is why the two topologies cannot drift apart.
svc_host() {
  case "$1" in
    postgres) [[ "$TOPOLOGY" == "A" ]] && echo "filearr-postgres" || echo "${IP_POSTGRES}" ;;
    meili)    [[ "$TOPOLOGY" == "A" ]] && echo "filearr-meilisearch" || echo "${IP_MEILI}" ;;
    app)      [[ "$TOPOLOGY" == "A" ]] && echo "filearr" || echo "${IP_APP}" ;;
    stepca)   [[ "$TOPOLOGY" == "A" ]] && echo "filearr-stepca" || echo "${IP_STEPCA}" ;;
  esac
}

generate_templates() {
  step "container templates -> ${TEMPLATE_DIR}"

  local pgpw meilikey seckey proxysec cftok
  pgpw="$(secret_get POSTGRES_PASSWORD)"
  meilikey="$(secret_get MEILI_MASTER_KEY)"
  seckey="$(secret_get FILEARR_SECRET_KEY)"
  proxysec="$(secret_get FILEARR_PROXY_SHARED_SECRET)"
  cftok="$(secret_get CLOUDFLARE_API_TOKEN)"

  local order; order="$([[ "$TIER" == "full" ]] && echo "$APPLY_ORDER_FULL" || echo "$APPLY_ORDER_SIMPLE")"
  local name f
  for name in $order; do
    f="${TEMPLATE_DIR}/my-${name}.xml"
    if ! should_generate "$name"; then
      info "my-${name}.xml: SKIPPED — container '${name}' exists (use --force to overwrite)"
      continue
    fi
    fetch_template "$name" "$f"
    apply_network "$f" "$name"

    case "$name" in
      filearr-postgres)
        set_cfg "$f" 'Name="Data"' "${APPDATA_CACHE}/filearr-postgres"
        set_cfg "$f" 'Target="POSTGRES_USER"' "$PG_USER"
        set_cfg "$f" 'Target="POSTGRES_PASSWORD"' "$pgpw"
        set_cfg "$f" 'Target="POSTGRES_DB"' "$PG_DB"
        ;;
      filearr-meilisearch)
        set_cfg "$f" 'Name="Data"' "${APPDATA_CACHE}/filearr-meilisearch"
        set_cfg "$f" 'Target="MEILI_MASTER_KEY"' "$meilikey"
        ;;
      filearr)
        set_cfg "$f" 'Name="WebUI Port"' "$WEBUI_PORT"
        set_cfg "$f" 'Name="Config"' "${APPDATA_USER}/filearr"
        set_cfg "$f" 'Name="Media"' "$MEDIA_PATH"
        set_cfg "$f" 'Target="FILEARR_DATABASE_URL"' \
          "postgresql+psycopg://${PG_USER}:${pgpw}@$(svc_host postgres):5432/${PG_DB}"
        set_cfg "$f" 'Target="FILEARR_PROCRASTINATE_DSN"' \
          "postgresql://${PG_USER}:${pgpw}@$(svc_host postgres):5432/${PG_DB}"
        set_cfg "$f" 'Target="FILEARR_MEILI_URL"' "http://$(svc_host meili):7700"
        set_cfg "$f" 'Target="FILEARR_MEILI_MASTER_KEY"' "$meilikey"
        set_cfg "$f" 'Target="FILEARR_SECRET_KEY"' "$seckey"
        set_cfg "$f" 'Target="TZ"' "$TZ_"
        # FEATURES answered in the wizard (full tier forces agents on below).
        set_cfg "$f" 'Target="FILEARR_SEMANTIC_ENABLED"'      "$SEMANTIC"
        set_cfg "$f" 'Target="FILEARR_AGENTS_ENABLED"'        "$AGENTS"
        set_cfg "$f" 'Target="FILEARR_CONTENT_SNIFF_ENABLED"' "$CONTENT_SNIFF"
        set_cfg "$f" 'Target="FILEARR_THUMBNAIL_BUDGET_GB"'   "$THUMB_BUDGET_GB"
        set_cfg "$f" 'Target="FILEARR_UPDATE_CHECK_AUTO"'     "$UPDATE_CHECK"
        # The disk-monitor read-only view must point at postgres's REAL data path.
        set_cfg "$f" 'Name="Postgres Data (disk monitor)"' "${APPDATA_CACHE}/filearr-postgres"
        if [[ "$TIER" == "full" ]]; then
          set_cfg "$f" 'Target="FILEARR_AGENTS_ENABLED"' "true"
          set_cfg "$f" 'Target="FILEARR_AGENT_AUTH_MODE"' "fingerprint"
          set_cfg "$f" 'Target="FILEARR_PROXY_SHARED_SECRET"' "$proxysec"
          set_cfg "$f" 'Target="FILEARR_CA_PROVISIONER"' "filearr-agents"
          # CA URL stays a NAME in both topologies: it is what agents bootstrap
          # against, served by Caddy on 443, and the app itself fetches the root
          # from <CA URL>/root/<fingerprint> — so the app container has to
          # resolve it too. CA Root Fingerprint and CA Provisioner JWK are filled
          # in during the step-ca step of the walkthrough, before you ever open
          # this container's Edit page.
          [[ -n "$TLS_DOMAIN" ]] && set_cfg "$f" 'Target="FILEARR_CA_URL"' "https://ca.${TLS_DOMAIN}"
          [[ -n "$TLS_DOMAIN" ]] && set_cfg "$f" 'Target="FILEARR_PUBLIC_BASE_URL"' "https://filearr.${TLS_DOMAIN}"
        fi
        ;;
      filearr-stepca)
        set_cfg "$f" 'Name="Data"' "${APPDATA_CACHE}/filearr-stepca"
        set_cfg "$f" 'Target="DOCKER_STEPCA_INIT_PROVISIONER_NAME"' "filearr-agents"
        set_cfg "$f" 'Target="DOCKER_STEPCA_INIT_REMOTE_MANAGEMENT"' "true"
        set_cfg "$f" 'Target="TZ"' "$TZ_"
        # CA DNS Names is FIRST BOOT ONLY — the certificate is minted once and
        # editing this later does nothing short of re-initialising the CA and
        # re-enrolling every agent. Under topology B the container name resolves
        # to nothing, so the fixed IP goes in instead.
        local dns="localhost"
        if [[ "$TOPOLOGY" == "A" ]]; then dns="${dns},filearr-stepca"
        else [[ -n "${IP_STEPCA:-}" ]] && dns="${dns},${IP_STEPCA}"; fi
        [[ -n "$TLS_DOMAIN" ]] && dns="${dns},ca.${TLS_DOMAIN}"
        set_cfg "$f" 'Target="DOCKER_STEPCA_INIT_DNS_NAMES"' "$dns"
        ;;
      filearr-caddy)
        set_cfg "$f" 'Target="FILEARR_CADDY_PROFILE"' "$CADDY_PROFILE"
        set_cfg "$f" 'Name="Certificates"' "${APPDATA_CACHE}/filearr-caddy"
        set_cfg "$f" 'Name="Caddy State"' "${APPDATA_CACHE}/filearr-caddy-config"
        set_cfg "$f" 'Name="step-ca Root (read-only)"' "${APPDATA_CACHE}/filearr-stepca"
        set_cfg "$f" 'Target="FILEARR_APP_UPSTREAM"' "$(svc_host app):8000"
        set_cfg "$f" 'Target="FILEARR_CA_UPSTREAM"' "$(svc_host stepca):9000"
        set_cfg "$f" 'Target="FILEARR_PROXY_SHARED_SECRET"' "$proxysec"
        set_cfg "$f" 'Target="TZ"' "$TZ_"
        [[ -n "$TLS_DOMAIN" ]] && set_cfg "$f" 'Target="FILEARR_TLS_DOMAIN"' "$TLS_DOMAIN"
        [[ -n "$ACME_EMAIL" ]] && set_cfg "$f" 'Target="FILEARR_ACME_EMAIL"' "$ACME_EMAIL"
        [[ -n "$cftok" ]] && set_cfg "$f" 'Target="CLOUDFLARE_API_TOKEN"' "$cftok"
        if [[ "$TOPOLOGY" == "A" ]]; then
          # TRAP 3: Post Arguments is not a hook — it is text appended to the
          # docker run command line Unraid assembles, run by a shell, so a
          # leading && deliberately breaks out of it. It therefore fires exactly
          # when the attachment is lost: on every Apply and every image update.
          # XML-escaped, because & is not legal element text.
          add_elem_before_close "$f" "<PostArgs>&amp;&amp; docker network connect filearr filearr-caddy</PostArgs>"
        fi
        ;;
    esac
    info "wrote my-${name}.xml"
  done

  echo
  info "These are the my-*.xml files Unraid itself writes on Apply, so there is"
  info "no pristine duplicate to delete afterwards — the trap where two templates"
  info "claim one container name and the Edit page loads the DEFAULTS does not"
  info "arise on this path."
}

phase1() {
  phase "1 — prepare (settings, network, directories, secrets, templates)"
  local asked=0
  if [[ "$RECONFIGURE" == 1 || ! -f "$CONF" ]]; then
    wizard; asked=1
  else
    info "using saved answers from $CONF (--reconfigure to change)"
  fi
  # Re-run the preflight ONLY when the wizard just produced new answers: the
  # pass that gated this phase ran before the topology and the addresses were
  # known, so it could not check them. Without new answers it would print the
  # identical table twice, which trains people to skim it.
  if [[ "$asked" == 1 ]]; then
    echo
    info "re-checking the environment against the answers you just gave"
    preflight || die "preflight failed — fix the FAIL rows above and re-run"
    if [[ "$PF_WARN" -gt 0 ]]; then
      confirm "Continue past ${PF_WARN} warning(s)?" y || exit 0
    fi
  fi
  check_preserve_networks
  create_filearr_network
  create_appdata_dirs
  make_secrets
  install_reattach_helper
  generate_templates
  state_set PHASE1 1
}

# --------------------------------------------------------------------------- #
# PHASE 2 — the guided walkthrough                                             #
# --------------------------------------------------------------------------- #

# probe_* — genuine readiness, not "the container is running". A Postgres that
# is still replaying WAL, a Meilisearch still opening its index and an app still
# bootstrapping its schema all report Running=true while being useless to the
# next container in the order, which is how ordering races get blamed on the
# software.

probe_postgres() {
  docker exec filearr-postgres pg_isready -U "$PG_USER" >/dev/null 2>&1
}

# HTTP probes have a topology problem worth being explicit about: under topology
# B every container is on macvlan/ipvlan, and a macvlan child and its parent are
# isolated BY DESIGN — this server cannot curl its own containers. Caddy is on
# br0 in BOTH topologies, so the same applies to it. When a probe cannot reach
# an address for that reason we say so and downgrade to a running-state check
# rather than reporting a healthy container as broken.
probe_http() {
  curl -fsS -k --max-time 5 -o /dev/null "$1" 2>/dev/null
}

probe_meili() {
  local ip
  if [[ "$TOPOLOGY" == "A" ]]; then
    ip="$(container_ip filearr-meilisearch filearr)"
    [[ -n "$ip" ]] || return 1
    probe_http "http://${ip}:7700/health"
  else
    probe_http "http://${IP_MEILI}:7700/health"
  fi
}

probe_app() {
  if [[ "$TOPOLOGY" == "A" ]]; then
    probe_http "http://127.0.0.1:${WEBUI_PORT}/api/v1/health"
  else
    probe_http "http://${IP_APP}:8000/api/v1/health"
  fi
}

probe_stepca() {
  docker exec filearr-stepca step ca health \
    --ca-url https://localhost:9000 --root /home/step/certs/root_ca.crt >/dev/null 2>&1
}

probe_caddy() {
  [[ -n "${IP_CADDY:-}" ]] || return 1
  probe_http "https://${IP_CADDY}/"
}

# macvlan isolation makes a host-side probe impossible for these; used to soften
# a probe failure into an honest "verify from a laptop" instead of a red herring.
probe_is_isolated() {
  local name="$1"
  [[ "$name" == "filearr-caddy" ]] && return 0
  [[ "$TOPOLOGY" == "B" && "$name" != "filearr-postgres" ]] && return 0
  return 1
}

run_probe() {
  case "$1" in
    filearr-postgres)    probe_postgres ;;
    filearr-meilisearch) probe_meili ;;
    filearr)             probe_app ;;
    filearr-stepca)      probe_stepca ;;
    filearr-caddy)       probe_caddy ;;
    *) return 0 ;;
  esac
}

probe_description() {
  case "$1" in
    filearr-postgres)    echo "docker exec filearr-postgres pg_isready -U ${PG_USER}" ;;
    filearr-meilisearch) echo "GET /health on the container's address" ;;
    filearr)             echo "GET /api/v1/health" ;;
    filearr-stepca)      echo "docker exec filearr-stepca step ca health" ;;
    filearr-caddy)       echo "GET https://${IP_CADDY:-<caddy-ip>}/ (self-signed accepted)" ;;
  esac
}

# What the operator has to click, per container, in words that match the UI.
apply_instructions() {
  local name="$1"
  # The dropdown shows user templates by NAME under "User templates" — the
  # on-disk my- prefix is not displayed, so telling the operator to look for
  # my-<name> sends them hunting for something that is not there.
  echo "    Docker tab -> ADD CONTAINER -> Template: ${name} (under 'User templates') -> APPLY"
  echo "    Every field is already filled in. Nothing to type."
  case "$name" in
    filearr-postgres)
      echo "    Expect the log to end with: 'database system is ready to accept connections'"
      ;;
    filearr-meilisearch)
      echo "    MEILI_UPGRADE_DB is on deliberately: a newer engine refuses to open"
      echo "    an older database and would restart-loop without it."
      ;;
    filearr-stepca)
      echo "    FIRST BOOT prints the root fingerprint and the CA admin password"
      echo "    into the log ONCE. Do not clear the log — this script reads them"
      echo "    for you in a moment and persists the password so it stops"
      echo "    mattering (TRAP 5)."
      ;;
    filearr)
      echo "    First start bootstraps the database itself (idempotent, retried"
      echo "    while Postgres is still coming up) — no console step."
      ;;
    filearr-caddy)
      echo "    Network Type is Custom : ${BRIDGE_IF} with fixed IP ${IP_CADDY:-<unset>}."
      if [[ "$TOPOLOGY" == "A" ]]; then
        echo "    Post Arguments carries the re-attach to the 'filearr' network."
        echo "    Verify after Apply: docker inspect -f '{{json .NetworkSettings.Networks}}' filearr-caddy"
      fi
      ;;
  esac
}

wait_for_container() {
  local name="$1" deadline=$((SECONDS + PROBE_TIMEOUT)) shown=0
  while (( SECONDS < deadline )); do
    if container_exists "$name"; then
      if [[ "$shown" == 0 ]]; then info "container '${name}' exists — waiting for it to be ready…"; shown=1; fi
      if container_running "$name" && run_probe "$name"; then return 0; fi
    fi
    sleep 3
  done
  return 1
}

# The interactive loop. Deliberately not a fire-and-forget wall of instructions:
# the whole point is that each container is verified before the next one is
# suggested, because every ordering bug in this stack presents as a confusing
# error two steps later.
walkthrough() {
  phase "2 — apply the containers (guided, one at a time)"
  echo "  Filearr's containers are created by Unraid's Docker tab, and that click"
  echo "  is the one thing a shell cannot do for you. So: this loop tells you"
  echo "  exactly what to Apply, waits, and then PROBES the container for real"
  echo "  readiness before moving on."
  echo
  echo "  At each prompt:  Enter = I applied it   s = skip   r = retry   q = quit (resumable)"

  local order; order="$([[ "$TIER" == "full" ]] && echo "$APPLY_ORDER_FULL" || echo "$APPLY_ORDER_SIMPLE")"
  local name
  for name in $order; do
    if state_done "VERIFIED_${name}"; then
      info "✓ ${name} — already verified on an earlier run (skipping)"
      continue
    fi

    while :; do
      echo
      echo "───────────────────────────────────────────────────────────────────────"
      echo "  APPLY: ${name}"
      echo "───────────────────────────────────────────────────────────────────────"
      apply_instructions "$name"
      echo
      echo "    readiness probe: $(probe_description "$name")"
      local ans
      read -r -p "  [Enter] applied · [s]kip · [r]etry · [q]uit: " ans || ans=q
      case "$ans" in
        s|S) warn "skipped ${name} — later steps that depend on it may fail"; break ;;
        q|Q) echo; info "stopping here. Re-run this script to resume — verified containers are skipped."; exit 0 ;;
      esac

      if wait_for_container "$name"; then
        info "✓ ${name} is up and answering"
        state_set "VERIFIED_${name}" 1
        # The CA harvest is not a separate phase: it belongs to the moment the CA
        # first boots, because that is when its log still holds the two values
        # that are printed exactly once.
        [[ "$name" == "filearr-stepca" ]] && harvest_ca
        break
      fi

      echo
      warn "${name} did not become ready within ${PROBE_TIMEOUT}s."
      if container_exists "$name"; then
        if probe_is_isolated "$name"; then
          warn "This container is on ${BRIDGE_IF} (macvlan/ipvlan), and a macvlan"
          warn "child and its parent are isolated BY DESIGN — THIS SERVER cannot"
          warn "reach it even when it is perfectly healthy. Container state is:"
          warn "  $(docker container inspect -f '{{.State.Status}}' "$name" 2>/dev/null)"
          warn "Check it from a laptop on the LAN, or enable Settings -> Docker ->"
          warn "Host access to custom networks. If it is healthy, answer 's'."
        else
          warn "It exists and its state is: $(docker container inspect -f '{{.State.Status}}' "$name" 2>/dev/null)"
        fi
        warn "Read its log:   docker logs --tail 50 ${name}"
        case "$name" in
          filearr-stepca)
            warn "If the log says '/entrypoint.sh: line 56: /home/step/password:"
            warn "Permission denied', the appdata chown was undone (TRAP 4):"
            warn "  docker stop filearr-stepca"
            warn "  chown -R 1000:1000 ${APPDATA_CACHE}/filearr-stepca"
            warn "  docker start filearr-stepca"
            ;;
          filearr)
            warn "A database error here usually means the DSN password does not"
            warn "match filearr-postgres. Both came from ${SECRETS}; re-Apply"
            warn "filearr-postgres if you changed it by hand."
            ;;
        esac
      else
        warn "No container named '${name}' exists yet — was the Apply saved?"
        warn "The template is ${TEMPLATE_DIR}/my-${name}.xml"
      fi
      echo
      # loop back: 'r' is just falling through to the prompt again
    done
  done
  state_set PHASE2 1
}

# --------------------------------------------------------------------------- #
# The step-ca harvest (TRAPs 5, 6, 7) — runs inline at the step-ca step         #
# --------------------------------------------------------------------------- #

# TRAP 5 — two values are printed ONCE into the first-boot log and never again:
# the root certificate FINGERPRINT (recoverable later from the root cert itself)
# and the CA ADMINISTRATIVE PASSWORD (not recoverable at all). Persisting the
# password inside the CA volume as secrets/admin_password — 0600, owned by the
# container user — is what makes container-log retention stop mattering. It is
# the same convention deploy-proxmox.sh uses, so a CA moved between the two
# deployments behaves identically.
persist_admin_password() {
  if docker exec filearr-stepca test -f /home/step/secrets/admin_password 2>/dev/null; then
    info "secrets/admin_password already persisted in the CA volume"
    return 0
  fi
  local apw
  # "Your CA administrative password is: xxxxx" / "...password is: xxxxx"
  apw="$(docker logs filearr-stepca 2>&1 | grep -i 'password is' | tail -1 | sed 's/.*password is: *//' | tr -d '\r\n ' || true)"
  if [[ -z "$apw" ]]; then
    warn "no 'password is' line in the container log — either the log has rolled,"
    warn "or you set Init Password yourself. If the JWK decrypt below fails, put"
    warn "the password you chose into the CA with:"
    warn "  printf '%s' '<password>' | docker exec -i filearr-stepca sh -c 'umask 077; cat > /home/step/secrets/admin_password'"
    return 0
  fi
  # Written THROUGH docker exec, so the file is created BY the container user
  # (step, 1000) and needs no chown — the whole point of trap 7's fix.
  printf '%s' "$apw" | docker exec -i filearr-stepca sh -c 'umask 077; cat > /home/step/secrets/admin_password'
  info "CA admin password persisted to secrets/admin_password (fingerprint $(fingerprint "$apw"))"
  info "log retention no longer matters for it"
  # Also recorded in ${SECRETS}: the in-volume copy dies with the CA volume, and
  # this is the password that opens the provisioner key. If both copies are lost
  # AND the first-boot log has rolled, it is unrecoverable and the only way
  # forward is rotating the provisioner key.
  secret_put STEPCA_ADMIN_PASSWORD "$apw"
}

# Which password file opens the provisioner JWE? It depends on the init mode and
# it trips people up constantly: with Remote Management on (our default) the
# provisioner key is encrypted under the CA ADMINISTRATIVE password, NOT
# /home/step/secrets/password, which is the CA KEY password. Try both, in the
# order that is cheapest to be wrong about.
ca_password_file() {
  local f
  for f in /home/step/secrets/admin_password /home/step/secrets/password; do
    docker exec filearr-stepca test -f "$f" 2>/dev/null && { echo "$f"; return 0; }
  done
  echo ""
}

# TRAP 7 — eliminated, not handled.
#
# The documented manual procedure stages two files on the HOST: the JWE and the
# password. Files written from the Unraid shell are owned by ROOT, the container
# runs as step (1000), and the decrypt fails with
#     open /home/step/adminpw failed: permission denied
# — which is why the guide has to tell you to chown BOTH staging files. This
# implementation writes no host-side files at all: the encrypted key is read out
# of the CA over docker exec, piped straight back in over docker exec's stdin,
# and the plaintext is captured on stdout. There is nothing on the host to own.
extract_provisioner_jwk() {
  local prov="filearr-agents"

  # The encrypted key is published by the CA's own provisioner list (serving the
  # JWE publicly is by design — that is how `step ca token` works client-side;
  # only the password can open it). Run `step ca provisioner list` INSIDE the
  # container: it needs no admin auth, and it works whether or not port 9000 is
  # published, which under topology A it deliberately is not.
  local listing enc=""
  listing="$(docker exec filearr-stepca sh -c \
    'step ca provisioner list --ca-url https://localhost:9000 --root /home/step/certs/root_ca.crt' 2>/dev/null || true)"
  [[ -n "$listing" ]] || { warn "the CA served no provisioner list — is it healthy?"; return 1; }

  # No jq on Unraid. Flatten the array to one object per line (the provisioner
  # objects nest "key"/"claims" objects, but a nested `},{` pair cannot occur in
  # that shape), then pick the object that names our provisioner and pull its
  # encryptedKey with the same grep/sed the guide's manual command uses.
  enc="$(printf '%s' "$listing" \
    | tr -d '\n' \
    | sed 's/}[[:space:]]*,[[:space:]]*{/}\n{/g' \
    | grep "\"name\"[[:space:]]*:[[:space:]]*\"${prov}\"" \
    | grep -o '"encryptedKey"[[:space:]]*:[[:space:]]*"[^"]*"' \
    | head -1 | cut -d'"' -f4 || true)"
  if [[ -z "$enc" ]]; then
    # Fall back to the guide's simpler head -1 form for a CA with a single
    # provisioner, rather than failing on a formatting difference.
    enc="$(printf '%s' "$listing" | grep -o '"encryptedKey"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | cut -d'"' -f4 || true)"
  fi
  [[ -n "$enc" ]] || { warn "no JWK provisioner named '${prov}' in the CA's list"; return 1; }

  local pwf jwk=""
  for pwf in /home/step/secrets/admin_password /home/step/secrets/password; do
    docker exec filearr-stepca test -f "$pwf" 2>/dev/null || continue
    # JWE in on stdin, plaintext out on stdout. No host staging file exists at
    # any point (TRAP 7).
    jwk="$(printf '%s' "$enc" | docker exec -i filearr-stepca \
      step crypto jwe decrypt --password-file "$pwf" 2>/dev/null || true)"
    # A decrypted private JWK is a JSON object carrying a "d" member; anything
    # else is a wrong password, which step reports as garbage rather than an
    # error we can rely on.
    if [[ "$jwk" == '{'*'"d"'*'}' ]]; then
      info "provisioner JWK decrypted with ${pwf}"
      printf '%s' "$jwk"
      return 0
    fi
    jwk=""
  done
  warn "JWK decrypt failed with every password file the CA holds."
  warn "Without it, agent registration still succeeds but ca_ott comes back null"
  warn "and enrolment cannot finish. See the Unraid guide, step 4."
  return 1
}

# TRAP 6 — every admin-API call needs the trio, or step stops with
#     "No admin credentials found. You must login to execute admin commands."
# With Remote Management on, provisioners live in step-ca's ADMIN DATABASE and
# editing ca.json does nothing at all — this is the only way to set the claims.
set_provisioner_claims() {
  local pwf; pwf="$(ca_password_file)"
  [[ -n "$pwf" ]] || { warn "no CA password file — skipping certificate-lifetime tuning"; return 0; }

  # Already tuned? The public provisioner list carries the claims.
  if docker exec filearr-stepca sh -c \
      'step ca provisioner list --ca-url https://localhost:9000 --root /home/step/certs/root_ca.crt' 2>/dev/null \
      | grep -q minTLSCertDuration; then
    info "certificate lifetimes already set"
    return 0
  fi

  # 24h/48h/72h with a bounded grace: --allow-renewal-after-expiry is the
  # difference between "the laptop was off for a week" and "re-enrol the laptop".
  if docker exec filearr-stepca step ca provisioner update filearr-agents \
      --x509-min-dur=24h --x509-default-dur=48h --x509-max-dur=72h \
      --allow-renewal-after-expiry \
      --admin-subject=step --admin-provisioner=filearr-agents \
      --admin-password-file="$pwf" \
      --ca-url https://localhost:9000 --root /home/step/certs/root_ca.crt >/dev/null 2>&1; then
    info "certificate lifetimes set: 24h min / 48h default / 72h max, renewal after expiry allowed"
  else
    warn "the admin-API claims update failed — certificates still issue with"
    warn "step-ca's defaults, so this is not fatal. Retry by hand with the trio:"
    warn "  --admin-subject=step --admin-provisioner=filearr-agents --admin-password-file=${pwf}"
  fi
}

# Patch the CA values into the app's template BEFORE the operator ever opens its
# Edit page. This is why step-ca is applied before filearr in the order.
patch_app_template_with_ca() {
  local fp="$1" jwk="$2"
  local f="${TEMPLATE_DIR}/my-filearr.xml"
  if [[ ! -f "$f" ]]; then
    warn "no ${f} to patch — fill CA Root Fingerprint and CA Provisioner JWK by hand"
    return 0
  fi
  [[ -n "$fp" ]] && set_cfg "$f" 'Target="FILEARR_CA_FINGERPRINT"' "$fp"
  [[ -n "$jwk" ]] && set_cfg "$f" 'Target="FILEARR_CA_PROVISIONER_JWK"' "$jwk"
  set_cfg "$f" 'Target="FILEARR_CA_PROVISIONER"' "filearr-agents"
  info "patched CA values into my-filearr.xml"
  # If the app container already exists (an out-of-order run, or --force), the
  # template edit is visible on its Edit page but is NOT live until Apply.
  if container_exists filearr; then
    warn "the 'filearr' container already exists: open Docker tab -> filearr ->"
    warn "Edit -> APPLY once, so it picks up the CA values just written."
  fi
}

harvest_ca() {
  step "step-ca harvest (TRAPs 5, 6, 7)"

  # The root fingerprint is PUBLIC pinning material and, unlike the admin
  # password, always recomputable from the root certificate itself — so read it
  # from the cert rather than depending on the log.
  local fp
  fp="$(docker exec filearr-stepca step certificate fingerprint /home/step/certs/root_ca.crt 2>/dev/null | tr -d '\r\n' || true)"
  if [[ -n "$fp" ]]; then
    info "CA root fingerprint: ${fp}"
    info "(public pinning material — safe to print, and recomputable at any time)"
  else
    warn "could not read the root fingerprint from the CA"
  fi

  persist_admin_password

  local jwk=""
  jwk="$(extract_provisioner_jwk || true)"
  if [[ -n "$jwk" ]]; then
    info "provisioner private JWK extracted (fingerprint $(fingerprint "$jwk")) — not printed here"
    # Keep it with the other secrets so a rebuild does not need the CA to be up.
    secret_put FILEARR_CA_PROVISIONER_JWK "$jwk"
  fi
  # The root fingerprint is not a secret, but the summary needs it after the CA
  # container is long forgotten, so record it alongside.
  [[ -n "$fp" ]] && state_set CA_FINGERPRINT "$fp"

  set_provisioner_claims
  patch_app_template_with_ca "$fp" "$jwk"

  if [[ -z "$jwk" ]]; then
    echo
    warn "*******************************************************************"
    warn "** CA Provisioner JWK IS STILL EMPTY. Agent registration will    **"
    warn "** succeed and every enrolment will then fail on a null ca_ott.  **"
    warn "** Re-run this script to retry the extraction automatically.     **"
    warn "*******************************************************************"
  fi
}

# --------------------------------------------------------------------------- #
# PHASE 3 — summary                                                            #
# --------------------------------------------------------------------------- #

# The handoff summary. This is the operator's one moment to write everything
# down, so it is deliberately generous — and deliberately split in two.
#
# Part one is configuration: addresses, paths, the DNS table, the CA
# fingerprint. None of it is secret and all of it is needed.
#
# Part two is the secrets themselves, PRINTED IN CLEAR, behind an explicit
# prompt. That is a considered exception to this script's own rule (everywhere
# else only fingerprints are shown), made because the alternative — an operator
# who never opens ${SECRETS} and discovers on restore day that
# FILEARR_SECRET_KEY was never copied anywhere — is the worse failure. The
# prompt exists so the disclosure is a decision rather than a surprise: it names
# the risk (screen, scrollback, screen-share) before anything is printed, and
# the block ends with how to clear the scrollback.
#
# Reprintable at any time with --summary, so closing the terminal loses nothing.
print_dns_table() {
  printf '    %-30s %-5s %-16s %s\n' "NAME" "TYPE" "POINTS AT" "SERVES"
  printf '    %-30s %-5s %-16s %s\n' "filearr.${TLS_DOMAIN}" "A" "${IP_CADDY:-<caddy-ip>}" "web UI and API"
  printf '    %-30s %-5s %-16s %s\n' "agents.${TLS_DOMAIN}" "A" "${IP_CADDY:-<caddy-ip>}" "agent plane (client cert REQUIRED)"
  printf '    %-30s %-5s %-16s %s\n' "ca.${TLS_DOMAIN}" "A" "${IP_CADDY:-<caddy-ip>}" "step-ca, raw passthrough"
}

app_console_url() {
  if [[ "$TOPOLOGY" == "A" ]]; then
    local h; h="$(hostname -i 2>/dev/null | awk '{print $1}')"
    echo "http://${h:-<tower-ip>}:${WEBUI_PORT}"
  else
    echo "http://${IP_APP:-<filearr-ip>}:8000"
  fi
}

summary_part_one() {
  echo
  echo "═══════════════════════════════════════════════════════════════════════════"
  echo "  PART 1 — CONFIGURATION AND ACCESS"
  echo "═══════════════════════════════════════════════════════════════════════════"

  echo
  echo "  CONSOLE"
  echo "    $(app_console_url)"
  if [[ "$TIER" == "full" && -n "$TLS_DOMAIN" ]]; then
    echo "    https://filearr.${TLS_DOMAIN}/            (through filearr-caddy, once DNS resolves)"
    echo "    https://filearr.${TLS_DOMAIN}/api/docs    (interactive API reference)"
    [[ "$CADDY_PROFILE" == "internal" ]] && \
    echo "    On the 'internal' profile the certificate is Caddy's own self-signed"
    [[ "$CADDY_PROFILE" == "internal" ]] && \
    echo "    root: expect a browser warning until you switch to 'acme'."
  fi
  if [[ "$TOPOLOGY" == "B" ]]; then
    echo "    NOTE: under topology B this address is NOT reachable from the Unraid"
    echo "    box itself unless Settings -> Docker -> Host access to custom"
    echo "    networks is Yes. Every other device on the LAN reaches it fine."
  fi

  echo
  echo "  TOPOLOGY: ${TOPOLOGY}   TIER: ${TIER}   LAN bridge: ${BRIDGE_IF}"
  echo "  FEATURES: semantic=${SEMANTIC}  agents=${AGENTS}  content-sniff=${CONTENT_SNIFF}"
  echo "            thumbnail-budget=${THUMB_BUDGET_GB}GiB  auto-update-check=${UPDATE_CHECK}"
  if [[ "$TOPOLOGY" == "B" || -n "${IP_CADDY:-}" ]]; then
    echo "  ADDRESSES"
    [[ -n "${IP_POSTGRES:-}" ]] && printf '    %-22s %-16s %s\n' "filearr-postgres" "$IP_POSTGRES" "5432"
    [[ -n "${IP_MEILI:-}"    ]] && printf '    %-22s %-16s %s\n' "filearr-meilisearch" "$IP_MEILI" "7700"
    [[ -n "${IP_APP:-}"      ]] && printf '    %-22s %-16s %s\n' "filearr" "$IP_APP" "8000"
    [[ -n "${IP_STEPCA:-}"   ]] && printf '    %-22s %-16s %s\n' "filearr-stepca" "$IP_STEPCA" "9000"
    [[ -n "${IP_CADDY:-}"    ]] && printf '    %-22s %-16s %s\n' "filearr-caddy" "$IP_CADDY" "80, 443"
    echo "    These are fixed in the templates, not DHCP leases. Keep them out of"
    echo "    the DHCP pool: a moved address presents as 'cannot reach the"
    echo "    database' about a database that is running perfectly."
  fi

  echo
  echo "  PATHS"
  printf '    %-38s %s\n' "${APPDATA_CACHE}/filearr-postgres" "database (pool path, never /mnt/user)"
  printf '    %-38s %s\n' "${APPDATA_CACHE}/filearr-meilisearch" "search index (disposable, rebuildable)"
  printf '    %-38s %s\n' "${APPDATA_USER}/filearr" "app /config: thumbnails, caches, exports"
  if [[ "$TIER" == "full" ]]; then
    printf '    %-38s %s\n' "${APPDATA_CACHE}/filearr-stepca" "CA PRIVATE KEY MATERIAL — irreplaceable"
    printf '    %-38s %s\n' "${APPDATA_CACHE}/filearr-caddy" "certificates"
  fi
  printf '    %-38s %s\n' "$MEDIA_PATH" "media, mounted READ-ONLY as /data/media"
  echo
  printf '    %-38s %s\n' "$TEMPLATE_DIR" "the my-filearr-*.xml templates"
  printf '    %-38s %s\n' "$CONF" "saved answers"
  printf '    %-38s %s\n' "$STATE" "phase / verification state"
  printf '    %-38s %s\n' "$SECRETS" "the secrets (part 2 below)"

  if [[ "$TIER" == "full" ]]; then
    local fp; fp="$(state_get CA_FINGERPRINT)"
    echo
    echo "  CA ROOT FINGERPRINT (public pinning material — not a secret)"
    echo "    ${fp:-<not harvested yet>}"
    echo "    Recomputable any time:"
    echo "      docker exec filearr-stepca step certificate fingerprint /home/step/certs/root_ca.crt"
  fi

  if [[ "$TIER" == "full" && -n "$TLS_DOMAIN" ]]; then
    echo
    echo "  DNS RECORDS — publish these on your LAN resolver. All three point at"
    echo "  filearr-caddy, including ca., which Caddy raw-passes through on 443."
    echo
    print_dns_table
    echo
    echo "    Where, best first: your router/firewall's resolver (OPNsense:"
    echo "    Services -> Unbound DNS -> Overrides -> Host Overrides; pfSense:"
    echo "    Services -> DNS Resolver -> Host Overrides), then Pi-hole (Local DNS"
    echo "    -> DNS Records) or AdGuard Home (Filters -> DNS rewrites, where one"
    echo "    *.${TLS_DOMAIN} rewrite covers all three), then a real DNS server."
    echo "    A hosts file is the last resort and does not work at all for the"
    echo "    filearr CONTAINER, which has to resolve ca.${TLS_DOMAIN} itself."
    echo
    echo "    With the acme profile your PUBLIC zone stays empty: the wildcard"
    echo "    comes from a DNS-01 TXT record Caddy creates and deletes through the"
    echo "    Cloudflare API. Nothing is port-forwarded; this works behind NAT."
  fi
}

summary_part_two_secrets() {
  echo
  echo "═══════════════════════════════════════════════════════════════════════════"
  echo "  PART 2 — SECRETS"
  echo "═══════════════════════════════════════════════════════════════════════════"
  echo
  echo "  The next block prints your secrets IN CLEAR. Anyone who can read this"
  echo "  screen, your scrollback, or a recording of this session can read them."
  echo "  If you are screen-sharing, stop now: everything below is also in"
  echo "  ${SECRETS}, and \`bash $0 --summary\` reprints it whenever you like."
  echo
  read -r -p "  Press Enter to display secrets (Ctrl-C to skip): " _ || { echo; return 0; }
  echo

  local v
  v="$(secret_get FILEARR_SECRET_KEY)"
  if [[ -n "$v" ]]; then
    echo "  FILEARR_SECRET_KEY"
    echo "    ${v}"
    echo "    NEVER ROTATE THIS ONCE SET. It is the envelope key for alert-channel"
    echo "    credentials (SMTP passwords, webhook secrets, Apprise URLs) and it is"
    echo "    NOT inside a database dump. Change it, or restore a dump under a"
    echo "    different one, and every stored credential becomes permanently"
    echo "    undecryptable — silently, with every API call still reporting success."
    echo
  fi
  v="$(secret_get FILEARR_PROXY_SHARED_SECRET)"
  if [[ -n "$v" ]]; then
    echo "  FILEARR_PROXY_SHARED_SECRET"
    echo "    ${v}"
    echo "    Byte-identical on 'filearr' and 'filearr-caddy'. Do not rotate"
    echo "    casually: the agent plane fails CLOSED, so a mismatch locks out the"
    echo "    whole fleet until both containers carry the new value."
    echo
  fi
  v="$(secret_get POSTGRES_PASSWORD)"
  if [[ -n "$v" ]]; then
    echo "  POSTGRES_PASSWORD"
    echo "    ${v}"
    echo "    Appears in three places that must agree: filearr-postgres, and both"
    echo "    DSNs on filearr. Rotating means changing it in the database first,"
    echo "    then both DSNs."
    echo
  fi
  v="$(secret_get MEILI_MASTER_KEY)"
  if [[ -n "$v" ]]; then
    echo "  MEILI_MASTER_KEY"
    echo "    ${v}"
    echo "    Must match on filearr-meilisearch and filearr. Safe to rotate: the"
    echo "    index is a disposable projection and rebuilds from Postgres."
    echo
  fi
  v="$(secret_get STEPCA_ADMIN_PASSWORD)"
  if [[ -n "$v" ]]; then
    echo "  step-ca ADMINISTRATIVE PASSWORD"
    echo "    ${v}"
    echo "    Printed ONCE into the CA's first-boot log and never again. It is what"
    echo "    decrypts the provisioner key and authenticates every admin-API call."
    echo "    Also persisted inside the CA volume as secrets/admin_password. Lose"
    echo "    BOTH copies and it is unrecoverable — the only way forward is"
    echo "    rotating the provisioner key entirely."
    echo
  fi
  v="$(secret_get FILEARR_CA_PROVISIONER_JWK)"
  if [[ -n "$v" ]]; then
    echo "  FILEARR_CA_PROVISIONER_JWK  (already filled into the filearr template)"
    echo "    ${v}"
    echo "    Same class as FILEARR_SECRET_KEY. Without it agents register fine and"
    echo "    then fail enrolment on a null ca_ott."
    echo
  fi
  v="$(secret_get CLOUDFLARE_API_TOKEN)"
  if [[ -n "$v" ]]; then
    echo "  CLOUDFLARE_API_TOKEN"
    echo "    ${v}"
    echo "    Scope Zone:DNS:Edit on your zone only. Revocable and re-issuable at"
    echo "    Cloudflare at any time — the least painful secret here to replace."
    echo
  fi

  echo "  ── when you have written these down ──"
  echo "    Clear the scrollback:   printf '\\033[3J\\033[H\\033[2J'"
  echo "    or simply close the Unraid web terminal window, which discards it."
  echo "    Nothing above was written to your shell history: every value came from"
  echo "    ${SECRETS}, not from a command you typed."
}

summary_safeguarding() {
  echo
  echo "═══════════════════════════════════════════════════════════════════════════"
  echo "  SAFEGUARDING — do this today, not on restore day"
  echo "═══════════════════════════════════════════════════════════════════════════"
  echo
  echo "  ${SECRETS} is the CANONICAL copy of every secret above (mode 0600 where"
  echo "  the filesystem can express it). Copy it off this box now:"
  echo
  echo "      cp ${SECRETS} /mnt/user/backups/filearr-secrets.env      # then off-box"
  echo "      scp ${SECRETS} you@yourdesktop:~/filearr-secrets.env"
  echo
  echo "  YOUR UNRAID FLASH BACKUP ALREADY CONTAINS THESE SECRETS. Tools -> Flash"
  echo "  Backup (and the My Servers flash backup) archives all of /boot/config,"
  echo "  and that is where this file lives. That is convenient — the secrets"
  echo "  survive a dead flash drive — and it is also a fact you need to know:"
  echo "  treat your flash backups as secret material and store them accordingly."
  echo "  The flash drive itself remains a single point of failure; a flash"
  echo "  backup is the only thing that makes it survivable."
  echo
  echo "  TWO THINGS A RESTORE CANNOT RECONSTRUCT, no matter how good the dump is:"
  echo "    1. ${SECRETS}"
  echo "       FILEARR_SECRET_KEY is not in any database dump. Restoring without"
  echo "       it looks like a complete success and quietly orphans every stored"
  echo "       alert-channel credential."
  if [[ "$TIER" == "full" ]]; then
    echo "    2. ${APPDATA_CACHE}/filearr-stepca"
    echo "       CA private key material. A new CA invalidates every certificate it"
    echo "       ever issued and every agent has to re-enrol. Back it up:"
    echo "         tar czf /mnt/user/backups/filearr/stepca-\$(date -u +%Y%m%dT%H%M%SZ).tar.gz \\"
    echo "           -C ${APPDATA_CACHE} filearr-stepca"
  else
    echo "    2. (n/a at this tier — no CA is deployed)"
  fi
  echo
  echo "  THE ONGOING STORY"
  echo "    Native Unraid backup commands, a User Scripts nightly schedule and the"
  echo "    full restore sequence:"
  echo "      docs-site/deployment/unraid.md#backup-and-restore"
  echo "      docs-site/deployment/unraid.md#scheduling-with-user-scripts"
  echo "    On a machine with a checkout, the bundle backup (dump + .env + step-ca"
  echo "    + manifest) and its verifier:"
  echo "      scripts/backup.sh"
  echo "      scripts/verify-backup.sh backups/filearr-<timestamp>/"
  echo "    A backup you have never restored is a hypothesis; verify-backup.sh"
  echo "    restores into a throwaway Postgres and makes it a fact."
}

summary_next_steps() {
  echo
  echo "═══════════════════════════════════════════════════════════════════════════"
  echo "  NEXT STEPS"
  echo "═══════════════════════════════════════════════════════════════════════════"
  echo
  echo "  a. CREATE THE ADMIN ACCOUNT. Open $(app_console_url) — auth is enabled in"
  echo "     the template, so the first visit shows a one-time bootstrap screen;"
  echo "     once an admin exists that endpoint is closed for good."
  if [[ "$TIER" == "full" && -n "$TLS_DOMAIN" ]]; then
    echo "     Session cookies are Secure, so do this over https://filearr.${TLS_DOMAIN}/"
    echo "     once DNS and the proxy are up."
  else
    echo "     Session cookies are Secure: plain HTTP is fine for LAN evaluation,"
    echo "     but put TLS in front before you rely on logins."
  fi
  if [[ "$TIER" == "full" && -n "$TLS_DOMAIN" ]]; then
    echo
    echo "  b. PUBLISH THE DNS RECORDS if you have not — the table is in Part 1"
    echo "     above (\`bash $0 --summary\` reprints it). Nothing with a public"
    echo "     hostname works until they resolve."
  else
    echo
    echo "  b. (no DNS records needed at this tier)"
  fi
  echo
  echo "  c. CREATE YOUR FIRST LIBRARY. Console -> Admin -> Libraries -> New. The"
  echo "     path is the IN-CONTAINER path: ${MEDIA_PATH}/movies is /data/media/movies"
  echo "     here. The folder browser only offers paths inside the mapped roots,"
  echo "     which is the fastest way to confirm you mapped what you think. Then"
  echo "     Scan it — items appear in search immediately, with depth (duration,"
  echo "     codec, EXIF, hashes) filling in as background extraction runs."
  echo
  if [[ "$TIER" == "full" ]]; then
    echo "  d. ENROL YOUR FIRST AGENT. Console -> Agents -> Mint enrollment token"
    echo "     (shown once, single-use, one hour). Give the agent the central URL"
    echo "     and that token. Enrolment ALWAYS runs against the main URL, never"
    echo "     agents.${TLS_DOMAIN:-<domain>} — an agent that has not enrolled has no"
    echo "     client certificate yet. Full walkthrough: docs-site/agents.md"
    echo "     Then, and only once every agent shows the mTLS transport badge,"
    echo "     move Agent Auth Mode 'fingerprint' -> 'both' -> 'mtls-header'. It"
    echo "     fails closed by design; flipping it early locks out the fleet."
  else
    echo "  d. (agents need the full-parity tier — re-run with --reconfigure to add"
    echo "     step-ca and Caddy whenever you want them)"
  fi
  echo
  echo "  e. RE-VALIDATE ANY TIME:   bash $0 --check"
  echo "     Re-print this summary:  bash $0 --summary"
  echo
  echo "  f. SCHEDULE BACKUPS. Nothing here is automatic:"
  echo "     docs-site/deployment/unraid.md#scheduling-with-user-scripts"
  echo
  echo "  STILL YOURS TO DO, AND WHY — the honest list"
  echo "    * The Apply clicks. Unraid creates containers through dockerMan in the"
  echo "      webGUI; there is no supported CLI, and driving the GUI from a script"
  echo "      would break on the next OS update."
  echo "    * The DNS records. They live on your resolver, not on this box."
  if [[ "$TIER" == "full" ]]; then
    echo "    * Switching Caddy 'internal' -> 'acme' when DNS and the Cloudflare"
    echo "      token are ready (Edit -> Profile -> Apply). Deliberately a separate"
    echo "      act: 'internal' proves the proxy reaches the app before any"
    echo "      certificate machinery is involved."
    echo "    * The mtls-header cutover, for the fail-closed reason in (d)."
  fi
}

phase3() {
  phase "3 — handoff summary"
  echo
  echo "  Setup is complete. What follows is everything you need to write down."
  summary_part_one
  summary_safeguarding
  summary_next_steps
  summary_part_two_secrets
  echo
  echo "═══════════════════════════════════════════════════════════════════════════"
  state_set PHASE3 1
}

# --------------------------------------------------------------------------- #
# --check — the post-deploy validator                                          #
# --------------------------------------------------------------------------- #

check_mode() {
  load_conf
  [[ -f "$CONF" ]] || die "no saved answers at $CONF — run the script normally first"
  preflight || true

  phase "check — post-deploy assertions"
  step "directories and ownership"
  local d
  for d in "${APPDATA_CACHE}/filearr-postgres" "${APPDATA_CACHE}/filearr-meilisearch" "${APPDATA_USER}/filearr"; do
    [[ -d "$d" ]] && pass "dir $d" "" || failr "dir $d" "missing"
  done
  if [[ "$TIER" == "full" ]]; then
    d="${APPDATA_CACHE}/filearr-stepca"
    if [[ -d "$d" ]]; then
      local own; own="$(stat -c '%u:%g' "$d" 2>/dev/null || echo '?')"
      # TRAP 4 again: this is the check to re-run after Unraid's unsafe New
      # Permissions tool, which resets appdata ownership and re-breaks the CA.
      [[ "$own" == "1000:1000" ]] && pass "step-ca appdata ownership" "1000:1000" \
        || failr "step-ca appdata ownership" "$own — must be 1000:1000 (chown -R 1000:1000 $d)"
    else
      failr "dir $d" "missing"
    fi
  fi

  step "containers"
  local order name; order="$([[ "$TIER" == "full" ]] && echo "$APPLY_ORDER_FULL" || echo "$APPLY_ORDER_SIMPLE")"
  for name in $order; do
    if ! container_exists "$name"; then failr "container $name" "does not exist"; continue; fi
    if ! container_running "$name"; then failr "container $name" "$(docker container inspect -f '{{.State.Status}}' "$name")"; continue; fi
    if run_probe "$name"; then
      pass "container $name" "running and answering"
    elif probe_is_isolated "$name"; then
      warnr "container $name" "running; not probeable from this host (macvlan isolation)"
    else
      failr "container $name" "running but the readiness probe failed"
    fi
  done

  if [[ "$TIER" == "full" && "$TOPOLOGY" == "A" ]] && container_exists filearr-caddy; then
    step "caddy dual-homing (TRAP 3)"
    local nets; nets="$(docker container inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' filearr-caddy 2>/dev/null || true)"
    if grep -qw filearr <<<"$nets" && grep -qw "$BRIDGE_IF" <<<"$nets"; then
      pass "caddy on both networks" "$nets"
    else
      failr "caddy on both networks" "only: $nets — run $HELPER, and check the template's Post Arguments"
    fi
  fi

  if [[ "$TIER" == "full" ]]; then
    step "CA values"
    local f="${TEMPLATE_DIR}/my-filearr.xml"
    if [[ -f "$f" ]]; then
      [[ -n "$(get_cfg "$f" 'Target="FILEARR_CA_FINGERPRINT"')" ]] \
        && pass "template: CA Root Fingerprint" "present" || failr "template: CA Root Fingerprint" "empty"
      [[ -n "$(get_cfg "$f" 'Target="FILEARR_CA_PROVISIONER_JWK"')" ]] \
        && pass "template: CA Provisioner JWK" "present (value never printed)" \
        || failr "template: CA Provisioner JWK" "empty — enrolment will fail on a null ca_ott"
      [[ -n "$(get_cfg "$f" 'Target="FILEARR_CA_PROVISIONER"')" ]] \
        && pass "template: CA Provisioner" "present" || failr "template: CA Provisioner" "empty"
    else
      failr "template my-filearr.xml" "missing"
    fi
    if container_running filearr-stepca; then
      docker exec filearr-stepca test -f /home/step/secrets/admin_password 2>/dev/null \
        && pass "CA admin password persisted" "secrets/admin_password (TRAP 5)" \
        || failr "CA admin password persisted" "missing — log retention still matters"
      docker exec filearr-stepca sh -c \
        'step ca provisioner list --ca-url https://localhost:9000 --root /home/step/certs/root_ca.crt' 2>/dev/null \
        | grep -q minTLSCertDuration \
        && pass "provisioner claims" "24h/48h/72h set" \
        || warnr "provisioner claims" "unset — certificates issue with step-ca defaults"
    fi
  fi

  step "secrets"
  if [[ -f "$SECRETS" ]]; then
    pass "secrets file" "$SECRETS"
    local k v
    for k in POSTGRES_PASSWORD MEILI_MASTER_KEY FILEARR_SECRET_KEY; do
      v="$(secret_get "$k")"
      [[ -n "$v" ]] && pass "  $k" "fingerprint $(fingerprint "$v")" || failr "  $k" "missing"
    done
  else
    failr "secrets file" "$SECRETS missing"
  fi

  echo
  echo "═══════════════════════════════════════════════════════════════════════════"
  if [[ "$PF_FAIL" -gt 0 ]]; then
    echo "  CHECK: ${PF_FAIL} FAIL, ${PF_WARN} WARN"
    exit 1
  fi
  echo "  CHECK: all assertions passed (${PF_WARN} warning(s))"
  exit 0
}

# --------------------------------------------------------------------------- #
# main                                                                         #
# --------------------------------------------------------------------------- #

usage() {
  cat <<EOF
setup-unraid.sh ${VERSION} — guided setup for the Filearr stack on Unraid.

Run it in the Unraid terminal (web terminal icon, or SSH) as root:

    bash /boot/config/plugins/filearr/setup-unraid.sh

  (no flags)       run the next unfinished phase and continue to the end
  --check          verify only — PASS/FAIL per item, non-zero exit on failure
  --summary        re-print the handoff summary (secrets stay behind a prompt)
  --reconfigure    re-ask the wizard (existing secrets are always kept)
  --force          regenerate templates even for containers that already exist
  --local-dir DIR  take templates from a local checkout's unraid/ (air-gapped)
  --phase N        run exactly phase 0|1|2|3 and stop
  --yes            assume yes to WARN confirmations (never to the Docker cycle)
  --version, --help

Phases: 0 preflight (read-only gate) · 1 prepare (settings, network, dirs,
secrets, templates) · 2 guided per-container Apply walkthrough with readiness
probes and the inline step-ca harvest · 3 handoff summary.

Everything is resumable: finished phases and verified containers are recorded
in ${STATE} and skipped on the next run.

Full guide: docs-site/deployment/unraid.md
EOF
  exit 0
}

CURRENT_STEP="argument parsing"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --check)       MODE="check" ;;
    --summary)     MODE="summary" ;;
    --reconfigure) RECONFIGURE=1 ;;
    --force)       FORCE=1 ;;
    --yes|-y)      ASSUME_YES=1 ;;
    --local-dir)   shift; LOCAL_DIR="${1:-}"; [[ -d "$LOCAL_DIR" ]] || die "--local-dir: '$LOCAL_DIR' is not a directory" ;;
    --phase)       shift; PHASE_ONLY="${1:-}" ;;
    --help|-h)     usage ;;
    --version)     echo "setup-unraid.sh ${VERSION}"; exit 0 ;;
    *)             die "unknown option: $1 (try --help)" ;;
  esac
  shift
done

mkdir -p "$STATE_DIR"
touch "$STATE" 2>/dev/null || true
load_conf

# Defaults for everything the wizard may not have set yet, so `set -u` never
# fires before the first run has answered anything.
# BRIDGE_IF deliberately has NO default: the wizard picks it from the networks
# Docker actually has, and preflight treats "empty" as "not chosen yet" rather
# than validating a guess (a box with no br0 must not FAIL before it is asked).
TIER="${TIER:-simple}"; TOPOLOGY="${TOPOLOGY:-A}"; BRIDGE_IF="${BRIDGE_IF:-}"
APPDATA_CACHE="${APPDATA_CACHE:-/mnt/cache/appdata}"; APPDATA_USER="${APPDATA_USER:-/mnt/user/appdata}"
MEDIA_PATH="${MEDIA_PATH:-/mnt/user/data/media}"; WEBUI_PORT="${WEBUI_PORT:-8484}"
TZ_="${TZ_:-Etc/UTC}"; PG_USER="${PG_USER:-filearr}"; PG_DB="${PG_DB:-filearr}"
IP_POSTGRES="${IP_POSTGRES:-}"; IP_MEILI="${IP_MEILI:-}"; IP_APP="${IP_APP:-}"
IP_STEPCA="${IP_STEPCA:-}"; IP_CADDY="${IP_CADDY:-}"
CADDY_PROFILE="${CADDY_PROFILE:-internal}"; TLS_DOMAIN="${TLS_DOMAIN:-}"; ACME_EMAIL="${ACME_EMAIL:-}"
SEMANTIC="${SEMANTIC:-false}"; AGENTS="${AGENTS:-false}"; CONTENT_SNIFF="${CONTENT_SNIFF:-false}"
THUMB_BUDGET_GB="${THUMB_BUDGET_GB:-5}"; UPDATE_CHECK="${UPDATE_CHECK:-false}"

if [[ "$MODE" == "check" ]]; then check_mode; fi
if [[ "$MODE" == "summary" ]]; then
  [[ -f "$CONF" ]] || die "no saved answers at $CONF — run the script normally first"
  summary_part_one
  summary_safeguarding
  summary_next_steps
  summary_part_two_secrets
  echo
  exit 0
fi

echo "Filearr — Unraid setup  ${VERSION}"
echo "State and answers: ${STATE_DIR}"

case "${PHASE_ONLY}" in
  # `if preflight` rather than `preflight; exit $?`: a preflight that reports
  # FAIL is a RESULT, not a crash, and a bare call would trip the ERR trap under
  # set -e and print a scary "SETUP FAILED" over a perfectly good report.
  0) if preflight; then exit 0; else exit 1; fi ;;
  1) phase1; exit 0 ;;
  2) walkthrough; exit 0 ;;
  3) phase3; exit 0 ;;
  "") ;;
  *) die "--phase takes 0, 1, 2 or 3" ;;
esac

# Full run, resuming wherever the last one stopped.
if ! state_done PHASE1; then
  preflight || die "preflight failed — fix the FAIL rows above and re-run"
  if [[ "$PF_WARN" -gt 0 ]]; then
    echo
    confirm "Continue past ${PF_WARN} warning(s)?" y || exit 0
  fi
  phase1
else
  info "phase 1 already done — re-run with --reconfigure to change answers, --force to rewrite templates"
  [[ "$RECONFIGURE" == 1 || "$FORCE" == 1 ]] && phase1
fi

walkthrough
phase3
