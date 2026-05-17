# Daily Adventure — Stream Deck Runner

Controls a [Elgato Stream Deck](https://www.elgato.com/stream-deck) from a Raspberry Pi, connected to your [Daily Adventure](https://www.dailyadventure.io) account. Assign any Yoto card to a button — press it to play on your Yoto player.

## Requirements

- Raspberry Pi (any model) connected to the internet
- Elgato Stream Deck plugged in via USB
- A Daily Adventure account with Yoto linked

## Install

SSH into your Pi and run:

```bash
curl -sSL https://raw.githubusercontent.com/innatusdigital/daily-adventure-streamdeck/main/install.sh | bash
```

The installer will:
1. Install system and Python dependencies
2. Download this repository to `~/daily-adventure-streamdeck`
3. Prompt for your Daily Adventure API key (one time only)
4. Set up and start a systemd service that runs on boot

## Getting your API key

1. Go to [dailyadventure.io/streamdeck](https://www.dailyadventure.io/streamdeck)
2. Scroll to **API Key** and click **Generate Key**
3. Copy the key — you'll paste it during install

## Button layout

| Slot | Function |
|------|----------|
| 1–13 | Assigned Yoto cards (set from the web UI) |
| 14 | Play / Pause toggle |
| 15 | Settings menu (select player, refresh, update) |

## Updating

Re-run the same install command — it pulls the latest code, updates dependencies, and restarts the service. Your config (`/etc/streamdeck.conf`) is never touched by updates.

```bash
curl -sSL https://raw.githubusercontent.com/innatusdigital/daily-adventure-streamdeck/main/install.sh | bash
```

## Useful commands

```bash
# Live logs
sudo journalctl -u streamdeck -f

# Service status
sudo systemctl status streamdeck

# Restart
sudo systemctl restart streamdeck
```

## Night mode

Set dim and off times from the web UI at [dailyadventure.io/streamdeck](https://www.dailyadventure.io/streamdeck). The Pi picks them up automatically. Press any button while the deck is dimmed or off to wake it without triggering that button.

## Config file

Located at `/etc/streamdeck.conf`. Edit with `sudo nano /etc/streamdeck.conf`.

| Variable | Description |
|----------|-------------|
| `DA_API_URL` | Your Daily Adventure URL (default: `https://www.dailyadventure.io`) |
| `DA_API_KEY` | Your API key from the web UI |
| `DA_POLL_INTERVAL` | How often (seconds) to refresh button config (default: `300`) |
