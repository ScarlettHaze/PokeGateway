#!/usr/bin/env bash
# Detect this machine's LAN IP, make sure certificates exist, and launch the
# full stack (PokeGateway + the SSLv3/RC4 proxy) with docker compose.
set -euo pipefail
cd "$(dirname "$0")"

# 1. Config file.
if [[ ! -f config.ini ]]; then
    cp config.example.ini config.ini
    echo "Created config.ini from config.example.ini"
fi

# 2. LAN IP (used as the DNS answer and svchost).
detect_ip() {
    if command -v ip >/dev/null 2>&1; then
        ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}'
    else
        # macOS / BSD
        route get 1.1.1.1 2>/dev/null | awk '/interface:/{print $2}' | \
            xargs -I{} ipconfig getifaddr {} 2>/dev/null
    fi
}
SERVER_IP="${SERVER_IP:-$(detect_ip || true)}"
if [[ -z "${SERVER_IP:-}" ]]; then
    echo "Could not auto-detect your LAN IP. Set it with: SERVER_IP=1.2.3.4 ./start.sh" >&2
    exit 1
fi
# Write it into config.ini (replace public_ip line).
sed -i.bak -E "s/^public_ip = .*/public_ip = ${SERVER_IP}/" config.ini && rm -f config.ini.bak
echo "Detected server IP: ${SERVER_IP}"

# 3. Certificates.
mkdir -p certs data dlc
if [[ ! -f certs/server-chain.crt || ! -f certs/server.key ]]; then
    echo
    echo "No login certificate found. Generating one..."
    if ! bash deploy/sslproxy/gen-certs.sh -d certs; then
        cat >&2 <<EOF

The DS login (nas.nintendowifi.net) needs a certificate signed by the Wii
NWC certificate. Put WII_NWC_1_CERT.p12 (or NWC.crt + NWC.key) in ./certs
and run ./start.sh again. Without it, only plain-HTTP services work.
See the README section "SSL certificates".
EOF
    fi
fi

# 4. Launch. Use Compose v2 (`docker compose`) if present, else the older
#    standalone `docker-compose`.
if docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"
else
    cat >&2 <<EOF
error: neither 'docker compose' nor 'docker-compose' is available.
Install Docker Compose:
  - Docker Desktop includes it, or
  - macOS:  brew install docker-compose
Then run ./start.sh again.
EOF
    exit 1
fi

echo
echo "Starting Docker stack with: ${COMPOSE}"
${COMPOSE} up -d --build
echo
echo "PokeGateway is up. On your DS, set both DNS servers to ${SERVER_IP}."
echo "Follow logs with:  ${COMPOSE} logs -f"
