#!/usr/bin/env bash
# Jednorazowa konfiguracja uprawnien + ikona w menu aplikacji (Fedora).
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"

sudo dnf install -y python3-evdev python3-gobject python3-cairo gtk4 libadwaita libnotify wl-clipboard

echo uinput | sudo tee /etc/modules-load.d/uinput.conf >/dev/null
sudo modprobe uinput

# Reguly udev generowane na podstawie podlaczonej myszy (wezszy dostep niz "wszystkie urzadzenia Logitecha").
# Dla pewnosci polacz mysz przed uruchomieniem tego skryptu.
python3 "$DIR/mxremap.py" --udev-rules | sudo tee /etc/udev/rules.d/70-mxremap.rules
sudo udevadm control --reload
sudo udevadm trigger

chmod +x "$DIR/mxremap.py" "$DIR/mxremap_gui.py" "$DIR/mxremap_tray.py"
mkdir -p ~/.local/share/applications
rm -f ~/.local/share/applications/mxremap.desktop
cat > ~/.local/share/applications/pl.mxremap.App.desktop <<DESK
[Desktop Entry]
Type=Application
Name=MX Remap
Comment=Konfigurator przyciskow i ustawien myszy Logitech
Exec=python3 $DIR/mxremap_gui.py
Icon=input-mouse
Categories=Settings;HardwareSettings;
StartupNotify=true
DESK

# Silnik jako usluga systemd uzytkownika (sam wstaje po zalogowaniu i po awarii); stary autostart zbedny
rm -f ~/.config/autostart/mxremap.desktop
pkill -u "$USER" -f 'mxremap\.py -c' || true
python3 "$DIR/mxremap.py" --install-service || echo "Uwaga: nie udalo sie wlaczyc uslugi - program uzyje zwyklego autostartu."

# Poza GNOME (KDE, XFCE...) ikone baterii pokazuje zasobnik systemowy - wlaczany w programie (lub: --install-tray)
case "${XDG_CURRENT_DESKTOP:-}" in
  *GNOME*) ;;
  *) python3 "$DIR/mxremap.py" --install-tray || true ;;
esac

echo "Gotowe. Jesli program zglasza brak uprawnien - wyloguj sie i zaloguj ponownie (lub odlacz/podlacz mysz)."
