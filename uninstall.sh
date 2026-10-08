#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the padnamer authors
# Removes padnamer and un-hides the original controllers.
# Your settings and controller list in ~/.config/padnamer are kept.
set -euo pipefail

sudo systemctl disable --now padnamer.service 2>/dev/null || true
sudo rm -f /etc/systemd/system/padnamer.service
if [[ -f /etc/udev/rules.d/72-padnamer.rules ]]; then
    sudo rm -f /etc/udev/rules.d/72-padnamer.rules
    sudo udevadm control --reload
    sudo udevadm trigger --action=change --subsystem-match=input --subsystem-match=hidraw
fi
sudo systemctl daemon-reload
if [[ -d $HOME/homebrew/plugins/padnamer ]]; then
    sudo rm -rf "$HOME/homebrew/plugins/padnamer"
    sudo systemctl restart plugin_loader.service 2>/dev/null || true
fi
rm -f "$HOME/.local/bin/padnamer" "$HOME/.local/share/applications/padnamer-controllers.desktop"
rm -rf "$HOME/.local/share/padnamer"
echo "padnamer removed. Settings left in ~/.config/padnamer."
