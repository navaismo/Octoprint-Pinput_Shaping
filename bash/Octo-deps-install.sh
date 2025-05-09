#!/usr/bin/env bash
set -euo pipefail

TOTAL=3
CURRENT=1

# Function to show step
step() {
    echo -e "\n[$CURRENT/$TOTAL] $1..."
    ((CURRENT++))
}

# Step 1: Enable SPI
step "Enable SPI"
sudo sed -i \
    -e 's/^#\?dtparam=spi=.*/dtparam=spi=on/' \
    /boot/config.txt
sudo grep -q '^dtparam=spi=on' /boot/config.txt ||
    echo 'dtparam=spi=on' | sudo tee -a /boot/config.txt

# Step 2: Installing Deps
step "Installing Deps"
sudo apt-get update
sudo apt-get install -y build-essential make gcc git pigpio python3-pigpio libopenblas-dev

# Step 3: installing ADXL345SPI Tool (workaround para git como root)
step "Installing ADXL345SPI Tool"

# We detect the real user (for when the script is executed with Sudo)
CLONE_USER="${SUDO_USER:-$USER}"

if [ "$(id -u)" -eq 0 ]; then
    echo "Cloning as $CLONE_USER to avoid Git root warning..."
    sudo -u "$CLONE_USER" mkdir -p ADXLTool
    cd ADXLTool
    sudo -u "$CLONE_USER" git clone https://github.com/navaismo/adxl345spi.git
else
    git clone https://github.com/navaismo/adxl345spi.git
fi

cd adxl345spi
sudo make
sudo make install

if [ "$(id -u)" -eq 0 ]; then
    echo "Cloning as $CLONE_USER to avoid Git root warning..."
    sudo -u "$CLONE_USER" echo -e "octoprint ALL=(ALL) NOPASSWD: $(which adxl345spi)\n$CLONE_USER ALL=(ALL) NOPASSWD: $(which adxl345spi)" |
        sudo tee /etc/sudoers.d/octoprint_adxl

else
    echo -e "octoprint ALL=(ALL) NOPASSWD: $(which adxl345spi)\n$(whoami) ALL=(ALL) NOPASSWD: $(which adxl345spi)" |
        sudo tee /etc/sudoers.d/octoprint_adxl
fi

echo -e "\n✔ All steps completed successfully!"
echo -e "Reboot the system to apply changes.\n"
