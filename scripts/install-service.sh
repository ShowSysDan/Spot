#!/usr/bin/env bash
# Install Spot as a regular (non-templated) systemd service.
#
# Usage (run from anywhere):
#   sudo ./scripts/install-service.sh                   # uses current user + repo dir
#   sudo ./scripts/install-service.sh dan /home/dan/Spot
#   SPOT_USER=dan SPOT_HOME=/home/dan/Spot sudo -E ./scripts/install-service.sh
#
# The result is /etc/systemd/system/spot.service, enabled and started.
set -euo pipefail

# Resolve defaults: under `sudo`, $SUDO_USER points at the original user.
SPOT_USER="${SPOT_USER:-${1:-${SUDO_USER:-$(id -un)}}}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_HOME="$(cd "$SCRIPT_DIR/.." && pwd)"
SPOT_HOME="${SPOT_HOME:-${2:-$DEFAULT_HOME}}"

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    cat <<EOF
Usage: $0 [SPOT_USER] [SPOT_HOME]

Defaults:
  SPOT_USER = ${SPOT_USER}
  SPOT_HOME = ${SPOT_HOME}

Substitutes those values into spot.service, installs it as
/etc/systemd/system/spot.service, then enables and starts the service.
Requires sudo / root.
EOF
    exit 0
fi

if [[ $EUID -ne 0 ]]; then
    echo "This script must be run as root (try: sudo $0)" >&2
    exit 1
fi

UNIT_SRC="$SPOT_HOME/spot.service"
UNIT_DST="/etc/systemd/system/spot.service"
VENV_BIN="$SPOT_HOME/.venv/bin/gunicorn"
ENV_FILE="$SPOT_HOME/.env"

[[ -f "$UNIT_SRC" ]] || { echo "Not found: $UNIT_SRC" >&2; exit 1; }
[[ -x "$VENV_BIN" ]] || { echo "Not found / not executable: $VENV_BIN
(Did you run '.venv/bin/pip install -r requirements.txt'?)" >&2; exit 1; }
[[ -f "$ENV_FILE" ]] || { echo "Not found: $ENV_FILE  (cp .env.example .env first)" >&2; exit 1; }
id -u "$SPOT_USER" >/dev/null 2>&1 || { echo "User does not exist: $SPOT_USER" >&2; exit 1; }

echo "Installing spot.service:"
echo "  user:  $SPOT_USER"
echo "  home:  $SPOT_HOME"

TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
sed -e "s|__SPOT_USER__|$SPOT_USER|g" \
    -e "s|__SPOT_HOME__|$SPOT_HOME|g" \
    "$UNIT_SRC" > "$TMP"

install -m 0644 "$TMP" "$UNIT_DST"
systemctl daemon-reload
systemctl enable --now spot.service
systemctl --no-pager status spot.service || true

echo
echo "Done. Useful commands:"
echo "  sudo systemctl status spot"
echo "  sudo systemctl restart spot"
echo "  sudo journalctl -u spot -f"
