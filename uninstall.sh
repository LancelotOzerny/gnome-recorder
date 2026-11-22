#!/usr/bin/env bash
set -euo pipefail
python3 "$(dirname "$0")/shortcuts.py" uninstall || true
rm -f "$HOME/.config/autostart/io.github.lancy.GnomeRecorder.desktop"
rm -f "$HOME/.local/share/applications/io.github.lancy.GnomeRecorder.desktop"
rm -rf "$HOME/.local/share/gnome-recorder"
echo 'Программа удалена. Ваши настройки и записи сохранены.'
