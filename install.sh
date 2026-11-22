#!/usr/bin/env bash
set -euo pipefail

echo 'GNOME Recorder — установка для Ubuntu / GNOME'
sudo apt update
sudo apt install -y python3-gi python3-gst-1.0 gir1.2-gtk-3.0 \
  gir1.2-ayatanaappindicator3-0.1 libayatana-appindicator3-1 \
  gstreamer1.0-tools gstreamer1.0-pipewire gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
  gstreamer1.0-plugins-ugly gstreamer1.0-libav \
  pulseaudio-utils xdg-desktop-portal xdg-desktop-portal-gnome

APP_DIR="$HOME/.local/share/gnome-recorder"
mkdir -p "$APP_DIR" "$HOME/.local/share/applications" "$HOME/.config/autostart"
cp "$(dirname "$0")/recorder.py" "$APP_DIR/recorder.py"
# Копируем иконки интерфейса вместе с программой.
mkdir -p "$APP_DIR/icons"
cp "$(dirname "$0")"/icons/*.png "$APP_DIR/icons/"
chmod +x "$APP_DIR/recorder.py"
cat > "$HOME/.local/share/applications/io.github.lancy.GnomeRecorder.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=GNOME Recorder
Name[ru]=Запись экрана и аудио
Comment=Screen and audio recorder for GNOME Wayland
Exec=/usr/bin/python3 $APP_DIR/recorder.py
Icon=media-record
Terminal=false
Categories=AudioVideo;Recorder;
StartupNotify=false
EOF
cp "$HOME/.local/share/applications/io.github.lancy.GnomeRecorder.desktop" \
   "$HOME/.config/autostart/io.github.lancy.GnomeRecorder.desktop"

# GSettings customized shortcuts (do not overwrite existing bindings).
python3 "$(dirname "$0")/shortcuts.py" install "$APP_DIR/recorder.py"
echo
echo 'Установка завершена.'
echo "Запуск: /usr/bin/python3 $APP_DIR/recorder.py"
echo 'Вход в систему: автозапуск включён (можно отключить в Приложениях автозагрузки).'
echo 'Горячие клавиши: Ctrl+Alt+R, Ctrl+Alt+P, Ctrl+Alt+S.'
