#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "$0")" && pwd)/install-common.sh"
require_root

config=/boot/firmware/config.txt
if [[ ! -f "$config" ]]; then
    config=/boot/config.txt
fi
if [[ ! -f "$config" ]]; then
    echo "Raspberry Pi boot configuration was not found." >&2
    exit 1
fi
if grep -Eq '^#?dtparam=spi=' "$config"; then
    sed -i -E 's/^#?dtparam=spi=.*/dtparam=spi=on/' "$config"
else
    echo 'dtparam=spi=on' >> "$config"
fi

apt-get update
apt-get install -y build-essential make gcc git pigpio python3-pigpio libopenblas-dev sudo
checkout_wrapper adxl345spi 13487d93e9cdd5a1f95386ac77badcc5da444b8a
make
make install

# SPI GPIO access requires the privileged C wrapper. Validate sudoers before
# replacing the active rule, and keep the installed executable root-owned.
chown root:root /usr/local/bin/adxl345spi
chmod 755 /usr/local/bin/adxl345spi
printf '%s ALL=(root) NOPASSWD: /usr/local/bin/adxl345spi\n' "$OCTOPRINT_USER" > "$INSTALL_WORK_DIR/sudoers"
visudo -cf "$INSTALL_WORK_DIR/sudoers"
install -o root -g root -m 440 "$INSTALL_WORK_DIR/sudoers" /etc/sudoers.d/octoprint_adxl
echo "Installed ADXL345 SPI wrapper. Reboot to apply SPI changes."
