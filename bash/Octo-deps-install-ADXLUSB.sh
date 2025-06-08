#!/usr/bin/env bash
set -euo pipefail

TOTAL=2
CURRENT=1
WRAPPER_DST="/usr/local/bin/adxl345spi"
WRAPPER_SRC="adxl345usb"

# Function to show step
step() {
    echo -e "\n[$CURRENT/$TOTAL] $1..."
    ((CURRENT++))
}

# Step 1: Installing Deps
step "Installing Deps"
sudo apt-get update
sudo apt-get install -y build-essential make gcc git pigpio python3-pigpio libopenblas-dev 

# Step 2: installing ADXL345SPI Tool (workaround para git como root)
step "Installing ADXL345USB Tool"

# We detect the real user (for when the script is executed with Sudo)
CLONE_USER="${SUDO_USER:-$USER}"

if [ "$(id -u)" -eq 0 ]; then
    echo "Cloning as $CLONE_USER to avoid Git root warning..."
    sudo -u "$CLONE_USER" mkdir -p ADXLTool
    cd ADXLTool
    sudo -u "$CLONE_USER" git clone https://github.com/navaismo/adxl345usb

else
    git clone https://github.com/navaismo/adxl345usb
fi

cd adxl345usb


if [ "$(id -u)" -eq 0 ]; then

    echo "Installing ADXL345USB wrapper → ${WRAPPER_DST}"
    install -m 755 "$WRAPPER_SRC" "$WRAPPER_DST"

    echo "Cloning as $CLONE_USER to avoid Git root warning..."
    sudo -u "$CLONE_USER" echo -e "octoprint ALL=(ALL) NOPASSWD: $(which adxl345spi)\n$CLONE_USER ALL=(ALL) NOPASSWD: $(which adxl345spi)" |
        sudo tee /etc/sudoers.d/octoprint_adxl

else
    echo -e "octoprint ALL=(ALL) NOPASSWD: $(which adxl345spi)\n$(whoami) ALL=(ALL) NOPASSWD: $(which adxl345spi)" |
        sudo tee /etc/sudoers.d/octoprint_adxl
fi

echo -e "\n✔ All steps completed successfully!"
echo -e "Reboot the system to apply changes.\n"
