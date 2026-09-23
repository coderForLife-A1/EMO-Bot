#!/usr/bin/env bash
# Self-signed HTTPS certificate for the laptop console, for CONSOLE_HOST=0.0.0.0 (browsers only allow the
# microphone on https:// or localhost). Not needed with the default SSH tunnel.
#
#   ./tools/make_console_cert.sh            # certificate for this Pi's hostname and IP addresses
#   ./tools/make_console_cert.sh emo.local  # ...plus extra names
#
# Writes deploy/certs/console.crt and console.key (git-ignored) and prints the .env lines to add.
# The browser warns about the certificate once; accept it for this site.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
out="$repo/deploy/certs"
mkdir -p "$out"
chmod 700 "$out"

names=("$(hostname)" "$(hostname).local" localhost "$@")
san=""
for n in "${names[@]}"; do san+="DNS:$n,"; done
for ip in $(hostname -I) 127.0.0.1; do san+="IP:$ip,"; done
san="${san%,}"

openssl req -x509 -newkey rsa:2048 -sha256 -days 825 -nodes \
    -keyout "$out/console.key" -out "$out/console.crt" \
    -subj "/CN=$(hostname) EMO console" -addext "subjectAltName=$san" 2> /dev/null
chmod 600 "$out/console.key"

echo "Wrote $out/console.crt and $out/console.key ($san)"
echo "Add to .env:"
echo "  CONSOLE_HOST=0.0.0.0"
echo "  CONSOLE_TOKEN=$(openssl rand -hex 16)"
echo "  CONSOLE_CERT=$out/console.crt"
echo "  CONSOLE_KEY=$out/console.key"
