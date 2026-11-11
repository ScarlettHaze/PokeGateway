#!/usr/bin/env bash
# Generate the TLS certificates the Nintendo DS needs, into ./certs.
#
# The DS accepts a server certificate only if it chains to Nintendo's CA.
# The "nds-constrain't" bug lets a certificate signed by the Wii's NWC
# client certificate (which Nintendo's CA signed) act as that chain. So you
# must supply the Wii NWC certificate and key. This script does the rest.
#
# Provide the NWC material in ./certs as EITHER:
#   * WII_NWC_1_CERT.p12   (PKCS#12; default password "alpine" or pass -p)
#   * NWC.crt and NWC.key  (PEM)
#
# You can dump these from your own Wii, or get the public p12 (as used by
# the DS preservation community) from, e.g.:
#   https://github.com/FreakedOutGamer/NDS-pokemon-mystery-gift-distribution
#
# Usage: deploy/sslproxy/gen-certs.sh [-d certs_dir] [-p p12_password]
set -euo pipefail

CERT_DIR="certs"
P12_PASS="alpine"
while getopts "d:p:h" opt; do
    case "$opt" in
        d) CERT_DIR="$OPTARG" ;;
        p) P12_PASS="$OPTARG" ;;
        h) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "try -h" >&2; exit 2 ;;
    esac
done

mkdir -p "$CERT_DIR"
cd "$CERT_DIR"

# 1. Obtain the Wii NWC certificate and key (PEM).
if [[ ! -f NWC.crt || ! -f NWC.key ]]; then
    if [[ -f WII_NWC_1_CERT.p12 ]]; then
        echo "Extracting NWC.crt / NWC.key from WII_NWC_1_CERT.p12 ..."
        # OpenSSL 3 reads this old (RC2/3DES) PKCS#12 only with -legacy.
        LEGACY=""
        if ! openssl pkcs12 -in WII_NWC_1_CERT.p12 -passin "pass:${P12_PASS}" \
                -nokeys -clcerts >/dev/null 2>&1; then
            LEGACY="-legacy"
        fi
        openssl pkcs12 $LEGACY -in WII_NWC_1_CERT.p12 -passin "pass:${P12_PASS}" \
            -nokeys -clcerts 2>/dev/null | openssl x509 -out NWC.crt
        openssl pkcs12 $LEGACY -in WII_NWC_1_CERT.p12 -passin "pass:${P12_PASS}" \
            -nocerts -nodes 2>/dev/null | openssl rsa -out NWC.key 2>/dev/null
    else
        cat >&2 <<EOF
error: no Wii NWC certificate found in '$PWD'.
Put either WII_NWC_1_CERT.p12, or NWC.crt and NWC.key, here and re-run.
See the comments at the top of this script for where to get them.
EOF
        exit 1
    fi
fi

# 2. Server certificate for all the HTTPS hosts the DS contacts: 1024-bit
#    RSA, SHA-1, signed by the NWC certificate. (The DS supports nothing
#    newer than RSA-1024/SHA-1.)
#
#    The DS matches the certificate name label-for-label, with '*' matching
#    one label. CN=*.*.* only covers 3-label hosts (nas.nintendowifi.net),
#    so GTS (gamestats2.gs.nintendowifi.net, 4 labels) and pkvldtprod
#    (pkvldtprod.nintendo.co.jp, 4 labels) would fail. We add SANs covering
#    3-, 4- and 5-label hosts plus the specific domains.
if [[ ! -f server.key ]]; then
    openssl genrsa -out server.key 1024 2>/dev/null
fi
cat > san.ext <<'EOF'
subjectAltName=DNS:*.*.*,DNS:*.*.*.*,DNS:*.*.*.*.*,DNS:*.nintendowifi.net,DNS:*.gs.nintendowifi.net,DNS:*.available.gs.nintendowifi.net,DNS:*.master.gs.nintendowifi.net,DNS:*.nintendo.co.jp,DNS:*.pokemon-gl.com,DNS:nintendowifi.net
EOF
openssl req -new -key server.key -subj "/CN=*.*.*" -out server.csr
openssl x509 -req -in server.csr -CA NWC.crt -CAkey NWC.key -CAcreateserial \
    -sha1 -days 3650 -extfile san.ext -out server.crt 2>/dev/null
cat server.crt NWC.crt > server-chain.crt

# 3. Gen 5 Global Terminal certificate: self-signed, not validated by the game.
if [[ ! -f gt5.key || ! -f gt5.crt ]]; then
    openssl req -x509 -newkey rsa:1024 -nodes -days 3650 \
        -subj "/CN=pkgdsprod.nintendo.co.jp" -keyout gt5.key -out gt5.crt 2>/dev/null
fi

echo "Done. Certificates written to $PWD:"
ls -1 server-chain.crt server.key gt5.crt gt5.key
echo
echo "Chain check:"
openssl verify -CAfile NWC.crt -no_check_time server.crt 2>/dev/null \
    || echo "  (self-signed NWC root is expected; the DS trusts it via the CA chain)"
