#!/usr/bin/env bash
set -euo pipefail

TOTAL=2
CURRENT=1

# Function to show step
step() {
    echo -e "\n[$CURRENT/$TOTAL] $1..."
    ((CURRENT++))
}

# Step 1: Installing Deps
step "Installing Deps"
sudo apt-get update
sudo apt-get install -y build-essential make gcc git pigpio

# Step 2: installing ADXL345SPI Tool (workaround para git como root)
step "Installing LIS2DWUSB Tool"

# We detect the real user (for when the script is executed with Sudo)
CLONE_USER="${SUDO_USER:-$USER}"

if [ "$(id -u)" -eq 0 ]; then
    echo "Cloning as $CLONE_USER to avoid Git root warning..."
    sudo -u "$CLONE_USER" mkdir -p LISTool
    cd LISTool
    sudo -u "$CLONE_USER" git clone https://github.com/navaismo/lis2dwusb.git
else
    git clone https://github.com/navaismo/lis2dwusb.git
fi

cd lis2dwusb/Linux_Wrapper
sudo make
sudo make install

if [ "$(id -u)" -eq 0 ]; then
    echo "Cloning as $CLONE_USER to avoid Git root warning..."
    sudo -u "$CLONE_USER" echo -e "octoprint ALL=(ALL) NOPASSWD: $(which lis2dwusb)\n$CLONE_USER ALL=(ALL) NOPASSWD: $(which lis2dwusb)" |
        sudo tee /etc/sudoers.d/octoprint_lis2dw

else
    echo -e "octoprint ALL=(ALL) NOPASSWD: $(which lis2dwusb)\n$(whoami) ALL=(ALL) NOPASSWD: $(which lis2dwusb)" |
        sudo tee /etc/sudoers.d/octoprint_lis2dw
fi

echo -e "\n✔ All steps completed successfully!"
echo -e "Reboot the system to apply changes.\n"
