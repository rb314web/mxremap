# Współtworzenie

- Testy: `make test` (potrzebne `python3-evdev`, `python3-gobject`, `python3-cairo`, `gtk4`, `libadwaita`).
- Nowy model myszy: uruchom `python3 mxremap.py --hidpp-info` przy podłączonej myszy i dołącz wynik do zgłoszenia.
- Błąd: podaj dystrybucję, środowisko (GNOME/KDE), model myszy i wynik `journalctl --user -u mxremap`.
- Wydanie: zmień wpis w `CHANGELOG.md`, utwórz tag `vX.Y.Z` — workflow zbuduje RPM i doda go do wydania.
