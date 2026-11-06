#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "$0")" && pwd)/install-common.sh"
require_root

apt-get update
apt-get install -y build-essential make gcc git
# Protocol 2 and selectable ranges, merged into lis2dwusb/main.
checkout_wrapper lis2dwusb cc660288df4aed9c7106623c12f2587ed6314a65
cd Linux_Wrapper
make
make install
allow_serial_access
echo "Installed LIS2DW protocol 2 wrapper with selectable 2/4/8/16 g ranges."
echo "Update the RP2040 with matching firmware separately; this installer does not flash it."
