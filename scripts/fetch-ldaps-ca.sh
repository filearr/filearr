#!/usr/bin/env bash
# fetch-ldaps-ca.sh — pull an LDAPS server's certificate chain and emit a PEM
# CA bundle ready for Filearr (paste into Admin → Authentication → CA
# certificate (PEM), or mount and point FILEARR_LDAP_TLS_CA_CERT_FILE at it).
#
# AD domain controllers frequently present ONLY their leaf certificate, so
# when the presented chain is incomplete this walks the Authority Information
# Access (AIA) "CA Issuers" pointers — the URL each certificate carries to its
# issuer's certificate — until it reaches a self-signed root (or the pointers
# run out). Three strategies per hop, in order:
#   1. http(s) AIA URIs (DER, PEM and PKCS#7 payloads all handled);
#   2. ldap:/// AIA URIs — AD CS's DEFAULT publication — resolved against the
#      target DC with ldapsearch. AD refuses anonymous directory reads, so
#      pass a service bind with -D/-w for this path;
#   3. the AD CS CertEnroll convention guessed from the issuer DN
#      (http://<caHost>/CertEnroll/<caHostFQDN>_<CAName>.crt) — needs no
#      credentials at all when web enrollment is installed.
#
# The pull is UNVALIDATED (trust-on-first-use): verify the printed SHA-256
# fingerprints against your PKI documentation before trusting the bundle.
#
# Usage: fetch-ldaps-ca.sh [-D bind_dn -w bind_password] <host[:port]> [output.pem]
#        port defaults to 636 (use host:3269 for a Global Catalog)
# Needs: openssl, curl, awk; ldapsearch for the ldap:/// AIA path.

set -euo pipefail

MAX_AIA_DEPTH=4
MAX_AIA_BYTES=1048576

err() { printf '%s\n' "$*" >&2; }
die() { err "error: $*"; exit 1; }

BIND_DN=""
BIND_PW=""
while getopts "D:w:h" opt; do
    case $opt in
    D) BIND_DN=$OPTARG ;;
    w) BIND_PW=$OPTARG ;;
    *) die "usage: $0 [-D bind_dn -w bind_password] <host[:port]> [output.pem]" ;;
    esac
done
shift $((OPTIND - 1))

[ $# -ge 1 ] || die "usage: $0 [-D bind_dn -w bind_password] <host[:port]> [output.pem]"
HOST=${1%%:*}
PORT=636
case $1 in *:*) PORT=${1##*:} ;; esac
OUT=${2:-ldaps-ca.pem}

command -v openssl >/dev/null || die "openssl not found"
command -v curl >/dev/null || die "curl not found"

WORK=$(mktemp -d) || die "mktemp failed"
trap 'rm -rf "$WORK"' EXIT

fp() { openssl x509 -in "$1" -noout -fingerprint -sha256 | sed 's/.*=//'; }
subj() { openssl x509 -in "$1" -noout -subject | sed 's/^subject=//'; }
issuer() { openssl x509 -in "$1" -noout -issuer | sed 's/^issuer=//'; }
is_root() { [ "$(subj "$1")" = "$(issuer "$1")" ]; }
urldecode() { local s=${1//+/ }; printf '%b' "${s//%/\\x}"; }

# Normalize any certificate payload (X.509 DER/PEM, PKCS#7 DER/PEM) to a PEM
# file that may hold several certificates.
normalize() { # $1=input $2=output
    if openssl x509 -in "$1" -out "$2" 2>/dev/null ||
        openssl x509 -inform der -in "$1" -out "$2" 2>/dev/null ||
        openssl pkcs7 -print_certs -in "$1" -out "$2" 2>/dev/null ||
        openssl pkcs7 -print_certs -inform der -in "$1" -out "$2" 2>/dev/null; then
        [ -s "$2" ]
    else
        return 1
    fi
}

# Search a normalized multi-cert PEM ($1) for a cert whose subject is $2; on a
# hit, store it as the next chain file and set FOUND to its path.
FOUND=""
pick_issuer() { # $1=pem $2=wanted-subject
    csplit -sz -f "$WORK/aiacert-" -b '%02d.pem' "$1" \
        '/BEGIN CERTIFICATE/' '{*}' 2>/dev/null || return 0
    local cand
    for cand in "$WORK"/aiacert-*.pem; do
        [ -s "$cand" ] || continue
        # pkcs7 -print_certs preamble text can land in its own chunk
        openssl x509 -in "$cand" -noout 2>/dev/null || continue
        if [ "$(subj "$cand")" = "$2" ] && [ -z "${SEEN[$(fp "$cand")]:-}" ]; then
            FOUND="$WORK/cert-$(printf '%02d' "$N").pem"
            cp "$cand" "$FOUND"
            SEEN[$(fp "$FOUND")]=1
            N=$((N + 1))
            break
        fi
    done
    rm -f "$WORK"/aiacert-*.pem
}

fetch_http() { # $1=url $2=wanted-subject
    err "-> following AIA pointer: $1"
    curl -fsSL --max-time 15 --max-filesize "$MAX_AIA_BYTES" \
        -o "$WORK/aia.bin" "$1" 2>/dev/null || return 0
    normalize "$WORK/aia.bin" "$WORK/aia.pem" || return 0
    pick_issuer "$WORK/aia.pem" "$2"
    rm -f "$WORK/aia.bin" "$WORK/aia.pem"
}

fetch_ldap() { # $1=ldap-uri $2=wanted-subject
    command -v ldapsearch >/dev/null || {
        err "   (skipping ldap AIA pointer — ldapsearch not installed)"
        return 0
    }
    if [ -z "$BIND_DN" ]; then
        err "   (ldap AIA pointer needs credentials — rerun with -D/-w, or rely"
        err "    on the CertEnroll fallback below)"
        return 0
    fi
    # ldap:///<DN>?<attrs>?<scope>?<filter> — hostless means "any DC".
    local rest dn
    rest=${1#ldap://}
    rest=${rest#/}
    dn=$(urldecode "${rest%%\?*}")
    err "-> resolving ldap AIA pointer against $HOST: $dn"
    LDAPTLS_REQCERT=never ldapsearch -LLL -o ldif-wrap=no -x \
        -H "ldaps://$HOST:636" -D "$BIND_DN" -w "$BIND_PW" \
        -b "$dn" -s base '(objectClass=*)' cACertificate 2>/dev/null |
        awk -F':: ' '/^cACertificate;?[^:]*:: / {print $2}' >"$WORK/ldap.b64" || true
    [ -s "$WORK/ldap.b64" ] || return 0
    : >"$WORK/aia.pem"
    while IFS= read -r b64; do
        printf '%s' "$b64" | base64 -d >"$WORK/aia.bin" 2>/dev/null || continue
        normalize "$WORK/aia.bin" "$WORK/one.pem" && cat "$WORK/one.pem" >>"$WORK/aia.pem"
    done <"$WORK/ldap.b64"
    [ -s "$WORK/aia.pem" ] && pick_issuer "$WORK/aia.pem" "$2"
    rm -f "$WORK/ldap.b64" "$WORK/aia.bin" "$WORK/one.pem" "$WORK/aia.pem"
}

# AD CS CertEnroll guess from the issuer DN (RFC2253: CN=...,DC=...,DC=...).
fetch_certenroll() { # $1=top-cert $2=wanted-subject
    local dn cn domain= token h
    dn=$(openssl x509 -in "$1" -noout -issuer -nameopt RFC2253 | sed 's/^issuer=//')
    cn=$(printf '%s' "$dn" | tr ',' '\n' | awk -F= '$1=="CN" {print $2; exit}')
    [ -n "$cn" ] || return 0
    # RFC2253 prints DCs leaf-first, which IS DNS order: DC=corp,DC=example → corp.example
    domain=$(printf '%s' "$dn" | tr ',' '\n' | awk -F= '$1=="DC" {print tolower($2)}' |
        paste -sd. -)
    local hosts=()
    if [ -n "$domain" ]; then
        for token in ${cn//-/ }; do
            [ "${token^^}" = "CA" ] && continue
            hosts+=("${token,,}.$domain")
        done
    fi
    hosts+=("$HOST")
    for h in "${hosts[@]}"; do
        [ -n "$FOUND" ] && break
        fetch_http "http://$h/CertEnroll/${h}_${cn}.crt" "$2"
    done
}

# ---- 1. pull whatever the server presents --------------------------------- #
err "-> pulling certificate chain from ${HOST}:${PORT} (unvalidated, TOFU)"
if ! openssl s_client -connect "${HOST}:${PORT}" -servername "$HOST" -showcerts \
    </dev/null 2>/dev/null |
    awk '/-----BEGIN CERTIFICATE-----/,/-----END CERTIFICATE-----/' \
        >"$WORK/presented.pem" || ! [ -s "$WORK/presented.pem" ]; then
    die "could not retrieve a certificate from ${HOST}:${PORT}"
fi
csplit -sz -f "$WORK/cert-" -b '%02d.pem' "$WORK/presented.pem" \
    '/BEGIN CERTIFICATE/' '{*}'
CHAIN=("$WORK"/cert-*.pem) # cert-00 = leaf
err "   server presented ${#CHAIN[@]} certificate(s)"

# ---- 2. follow AIA pointers until rooted ---------------------------------- #
declare -A SEEN
for c in "${CHAIN[@]}"; do SEEN[$(fp "$c")]=1; done
N=${#CHAIN[@]}
for _ in $(seq 1 "$MAX_AIA_DEPTH"); do
    top=${CHAIN[$((${#CHAIN[@]} - 1))]}
    is_root "$top" && break
    want=$(issuer "$top")
    FOUND=""
    while IFS= read -r url; do
        [ -n "$url" ] && [ -z "$FOUND" ] || continue
        case $url in
        http://* | https://*) fetch_http "$url" "$want" ;;
        ldap://*) fetch_ldap "$url" "$want" ;;
        esac
    done < <(openssl x509 -in "$top" -noout -ext authorityInfoAccess 2>/dev/null |
        awk -F'URI:' '/CA Issuers/ {print $2}')
    [ -z "$FOUND" ] && fetch_certenroll "$top" "$want"
    if [ -z "$FOUND" ]; then
        err "   issuer '$want' not reachable via AIA — stopping"
        break
    fi
    CHAIN+=("$FOUND")
done

# ---- 3. report + bundle (everything above the leaf, issuing-CA first) ----- #
err ""
err "   # role      subject / issuer / SHA-256"
for i in "${!CHAIN[@]}"; do
    c=${CHAIN[$i]}
    role=intermediate
    [ "$i" = 0 ] && role=leaf
    is_root "$c" && role=root
    err "   $i $(printf '%-12s' "$role") $(subj "$c")"
    err "     $(printf '%-12s' '') issued by: $(issuer "$c")"
    err "     $(printf '%-12s' '') SHA-256:   $(fp "$c")"
done
err ""

if [ ${#CHAIN[@]} -gt 1 ]; then
    cat "${CHAIN[@]:1}" >"$OUT"
    top=${CHAIN[$((${#CHAIN[@]} - 1))]}
    is_root "$top" || err "warning: chain is not rooted (no self-signed CA reached)"
    err "wrote $((${#CHAIN[@]} - 1)) CA certificate(s) to $OUT"
    err "verify the fingerprints above, then paste $OUT into Filearr's"
    err "'CA certificate (PEM)' box or set FILEARR_LDAP_TLS_CA_CERT_FILE."
else
    cat "${CHAIN[0]}" >"$OUT"
    err "warning: only the leaf certificate was obtainable (no chain presented,"
    err "no reachable AIA pointer). Wrote the LEAF to $OUT — trusting it works"
    err "but breaks at certificate renewal; prefer -D/-w credentials for the"
    err "ldap AIA path, or export the CA via certutil on a domain-joined"
    err "machine (see docs-site/security.md)."
fi
