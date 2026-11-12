#!/usr/bin/env bash
# macOS launcher — native, no Docker.
#
# PokeGateway now terminates the DS's SSLv3/RC4 connections itself (pure
# Python, via tlslite-ng), so there's no stunnel/Docker proxy and none of
# Colima's networking limitations. Everything runs as one process with its
# ports on the Mac's real LAN IP, reachable by the DS.
#
# Usage:  ./start-macos.sh       (override IP with SERVER_IP=1.2.3.4)
set -euo pipefail
cd "$(dirname "$0")"

[ -f config.ini ] || cp config.example.ini config.ini

# LAN IP of this Mac.
IP="${SERVER_IP:-$(route get 1.1.1.1 2>/dev/null | awk '/interface:/{print $2}' \
    | xargs -I{} ipconfig getifaddr {} 2>/dev/null || true)}"
if [ -z "${IP:-}" ]; then
    echo "Could not detect your LAN IP. Re-run as: SERVER_IP=192.168.x.y ./start-macos.sh" >&2
    exit 1
fi
echo "Server IP: ${IP}"

# Bind to the LAN IP (so DNS coexists with any localhost resolver like the
# dnsmasq that macOS/Colima may run on 127.0.0.1:53) and answer DNS with it.
sed -i '' -E "s/^public_ip = .*/public_ip = ${IP}/; s/^bind = .*/bind = ${IP}/" config.ini

mkdir -p certs data dlc

# Certificates for the built-in TLS.
if [ ! -f certs/server-chain.crt ] || [ ! -f certs/server.key ]; then
    echo "Generating certificates..."
    bash deploy/sslproxy/gen-certs.sh -d certs || {
        echo "Put WII_NWC_1_CERT.p12 (or NWC.crt + NWC.key) in ./certs and re-run." >&2
        echo "Without it the DS can't log in (HTTPS)." >&2
    }
fi

# Pure-Python TLS library (needed for the DS's HTTPS: login, GTS validation,
# Mystery Gift, Gen 5 Global Terminal).
PY="${PYTHON:-python3}"
if ! "$PY" -c "import tlslite" >/dev/null 2>&1; then
    echo "Installing tlslite-ng..."
    "$PY" -m pip install --user tlslite-ng || pip3 install --user tlslite-ng || true
fi

echo
echo "Starting PokeGateway (needs sudo to bind ports 53, 80 and 443)."
echo "On your DS, set both DNS servers to ${IP}. Press Ctrl-C to stop."
echo
# sudo runs as root, which doesn't see the user's pip --user packages, so
# pass the user site-packages path through to the sudo'd interpreter.
USER_SITE="$("$PY" -c 'import site; print(site.getusersitepackages())' 2>/dev/null || true)"
exec sudo -E PYTHONPATH="${USER_SITE}${PYTHONPATH:+:$PYTHONPATH}" "$PY" -m pokegateway -c config.ini
