#!/usr/bin/env bash
# fetch-ldaps-ca.sh — pull an LDAPS server's certificate chain and emit a PEM
# CA bundle ready for Filearr (paste into Admin → Authentication → CA
# certificate (PEM), or mount and point FILEARR_LDAP_TLS_CA_CERT_FILE at it).
#
# AD domain controllers frequently present ONLY their leaf certificate, so
# when the presented chain is incomplete this walks the Authority Information
# Access (AIA) "CA Issuers" pointers — the URL each certificate carries to its
# issuer's certificate — until it reaches a self-signed root (or the pointers
# run out). AIA payloads in DER, PEM and PKCS#7 are all handled.
#
# The pull is UNVALIDATED (trust-on-first-use): verify the printed SHA-256
# fingerprints against your PKI documentation before trusting the bundle.
#
# Usage: fetch-ldaps-ca.sh <host[:port]> [output.pem]
#        port defaults to 636 (use host:3269 for a Global Catalog)
# Needs: openssl, curl, awk.

set -euo pipefail

MAX_AIA_DEPTH=4
MAX_AIA_BYTES=1048576

err() { printf '%s\n' "$*" >&2; }
die() { err "error: $*"; exit 1; }

[ $# -ge 1 ] || die "usage: $0 <host[:port]> [output.pem]"
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

# Normalize any certificate payload (X.509 DER/PEM, PKCS#7 DER/PEM) to a PEM
# file that may hold several certificates. Prints the output path on success.
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
n=${#CHAIN[@]}
for _ in $(seq 1 "$MAX_AIA_DEPTH"); do
    top=${CHAIN[$((${#CHAIN[@]} - 1))]}
    is_root "$top" && break
    want=$(issuer "$top")
    found=""
    while IFS= read -r url; do
        [ -n "$url" ] || continue
        case $url in http://* | https://*) ;; *) continue ;; esac
        err "-> following AIA pointer: $url"
        curl -fsSL --max-time 15 --max-filesize "$MAX_AIA_BYTES" \
            -o "$WORK/aia.bin" "$url" 2>/dev/null || continue
        normalize "$WORK/aia.bin" "$WORK/aia.pem" || continue
        csplit -sz -f "$WORK/aiacert-" -b '%02d.pem' "$WORK/aia.pem" \
            '/BEGIN CERTIFICATE/' '{*}' 2>/dev/null || continue
        for cand in "$WORK"/aiacert-*.pem; do
            [ -s "$cand" ] || continue
            # pkcs7 -print_certs preamble text can land in its own chunk
            openssl x509 -in "$cand" -noout 2>/dev/null || continue
            if [ "$(subj "$cand")" = "$want" ] && [ -z "${SEEN[$(fp "$cand")]:-}" ]; then
                found="$WORK/cert-$(printf '%02d' "$n").pem"
                cp "$cand" "$found"
                SEEN[$(fp "$found")]=1
                n=$((n + 1))
                break
            fi
        done
        rm -f "$WORK"/aiacert-*.pem "$WORK/aia.bin" "$WORK/aia.pem"
        [ -n "$found" ] && break
    done < <(openssl x509 -in "$top" -noout -ext authorityInfoAccess 2>/dev/null |
        awk -F'URI:' '/CA Issuers/ {print $2}')
    if [ -z "$found" ]; then
        err "   issuer '$want' not reachable via AIA — stopping"
        break
    fi
    CHAIN+=("$found")
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
    err "but breaks at certificate renewal; prefer exporting the CA via"
    err "certutil on a domain-joined machine (see docs-site/security.md)."
fi
