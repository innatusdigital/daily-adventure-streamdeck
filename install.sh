#!/usr/bin/env bash
# Daily Adventure Stream Deck — installer & updater
# Usage: curl -sSL https://raw.githubusercontent.com/innatusdigital/daily-adventure-streamdeck/main/install.sh | bash
set -euo pipefail

REPO_URL="https://github.com/innatusdigital/daily-adventure-streamdeck"
INSTALL_DIR="$HOME/daily-adventure-streamdeck"
VENV_DIR="$INSTALL_DIR/.venv"
CONF_FILE="/etc/streamdeck.conf"
SERVICE_NAME="streamdeck"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()    { echo -e "${GREEN}==>${NC} $*"; }
warning() { echo -e "${YELLOW}[!]${NC} $*"; }

echo ""
echo "  Daily Adventure — Stream Deck Installer"
echo "  ─────────────────────────────────────────"
echo ""

# -- System dependencies
# `apt-get update` is best-effort — Pis often carry stale third-party repos
# (Mopidy, Coral, etc.) that return non-zero. As long as the packages we need
# are already in the cached lists, install still works. Don't let one broken
# repo block the install.
info "Installing system packages..."
sudo apt-get update -qq || warning "apt-get update reported warnings (likely stale third-party repos) — continuing"
sudo apt-get install -y git python3-venv libhidapi-libusb0 librsvg2-bin \
    fonts-noto-color-emoji 2>&1 | grep -E "^(Get|Setting|Unpacking|E:)" || true

# -- Clone or update
if [ -d "$INSTALL_DIR/.git" ]; then
    info "Updating existing installation..."
    git -C "$INSTALL_DIR" pull --ff-only
else
    info "Cloning repository..."
    git clone "$REPO_URL" "$INSTALL_DIR"
fi

# -- Python virtual environment
info "Setting up Python environment..."
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip --quiet
"$VENV_DIR/bin/pip" install streamdeck Pillow requests --quiet

# -- Render icons from SVG
info "Rendering icons..."
mkdir -p "$INSTALL_DIR/icons"
rsvg-convert -w 512 -h 512 \
    "$INSTALL_DIR/icons/playpause.svg" \
    -o "$INSTALL_DIR/icons/playpause.png"

# -- udev rule (non-root USB access to Stream Deck)
UDEV_RULE="/etc/udev/rules.d/50-streamdeck.rules"
if [ ! -f "$UDEV_RULE" ]; then
    info "Installing udev rule for Stream Deck USB access..."
    echo 'SUBSYSTEM=="usb", ATTRS{idVendor}=="0fd9", GROUP="plugdev", TAG+="uaccess"' \
        | sudo tee "$UDEV_RULE" > /dev/null
    sudo udevadm control --reload-rules
    sudo usermod -aG plugdev "$USER"
    warning "You may need to unplug and replug the Stream Deck, or reboot, for USB access to take effect."
fi

# -- First-time config
if [ ! -f "$CONF_FILE" ]; then
    if [ -n "${1:-}" ]; then
        api_key="$1"
        api_url="https://www.dailyadventure.io"
        info "Using API key from install command."
    else
        echo ""
        echo "  First-time setup"
        echo "  ─────────────────"
        echo "  Open https://www.dailyadventure.io/streamdeck in your browser,"
        echo "  go to the API Key section, and generate a key."
        echo ""
        read -rp "  API URL [https://www.dailyadventure.io]: " api_url
        api_url="${api_url:-https://www.dailyadventure.io}"
        echo ""
        read -rp "  API Key (da_...): " api_key
        echo ""
    fi

    printf 'DA_API_URL=%s\nDA_API_KEY=%s\n' "$api_url" "$api_key" \
        | sudo tee "$CONF_FILE" > /dev/null
    sudo chmod 600 "$CONF_FILE"
    info "Config saved to $CONF_FILE"
else
    info "Config already exists at $CONF_FILE — skipping."
fi

# -- systemd service (regenerated each install so paths stay correct)
info "Installing systemd service..."
sudo tee "$SERVICE_FILE" > /dev/null << EOF
[Unit]
Description=Daily Adventure Stream Deck Runner
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
EnvironmentFile=$CONF_FILE
ExecStart=$VENV_DIR/bin/python3 $INSTALL_DIR/streamdeck_runner.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

echo ""
info "All done!"
echo ""
echo "  Useful commands:"
echo "    View logs:   sudo journalctl -u $SERVICE_NAME -f"
echo "    Status:      sudo systemctl status $SERVICE_NAME"
echo "    Update:      curl -sSL https://raw.githubusercontent.com/innatusdigital/daily-adventure-streamdeck/main/install.sh | bash"
echo ""
