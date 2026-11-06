#!/usr/bin/env bash
# Shared installation helpers for Raspberry Pi OS / Debian hosts.
set -euo pipefail

require_root() {
    if [[ "$EUID" -ne 0 ]]; then
        echo "Run this installer with sudo." >&2
        exit 1
    fi
    OCTOPRINT_USER="${OCTOPRINT_USER:-${SUDO_USER:-octoprint}}"
    if ! id "$OCTOPRINT_USER" >/dev/null 2>&1; then
        echo "Set OCTOPRINT_USER to the account running OctoPrint." >&2
        exit 1
    fi
}

checkout_wrapper() {
    local repository="$1"
    local revision="$2"
    INSTALL_WORK_DIR="$(mktemp -d -t pinput-sensor.XXXXXXXX)"
    trap 'rm -rf -- "$INSTALL_WORK_DIR"' EXIT
    git clone "https://github.com/navaismo/$repository.git" "$INSTALL_WORK_DIR/source"
    git -C "$INSTALL_WORK_DIR/source" checkout --detach "$revision"
    cd "$INSTALL_WORK_DIR/source"
}

allow_serial_access() {
    usermod -a -G dialout "$OCTOPRINT_USER"
    echo "Serial access granted to $OCTOPRINT_USER; restart its login session and OctoPrint."
}
