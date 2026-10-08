#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the padnamer authors
# padnamer installer for SteamOS (and other systemd Linux).
# Run from Desktop Mode as your normal user (deck). It asks for your sudo password.
# Safe to re-run: use it again after a SteamOS update if anything goes missing.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/.local/share/padnamer"
CFG_DIR="$HOME/.config/padnamer"
CFG="$CFG_DIR/config.toml"
UNIT=/etc/systemd/system/padnamer.service
DECKY_PLUGINS="$HOME/homebrew/plugins"

if [[ $EUID -eq 0 ]]; then
    echo "Run this as your normal user (e.g. deck), not with sudo. It will ask for sudo itself."
    exit 1
fi
if ! /usr/bin/python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
    echo "padnamer needs Python 3.11 or newer at /usr/bin/python3."
    exit 1
fi

echo "Installing padnamer to $APP"
mkdir -p "$APP/web" "$CFG_DIR" "$HOME/.local/bin" "$HOME/.local/share/applications"
install -m 755 "$HERE/padnamer.py" "$APP/padnamer.py"
install -m 644 "$HERE/web/index.html" "$APP/web/index.html"
install -m 755 "$HERE/padnamer-ui" "$APP/padnamer-ui"
ln -sf "$APP/padnamer.py" "$HOME/.local/bin/padnamer"
sed -e "s#@APP@#$APP#g" "$HERE/padnamer-controllers.desktop" > "$HOME/.local/share/applications/padnamer-controllers.desktop"

if [[ ! -f $CFG ]]; then
    install -m 644 "$HERE/config.example.toml" "$CFG"
    echo "Created $CFG"
fi
/usr/bin/python3 "$APP/padnamer.py" check --config "$CFG" >/dev/null

echo "Installing the system service (needs sudo)..."
sed -e "s#@APP@#$APP#g" -e "s#@CFG@#$CFG#g" "$HERE/padnamer.service" | sudo tee "$UNIT" >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable padnamer.service >/dev/null
sudo systemctl restart padnamer.service

if [[ -d $DECKY_PLUGINS ]]; then
    echo "Installing the Quick Access panel into Decky Loader..."
    sudo rm -rf "$DECKY_PLUGINS/padnamer"
    sudo mkdir -p "$DECKY_PLUGINS/padnamer/dist"
    for f in plugin.json package.json main.py LICENSE LICENSE.template-BSD-3-Clause; do
        sudo install -m 644 "$HERE/decky/$f" "$DECKY_PLUGINS/padnamer/$f"
    done
    sudo install -m 644 "$HERE/decky/dist/index.js" "$DECKY_PLUGINS/padnamer/dist/index.js"
    sudo systemctl restart plugin_loader.service 2>/dev/null || true
    DECKY_NOTE="Quick Access panel installed (… button → plug icon → padnamer)."
else
    DECKY_NOTE="Decky Loader not found: the Quick Access panel was skipped. Install Decky, then re-run this."
fi

sleep 2
if systemctl is-active --quiet padnamer.service; then
    echo
    /usr/bin/python3 "$APP/padnamer.py" status || true
    echo
    echo "Controller page: http://127.0.0.1:8742 (on your phone: see the address and PIN on that page)"
    echo "$DECKY_NOTE"
    echo "Library tile:    Steam → Add a Non-Steam Game → tick 'Controllers'"
    echo "Logs:            journalctl -u padnamer -f"
else
    echo "padnamer failed to start:"
    sudo journalctl -u padnamer -n 30 --no-pager
    exit 1
fi
