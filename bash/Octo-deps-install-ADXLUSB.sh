#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "$0")" && pwd)/install-common.sh"
require_root

apt-get update
apt-get install -y git python3-serial
checkout_wrapper adxl345usb e7c6f186c3455eb34f5ee071b51c4164fa7ec8c0
# Keep USB and SPI wrappers distinct; never overwrite adxl345spi.
install -o root -g root -m 755 adxl345usb /usr/local/bin/adxl345usb
allow_serial_access
echo "Installed ADXL345 USB wrapper. Configure the sensor serial port in the plugin settings."
