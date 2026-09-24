#!/usr/bin/env bash
# Install the EMO-Bot systemd service for the current user and this checkout (RUNNING.md, section 10).
#
#   ./deploy/install-service.sh            # install, enable and start
#   ./deploy/install-service.sh --dry-run  # print the unit it would install
#
# Fills User= and the paths in deploy/emo-bot.service, checks the pieces the service needs (venv, .env,
# serial port access), then enables it. Needs sudo for /etc/systemd/system.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
user="$(id -un)"
unit_src="$repo/deploy/emo-bot.service"
unit_dst=/etc/systemd/system/emo-bot.service

unit="$(sed -e "s|^User=.*|User=$user|" \
            -e "s|/home/pi/EMO-Bot|$repo|g" "$unit_src")"

if [[ "${1:-}" == "--dry-run" ]]; then
    printf '%s\n' "$unit"
    exit 0
fi

fail() { echo "error: $*" >&2; exit 1; }
[[ -x "$repo/.venv/bin/python" ]] || fail "no virtualenv at $repo/.venv (RUNNING.md, section 6)"
[[ -f "$repo/.env" ]] || fail "no $repo/.env: cp .env.example .env and fill it in (section 7)"
id -nG "$user" | grep -qw dialout || fail "$user is not in the dialout group: sudo usermod -aG dialout $user, then log in again"
"$repo/.venv/bin/python" -c "import aiohttp, serial, paho.mqtt, py_trees, httpx" \
    || fail "missing Python packages: $repo/.venv/bin/python -m pip install -r requirements.txt (or uv pip install)"
systemctl is-active --quiet mosquitto \
    || echo "warning: mosquitto isn't running. The robot works without it, but MQTT control and monitoring won't (sudo apt install mosquitto)"

printf '%s\n' "$unit" | sudo tee "$unit_dst" > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now emo-bot
echo "Installed $unit_dst for $user ($repo). Logs: journalctl -u emo-bot -f"
