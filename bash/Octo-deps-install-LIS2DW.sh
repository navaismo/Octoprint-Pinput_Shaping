#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "$0")" && pwd)/install-common.sh"
require_root

apt-get update
apt-get install -y build-essential make gcc git
checkout_wrapper lis2dwusb 02d01ba0d2b7161cbe82fe76e1d5b4f62c9b779b
cd Linux_Wrapper
make
make install
allow_serial_access
echo "Installed LIS2DW wrapper for sensor checks."
echo "This wrapper rounds timestamps to milliseconds. Resonance captures are rejected until a precise acquisition-clock wrapper is installed."
