#!/usr/bin/env python3
"""MX Remap - nowoczesny konfigurator przyciskow Logitech MX Master 2S (GTK4 + libadwaita)."""
import json, os, signal, subprocess, sys, time, warnings
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk, Gdk, GLib, Gio
from evdev import ecodes as e

try:                                              # schemat myszy wymaga pycairo i PangoCairo
    import cairo  # noqa: F401
    gi.require_version("Pango", "1.0"); gi.require_version("PangoCairo", "1.0")
    from gi.repository import Pango, PangoCairo
    import math
    HAVE_DIAGRAM = True
except Exception:
    HAVE_DIAGRAM = False

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mxremap

ENGINE = os.path.join(HERE, "mxremap.py")
CFG_DIR = os.path.expanduser("~/.config/mxremap")
CFG = os.path.join(CFG_DIR, "config.json")
AUTOSTART = os.path.expanduser("~/.config/autostart/mxremap.desktop")
TRAY_AUTOSTART = os.path.expanduser("~/.config/autostart/mxremap-tray.desktop")
TRAY = os.path.join(HERE, "mxremap_tray.py")
LOG = os.path.join(CFG_DIR, "engine.log")
ENGINE_FILE = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}",
                           "mxremap-engine.json")
MOUSE_FILE = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}",
                          "mxremap-mouse.json")
SS_MODES = ["auto", "ratchet", "free"]
PGREP = r"mxremap\.py -c"


def write_autostart():
    os.makedirs(os.path.dirname(AUTOSTART), exist_ok=True)
    open(AUTOSTART, "w", encoding="utf-8").write(
        "[Desktop Entry]\nType=Application\nName=MX Remap (silnik)\n"
        "Comment=Przekierowanie przyciskow i odczyt baterii myszy MX Master\n"
        f'Exec="{sys.executable}" "{ENGINE}" -c "{CFG}" --wait --log "{LOG}"\n'
        "Terminal=false\nNoDisplay=true\nX-GNOME-Autostart-enabled=true\nX-GNOME-Autostart-Delay=3\n")

# (klucz, tytul, ikona)
SECTIONS = [
    ("Główne przyciski", None, [
        ("BTN_SIDE", "Wstecz", "go-previous-symbolic"),
        ("BTN_EXTRA", "Dalej", "go-next-symbolic"),
        ("BTN_MIDDLE", "Klik kółka", "input-mouse-symbolic"),
        ("BTN_LEFT", "Lewy przycisk", "input-mouse-symbolic"),
        ("BTN_RIGHT", "Prawy przycisk", "input-mouse-symbolic"),
    ]),
    ("Kółko boczne", "Obrót kółka kciukiem – jedna akcja na jedno „kliknięcie” kółka", [
        ("WHEEL_LEFT", "Obrót w lewo", "object-rotate-left-symbolic"),
        ("WHEEL_RIGHT", "Obrót w prawo", "object-rotate-right-symbolic"),
    ]),
    ("Ukryte przyciski (HID++)", "Przyciski, które mysz zgłasza tylko przez protokół Logitecha", []),   # wiersze zależą od modelu
]
HID_TITLES = {0xC3: "Przycisk kciuka (gesty)", 0xC4: "Przycisk pod kółkiem (tryb)"}


def hid_key(cid):
    return f"HIDPP_{cid:X}"
MOUSE_BTNS = ["BTN_LEFT", "BTN_RIGHT", "BTN_MIDDLE", "BTN_SIDE", "BTN_EXTRA", "BTN_FORWARD", "BTN_BACK", "BTN_TASK"]


def K(*keys):
    return {"type": "key", "keys": list(keys)}


def B(btn):
    return {"type": "button", "button": btn}


C, S, A, M = "KEY_LEFTCTRL", "KEY_LEFTSHIFT", "KEY_LEFTALT", "KEY_LEFTMETA"

# (id_kategorii, nazwa, [(id_akcji, nazwa, akcja), ...])
CATEGORIES = [
    ("default", "Domyślnie", []),
    ("volume", "Głośność", [
        ("vol_up", "Głośniej", K("KEY_VOLUMEUP")),
        ("vol_down", "Ciszej", K("KEY_VOLUMEDOWN")),
        ("vol_mute", "Wycisz / włącz dźwięk", K("KEY_MUTE")),
        ("mic_mute", "Wycisz mikrofon", K("KEY_MICMUTE")),
        ("audio_next", "Przełącz wyjście dźwięku (głośniki ↔ słuchawki)", {"type": "audio_next"}),
    ]),
    ("media", "Multimedia", [
        ("play_pause", "Odtwarzaj / pauza", K("KEY_PLAYPAUSE")),
        ("next_track", "Następny utwór", K("KEY_NEXTSONG")),
        ("prev_track", "Poprzedni utwór", K("KEY_PREVIOUSSONG")),
        ("stop_media", "Zatrzymaj", K("KEY_STOPCD")),
    ]),
    ("system", "System", [
        ("super", "Klawisz Windows (przegląd)", K(M)),
        ("apps", "Lista aplikacji", K(M, "KEY_A")),
        ("alt_tab", "Przełączanie okien (Alt+Tab)", K(A, "KEY_TAB")),
        ("screenshot", "Zrzut ekranu – wybór obszaru", K("KEY_SYSRQ")),
        ("screenshot_screen", "Zrzut całego ekranu", K(S, "KEY_SYSRQ")),
        ("screenshot_window", "Zrzut aktywnego okna", K(A, "KEY_SYSRQ")),
        ("ws_next", "Następny pulpit", K(M, "KEY_PAGEDOWN")),
        ("ws_prev", "Poprzedni pulpit", K(M, "KEY_PAGEUP")),
        ("maximize", "Maksymalizuj okno", K(M, "KEY_UP")),
        ("close_win", "Zamknij okno", K(A, "KEY_F4")),
        ("lock", "Zablokuj ekran", K(M, "KEY_L")),
    ]),
    ("open", "Otwórz aplikację lub stronę", [
        ("open_app", "Aplikacja…", None),
        ("open_url", "Strona internetowa, plik lub folder…", None),
    ]),
    ("text", "Tekst i makra", [
        ("insert_text", "Wstaw tekst…", None),
        ("macro", "Makro – kilka kroków po kolei…", None),
    ]),
    ("edit", "Edycja", [
        ("copy", "Kopiuj", K(C, "KEY_C")),
        ("paste", "Wklej", K(C, "KEY_V")),
        ("cut", "Wytnij", K(C, "KEY_X")),
        ("undo", "Cofnij", K(C, "KEY_Z")),
        ("redo", "Ponów", K(C, S, "KEY_Z")),
        ("select_all", "Zaznacz wszystko", K(C, "KEY_A")),
        ("find", "Szukaj", K(C, "KEY_F")),
    ]),
    ("browser", "Przeglądarka i zoom", [
        ("back", "Wstecz", K(A, "KEY_LEFT")),
        ("forward", "Dalej", K(A, "KEY_RIGHT")),
        ("tab_next", "Następna karta", K(C, "KEY_PAGEDOWN")),
        ("tab_prev", "Poprzednia karta", K(C, "KEY_PAGEUP")),
        ("tab_new", "Nowa karta", K(C, "KEY_T")),
        ("tab_close", "Zamknij kartę", K(C, "KEY_W")),
        ("tab_reopen", "Przywróć zamkniętą kartę", K(C, S, "KEY_T")),
        ("refresh", "Odśwież", K("KEY_F5")),
        ("zoom_in", "Powiększ", K(C, "KEY_EQUAL")),
        ("zoom_out", "Pomniejsz", K(C, "KEY_MINUS")),
        ("zoom_reset", "Zoom 100%", K(C, "KEY_0")),
    ]),
    ("mouse", "Przycisk myszy", [
        ("m_middle", "Klik środkowy", B("BTN_MIDDLE")),
        ("m_left", "Klik lewy", B("BTN_LEFT")),
        ("m_right", "Klik prawy", B("BTN_RIGHT")),
        ("m_back", "Wstecz (przycisk myszy)", B("BTN_SIDE")),
        ("m_forward", "Dalej (przycisk myszy)", B("BTN_EXTRA")),
    ]),
    ("none", "Wyłączony", []),
    ("custom", "Zaawansowane", [
        ("custom_key", "Własny skrót klawiszowy", None),
        ("custom_cmd", "Własna komenda", None),
    ]),
]
def detect_desktop(env=None):
    """'kde', 'gnome' albo 'other' na podstawie zmiennych srodowiska."""
    env = os.environ if env is None else env
    cur = (env.get("XDG_CURRENT_DESKTOP", "") + ":" + env.get("DESKTOP_SESSION", "")).upper()
    if "KDE" in cur or "PLASMA" in cur or env.get("KDE_FULL_SESSION"):
        return "kde"
    return "gnome" if "GNOME" in cur or "UNITY" in cur or "BUDGIE" in cur else "other"


# domyslne skroty KDE Plasma: id akcji -> (nowa etykieta albo None, nowa akcja albo None)
KDE_PRESETS = {
    "ws_next": (None, K(M, C, "KEY_RIGHT")),
    "ws_prev": (None, K(M, C, "KEY_LEFT")),
    "screenshot": ("Zrzut ekranu – Spectacle", None),
    "screenshot_screen": (None, K(S, "KEY_SYSRQ")),
    "screenshot_window": (None, K(M, "KEY_SYSRQ")),
    "apps": ("Przegląd okien i pulpitów (Overview)", K(M, "KEY_W")),
    "maximize": (None, K(M, "KEY_PAGEUP")),
}
KDE_HINTS = {
    "super": "Działa jak klawisz Windows: otwiera menu aplikacji Plasmy.",
    "apps": "Efekt Przegląd Plasmy (Meta+W): miniatury okien i pulpitów.",
    "screenshot": "Otwiera Spectacle – obszar zaznaczasz myszą.",
    "screenshot_screen": "Robi zrzut całego ekranu (domyślny skrót Spectacle: Shift+Print).",
    "screenshot_window": "Robi zrzut aktywnego okna (domyślny skrót Spectacle: Meta+Print).",
    "ws_next": "Przełącza na następny pulpit (domyślny skrót Plasmy: Meta+Ctrl+→).",
    "ws_prev": "Przełącza na poprzedni pulpit (domyślny skrót Plasmy: Meta+Ctrl+←).",
}


def localize(categories, overrides):
    """Kopia kategorii z podmienionymi etykietami/akcjami (np. pod skroty KDE)."""
    out = []
    for cid, cname, items in categories:
        new = []
        for pid, label, action in items:
            if pid in overrides:
                lab, act = overrides[pid]
                label, action = lab or label, act or action
            new.append((pid, label, action))
        out.append((cid, cname, new))
    return out


DESKTOP = detect_desktop()
if DESKTOP == "kde":
    CATEGORIES = localize(CATEGORIES, KDE_PRESETS)

WAIT_CAT = ("wait", "Pauza", [
    ("wait_200", "200 ms", {"type": "wait", "ms": 200}),
    ("wait_500", "0,5 sekundy", {"type": "wait", "ms": 500}),
    ("wait_1000", "1 sekunda", {"type": "wait", "ms": 1000}),
    ("wait_2000", "2 sekundy", {"type": "wait", "ms": 2000}),
])
PRESET_INDEX = {p[0]: (c[0], p[1], p[2]) for c in CATEGORIES + [WAIT_CAT] for p in c[2]}
# wiersze, ktore moga miec gesty (przytrzymaj + przesun)
CATEGORIES_G = [CATEGORIES[0], ("gesture", "Gesty (przytrzymaj i przesuń)", [])] + CATEGORIES[1:]
NO_GESTURE = {"BTN_LEFT", "BTN_RIGHT", "WHEEL_LEFT", "WHEEL_RIGHT"}
# akcje wewnatrz gestu: bez "Domyslnie"/"Wyłączony"/"Gesty", za to "Brak akcji"
# akcje wewnatrz gestu/makra: "Brak akcji" zamiast "Domyslnie", bez "Wyłączony" i bez zagniezdzonych makr
INNER = [("default", "Brak akcji", [])] + [
    (c[0], c[1], [p for p in c[2] if p[0] != "macro"]) for c in CATEGORIES if c[0] not in ("default", "none")]
INNER_STEP = INNER + [WAIT_CAT]
GESTURES = [("click", "Kliknięcie (bez ruchu)"), ("up", "Przesunięcie w górę"), ("down", "Przesunięcie w dół"),
            ("left", "Przesunięcie w lewo"), ("right", "Przesunięcie w prawo")]
GEST_ARROWS = {"up": "↑", "down": "↓", "left": "←", "right": "→"}


def gesture_prefix(gid):
    if gid in GEST_ARROWS:
        lab = Gtk.Label(label=GEST_ARROWS[gid], width_chars=2)
        lab.add_css_class("title-3")
        return lab
    return Gtk.Image.new_from_icon_name("input-mouse-symbolic")

# (krotki opis domyslnego dzialania, dluzszy opis w okienku ⓘ)
ROW_INFO = {
    "BTN_SIDE": ("cofa o stronę", "Przycisk z boku myszy, pod kciukiem. W przeglądarce i menedżerze plików cofa o jedną stronę."),
    "BTN_EXTRA": ("przechodzi dalej", "Drugi przycisk z boku myszy, pod kciukiem. W przeglądarce i menedżerze plików przechodzi o stronę do przodu."),
    "BTN_MIDDLE": ("środkowy klik", "Wciśnięcie kółka. Domyślnie otwiera link w nowej karcie, zamyka kartę i włącza przewijanie kółkiem w niektórych programach."),
    "BTN_LEFT": ("zwykły klik", "Główny przycisk myszy. Uwaga: zmiana lewego przycisku może utrudnić obsługę komputera. W razie czego wybierz „Domyślnie”."),
    "BTN_RIGHT": ("menu kontekstowe", "Otwiera menu podręczne. Uwaga: zmiana prawego przycisku może utrudnić obsługę komputera. W razie czego wybierz „Domyślnie”."),
    "WHEEL_LEFT": ("przewija w lewo", "Małe kółko pod kciukiem obrócone w lewo. Domyślnie przewija poziomo. Każde „kliknięcie” kółka uruchamia akcję jeden raz."),
    "WHEEL_RIGHT": ("przewija w prawo", "Małe kółko pod kciukiem obrócone w prawo. Domyślnie przewija poziomo. Każde „kliknięcie” kółka uruchamia akcję jeden raz."),
    "HIDPP_C3": ("brak funkcji w systemie", "Przycisk gestów na podparciu kciuka. System go nie widzi, więc program odbiera go przez protokół Logitecha (HID++). Możesz mu przypisać akcję albo gesty: przytrzymaj i przesuń mysz."),
    "HIDPP_D7": ("brak funkcji w systemie", "Przycisk zmiany czułości (DPI). Fabrycznie przełącza ustawienia DPI; po przypisaniu akcji przestaje to robić."),
    "HIDPP_C4": ("przełącza tryb kółka", "Przycisk tuż pod kółkiem przewijania. Fabrycznie przełącza kółko między trybem skokowym a swobodnym; po przypisaniu akcji przestaje to robić."),
}
HIDDEN_INFO = ("Przycisk dostępny tylko przez protokół Logitecha (HID++). Przypisz mu akcję albo gesty; "
               "działa wyłącznie, gdy działa silnik programu.")


def row_info(key):
    if key in ROW_INFO:
        return ROW_INFO[key]
    return ("brak funkcji w systemie", HIDDEN_INFO) if key.startswith("HIDPP_") else ("brak funkcji", EXTRA_INFO)


EXTRA_INFO = ("Dodatkowy przycisk wykryty na twojej myszy. Przypisz mu akcję albo usuń go z listy ikoną kosza.")
GROUP_INFO = {
    "Główne przyciski": "Zwykłe przyciski myszy. Każdy możesz przypisać do innej akcji, a „Domyślnie” przywraca fabryczne działanie.",
    "Kółko boczne": "Małe kółko pod kciukiem (przewijanie poziome). Nadaje się np. do głośności, przełączania kart albo pulpitów.",
    "Ukryte przyciski (HID++)": "Te przyciski mysz zgłasza tylko przez protokół Logitecha, więc działają wyłącznie, gdy działa silnik programu.",
}
# podpowiedzi pojawiajace sie pod wyborem akcji (klucz: id akcji albo id kategorii)
HINTS = {
    "gesture": "Przytrzymaj przycisk, przesuń mysz w górę, w dół, w lewo albo w prawo i puść – wykona się akcja dla tego kierunku. Bez ruchu to zwykłe kliknięcie. Podczas gestu kursor stoi w miejscu.",
    "super": "Działa jak klawisz Windows: otwiera przegląd czynności GNOME (okna i wyszukiwarka).",
    "apps": "Otwiera listę wszystkich zainstalowanych aplikacji.",
    "alt_tab": "Przełącza między otwartymi oknami. Dopóki trzymasz przycisk, wybierak okien pozostaje na ekranie.",
    "screenshot": "Otwiera narzędzie zrzutów ekranu GNOME – obszar zaznaczasz myszą.",
    "screenshot_screen": "Robi zrzut całego ekranu od razu i zapisuje go w folderze Obrazy.",
    "screenshot_window": "Robi zrzut aktywnego okna i zapisuje go w folderze Obrazy.",
    "ws_next": "Przełącza na następny pulpit (wirtualny obszar roboczy).",
    "ws_prev": "Przełącza na poprzedni pulpit (wirtualny obszar roboczy).",
    "audio_next": "Przełącza na kolejne urządzenie wyjściowe, np. głośniki → słuchawki, i przenosi grające aplikacje. Pomija odłączone wyjścia (np. HDMI bez kabla).",
    "insert_text": "Tekst jest kopiowany do schowka i wklejany skrótem Ctrl+V, więc zastąpi aktualną zawartość schowka. W terminalu użyj makra ze skrótem Ctrl+Shift+V.",
    "macro": "Makro wykonuje wybrane kroki po kolei, z krótką przerwą między nimi. Kroki dodajesz przyciskiem poniżej.",
    "custom_key": "Wpisz kombinację, np. ctrl+shift+t, albo kliknij ikonę nagrywania i naciśnij klawisze.",
    "custom_cmd": "Komenda uruchamia się jak w terminalu, np. wpctl set-mute @DEFAULT_AUDIO_SINK@ toggle.",
}
if DESKTOP == "kde":
    HINTS.update(KDE_HINTS)
SS_DESC = {
    "auto": "Kółko klika skokowo, ale przy szybkim obrocie samo przechodzi w tryb swobodny – wygodne do długich stron.",
    "ratchet": "Kółko zawsze klika skokowo – precyzyjne przewijanie, linijka po linijce.",
    "free": "Kółko kręci się swobodnie, bez oporu – szybkie przewijanie długich dokumentów.",
}


def info_button(text):
    """Okrągły przycisk ⓘ otwierający okienko z opisem (jak w ustawieniach GNOME)."""
    pop = Gtk.Popover()
    pop.set_child(Gtk.Label(label=text, wrap=True, max_width_chars=42, xalign=0,
                            margin_top=12, margin_bottom=12, margin_start=14, margin_end=14))
    btn = Gtk.MenuButton(icon_name="dialog-information-symbolic", valign=Gtk.Align.CENTER,
                         popover=pop, tooltip_text="Co to robi?")
    btn.add_css_class("flat"); btn.add_css_class("circular")
    return btn


def preset_action(pid):
    return dict(PRESET_INDEX[pid][2], preset=pid)


EXTRA_NAMES = {"BTN_FORWARD": "Przycisk „Dalej” (forward)", "BTN_BACK": "Przycisk „Wstecz” (back)",
               "BTN_TASK": "Przycisk dodatkowy (task)"}


def extra_title(name):
    return EXTRA_NAMES.get(name, f"Przycisk dodatkowy ({name})")


CSS = """
.status-icon { padding: 10px; border-radius: 14px; background: alpha(@window_fg_color, 0.07); }
.status-icon.running { color: @success_color; background: alpha(@success_color, 0.16); }
.mapped-icon { color: @accent_color; }
row.expander row.expander:not(:checked) image.expander-row-arrow:dir(ltr) { -gtk-icon-transform: rotate(0.5turn); opacity: 0.55; color: inherit; }
row.expander row.expander:not(:checked) image.expander-row-arrow:dir(rtl) { -gtk-icon-transform: rotate(-0.5turn); opacity: 0.55; color: inherit; }
"""


# ---------- bateria + wskaznik na pasku GNOME ----------
EXT_UUID = "mxbattery@mxremap"
EXT_DIR = os.path.expanduser(f"~/.local/share/gnome-shell/extensions/{EXT_UUID}")
EXT_META = """{
  "uuid": "mxbattery@mxremap",
  "name": "Bateria myszy MX",
  "description": "Pokazuje procent naładowania baterii myszy Logitech MX Master na górnym pasku",
  "shell-version": ["45", "46", "47", "48", "49", "50", "51", "52"],
  "version": 1
}
"""
EXT_JS = r'''import GObject from 'gi://GObject';
import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import St from 'gi://St';
import Clutter from 'gi://Clutter';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

const BASE = '/sys/class/power_supply';
const STATUS = {
    'Charging': 'Ładowanie',
    'Discharging': 'Rozładowywanie',
    'Full': 'Naładowana',
    'Not charging': 'Nie ładuje',
};
const LEVEL = {
    'Full': 'Pełna',
    'High': 'Wysoka',
    'Normal': 'Normalna',
    'Low': 'Niska',
    'Critical': 'Krytyczna',
};

function readFile(path) {
    try {
        const [ok, bytes] = GLib.file_get_contents(path);
        return ok ? new TextDecoder().decode(bytes).trim() : null;
    } catch (e) {
        return null;
    }
}

const STATE = GLib.build_filenamev([GLib.get_user_runtime_dir(), 'mxremap-battery.json']);

function readState() {
    const txt = readFile(STATE);
    if (!txt)
        return null;
    try {
        const d = JSON.parse(txt);
        if (Date.now() / 1000 - (d.time ?? 0) > 180)
            return null;
        if ((d.percent ?? null) === null && !d.level)
            return null;
        return {percent: d.percent ?? null, status: d.status || '', level: d.level || '', isMx: true};
    } catch (e) {
        return null;
    }
}

function readBattery() {
    return readState() ?? readSysfs();
}

function readSysfs() {
    let best = null;
    let en = null;
    try {
        en = Gio.File.new_for_path(BASE).enumerate_children(
            'standard::name', Gio.FileQueryInfoFlags.NONE, null);
    } catch (e) {
        return null;
    }
    let info;
    while ((info = en.next_file(null)) !== null) {
        const name = info.get_name();
        const dir = `${BASE}/${name}`;
        const model = (readFile(`${dir}/model_name`) || '').toLowerCase();
        const isMx = model.includes('mx ');
        if (!isMx && !name.startsWith('hidpp_battery'))
            continue;
        const cap = readFile(`${dir}/capacity`);
        const entry = {
            percent: cap !== null && /^\d+$/.test(cap) ? parseInt(cap) : null,
            status: readFile(`${dir}/status`) || '',
            level: readFile(`${dir}/capacity_level`) || '',
            isMx,
        };
        if (best === null || (isMx && !best.isMx))
            best = entry;
    }
    en.close(null);
    return best;
}

function iconFor(b) {
    if (b.percent === null)
        return 'battery-good-symbolic';
    const n = Math.max(0, Math.min(100, 10 * Math.round(b.percent / 10)));
    if (b.status === 'Charging')
        return `battery-level-${n}-charging-symbolic`;
    if (n === 100 && b.status === 'Full')
        return 'battery-level-100-charged-symbolic';
    return `battery-level-${n}-symbolic`;
}

const Indicator = GObject.registerClass(
class MxBatteryIndicator extends PanelMenu.Button {
    _init() {
        super._init(0.0, 'Bateria myszy MX');
        const box = new St.BoxLayout({style_class: 'panel-status-menu-box'});
        this._icon = new St.Icon({
            icon_name: 'battery-missing-symbolic',
            style_class: 'system-status-icon',
        });
        this._label = new St.Label({text: '-', y_align: Clutter.ActorAlign.CENTER});
        box.add_child(this._icon);
        box.add_child(this._label);
        this.add_child(box);

        this.menu.addMenuItem(new PopupMenu.PopupMenuItem(
            'Mysz Logitech MX Master', {reactive: false, can_focus: false}));
        this._detail = new PopupMenu.PopupMenuItem('', {reactive: false, can_focus: false});
        this.menu.addMenuItem(this._detail);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        const open = new PopupMenu.PopupMenuItem('Otwórz MX Remap');
        open.connect('activate', () => {
            try {
                GLib.spawn_command_line_async('gtk-launch pl.mxremap.App');
            } catch (e) {
                console.error(e);
            }
        });
        this.menu.addMenuItem(open);
        this.update();
    }

    update() {
        const b = readBattery();
        if (!b) {
            this.hide();
            return;
        }
        this.show();
        this._icon.icon_name = iconFor(b);
        let text = '-';
        if (b.percent !== null)
            text = `${b.percent}%`;
        else if (b.level)
            text = LEVEL[b.level] ?? b.level;
        this._label.set_text(text);
        const st = STATUS[b.status] ?? b.status;
        this._detail.label.set_text(`Bateria: ${text}${st ? ' · ' + st : ''}`);
    }
});

export default class MxBatteryExtension extends Extension {
    enable() {
        this._indicator = new Indicator();
        Main.panel.addToStatusArea(this.uuid, this._indicator, 0, 'right');
        this._timer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 30, () => {
            this._indicator.update();
            return GLib.SOURCE_CONTINUE;
        });
    }

    disable() {
        if (this._timer) {
            GLib.Source.remove(this._timer);
            this._timer = null;
        }
        this._indicator?.destroy();
        this._indicator = null;
    }
}
'''


def _rd(path):
    try:
        return open(path).read().strip()
    except OSError:
        return None


BATT_FILE = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}",
                         "mxremap-battery.json")


def read_battery():
    """Bateria myszy: dokladny procent z silnika (HID++), a gdy go brak - dane z jadra (/sys)."""
    try:
        d = json.load(open(BATT_FILE))
        if time.time() - d.get("time", 0) < 180 and (d.get("percent") is not None or d.get("level")):
            return {"percent": d.get("percent"), "status": d.get("status", ""),
                    "level": d.get("level", ""), "mx": True, "exact": True}
    except (OSError, ValueError):
        pass
    base, best = "/sys/class/power_supply", None
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return None
    for name in names:
        d = os.path.join(base, name)
        model = (_rd(d + "/model_name") or "").lower()
        is_mx = "mx " in model
        if not is_mx and not name.startswith("hidpp_battery"):
            continue
        cap = _rd(d + "/capacity")
        entry = {"percent": int(cap) if cap and cap.isdigit() else None,
                 "status": _rd(d + "/status") or "", "level": _rd(d + "/capacity_level") or "", "mx": is_mx}
        if best is None or (is_mx and not best["mx"]):
            best = entry
    return best


def gnome_available():
    src = Gio.SettingsSchemaSource.get_default()
    return src is not None and src.lookup("org.gnome.shell", True) is not None


def write_extension():
    os.makedirs(EXT_DIR, exist_ok=True)
    open(os.path.join(EXT_DIR, "metadata.json"), "w", encoding="utf-8").write(EXT_META)
    open(os.path.join(EXT_DIR, "extension.js"), "w", encoding="utf-8").write(EXT_JS)


def panel_enabled():
    if not gnome_available():                      # bez schematu GNOME Gio.Settings konczy caly program
        return False
    try:
        enabled = EXT_UUID in Gio.Settings.new("org.gnome.shell").get_strv("enabled-extensions")
    except Exception:
        return False
    return enabled and os.path.exists(os.path.join(EXT_DIR, "extension.js"))


# ---------- zamiana "ctrl+shift+t" <-> ["KEY_LEFTCTRL", ...] ----------
MOD = {"ctrl": "KEY_LEFTCTRL", "control": "KEY_LEFTCTRL", "shift": "KEY_LEFTSHIFT",
       "alt": "KEY_LEFTALT", "super": "KEY_LEFTMETA", "meta": "KEY_LEFTMETA", "win": "KEY_LEFTMETA"}
NAMED = {"enter": "KEY_ENTER", "return": "KEY_ENTER", "esc": "KEY_ESC", "escape": "KEY_ESC",
         "space": "KEY_SPACE", "tab": "KEY_TAB", "backspace": "KEY_BACKSPACE", "delete": "KEY_DELETE",
         "del": "KEY_DELETE", "pageup": "KEY_PAGEUP", "pagedown": "KEY_PAGEDOWN", "print": "KEY_SYSRQ",
         "volumeup": "KEY_VOLUMEUP", "volumedown": "KEY_VOLUMEDOWN", "mute": "KEY_MUTE",
         "playpause": "KEY_PLAYPAUSE", "nextsong": "KEY_NEXTSONG", "prevsong": "KEY_PREVIOUSSONG",
         "dot": "KEY_DOT", "period": "KEY_DOT"}
REV = {"KEY_LEFTCTRL": "ctrl", "KEY_RIGHTCTRL": "ctrl", "KEY_LEFTSHIFT": "shift", "KEY_RIGHTSHIFT": "shift",
       "KEY_LEFTALT": "alt", "KEY_RIGHTALT": "alt", "KEY_LEFTMETA": "super", "KEY_RIGHTMETA": "super",
       "KEY_SYSRQ": "print"}
MOD_CODES = {29, 42, 54, 56, 97, 100, 125, 126}


def parse_keys(text):
    keys = []
    for part in text.lower().replace(" ", "").split("+"):
        if not part:
            continue
        k = MOD.get(part) or NAMED.get(part) or "KEY_" + part.upper()
        if not hasattr(e, k):
            raise ValueError(f"Nieznany klawisz: {part}")
        keys.append(k)
    if not keys:
        raise ValueError("Podaj skrót, np. ctrl+shift+t")
    return keys


def keys_to_text(keys):
    return "+".join(REV.get(k, k[4:].lower()) for k in keys)


def first(name):
    return name[0] if isinstance(name, list) else name


def esc(s):
    return GLib.markup_escape_text(s)


# ---------- schemat myszy (klikalny) ----------
if HAVE_DIAGRAM:
    class MouseDiagram(Gtk.DrawingArea):
        """Poglądowy rysunek MX Master 2S: numery prowadzą do konfiguracji danego przycisku."""
        W, H = 640, 300
        OX = 60                                           # przesuniecie korpusu (miejsce na opisy z lewej)
        # (klucze wierszy, ksztalt, kotwica na korpusie, znacznik, strona opisu, etykieta)
        SPOTS = [
            (("BTN_LEFT",), ("region", "L"), (232, 80), (180, 45), "L", "Lewy przycisk"),
            (("BTN_RIGHT",), ("region", "R"), (288, 80), (460, 45), "R", "Prawy przycisk"),
            (("BTN_MIDDLE",), ("rrect", 253, 50, 14, 36, 7), (260, 62), (460, 95), "R", "Klik kółka"),
            (("HIDPP_C4",), ("rrect", 248, 94, 24, 10, 5), (270, 99), (460, 145), "R", "Przycisk trybu kółka"),
            (("WHEEL_LEFT", "WHEEL_RIGHT"), ("rrect", 180, 100, 12, 38, 6), (186, 119), (180, 105), "L", "Kółko boczne"),
            (("BTN_EXTRA",), ("rrect", 181, 148, 14, 12, 5), (188, 154), (180, 155), "L", "Dalej"),
            (("BTN_SIDE",), ("rrect", 183, 164, 14, 12, 5), (190, 170), (180, 200), "L", "Wstecz"),
            (("HIDPP_C3",), ("circle", 206, 215, 10), (206, 215), (180, 245), "L", "Przycisk gestów"),
        ]

        def __init__(self, on_pick, is_set, available=None):
            super().__init__()
            self.on_pick, self.is_set = on_pick, is_set
            self.available = available or (lambda keys: True)     # czy mysz ma taki przycisk
            self.compact, self._spots = False, []
            self.hover, self._hits = None, []
            self._tf = (1, 0, 0)
            self.set_content_width(560); self.set_content_height(300)
            self.set_hexpand(True)
            self.set_draw_func(self.draw)
            click = Gtk.GestureClick(); click.connect("released", self.on_click); self.add_controller(click)
            motion = Gtk.EventControllerMotion()
            motion.connect("motion", self.on_motion); motion.connect("leave", self.on_leave)
            self.add_controller(motion)

        def set_compact(self, compact):
            if compact != self.compact:
                self.compact = compact
                self.queue_draw()

        # --- kolory zgodne z motywem (jasny/ciemny) i kolorem akcentu systemu ---
        def colors(self):
            fg = self.get_color()
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    ok, acc = self.get_style_context().lookup_color("accent_bg_color")
                accent = (acc.red, acc.green, acc.blue) if ok else (0.21, 0.52, 0.89)
            except Exception:
                accent = (0.21, 0.52, 0.89)
            return (fg.red, fg.green, fg.blue), accent

        @staticmethod
        def body_path(cr):
            cr.new_path()
            cr.move_to(205, 60)
            cr.curve_to(205, 30, 315, 30, 315, 60)
            cr.curve_to(330, 130, 345, 200, 320, 250)
            cr.curve_to(300, 285, 220, 285, 200, 250)
            cr.curve_to(175, 200, 190, 130, 205, 60)
            cr.close_path()

        @staticmethod
        def rrect(cr, x, y, w, h, r):
            cr.new_sub_path()
            cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
            cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
            cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
            cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
            cr.close_path()

        def shape_path(self, cr, shape):
            if shape[0] == "rrect":
                self.rrect(cr, *shape[1:])
            elif shape[0] == "circle":
                cr.new_sub_path(); cr.arc(shape[1], shape[2], shape[3], 0, 2 * math.pi)

        def text(self, cr, txt, x, y, align, color, size=10.5, bold=False):
            layout = self.create_pango_layout(txt)
            base = self.get_pango_context().get_font_description()
            desc = base.copy() if base else Pango.FontDescription()
            desc.set_size(int(size * Pango.SCALE))
            desc.set_weight(Pango.Weight.BOLD if bold else Pango.Weight.NORMAL)
            layout.set_font_description(desc)
            pw, ph = layout.get_pixel_size()
            px = x - pw if align == "right" else x - pw / 2 if align == "center" else x
            cr.move_to(px, y - ph / 2)
            cr.set_source_rgba(*color)
            PangoCairo.show_layout(cr, layout)
            return px, pw

        def draw(self, _area, cr, w, h):
            fg, accent = self.colors()
            s = min(w / self.W, h / self.H)
            ox, oy = (w - self.W * s) / 2, (h - self.H * s) / 2
            self._tf, self._hits = (s, ox, oy), []
            cr.translate(ox, oy); cr.scale(s, s)
            spots = self._spots = [sp for sp in self.SPOTS if self.available(sp[0])]
            active = {i: self.is_set(sp[0]) for i, sp in enumerate(spots)}
            k, cx, cy = (0.82 if self.compact else 1.0), 260, 150    # mysz kompaktowa (np. MX Anywhere) jest mniejsza

            def T(x, y):
                return self.OX + cx + (x - cx) * k, cy + (y - cy) * k

            # korpus
            cr.save(); cr.translate(self.OX, 0)
            cr.translate(cx, cy); cr.scale(k, k); cr.translate(-cx, -cy)
            self.body_path(cr)
            cr.set_source_rgba(*fg, 0.05); cr.fill_preserve()
            cr.set_source_rgba(*fg, 0.45); cr.set_line_width(2); cr.stroke()
            # lewy i prawy przycisk (wypelnienie przyciete do korpusu)
            cr.save(); self.body_path(cr); cr.clip()
            for i, sp in enumerate(spots):
                if sp[1][0] == "region" and (active[i] or self.hover == i + 1):
                    x0 = 150 if sp[1][1] == "L" else 260
                    cr.rectangle(x0, 0, 110, 118)
                    cr.set_source_rgba(*accent, 0.55 if self.hover == i + 1 else 0.38); cr.fill()
            cr.restore()
            cr.set_source_rgba(*fg, 0.4); cr.set_line_width(1.5)
            cr.move_to(260, 38); cr.line_to(260, 48); cr.stroke()
            cr.move_to(194, 118); cr.curve_to(230, 128, 290, 128, 326, 118); cr.stroke()
            # male elementy (kolko, przyciski boczne, kolko boczne, przycisk gestow)
            for i, sp in enumerate(spots):
                if sp[1][0] in ("rrect", "circle"):
                    self.shape_path(cr, sp[1])
                    if self.hover == i + 1: cr.set_source_rgba(*accent, 0.7)
                    elif active[i]: cr.set_source_rgba(*accent, 0.55)
                    else: cr.set_source_rgba(*fg, 0.12)
                    cr.fill_preserve()
                    cr.set_source_rgba(*fg, 0.75); cr.set_line_width(1.5); cr.stroke()
            # rowki na kolkach
            cr.set_source_rgba(*fg, 0.45); cr.set_line_width(1)
            for yy in range(56, 84, 6):
                cr.move_to(255, yy); cr.line_to(265, yy); cr.stroke()
            if any(sp[0][0] == "WHEEL_LEFT" for sp in spots):         # rowki kolka bocznego tylko gdy mysz je ma
                for yy in range(106, 134, 6):
                    cr.move_to(182, yy); cr.line_to(190, yy); cr.stroke()
            cr.restore()

            # linie, znaczniki i etykiety
            for i, (_keys, shape, anchor, marker, side, label) in enumerate(spots):
                n, hov = i + 1, self.hover == i + 1
                ax, ay = T(*anchor)
                mx, my = marker
                cr.set_source_rgba(*(accent if hov else fg), 0.9 if hov else 0.35); cr.set_line_width(1.2)
                cr.move_to(mx + (10 if side == "L" else -10), my); cr.line_to(ax, ay); cr.stroke()
                cr.arc(ax, ay, 2.6, 0, 2 * math.pi); cr.fill()
                cr.new_sub_path(); cr.arc(mx, my, 10, 0, 2 * math.pi)
                if active[i] or hov:
                    cr.set_source_rgba(*accent, 1.0 if active[i] else 0.6); cr.fill()
                    num_color = (1, 1, 1, 1)
                else:
                    cr.set_source_rgba(*fg, 0.6); cr.set_line_width(1.5); cr.stroke()
                    num_color = (*fg, 0.9)
                self.text(cr, str(n), mx, my, "center", num_color, 9.5, True)
                px, pw = self.text(cr, label, mx - 18 if side == "L" else mx + 18, my,
                                   "right" if side == "L" else "left", (*fg, 1.0 if (hov or active[i]) else 0.85),
                                   10.5, active[i])
                x0, x1 = (px, mx + 10) if side == "L" else (mx - 10, px + pw)
                self._hits.append((n, (x0 * s + ox, (my - 13) * s + oy, x1 * s + ox, (my + 13) * s + oy)))
                if shape[0] == "region":
                    xa, xb = (150, 260) if shape[1] == "L" else (260, 370)
                    (hx0, _), (hx1, hy1) = T(xa, 0), T(xb, 118)
                    self._hits.append((n, (hx0 * s + ox, 0, hx1 * s + ox, hy1 * s + oy)))
                else:
                    bx, by, bw, bh = ((shape[1], shape[2], shape[3], shape[4]) if shape[0] == "rrect"
                                      else (shape[1] - shape[3], shape[2] - shape[3], 2 * shape[3], 2 * shape[3]))
                    (hx0, hy0), (hx1, hy1) = T(bx, by), T(bx + bw, by + bh)
                    self._hits.append((n, ((hx0 - 3) * s + ox, (hy0 - 3) * s + oy,
                                           (hx1 + 3) * s + ox, (hy1 + 3) * s + oy)))
            self.text(cr, "Schemat poglądowy  ·  kliknij numer, nazwę albo część myszy  ·  wypełnione = ustawione",
                      self.W / 2, 288, "center", (*fg, 0.55), 9)

        def hit(self, x, y):
            for n, (x0, y0, x1, y1) in self._hits:
                if x0 <= x <= x1 and y0 <= y <= y1:
                    return n
            return None

        def on_motion(self, _c, x, y):
            n = self.hit(x, y)
            if n != self.hover:
                self.hover = n
                self.set_cursor_from_name("pointer" if n else None)
                self.set_tooltip_text(f"{self._spots[n - 1][5]} – kliknij, aby skonfigurować" if n else None)
                self.queue_draw()

        def on_leave(self, *_):
            if self.hover is not None:
                self.hover = None
                self.set_cursor_from_name(None)
                self.queue_draw()

        def on_click(self, _g, _n, x, y):
            n = self.hit(x, y)
            if n:
                self.on_pick(self._spots[n - 1][0][0])


# ---------- wybor akcji (kategoria -> akcja / pola dodatkowe) ----------
class Picker:
    def __init__(self, win, add_row, on_change, cats, remove_row=None, step_cats=None):
        self.win, self.on_change, self.cats = win, on_change, cats
        self.add_row, self.remove_row, self.step_cats = add_row, remove_row, step_cats or INNER_STEP
        self.app = None                                  # {"desktop", "id", "name"} dla "Otworz aplikacje"
        self.steps = []                                  # [(ExpanderRow, Picker)] dla makra

        self.cat_row = Adw.ComboRow(title="Akcja", model=Gtk.StringList.new([c[1] for c in cats]))
        self.act_row = Adw.ComboRow(title="Co ma robić", model=Gtk.StringList.new(None))
        self.hint_row = Adw.ActionRow(title="Jak to działa")
        self.hint_row.add_prefix(Gtk.Image.new_from_icon_name("dialog-information-symbolic"))
        self.key_row = Adw.EntryRow(title="Skrót, np. ctrl+shift+t")
        rec = Gtk.Button(icon_name="media-record-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Nagraj skrót")
        rec.add_css_class("flat"); rec.connect("clicked", self.record)
        self.key_row.add_suffix(rec)
        self.cmd_row = Adw.EntryRow(title="Komenda powłoki")
        self.app_row = Adw.ActionRow(title="Aplikacja", subtitle="Nie wybrano")
        pick = Gtk.Button(label="Wybierz…", valign=Gtk.Align.CENTER)
        pick.connect("clicked", self.choose_app)
        self.app_row.add_suffix(pick)
        self.url_row = Adw.EntryRow(title="Adres strony, plik lub folder (np. wp.pl, ~/Pobrane)")
        self.text_row = Adw.EntryRow(title="Tekst do wstawienia (wklejany przez schowek)")
        self.delay_row = Adw.SpinRow.new_with_range(50, 2000, 50)
        self.delay_row.set_title("Opóźnienie między krokami"); self.delay_row.set_subtitle("milisekundy")
        self.delay_row.set_value(150)
        self.add_step_row = Adw.ActionRow(title="Kroki makra", subtitle="Wykonywane po kolei, od góry do dołu")
        ab = Gtk.Button(child=Adw.ButtonContent(icon_name="list-add-symbolic", label="Dodaj krok"),
                        valign=Gtk.Align.CENTER)
        ab.connect("clicked", lambda *_: self.add_step(expand=True))
        self.add_step_row.add_suffix(ab)
        for r in (self.cat_row, self.act_row, self.hint_row, self.key_row, self.cmd_row, self.app_row,
                  self.url_row, self.text_row, self.delay_row, self.add_step_row):
            add_row(r)

        self.cat_row.connect("notify::selected", self.on_cat)
        self.act_row.connect("notify::selected", self.refresh)
        for r in (self.key_row, self.cmd_row, self.url_row, self.text_row):
            r.connect("changed", self.refresh)
        self.delay_row.connect("notify::value", self.refresh)
        self.update_visibility()

    # --- podstawy ---
    def cat(self):
        return self.cats[self.cat_row.get_selected()]

    def preset(self):
        items, sel = self.cat()[2], self.act_row.get_selected()
        return items[sel] if items and sel < len(items) else None

    def pid(self):
        p = self.preset()
        return p[0] if p else None

    def on_cat(self, *_):
        self.act_row.set_model(Gtk.StringList.new([p[1] for p in self.cat()[2]] or None))
        self.act_row.set_selected(0)
        self.refresh()

    def update_visibility(self):
        pid = self.pid()
        self.act_row.set_visible(bool(self.cat()[2]))
        hint = HINTS.get(pid) or HINTS.get(self.cat()[0])
        self.hint_row.set_visible(bool(hint))
        if hint:
            self.hint_row.set_subtitle(esc(hint))
        self.key_row.set_visible(pid == "custom_key")
        self.cmd_row.set_visible(pid == "custom_cmd")
        self.app_row.set_visible(pid == "open_app")
        self.url_row.set_visible(pid == "open_url")
        self.text_row.set_visible(pid == "insert_text")
        for r in (self.delay_row, self.add_step_row):
            r.set_visible(pid == "macro")
        for ex, _p in self.steps:
            ex.set_visible(pid == "macro")

    def refresh(self, *_):
        self.update_visibility()
        self.on_change()

    def summary(self):
        cid, cname, _ = self.cat()
        pid, p = self.pid(), self.preset()
        if cid in ("default", "none"): return cname
        if p is None: return ""
        if pid == "custom_key":
            txt = " + ".join(x.capitalize() for x in self.key_row.get_text().split("+") if x.strip())
            return esc(txt) or "Własny skrót"
        if pid == "custom_cmd": return esc(self.cmd_row.get_text()) or "Własna komenda"
        if pid == "open_app": return "Otwórz: " + esc(self.app["name"]) if self.app else "Aplikacja – nie wybrano"
        if pid == "open_url": return "Otwórz: " + esc(self.url_row.get_text()) if self.url_row.get_text() else "Strona, plik lub folder"
        if pid == "insert_text":
            t = self.text_row.get_text()
            return "Tekst: " + esc(t[:40] + ("…" if len(t) > 40 else "")) if t else "Wstaw tekst"
        if pid == "macro": return f"Makro · kroków: {len(self.steps)}"
        return f"{esc(cname)} · {esc(p[1])}"

    # --- nagrywanie skrotu ---
    def record(self, *_):
        dlg = Adw.AlertDialog(heading="Nagrywanie skrótu", body="Naciśnij kombinację klawiszy…")
        dlg.add_response("cancel", "Anuluj")
        ctrl = Gtk.EventControllerKey()
        ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)

        def on_key(_c, keyval, keycode, state):
            c = keycode - 8                       # kod XKB -> kod evdev
            if c in MOD_CODES:
                return True
            name = first(e.KEY.get(c, ""))
            if not name:
                return True
            mods = []
            if state & Gdk.ModifierType.CONTROL_MASK: mods.append("KEY_LEFTCTRL")
            if state & Gdk.ModifierType.SHIFT_MASK: mods.append("KEY_LEFTSHIFT")
            if state & Gdk.ModifierType.ALT_MASK: mods.append("KEY_LEFTALT")
            if state & Gdk.ModifierType.SUPER_MASK: mods.append("KEY_LEFTMETA")
            self.key_row.set_text(keys_to_text(mods + [name]))
            dlg.force_close()
            return True

        ctrl.connect("key-pressed", on_key)
        dlg.add_controller(ctrl)
        dlg.present(self.win)

    # --- wybor aplikacji ---
    def set_app(self, app):
        self.app = app
        self.app_row.set_subtitle(esc(app["name"]) if app else "Nie wybrano")
        self.refresh()

    def choose_app(self, *_):
        dlg = Adw.Dialog(title="Wybierz aplikację", content_width=420, content_height=560)
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=10, margin_end=10, margin_bottom=10)
        search = Gtk.SearchEntry(placeholder_text="Szukaj aplikacji…")
        box.append(search)
        lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        lb.add_css_class("boxed-list")
        sw = Gtk.ScrolledWindow(vexpand=True, child=lb)
        box.append(sw)

        apps = sorted((a for a in Gio.AppInfo.get_all() if a.should_show()),
                      key=lambda a: a.get_display_name().lower())
        for a in apps:
            row = Adw.ActionRow(title=esc(a.get_display_name()), activatable=True)
            icon = a.get_icon()
            if icon:
                row.add_prefix(Gtk.Image.new_from_gicon(icon))
            row.name_lc = a.get_display_name().lower()
            row.connect("activated", lambda _r, a=a: (self.set_app({
                "desktop": getattr(a, "get_filename", lambda: None)() or "",
                "id": a.get_id() or "", "name": a.get_display_name()}), dlg.close()))
            lb.append(row)
        lb.set_filter_func(lambda row: search.get_text().lower() in getattr(row, "name_lc", ""))
        search.connect("search-changed", lambda *_: lb.invalidate_filter())
        tv.set_content(box)
        dlg.set_child(tv)
        dlg.present(self.win)

    # --- makro ---
    def add_step(self, action=None, expand=False):
        ex = Adw.ExpanderRow(title="")
        rm = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Usuń krok")
        rm.add_css_class("flat")
        ex.add_suffix(rm)
        holder = []
        p = Picker(self.win, ex.add_row, lambda: self.step_changed(ex, holder[0]), self.step_cats)
        holder.append(p)
        self.steps.append((ex, p))
        rm.connect("clicked", lambda *_: self.remove_step(ex))
        self.add_row(ex)
        if action:
            p.load(action)
        ex.set_subtitle(p.summary())
        ex.set_expanded(expand)
        self.renumber()
        self.refresh()

    def step_changed(self, ex, p):
        ex.set_subtitle(p.summary())
        self.on_change()

    def remove_step(self, ex):
        for i, (x, _p) in enumerate(self.steps):
            if x is ex:
                del self.steps[i]
                break
        self.remove_row(ex)
        self.renumber()
        self.refresh()

    def clear_steps(self):
        for ex, _p in list(self.steps):
            self.remove_row(ex)
        self.steps = []

    def renumber(self):
        for i, (ex, _p) in enumerate(self.steps):
            ex.set_title(f"Krok {i + 1}")

    # --- zapis / odczyt ---
    def locate(self, action):
        ids = [c[0] for c in self.cats]
        cid, pid = "default", None
        if action:
            t = action.get("type")
            if t == "none": cid = "none"
            elif action.get("preset") in PRESET_INDEX:
                cid, pid = PRESET_INDEX[action["preset"]][0], action["preset"]
            else:
                direct = {"app": ("open", "open_app"), "url": ("open", "open_url"),
                          "text": ("text", "insert_text"), "macro": ("text", "macro"),
                          "key": ("custom", "custom_key"), "command": ("custom", "custom_cmd")}
                for c, _n, items in CATEGORIES + [WAIT_CAT]:     # najpierw dopasuj gotowe akcje
                    for p_id, _l, a in items:
                        if a and a["type"] == t and a.get("keys") == action.get("keys") \
                                and a.get("button") == action.get("button") and a.get("ms") == action.get("ms"):
                            cid, pid = c, p_id
                if pid is None and t in direct:
                    cid, pid = direct[t]
        # sprawdz, czy ta lista kategorii to w ogole zawiera
        for c in self.cats:
            if c[0] == cid and (pid is None or pid in [p[0] for p in c[2]]):
                return cid, pid
        return "default", None

    def load(self, action):
        cid, pid = self.locate(action)
        self.clear_steps()
        self.cat_row.set_selected([c[0] for c in self.cats].index(cid))
        self.on_cat()                                   # odswiez liste akcji (takze gdy kategoria sie nie zmienila)
        if pid:
            self.act_row.set_selected([p[0] for p in self.cat()[2]].index(pid))
        if pid == "custom_key": self.key_row.set_text(keys_to_text(action.get("keys", [])))
        elif pid == "custom_cmd": self.cmd_row.set_text(action.get("command", ""))
        elif pid == "open_url": self.url_row.set_text(action.get("target", ""))
        elif pid == "insert_text": self.text_row.set_text(action.get("text", ""))
        elif pid == "open_app":
            self.set_app({"desktop": action.get("desktop", ""), "id": action.get("id", ""),
                          "name": action.get("name", "Aplikacja")})
        elif pid == "macro":
            self.delay_row.set_value(action.get("delay", 150))
            for step in action.get("steps", []):
                self.add_step(step)
        self.refresh()

    def get(self):
        cid, pid, p = self.cat()[0], self.pid(), self.preset()
        if cid == "default": return None
        if cid == "none": return {"type": "none"}
        if pid == "custom_key":
            return {"type": "key", "keys": parse_keys(self.key_row.get_text())}
        if pid == "custom_cmd":
            c = self.cmd_row.get_text().strip()
            if not c: raise ValueError("Podaj komendę do uruchomienia")
            return {"type": "command", "command": c}
        if pid == "open_app":
            if not self.app: raise ValueError("Wybierz aplikację do uruchomienia")
            return dict(self.app, type="app", preset=pid)
        if pid == "open_url":
            t = self.url_row.get_text().strip()
            if not t: raise ValueError("Podaj adres strony, plik lub folder")
            return {"type": "url", "target": t, "preset": pid}
        if pid == "insert_text":
            t = self.text_row.get_text()
            if not t: raise ValueError("Podaj tekst do wstawienia")
            return {"type": "text", "text": t, "preset": pid}
        if pid == "macro":
            steps = [a for a in (sp.get() for _ex, sp in self.steps) if a]
            if not steps: raise ValueError("Makro musi mieć co najmniej jeden krok")
            return {"type": "macro", "delay": int(self.delay_row.get_value()), "steps": steps, "preset": pid}
        return dict(p[2], preset=pid)


# ---------- wiersz jednego przycisku ----------
class ButtonRow(Adw.ExpanderRow):
    def __init__(self, win, key, title, icon, removable=False):
        super().__init__(title=title)
        self.win, self.key = win, key
        self.icon = Gtk.Image.new_from_icon_name(icon)
        self.add_prefix(self.icon)
        self.default_hint, long_help = row_info(key)
        self.add_suffix(info_button(long_help))
        if removable:
            rm = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Usuń")
            rm.add_css_class("flat"); rm.connect("clicked", lambda *_: win.remove_extra(self))
            self.add_suffix(rm)

        self.gest = {}                                   # id -> (ExpanderRow, Picker)
        allow = key not in NO_GESTURE
        self.picker = Picker(win, self.add_row, self.refresh, CATEGORIES_G if allow else CATEGORIES,
                             remove_row=self.remove)
        if allow:
            self.thr_row = Adw.SpinRow.new_with_range(30, 400, 10)
            self.thr_row.set_title("Czułość gestu")
            self.thr_row.set_subtitle("Ile ruchu myszą liczy się jako gest – mniej = łatwiej uruchomić")
            self.thr_row.set_value(100)
            self.add_row(self.thr_row)
            for gid, gtitle in GESTURES:
                ex = Adw.ExpanderRow(title=gtitle)
                ex.add_prefix(gesture_prefix(gid))
                p = Picker(win, ex.add_row, lambda gid=gid: self.gesture_changed(gid), INNER)
                self.gest[gid] = (ex, p)
                self.add_row(ex)
                ex.set_subtitle(p.summary())
        self.refresh()

    def is_gesture(self):
        return self.picker.cat()[0] == "gesture"

    def gesture_changed(self, gid):
        ex, p = self.gest[gid]
        ex.set_subtitle(p.summary())
        self.refresh()

    def summary(self):
        if self.is_gesture():
            n = sum(1 for _ex, p in self.gest.values() if p.cat()[0] != "default")
            return f"Gesty · ustawiono {n} z {len(self.gest)}"
        if self.picker.cat()[0] == "default":
            return f"Domyślnie · {self.default_hint}"
        return self.picker.summary()

    def refresh(self, *_):
        if not hasattr(self, "picker"):
            return
        g = self.is_gesture()
        if self.gest:
            self.thr_row.set_visible(g)
            for ex, _p in self.gest.values():
                ex.set_visible(g)
        self.set_subtitle(self.summary())
        if self.picker.cat()[0] == "default":
            self.icon.remove_css_class("mapped-icon")
        else:
            self.icon.add_css_class("mapped-icon")
        diagram = getattr(self.win, "diagram", None)
        if diagram:
            diagram.queue_draw()                          # schemat myszy pokazuje, co jest ustawione

    def load(self, action):
        if action and action.get("type") == "gesture" and self.gest:
            self.picker.cat_row.set_selected([c[0] for c in self.picker.cats].index("gesture"))
            self.thr_row.set_value(action.get("threshold", 100))
            for gid, (ex, p) in self.gest.items():
                p.load(action.get(gid))
                ex.set_subtitle(p.summary())
            self.refresh()
        else:
            self.picker.load(action)

    def get(self):
        if self.is_gesture():
            act = {"type": "gesture", "threshold": int(self.thr_row.get_value())}
            for gid, (_ex, p) in self.gest.items():
                a = p.get()
                if a: act[gid] = a
            return act
        return self.picker.get()


# ---------- okno glowne ----------
class Win(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="MX Remap", default_width=700, default_height=880)
        self.rows, self._guard, self._dev_guard = {}, False, False
        cfg = {}
        try:
            with open(CFG) as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            pass
        self.device = mxremap.read_device()                 # co umie mysz (zapisuje silnik)
        self._dev_mtime = self._device_mtime()
        self._cfg_buttons = cfg.get("buttons", {})
        self.groups, self._mice_names, self._tick = {}, [""], 0

        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)
        tv = Adw.ToolbarView()
        self.toasts.set_child(tv)

        hb = Adw.HeaderBar()
        self.win_title = Adw.WindowTitle(title="MX Remap", subtitle=self.device_subtitle())
        hb.set_title_widget(self.win_title)
        apply_btn = Gtk.Button(label="Zastosuj")
        apply_btn.add_css_class("suggested-action")
        apply_btn.connect("clicked", self.apply)
        hb.pack_end(apply_btn)
        tv.add_top_bar(hb)

        page = Adw.PreferencesPage()
        tv.set_content(page)

        # --- karta statusu ---
        g = Adw.PreferencesGroup()
        page.add(g)
        self.status_row = Adw.ActionRow(title=esc(self.device_title()), subtitle="Sprawdzanie…")
        self.status_icon = Gtk.Image.new_from_icon_name("input-mouse-symbolic")
        self.status_icon.set_pixel_size(28)
        self.status_icon.add_css_class("status-icon")
        self.status_row.add_prefix(self.status_icon)
        self.switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.switch.connect("notify::active", self.on_switch)
        self.status_row.add_suffix(self.switch)
        self.status_row.set_activatable_widget(self.switch)
        g.add(self.status_row)

        self.dev_row = Adw.ComboRow(title="Mysz", subtitle="Którą mysz Logitecha obsługiwać")
        self.dev_row.connect("notify::selected", lambda *_: None if self._dev_guard else self.on_device_selected())
        g.add(self.dev_row)
        self.set_mice(self.scan_mice(), cfg.get("device_name", ""))
        self._saved_device = self.device_name_cfg()

        self.bat_row = Adw.ActionRow(title="Bateria", subtitle="Sprawdzanie…")
        self.bat_icon = Gtk.Image.new_from_icon_name("battery-missing-symbolic")
        self.bat_row.add_prefix(self.bat_icon)
        self.bat_label = Gtk.Label(label="–", valign=Gtk.Align.CENTER)
        self.bat_label.add_css_class("numeric")
        self.bat_label.add_css_class("title-4")
        self.bat_bar = Gtk.LevelBar(min_value=0, max_value=1, valign=Gtk.Align.CENTER)
        self.bat_bar.set_size_request(110, -1)
        self.bat_row.add_suffix(self.bat_bar)
        self.bat_row.add_suffix(self.bat_label)
        g.add(self.bat_row)
        self.auto_row = Adw.SwitchRow(title="Uruchamiaj przy logowaniu",
                                      subtitle="Remapowanie startuje razem z sesją")
        self.use_service = mxremap.service_available()      # systemd uzytkownika dostepny?
        self.auto_row.set_active(os.path.exists(AUTOSTART) or (self.use_service and mxremap.service_enabled()))
        self.auto_row.connect("notify::active", self.on_autostart)
        prefs = os.path.join(CFG_DIR, "prefs.json")
        if not os.path.exists(prefs):                 # pierwsze uruchomienie: autostart domyslnie wlaczony
            os.makedirs(CFG_DIR, exist_ok=True)
            json.dump({"autostart_decided": True}, open(prefs, "w"))
            was_on = self.auto_row.get_active()
            self.auto_row.set_active(True)
            if was_on:
                self.on_autostart(self.auto_row, None)
        elif self.auto_row.get_active():
            self.on_autostart(self.auto_row, None)    # odswiez wpis (np. po przeniesieniu folderu) / migracja
        g.add(self.auto_row)

        self.panel_row = Adw.SwitchRow(title="Procent baterii na pasku systemu",
                                       subtitle="Wskaźnik w górnym pasku GNOME; dokładny procent czyta działający silnik")
        if gnome_available():
            self.panel_row.set_active(panel_enabled())
            self.panel_row.connect("notify::active", self.on_panel)
        self.panel_row.set_visible(gnome_available())        # w KDE i innych zamiast tego jest ikona w zasobniku
        g.add(self.panel_row)

        self.tray_row = Adw.SwitchRow(
            title="Ikona baterii w zasobniku systemowym",
            subtitle="KDE Plasma, XFCE, Cinnamon i inne; w GNOME wymaga rozszerzenia AppIndicator")
        self.tray_row.set_active(os.path.exists(TRAY_AUTOSTART) or (self.use_service and mxremap.tray_enabled()))
        self.tray_row.connect("notify::active", self.on_tray)
        g.add(self.tray_row)

        self.sug_row = Adw.ActionRow(title="Sugerowane ustawienia")
        sbtn = Gtk.Button(label="Ustaw", valign=Gtk.Align.CENTER)
        sbtn.connect("clicked", self.suggested)
        self.sug_row.add_suffix(sbtn)
        g.add(self.sug_row)

        # --- schemat myszy ---
        if HAVE_DIAGRAM:
            dg = Adw.PreferencesGroup(
                title="Twoja mysz",
                description="Kliknij numer, nazwę albo część myszy, aby ją skonfigurować. "
                            "Wypełnione numery oznaczają przyciski z własną akcją.")
            page.add(dg)
            self.diagram = MouseDiagram(self.focus_row, self.is_set, self.is_available)
            card = Gtk.Box(); card.add_css_class("card"); card.append(self.diagram)
            dg.add(card)

        # --- sekcje przyciskow ---
        btns, wheel = cfg.get("buttons", {}), cfg.get("thumb_wheel", {})
        known = set()
        for title, desc, items in SECTIONS:
            grp = Adw.PreferencesGroup(title=title)
            if desc: grp.set_description(desc)
            if title in GROUP_INFO: grp.set_header_suffix(info_button(GROUP_INFO[title]))
            page.add(grp)
            self.groups[title] = grp
            for key, name, icon in items:
                known.add(key)
                row = ButtonRow(self, key, name, icon)
                row.load({"WHEEL_LEFT": wheel.get("left"), "WHEEL_RIGHT": wheel.get("right")}[key]
                         if key.startswith("WHEEL_") else btns.get(key))
                self.rows[key] = row
                grp.add(row)

        # --- powiadomienia o baterii ---
        al = cfg.get("battery_alerts", {})
        ths = sorted(al.get("thresholds", [15, 5]), reverse=True) + [15, 5]
        self.alert_group = Adw.PreferencesGroup(
            title="Powiadomienia o baterii",
            description="Ostrzeżenie pojawi się raz na każdy próg, gdy mysz się rozładowuje (działa, gdy działa silnik)")
        page.add(self.alert_group)
        self.alert_on = Adw.SwitchRow(title="Ostrzegaj o niskiej baterii")
        self.alert_on.set_active(al.get("enabled", True))
        self.alert_hi = Adw.SpinRow.new_with_range(6, 50, 1)
        self.alert_hi.set_title("Pierwsze ostrzeżenie przy"); self.alert_hi.set_subtitle("procent baterii")
        self.alert_hi.set_value(max(6, min(50, ths[0])))
        self.alert_lo = Adw.SpinRow.new_with_range(1, 30, 1)
        self.alert_lo.set_title("Ostatnie (pilne) ostrzeżenie przy"); self.alert_lo.set_subtitle("procent baterii")
        self.alert_lo.set_value(max(1, min(30, ths[1])))
        test = Adw.ActionRow(title="Wyślij powiadomienie testowe", subtitle="Sprawdź, jak wygląda ostrzeżenie")
        tb = Gtk.Button(label="Test", valign=Gtk.Align.CENTER)
        tb.connect("clicked", lambda *_: mxremap.notify("Niski poziom baterii myszy",
                                                        "To jest powiadomienie testowe MX Remap.", False))
        test.add_suffix(tb)
        for r in (self.alert_on, self.alert_hi, self.alert_lo, test):
            self.alert_group.add(r)

        # --- ustawienia myszy: DPI i SmartShift ---
        mo = cfg.get("mouse", {})
        self._dpi_list, self._prefilled = [], False
        self.mouse_group = Adw.PreferencesGroup(
            title="Ustawienia myszy",
            description="Silnik ustawia je przy każdym połączeniu myszy (mysz sama ich nie pamięta)")
        page.add(self.mouse_group)

        self.dpi_on = Adw.SwitchRow(title="Ustaw czułość (DPI)", subtitle="Aktualnie w myszy: –")
        self.dpi_on.set_active(bool(mo.get("dpi")))
        self.dpi_row = Adw.ActionRow(title="Czułość", subtitle="Więcej DPI = szybszy kursor")
        sc = self.dpi_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 200, 4000, 100)
        sc.set_draw_value(True); sc.set_value_pos(Gtk.PositionType.RIGHT); sc.set_digits(0)
        sc.set_size_request(300, -1); sc.set_valign(Gtk.Align.CENTER)
        sc.set_value(mo.get("dpi") or 1000)
        sc.connect("value-changed", self.snap_dpi)
        self.dpi_row.add_suffix(sc)
        self.set_dpi_marks(200, 4000)
        self.dpi_on.add_suffix(info_button(
            "DPI to czułość czujnika: im więcej, tym dalej przesuwa się kursor przy tym samym ruchu ręki. "
            "Typowe wartości to 800–1600. Zakres zależy od myszy."))

        ss = mo.get("smartshift", {})
        self.ss_on = Adw.SwitchRow(title="Ustaw zachowanie kółka (SmartShift)", subtitle="Aktualnie: –")
        self.ss_on.set_active(bool(ss))
        self.ss_mode = Adw.ComboRow(title="Tryb kółka", model=Gtk.StringList.new([
            "SmartShift (auto)", "Zawsze skokowe", "Zawsze swobodne"]))
        self.ss_mode.set_selected(SS_MODES.index(ss.get("mode", "auto")) if ss.get("mode", "auto") in SS_MODES else 0)
        self.ss_thr = Adw.SpinRow.new_with_range(1, 50, 1)
        self.ss_thr.set_title("Próg SmartShift")
        self.ss_thr.set_subtitle("Im niższa wartość, tym łatwiej kółko przechodzi w tryb swobodny")
        self.ss_thr.set_value(ss.get("threshold", 25))
        self.ss_torque = Adw.SpinRow.new_with_range(1, 100, 1)
        self.ss_torque.set_title("Opór kółka")
        self.ss_torque.set_subtitle("Siła „klikania” kółka w trybie skokowym – więcej = wyraźniejszy opór")
        self.ss_torque.set_value(ss.get("torque", 50))
        self.ss_on.add_suffix(info_button(
            "SmartShift to automatyczne przełączanie kółka. Kółko zaczyna jako skokowe (z oporem), a gdy zakręcisz "
            "nim szybko, odblokowuje się i kręci swobodnie. Próg określa, jak szybko trzeba kręcić."))
        for r in (self.dpi_on, self.dpi_row, self.ss_on, self.ss_mode, self.ss_thr, self.ss_torque):
            self.mouse_group.add(r)
        for r in (self.dpi_on, self.ss_on):
            r.connect("notify::active", self.update_mouse_rows)
        self.ss_mode.connect("notify::selected", self.update_mouse_rows)
        self.update_mouse_rows()

        self.extra_group = Adw.PreferencesGroup(title="Dodatkowe przyciski",
                                                description="Przyciski, których nie ma powyżej – naciśnij je, a program je wykryje")
        det = Gtk.Button(child=Adw.ButtonContent(icon_name="list-add-symbolic", label="Wykryj przycisk"))
        det.set_valign(Gtk.Align.CENTER); det.connect("clicked", self.detect)
        self.extra_group.set_header_suffix(det)
        page.add(self.extra_group)
        for key, act in btns.items():
            if key not in known:
                self.add_extra(key).load(act)

        self.sync_hidden()
        self.apply_device()
        GLib.timeout_add_seconds(30, self.refresh_battery)
        GLib.timeout_add(800, lambda: (self.sync_extension(), False)[1])
        self.refresh_battery()
        GLib.timeout_add_seconds(2, self.refresh_status)
        self.refresh_status()

    # --- pomocnicze ---
    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=2))

    def error(self, msg):
        dlg = Adw.AlertDialog(heading="Coś poszło nie tak", body=msg)
        dlg.add_response("ok", "OK")
        dlg.present(self)

    # --- wybor myszy i dopasowanie do modelu ---
    def device_title(self):
        return self.device.get("label") or "Mysz Logitech"

    def device_subtitle(self):
        label = self.device.get("label")
        return f"Logitech {label}" if label else "Mysz Logitech"

    @staticmethod
    def _device_mtime():
        try:
            return os.path.getmtime(mxremap.DEVICE_FILE)
        except OSError:
            return 0

    @staticmethod
    def scan_mice():
        try:
            return mxremap.detect_mice()
        except Exception:                                  # brak uprawnien itp. - lista pusta
            return []

    def device_name_cfg(self):
        idx = self.dev_row.get_selected()
        return self._mice_names[idx] if 0 <= idx < len(self._mice_names) else ""

    def on_device_selected(self):
        self.dev_row.set_subtitle("Zmiana wejdzie w życie po „Zastosuj” (silnik uruchomi się ponownie)")

    def set_mice(self, mice, current):
        """Lista myszy w wierszu wyboru; zachowuje biezacy wybor (takze zapisana nazwe niepodlaczonej myszy)."""
        names = [m["name"] for m in mice]
        labels = ["Automatycznie (pierwsza mysz Logitecha)"] + [m["label"] for m in mice]
        cur = "" if mxremap.is_auto(current) else current
        sel = 0
        if cur:
            match = (next((n for n in names if n == cur), None)
                     or next((n for n in names if cur.lower() in n.lower()), None))
            if match is None:
                names.append(cur)
                labels.append(f"{mxremap.short_name(cur)} (niepodłączona)")
                match = cur
            sel = names.index(match) + 1
        self._dev_guard = True
        self._mice_names = [""] + names
        self.dev_row.set_model(Gtk.StringList.new(labels))
        self.dev_row.set_selected(sel)
        self._dev_guard = False
        self.dev_row.set_visible(len(mice) > 1 or bool(cur))     # jedna mysz i tryb auto: wiersz zbedny

    def refresh_mice(self):
        mice = self.scan_mice()
        if [m["name"] for m in mice] != [n for n in self._mice_names[1:] if n in {m["name"] for m in mice}]:
            self.set_mice(mice, self.device_name_cfg())

    def refresh_device(self):
        """Odswieza interfejs, gdy silnik zapisal nowy opis myszy (inny model albo odkryte przyciski)."""
        mt = self._device_mtime()
        if mt != self._dev_mtime:
            self._dev_mtime = mt
            self.device = mxremap.read_device()
            self.sync_hidden()
            self.apply_device()

    def apply_device(self):
        d = self.device
        buttons = d.get("buttons") or []
        has = lambda *names: not buttons or any(n in buttons for n in names)    # brak danych = pokaz wszystko
        for key, names in (("BTN_SIDE", ("BTN_SIDE", "BTN_BACK")), ("BTN_EXTRA", ("BTN_EXTRA", "BTN_FORWARD"))):
            if key in self.rows:
                self.rows[key].set_visible(has(*names))
        wheel = d.get("hwheel", True)
        for key in ("WHEEL_LEFT", "WHEEL_RIGHT"):
            if key in self.rows:
                self.rows[key].set_visible(wheel)
        if "Kółko boczne" in self.groups:
            self.groups["Kółko boczne"].set_visible(wheel)
        self.status_row.set_title(esc(self.device_title()))
        self.win_title.set_subtitle(self.device_subtitle())
        parts = []
        if wheel and "WHEEL_LEFT" in self.rows:
            parts.append("kółko boczne = głośność")
        if "HIDPP_C3" in self.rows and self.rows["HIDPP_C3"].get_visible():
            parts.append("przycisk gestów: klik = klawisz Windows, gesty = pulpity, aplikacje, zrzut")
        self.sug_row.set_visible(bool(parts))                  # nie ma co sugerowac, gdy mysz nie ma tych elementow
        text = "; ".join(parts)
        self.sug_row.set_subtitle(text[:1].upper() + text[1:])
        self.update_mouse_rows()
        if HAVE_DIAGRAM:
            self.diagram.set_compact("anywhere" in (d.get("label") or "").lower())
            self.diagram.queue_draw()

    def sync_hidden(self):
        """Wiersze przyciskow HID++ zaleznie od tego, co zglasza mysz (oraz co zapisano w konfiguracji)."""
        grp = self.groups.get("Ukryte przyciski (HID++)")
        if grp is None:
            return
        wanted = dict(mxremap.hidden_buttons(self.device))
        for k in self._cfg_buttons:                         # wczesniej ustawione przyciski zostaja widoczne
            if k.startswith("HIDPP_"):
                cid = int(k[6:], 16)
                wanted.setdefault(cid, mxremap.cid_name(cid))
        for cid, name in wanted.items():
            key = hid_key(cid)
            if key not in self.rows:
                row = ButtonRow(self, key, HID_TITLES.get(cid, name), "input-mouse-symbolic")
                row.load(self._cfg_buttons.get(key))
                self.rows[key] = row
                grp.add(row)
        any_visible = False
        for key, row in self.rows.items():
            if key.startswith("HIDPP_"):
                show = int(key[6:], 16) in wanted or row.picker.cat()[0] != "default"
                row.set_visible(show)
                any_visible |= show
        grp.set_visible(any_visible)
        if HAVE_DIAGRAM:
            self.diagram.queue_draw()

    def is_available(self, keys):
        return any(k in self.rows and self.rows[k].get_visible() for k in keys)

    def on_tray(self, row, _p):
        uid = str(os.getuid())
        if row.get_active():
            if not (self.use_service and mxremap.install_tray(enable=True, now=True)):
                os.makedirs(os.path.dirname(TRAY_AUTOSTART), exist_ok=True)
                with open(TRAY_AUTOSTART, "w", encoding="utf-8") as f:
                    f.write("[Desktop Entry]\nType=Application\nName=MX Remap (zasobnik)\n"
                            f'Exec="{sys.executable}" "{TRAY}"\nNoDisplay=true\nTerminal=false\n')
                subprocess.Popen([sys.executable, TRAY], start_new_session=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.toast("Ikona baterii włączona w zasobniku systemowym")
        else:
            if self.use_service:
                mxremap.systemctl("disable", "--now", mxremap.TRAY_SERVICE_NAME)
            if os.path.exists(TRAY_AUTOSTART):
                os.remove(TRAY_AUTOSTART)
            subprocess.run(["pkill", "-u", uid, "-f", r"mxremap_tray\.py"])
            self.toast("Ikona w zasobniku wyłączona")

    def focus_row(self, key):
        row = self.rows.get(key)
        if row:
            row.set_expanded(True)
            row.grab_focus()                               # przewija listę do tego wiersza

    def is_set(self, keys):
        return any(k in self.rows and self.rows[k].picker.cat()[0] != "default" for k in keys)

    def set_dpi_marks(self, lo, hi):
        sc = self.dpi_scale
        sc.clear_marks()
        sc.add_mark(lo, Gtk.PositionType.BOTTOM, "wolniej")
        sc.add_mark(hi, Gtk.PositionType.BOTTOM, "szybciej")

    def update_mouse_rows(self, *_):
        self.dpi_row.set_visible(self.dpi_on.get_active())
        on = self.ss_on.get_active()
        self.ss_mode.set_visible(on)
        self.ss_mode.set_subtitle(SS_DESC[SS_MODES[self.ss_mode.get_selected()]])
        self.ss_thr.set_visible(on and SS_MODES[self.ss_mode.get_selected()] == "auto")
        self.ss_torque.set_visible(on and bool(self.device.get("smartshift_torque")))   # tylko modele z regulacja oporu

    def snap_dpi(self, sc):
        step = int(sc.get_adjustment().get_step_increment()) or 100
        v = sc.get_value()
        r = round(v / step) * step
        if abs(r - v) > 0.01:
            sc.set_value(r)

    def refresh_mouse(self):
        """Odczytuje z pliku silnika aktualne DPI/SmartShift oraz zakres DPI obslugiwany przez mysz."""
        try:
            d = json.load(open(MOUSE_FILE))
        except (OSError, ValueError):
            return
        lst = d.get("dpi_list") or []
        if lst and lst != self._dpi_list:
            self._dpi_list = lst
            step = min((b - a for a, b in zip(lst, lst[1:]) if b > a), default=100)
            sc, keep = self.dpi_scale, self.dpi_scale.get_value()
            sc.get_adjustment().configure(min(max(keep, lst[0]), lst[-1]), lst[0], lst[-1], step, step, 0)
            self.set_dpi_marks(lst[0], lst[-1])
        cur = d.get("dpi")
        self.dpi_on.set_subtitle(f"Aktualnie w myszy: {cur} DPI" if cur else "Aktualnie w myszy: –")
        if cur and not self._prefilled and not self.dpi_on.get_active():
            self.dpi_scale.set_value(cur)               # pierwszy raz: suwak od razu na biezacej wartosci
        self._prefilled = bool(cur) or self._prefilled
        cs = d.get("smartshift")
        if cs:
            if cs["mode"] == 1: txt = "swobodne"
            elif cs["threshold"] == 255: txt = "zawsze skokowe"
            else: txt = f"SmartShift, próg {cs['threshold']}"
            self.ss_on.set_subtitle("Aktualnie: " + txt)

    def refresh_battery(self):
        b = read_battery()
        if not b:
            self.bat_icon.set_from_icon_name("battery-missing-symbolic")
            self.bat_row.set_subtitle("Niedostępna – mysz rozłączona lub system nie podaje danych")
            self.bat_bar.set_visible(False)
            self.bat_label.set_text("–")
            return True
        status = {"Charging": "Ładowanie", "Discharging": "Rozładowywanie", "Full": "Naładowana",
                  "Not charging": "Nie ładuje"}.get(b["status"], b["status"])
        p = b["percent"]
        if p is not None:
            n = max(0, min(100, 10 * round(p / 10)))
            if b["status"] == "Charging": icon = f"battery-level-{n}-charging-symbolic"
            elif n == 100: icon = "battery-level-100-charged-symbolic"
            else: icon = f"battery-level-{n}-symbolic"
            self.bat_icon.set_from_icon_name(icon)
            self.bat_bar.set_visible(True)
            self.bat_bar.set_value(p / 100)
            self.bat_label.set_text(f"{p}%")
            self.bat_row.set_subtitle(status or "Poziom naładowania")
        else:
            lv = {"Full": "Pełna", "High": "Wysoka", "Normal": "Normalna", "Low": "Niska",
                  "Critical": "Krytyczna"}.get(b["level"], b["level"] or "–")
            self.bat_icon.set_from_icon_name("battery-good-symbolic")
            self.bat_bar.set_visible(False)
            self.bat_label.set_text(lv)
            self.bat_row.set_subtitle((status + " · " if status else "") +
                                      "dokładny procent pojawi się, gdy silnik działa (przełącznik wyżej)")
        return True

    def sync_extension(self):
        """Jesli wskaznik jest wlaczony, a program ma nowsza wersje rozszerzenia - zaktualizuj pliki."""
        if not panel_enabled():
            return
        try:
            if open(os.path.join(EXT_DIR, "extension.js"), encoding="utf-8").read() != EXT_JS:
                write_extension()
                self.toast("Zaktualizowano wskaźnik baterii – wyloguj się i zaloguj ponownie, aby zobaczyć zmiany")
        except OSError:
            pass

    def on_panel(self, row, _p):
        def run(*cmd):
            try:
                return subprocess.run(cmd, capture_output=True).returncode == 0
            except OSError:
                return False
        st = Gio.Settings.new("org.gnome.shell")
        rest = [x for x in st.get_strv("enabled-extensions") if x != EXT_UUID]
        if row.get_active():
            write_extension()
            st.set_strv("enabled-extensions", rest + [EXT_UUID])
            Gio.Settings.sync()
            run("gdbus", "call", "--session", "--dest", "org.gnome.Shell", "--object-path", "/org/gnome/Shell",
                "--method", "org.gnome.Shell.Extensions.LoadUserExtension", EXT_UUID)
            if not self.engine_running():
                self.apply()                       # silnik czyta procent baterii przez HID++
            if run("gnome-extensions", "enable", EXT_UUID):
                self.toast("Wskaźnik baterii włączony na pasku")
            else:
                self.toast("Włączono – wyloguj się i zaloguj ponownie, aby wskaźnik się pojawił")
        else:
            st.set_strv("enabled-extensions", rest)
            Gio.Settings.sync()
            run("gnome-extensions", "disable", EXT_UUID)
            self.toast("Wskaźnik baterii wyłączony")

    def suggested(self, *_):
        for key, pid in (("WHEEL_LEFT", "vol_down"), ("WHEEL_RIGHT", "vol_up")):
            if key in self.rows:
                self.rows[key].load(preset_action(pid))
        if "HIDPP_C3" in self.rows:
            self.rows["HIDPP_C3"].load({
                "type": "gesture", "threshold": 100,
                "click": preset_action("super"), "up": preset_action("apps"), "down": preset_action("screenshot"),
                "left": preset_action("ws_prev"), "right": preset_action("ws_next")})
        self.toast("Ustawiono – kliknij „Zastosuj”")

    def add_extra(self, name):
        row = ButtonRow(self, name, extra_title(name), "input-mouse-symbolic", removable=True)
        self.rows[name] = row
        self.extra_group.add(row)
        return row

    def remove_extra(self, row):
        self.extra_group.remove(row)
        self.rows.pop(row.key, None)

    # --- konfiguracja ---
    def save_config(self):
        btns, wheel = {}, {}
        for key, r in self.rows.items():
            act = r.get()
            if not act: continue
            if key == "WHEEL_LEFT": wheel["left"] = act
            elif key == "WHEEL_RIGHT": wheel["right"] = act
            else: btns[key] = act
        os.makedirs(CFG_DIR, exist_ok=True)
        hi, lo = int(self.alert_hi.get_value()), int(self.alert_lo.get_value())
        alerts = {"enabled": self.alert_on.get_active(), "thresholds": sorted({hi, lo}, reverse=True)}
        mouse = {}
        if self.dpi_on.get_active():
            mouse["dpi"] = int(self.dpi_scale.get_value())
        if self.ss_on.get_active():
            mouse["smartshift"] = {"mode": SS_MODES[self.ss_mode.get_selected()],
                                   "threshold": int(self.ss_thr.get_value())}
            if self.ss_torque.get_visible():                 # tylko modele z regulacja oporu kolka
                mouse["smartshift"]["torque"] = int(self.ss_torque.get_value())
        json.dump({"device_name": self.device_name_cfg(), "buttons": btns, "thumb_wheel": wheel,
                   "battery_alerts": alerts, "mouse": mouse}, open(CFG, "w", encoding="utf-8"), indent=2,
                  ensure_ascii=False)

    def on_autostart(self, row, _p):
        if row.get_active():
            if self.use_service and mxremap.install_service(enable=True):
                if os.path.exists(AUTOSTART):                # migracja: stary autostart zastepuje usluga
                    os.remove(AUTOSTART)
            else:
                write_autostart()
        else:
            if self.use_service:
                mxremap.systemctl("disable", mxremap.SERVICE_NAME)
            if os.path.exists(AUTOSTART):
                os.remove(AUTOSTART)

    # --- silnik ---
    def engine_running(self):
        return subprocess.run(["pgrep", "-u", str(os.getuid()), "-f", PGREP],
                              capture_output=True).returncode == 0

    def engine_pid(self):
        """PID dzialajacego silnika, ktory umie przeladowac konfiguracje (SIGHUP) - inaczej None."""
        try:
            d = json.load(open(ENGINE_FILE))
            os.kill(d["pid"], 0)
            return d["pid"] if d.get("api", 1) >= 5 else None
        except (OSError, ValueError, KeyError):
            return None

    def stop_engine(self):
        if self.use_service:
            mxremap.systemctl("stop", mxremap.SERVICE_NAME)
        subprocess.run(["pkill", "-u", str(os.getuid()), "-f", PGREP])      # silniki uruchomione inaczej

    def start_engine(self):
        os.makedirs(CFG_DIR, exist_ok=True)
        if self.use_service:                                # usluga systemd: sama wznowi silnik po awarii
            mxremap.install_service(enable=False)           # odswiez sciezki w jednostce
            if mxremap.systemctl("restart", mxremap.SERVICE_NAME):
                GLib.timeout_add(1500, lambda: (self.refresh_status(), False)[1])
                return
        log = open(os.path.join(CFG_DIR, "engine.log"), "w")
        subprocess.Popen([sys.executable, ENGINE, "-c", CFG, "--wait"], stdout=log, stderr=log,
                         start_new_session=True)
        GLib.timeout_add(1500, lambda: (self.refresh_status(), False)[1])

    def refresh_status(self):
        msg = ""
        try:
            lines = open(os.path.join(CFG_DIR, "engine.log")).read().strip().splitlines()
            msg = next((l for l in reversed(lines) if l.startswith("HID++")), lines[-1] if lines else "")
        except OSError:
            pass
        self.refresh_mouse()
        self.refresh_device()
        self._tick += 1
        if self._tick % 5 == 0:                              # co ~10 s sprawdz, czy podlaczono/odlaczono mysz
            self.refresh_mice()
        running = self.engine_running()
        self._guard = True
        self.switch.set_active(running)
        self._guard = False
        if running:
            self.status_icon.add_css_class("running")
        else:
            self.status_icon.remove_css_class("running")
        head = "Działa – przyciski są przekierowane" if running else "Zatrzymany"
        self.status_row.set_subtitle(esc(head) + (f"\n<small>{esc(msg[:140])}</small>" if msg else ""))
        return True

    def on_switch(self, sw, _p):
        if self._guard:
            return
        if sw.get_active():
            self.apply()
        else:
            self.stop_engine()
            self.toast("Remapowanie zatrzymane")
            GLib.timeout_add(500, lambda: (self.refresh_status(), False)[1])

    def apply(self, *_):
        try:
            self.save_config()
        except ValueError as ex:
            self._guard = True; self.switch.set_active(False); self._guard = False
            return self.error(str(ex))
        new_dev = self.device_name_cfg()
        changed, self._saved_device = new_dev != self._saved_device, new_dev
        self.dev_row.set_subtitle("Którą mysz Logitecha obsługiwać")
        pid = None if changed else self.engine_pid()     # inna mysz = pelny restart silnika
        if pid:                                   # szybka sciezka: przeladowanie bez restartu (bez przerwy w dzialaniu)
            os.kill(pid, signal.SIGHUP)
            GLib.timeout_add(500, lambda: (self.refresh_status(), False)[1])
        else:                                     # silnik nie dziala albo jest starej wersji - uruchom od nowa
            self.stop_engine()
            GLib.timeout_add(500, lambda: (self.start_engine(), False)[1])
        self.toast("Zapisano i zastosowano")

    # --- wykrywanie przycisku ---
    def detect(self, *_):
        self.stop_engine()
        try:
            dev = mxremap.find_device(self.device_name_cfg())
        except PermissionError:
            return self.error("Brak uprawnień do /dev/input.\nUruchom install.sh i zaloguj się ponownie.")
        if dev is None:
            return self.error("Nie znaleziono myszy Logitecha. Czy jest połączona?")

        dlg = Adw.AlertDialog(heading="Wykryj przycisk", body="Naciśnij na myszy przycisk, który chcesz dodać…")
        dlg.add_response("cancel", "Anuluj")
        found, state = {}, {}

        def on_io(fd, cond):
            try:
                for ev in dev.read():
                    if ev.type == e.EV_KEY and ev.value == 1 and ev.code >= 0x110:
                        found["code"] = ev.code
                        state["src"] = None
                        dlg.force_close()
                        return False
            except BlockingIOError:
                pass
            return True

        def on_closed(_d):
            if state.get("src"):
                GLib.source_remove(state["src"])
                state["src"] = None
            dev.close()
            if "code" in found:
                names = e.BTN.get(found["code"])
                names = names if isinstance(names, list) else [names]
                name = next((n for n in MOUSE_BTNS if n in names), names[0])
                if name in self.rows:
                    self.rows[name].set_expanded(True)
                    self.toast(f"„{extra_title(name)}” jest już na liście")
                else:
                    self.add_extra(name).set_expanded(True)
                    self.toast(f"Dodano: {extra_title(name)}")

        state["src"] = GLib.io_add_watch(dev.fd, GLib.IO_IN, on_io)
        dlg.connect("closed", on_closed)
        dlg.present(self)


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id="pl.mxremap.App")
        self.connect("activate", self.on_activate)

    def on_activate(self, app):
        css = Gtk.CssProvider()
        try: css.load_from_string(CSS)
        except AttributeError: css.load_from_data(CSS.encode())
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        (self.props.active_window or Win(app)).present()


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
