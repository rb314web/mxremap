# MX Remap

Nowoczesny konfigurator myszy Logitech dla Linuksa — odpowiednik Logitech Options+ (GTK4 / libadwaita, GNOME i KDE).

- **Wszystkie przyciski**, także przycisk pod kciukiem i zmiana trybu kółka (przez HID++)
- **Gotowe akcje** zamiast wpisywania skrótów: głośność, pulpity, przycisk Windows, zrzuty ekranu, multimedia…
- **Gesty** przycisku kciuka, **makra**, otwieranie aplikacji i stron, wstawianie tekstu, przełączanie wyjścia dźwięku
- **Bateria**: w oknie, w pasku GNOME, w zasobniku systemowym (KDE/XFCE/…) oraz powiadomienia o niskim poziomie
- **DPI** i **SmartShift** (z regulacją oporu na MX Master 3/3S)
- Podpowiedzi graficzne i opisy przy funkcjach, polski interfejs
- Silnik jako usługa systemd użytkownika — startuje sam, zmiany działają od razu

Obsługiwane myszy: MX Master 2S, 3, 3S oraz MX Anywhere (inne modele Logitecha z HID++ 2.0 powinny działać częściowo).

> **Status:** projekt był testowany automatycznie (testy na atrapach urządzeń, GUI na wirtualnym ekranie). Zgłoszenia z prawdziwych myszy i dystrybucji są bardzo mile widziane.

## Instalacja

### Fedora (RPM)
Pobierz `.rpm` z [Releases](../../releases) i:
```bash
sudo dnf install ./mxremap-*.noarch.rpm
```
Podczas instalacji pakiet ustawia uprawnienia udev tylko dla podłączonej myszy — podłącz ją przed instalacją (jeśli zmienisz mysz: `sudo sh -c 'mxremap-engine --udev-rules > /etc/udev/rules.d/70-mxremap.rules && udevadm control --reload && udevadm trigger'`). Wyloguj się i zaloguj (lub odłącz i podłącz mysz), potem uruchom **MX Remap** z menu aplikacji.

### Ze źródeł (Fedora i pochodne)
```bash
git clone https://github.com/rb314web/mxremap && cd mxremap
bash install.sh
```
Skrypt doinstaluje zależności, ustawi uprawnienia udev (dla podłączonej myszy) i włączy usługę.

### Inne dystrybucje
Potrzebne: Python 3, `python-evdev`, PyGObject, pycairo, GTK 4, libadwaita, `libnotify`, `wl-clipboard`. Uruchom `python3 mxremap_gui.py`; reguły udev wygenerujesz poleceniem `python3 mxremap.py --udev-rules`.

## Użycie

Okno programu: wybierz przycisk → wybierz akcję → **Zastosuj**. Z linii poleceń:
```bash
python3 mxremap.py --list-mice        # wykryte myszy
python3 mxremap.py --hidpp-info       # informacje HID++ (do zgłoszeń błędów)
python3 mxremap.py --install-service  # usługa systemd użytkownika
python3 mxremap.py --install-tray     # ikona baterii w zasobniku
```
Logi: `journalctl --user -u mxremap`.

## Dlaczego nie Flatpak?
Sandbox Flatpaka blokuje `/dev/hidraw`, `uinput` i reguły udev, więc przechwytywanie przycisków nie działa niezawodnie. Oficjalna forma dystrybucji to RPM.

## Rozwój
```bash
make test   # testy
make rpm    # budowa pakietu RPM
```
Patrz [CONTRIBUTING.md](CONTRIBUTING.md). Licencja: [MIT](LICENSE).

---

## English summary
MX Remap is a modern GTK4/libadwaita button remapper and settings tool for Logitech MX Master / MX Anywhere mice on Linux (GNOME and KDE): all buttons incl. the thumb button via HID++, gestures, macros, battery indicator and notifications, DPI and SmartShift. Install the RPM from Releases (`sudo dnf install ./mxremap-*.rpm`) or run `bash install.sh`. The UI is currently in Polish. Not affiliated with Logitech.
