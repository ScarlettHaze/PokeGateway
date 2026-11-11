#!/usr/bin/env bash
# macOS launcher.
#
# Docker-on-Mac runs in a VM, so `network_mode: host` can't expose ports on
# your Mac's LAN IP where the DS can reach them. This script instead runs
# PokeGateway NATIVELY (ports land on the Mac's real IP) and runs only the
# SSLv3/RC4 proxy in a container with published ports pointed back at the
# native server. This is the combination verified to log a real DS in.
#
# Usage:  ./start-macos.sh         (override IP with SERVER_IP=1.2.3.4)
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

# Bind everything to the LAN IP (so DNS coexists with any localhost resolver)
# and answer DNS with it.
sed -i '' -E "s/^public_ip = .*/public_ip = ${IP}/; s/^bind = .*/bind = ${IP}/" config.ini

mkdir -p certs data dlc

# Certificates.
if [ ! -f certs/server-chain.crt ] || [ ! -f certs/server.key ]; then
    echo "Generating certificates..."
    bash deploy/sslproxy/gen-certs.sh -d certs || {
        echo "Put WII_NWC_1_CERT.p12 (or NWC.crt + NWC.key) in ./certs and re-run." >&2
        echo "Continuing without a login certificate; HTTPS login will fail." >&2
    }
fi

# Build the proxy image if it isn't present yet.
if ! docker image inspect pokegateway-sslproxy >/dev/null 2>&1; then
    echo "Building the SSL proxy image (first run only, a few minutes)..."
    docker build -t pokegateway-sslproxy deploy/sslproxy
fi

# Proxy config that forwards to the native server on the Mac's LAN IP.
sed "s/127.0.0.1:80/${IP}:80/; s/127.0.0.1:12411/${IP}:12411/" \
    deploy/sslproxy/stunnel.conf > stunnel-host.conf

# (Re)start the proxy container in the background, published on all interfaces.
docker rm -f pokegateway-proxy >/dev/null 2>&1 || true
docker run -d --name pokegateway-proxy \
    -p 0.0.0.0:443:443 -p 0.0.0.0:12401:12401 \
    -v "$PWD/certs:/certs:ro" \
    -v "$PWD/stunnel-host.conf:/etc/stunnel/stunnel.conf:ro" \
    pokegateway-sslproxy >/dev/null
echo "SSL proxy container started (ports 443, 12401)."

cleanup() { docker rm -f pokegateway-proxy >/dev/null 2>&1 || true; }
trap cleanup EXIT INT TERM

echo
echo "Starting PokeGateway (needs sudo to bind ports 53 and 80)..."
echo "On your DS, set both DNS servers to ${IP}. Press Ctrl-C to stop everything."
echo
sudo python3 -m pokegateway -c config.ini
