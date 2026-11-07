#!/usr/bin/env python3
"""mxremap - silnik remapowania przyciskow myszy Logitech (MX Master, MX Anywhere i inne z HID++ 2.0).

Obsluguje:
  * zwykle przyciski i kolko boczne (evdev),
  * przyciski "ukryte" (kciuk/gesty, tryb kolka) przez protokol HID++ 2.0.

Zwykle uruchamiany przez GUI (mxremap_gui.py), ale dziala tez samodzielnie:
  ./mxremap.py -c ~/.config/mxremap/config.json --wait
  ./mxremap.py --hidpp-info        (diagnostyka HID++)
"""
import argparse, atexit, fcntl, glob, json, os, select, shutil, signal, subprocess, sys, threading, time, traceback
import evdev
from evdev import UInput, ecodes as e

# =====================================================================
#  HID++ 2.0
# =====================================================================
HIDPP_LONG = 0x11
SWID = 0x0A
F_REPROG_V4 = 0x1B04      # REPROG_CONTROLS_V4 - lista i przekierowanie przyciskow
F_DEVNAME = 0x0005        # nazwa urzadzenia
DIVERTABLE = 0x20         # flaga: przycisk mozna "zdywertowac"
F_DPI = 0x2201            # regulacja DPI
F_SMARTSHIFT = 0x2110     # SmartShift (MX Master, MX Master 2S)
F_SMARTSHIFT_ENH = 0x2111 # SmartShift z regulacja oporu (MX Master 3)
F_BATT_UNIFIED = 0x1004   # bateria (nowe urzadzenia)
F_BATT_STATUS = 0x1000    # bateria (np. MX Master 2S)
F_BATT_VOLTAGE = 0x1001   # bateria (napiecie)
VOLT_TABLE = [(4186, 100), (4067, 90), (3989, 80), (3922, 70), (3859, 60), (3811, 50),
              (3778, 40), (3751, 30), (3720, 20), (3677, 10), (3600, 5), (0, 0)]
BATT_FILE = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}",
                         "mxremap-battery.json")


ENGINE_FILE = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}",
                           "mxremap-engine.json")


def register_engine():
    """Zapisuje PID silnika (api=5: SIGHUP + gesty + akcje + ustawienia myszy = przeladuj konfiguracje bez restartu)."""
    try:
        with open(ENGINE_FILE, "w") as f:
            json.dump({"pid": os.getpid(), "api": 5}, f)
    except OSError:
        return

    def cleanup():
        try:
            if json.load(open(ENGINE_FILE)).get("pid") == os.getpid():
                os.remove(ENGINE_FILE)
        except (OSError, ValueError):
            pass
    atexit.register(cleanup)


MOUSE_FILE = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}",
                          "mxremap-mouse.json")


def write_state(path, data):
    """Zapisuje (atomowo) stan dla GUI i wskaznika na pasku GNOME."""
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(dict(data, time=int(time.time())), f)
        os.replace(tmp, path)
    except OSError:
        pass


def write_battery(b):
    write_state(BATT_FILE, b)


def notify(title, body, critical=False, icon="battery-caution-symbolic"):
    """Powiadomienie systemowe: notify-send, a gdy go brak - bezposrednio przez D-Bus (gdbus)."""
    if shutil.which("notify-send"):
        cmd = ["notify-send", "-a", "MX Remap", "-i", icon, "-u", "critical" if critical else "normal", title, body]
    else:
        cmd = ["gdbus", "call", "--session", "--dest", "org.freedesktop.Notifications",
               "--object-path", "/org/freedesktop/Notifications",
               "--method", "org.freedesktop.Notifications.Notify", "MX Remap", "0", icon, title, body,
               "[]", "{'urgency': <byte %d>}" % (2 if critical else 1), "0"]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


ALERTS_FILE = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}",
                           "mxremap-battery-alerts.json")


class BatteryAlerts:
    """Ostrzega o niskiej baterii raz na kazdy prog; kasuje stan po podlaczeniu ladowania."""

    def __init__(self):
        self.enabled, self.thresholds = True, [15, 5]
        self.name = "Mysz"
        try:
            with open(ALERTS_FILE) as f:
                self.notified = set(json.load(f))
        except (OSError, ValueError):
            self.notified = set()

    def configure(self, cfg):
        c = cfg.get("battery_alerts", {})
        self.enabled = bool(c.get("enabled", True))
        try:
            self.thresholds = sorted({int(x) for x in c.get("thresholds", [15, 5])}, reverse=True)
        except (TypeError, ValueError):
            self.thresholds = [15, 5]

    def _save(self):
        try:
            with open(ALERTS_FILE, "w") as f:
                json.dump(sorted(self.notified), f)
        except OSError:
            pass

    def check(self, b):
        p = b.get("percent")
        if not self.enabled or p is None or not self.thresholds:
            return
        if b.get("status") in ("Charging", "Full") or p > max(self.thresholds) + 5:
            if self.notified:                       # mysz naladowana / ladowana - zerujemy stan
                self.notified = set()
                self._save()
            return
        hit = [t for t in self.thresholds if p <= t]
        if not hit or min(hit) in self.notified:
            return
        t = min(hit)
        self.notified |= {x for x in self.thresholds if x >= t}   # nie powtarzaj wyzszych progow
        self._save()
        if t <= 5:
            notify("Bateria myszy prawie rozładowana", f"{self.name}: {p}% – podłącz mysz do ładowania.", True)
        else:
            notify("Niski poziom baterii myszy", f"{self.name}: pozostało {p}%.", False)


ALERTS = BatteryAlerts()


# =====================================================================
#  Dodatkowe akcje: aplikacje/strony, tekst, dzwiek, makra
# =====================================================================
SLOW = ("text", "macro", "audio_next", "wait")      # wykonywane w osobnym watku


def target_command(action):
    """Polecenie otwierajace aplikacje (type=app) albo strone/plik/folder (type=url)."""
    if action.get("type") == "app":
        path = action.get("desktop")
        if path and os.path.exists(path):
            return ["gio", "launch", path]
        return ["gtk-launch", (action.get("id") or "").removesuffix(".desktop")]
    target = action.get("target", "").strip()
    if target.startswith("~"):
        target = os.path.expanduser(target)
    elif "://" not in target and not target.startswith(("/", "mailto:", "file:")) and "." in target \
            and " " not in target:
        target = "https://" + target                      # "wp.pl" -> "https://wp.pl"
    return ["xdg-open", target]


def open_target(action):
    try:
        subprocess.Popen(target_command(action), start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as ex:
        print("Nie mozna otworzyc:", ex, flush=True)


def cycle_audio_output():
    """Przelacza domyslne wyjscie dzwieku na nastepne urzadzenie (PipeWire/PulseAudio przez pactl)."""
    def pactl(*args):
        return subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=3).stdout

    def usable(sink):
        for p in sink.get("ports") or []:
            if p.get("name") == sink.get("active_port") and p.get("availability") == "not available":
                return False
        return True

    try:
        sinks = [x for x in json.loads(pactl("-f", "json", "list", "sinks")) if usable(x)]
        default = pactl("get-default-sink").strip()
    except (OSError, ValueError, subprocess.TimeoutExpired):
        notify("Wyjście dźwięku", "Nie udało się odczytać urządzeń (brak polecenia pactl?).", False,
               "audio-speakers-symbolic")
        return
    if len(sinks) < 2:
        notify("Wyjście dźwięku", "Dostępne jest tylko jedno urządzenie wyjściowe.", False, "audio-speakers-symbolic")
        return
    names = [x["name"] for x in sinks]
    nxt = sinks[(names.index(default) + 1) % len(sinks)] if default in names else sinks[0]
    pactl("set-default-sink", nxt["name"])
    for line in pactl("list", "short", "sink-inputs").splitlines():     # przenies juz grajace strumienie
        sid = line.split("\t")[0]
        if sid.isdigit():
            pactl("move-sink-input", sid, nxt["name"])
    notify("Wyjście dźwięku", nxt.get("description") or nxt["name"], False, "audio-speakers-symbolic")


def paste_text(text, press_paste):
    """Wstawia tekst: kopiuje do schowka (wl-copy/xclip/xsel) i wciska Ctrl+V."""
    for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "-ib"]):
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, input=text.encode(), timeout=3,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except (OSError, subprocess.TimeoutExpired):
                return
            time.sleep(0.15)
            press_paste()
            return
    notify("Wstawianie tekstu", "Zainstaluj pakiet wl-clipboard:  sudo dnf install wl-clipboard", True)


# =====================================================================
#  Niezawodnosc: pojedyncza instancja, reguly udev, usluga systemd
# =====================================================================
RUNTIME = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
LOCK_FILE = os.path.join(RUNTIME, "mxremap.lock")
SERVICE_NAME = "mxremap.service"
SERVICE_FILE = os.path.expanduser(f"~/.config/systemd/user/{SERVICE_NAME}")
CONFIG_FILE = os.path.expanduser("~/.config/mxremap/config.json")
LOG_FILE = os.path.expanduser("~/.config/mxremap/engine.log")


def acquire_lock(wait=5.0):
    """Blokada pojedynczej instancji. Czeka chwile, bo poprzedni silnik moze jeszcze konczyc prace."""
    f = open(LOCK_FILE, "w")
    end = time.time() + wait
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f                                   # trzymaj referencje do konca procesu
        except OSError:
            if time.time() >= end:
                f.close()
                return None
            time.sleep(0.2)


def sysfs_mice(base="/sys/class/input"):
    """Nazwy myszy Logitecha z sysfs (czytelne dla zwyklego uzytkownika, jeszcze przed nadaniem uprawnien)."""
    names = []
    for node in sorted(glob.glob(base + "/input*")):
        try:
            with open(node + "/id/vendor") as f:
                vendor = int(f.read().strip(), 16)
            with open(node + "/name") as f:
                name = f.read().strip()
            with open(node + "/capabilities/rel") as f:
                rel = int(f.read().strip().split()[-1] or "0", 16)
        except (OSError, ValueError, IndexError):
            continue
        if vendor == LOGITECH_VENDOR and rel and not name.startswith(VIRTUAL_PREFIX) and name not in names:
            names.append(name)
    return names


def udev_rules(sysfs="/sys/class/hidraw", input_sysfs="/sys/class/input"):
    """Reguly udev tylko dla wykrytych myszy (zamiast dostepu do wszystkich urzadzen Logitecha).

    Dostep dostaje wylacznie interfejs HID++ (nie interfejs klawiatury odbiornika)."""
    rules = ["# Wygenerowane przez: mxremap.py --udev-rules",
             'KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", OPTIONS+="static_node=uinput"']
    names = sysfs_mice(input_sysfs)
    for n in names:
        rules.append('SUBSYSTEM=="input", KERNEL=="event*", ATTRS{name}=="%s", TAG+="uaccess"'
                     % n.replace("\\", "\\\\").replace('"', '\\"'))
    if not names:
        rules += ["# Nie wykryto myszy Logitecha - regula ogolna dla wszystkich myszy Logitecha.",
                  'SUBSYSTEM=="input", KERNEL=="event*", ATTRS{id/vendor}=="046d", ENV{ID_INPUT_MOUSE}=="1", TAG+="uaccess"']
    found = False
    for node in sorted(glob.glob(sysfs + "/hidraw*")):
        info = hidraw_info(node)
        if not info or info[1] != 0x046D or b"\x85\x11" not in info[3]:
            continue                                   # pomijamy nie-Logitech i interfejsy bez HID++
        bus, _vendor, product, _desc = info
        if bus == 5:                                   # Bluetooth
            rules.append(f'SUBSYSTEM=="hidraw", KERNELS=="0005:046D:{product:04X}.*", TAG+="uaccess"')
            found = True
        elif bus == 3:                                 # USB (odbiornik Unifying/Bolt albo kabel)
            iface = os.path.basename(os.path.dirname(os.path.realpath(node + "/device")))
            if ":" not in iface:
                continue
            num = iface.rsplit(".", 1)[-1]
            rules.append(f'SUBSYSTEM=="hidraw", ATTRS{{idVendor}}=="046d", ATTRS{{idProduct}}=="{product:04x}", '
                         f'ENV{{MXREMAP_HIDPP}}="1"')
            rules.append(f'SUBSYSTEM=="hidraw", ENV{{MXREMAP_HIDPP}}=="1", KERNELS=="*:1.{num}", TAG+="uaccess"')
            found = True
    if not found:
        rules += ["# Nie wykryto myszy - regula szeroka (wszystkie urzadzenia Logitecha).",
                  "# Uruchom install.sh ponownie przy podlaczonej myszy, aby ja zawezic.",
                  'SUBSYSTEM=="hidraw", KERNELS=="0003:046D:*|0005:046D:*", TAG+="uaccess"']
    out, seen = [], set()
    for r in rules:                                    # bez duplikatow, z zachowaniem kolejnosci
        if r not in seen or r.startswith("#"):
            out.append(r); seen.add(r)
    return "\n".join(out) + "\n"


def _q(text):
    """Cytowanie argumentu w pliku jednostki systemd."""
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def service_unit(python=None, engine=None, cfg=CONFIG_FILE, log=LOG_FILE):
    python, engine = python or sys.executable, engine or os.path.abspath(__file__)
    return ("[Unit]\n"
            "Description=MX Remap - silnik przyciskow myszy Logitech\n"
            "After=graphical-session.target\n"
            "PartOf=graphical-session.target\n"
            "StartLimitIntervalSec=120\n"
            "StartLimitBurst=10\n\n"
            "[Service]\n"
            "Type=simple\n"
            f"ExecStart={_q(python)} {_q(engine)} -c {_q(cfg)} --wait --log {_q(log)}\n"
            "Restart=on-failure\n"
            "RestartSec=3\n"
            "Environment=PYTHONUNBUFFERED=1\n\n"
            "[Install]\n"
            "WantedBy=graphical-session.target\n")


TRAY_SERVICE_NAME = "mxremap-tray.service"
TRAY_SERVICE_FILE = os.path.expanduser(f"~/.config/systemd/user/{TRAY_SERVICE_NAME}")


def tray_unit(python=None, tray=None):
    python = python or sys.executable
    tray = tray or os.path.join(os.path.dirname(os.path.abspath(__file__)), "mxremap_tray.py")
    return ("[Unit]\n"
            "Description=MX Remap - ikona baterii myszy w zasobniku systemowym\n"
            "After=graphical-session.target\n"
            "PartOf=graphical-session.target\n\n"
            "[Service]\n"
            "Type=simple\n"
            f"ExecStart={_q(python)} {_q(tray)}\n"
            "Restart=on-failure\n"
            "RestartSec=5\n\n"
            "[Install]\n"
            "WantedBy=graphical-session.target\n")


def systemctl(*args):
    try:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def service_available():
    return bool(shutil.which("systemctl")) and systemctl("show-environment")


def service_enabled():
    return systemctl("is-enabled", SERVICE_NAME)


def install_tray(enable=True, now=False):
    """Zapisuje jednostke ikony zasobnika i opcjonalnie wlacza jej autostart."""
    try:
        os.makedirs(os.path.dirname(TRAY_SERVICE_FILE), exist_ok=True)
        with open(TRAY_SERVICE_FILE, "w", encoding="utf-8") as f:
            f.write(tray_unit())
    except OSError:
        return False
    ok = systemctl("daemon-reload")
    if enable:
        ok = systemctl("enable", *(["--now"] if now else []), TRAY_SERVICE_NAME) and ok
    return ok


def tray_enabled():
    return systemctl("is-enabled", TRAY_SERVICE_NAME)


def install_service(enable=True, now=False):
    """Zapisuje jednostke uzytkownika (aktualne sciezki) i opcjonalnie wlacza autostart."""
    try:
        os.makedirs(os.path.dirname(SERVICE_FILE), exist_ok=True)
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(SERVICE_FILE, "w", encoding="utf-8") as f:
            f.write(service_unit())
    except OSError:
        return False
    ok = systemctl("daemon-reload")
    if enable:
        ok = systemctl("enable", *(["--now"] if now else []), SERVICE_NAME) and ok
    return ok


class Hidpp:
    def __init__(self, path, devidx=0):
        self.fd = os.open(path, os.O_RDWR)
        self.path, self.devidx = path, devidx
        self.on_report = None            # wywolywane dla "obcych" raportow (np. powiadomien o przyciskach)
        self._bat = None                 # (id_feature, indeks) po pierwszym rozpoznaniu
        self._fc = {}                    # cache: id cechy -> indeks

    def close(self):
        try: os.close(self.fd)
        except OSError: pass

    def read(self, timeout):
        r, _, _ = select.select([self.fd], [], [], timeout)
        return os.read(self.fd, 64) if r else None

    def request(self, feat, func, params=(), timeout=1.0):
        head = (func << 4) | SWID
        os.write(self.fd, (bytes([HIDPP_LONG, self.devidx, feat, head]) + bytes(params)).ljust(20, b"\0"))
        end = time.time() + timeout
        while True:
            left = end - time.time()
            if left <= 0:
                return None
            rep = self.read(left)
            if rep is None:
                return None
            if len(rep) >= 5 and rep[1] == self.devidx:
                if rep[2] in (0xFF, 0x8F):                 # blad HID++ 2.0 / 1.0
                    return None
                if rep[2] == feat and rep[3] == head:      # nasza odpowiedz
                    return rep[4:]
            if self.on_report:
                self.on_report(rep)

    def get_feature(self, fid):
        r = self.request(0, 0, [fid >> 8, fid & 0xFF])
        return r[0] if r and r[0] else None

    def feature(self, fid):
        if fid not in self._fc:
            self._fc[fid] = self.get_feature(fid)
        return self._fc[fid]

    # --- DPI (0x2201) ---
    def dpi_list(self, idx, sensor=0):
        r = self.request(idx, 1, [sensor])
        if not r:
            return []
        vals = [(r[i] << 8) | r[i + 1] for i in range(1, len(r) - 1, 2)]
        out, i = [], 0
        while i < len(vals):
            v = vals[i]
            if v == 0:
                break
            if v > 0xE000 and out:                       # zakres: krok, potem wartosc koncowa
                step = v - 0xE000
                end = vals[i + 1] if i + 1 < len(vals) else out[-1]
                out += list(range(out[-1] + step, end + 1, step))
                i += 2
            else:
                out.append(v)
                i += 1
        return out

    def get_dpi(self, idx, sensor=0):
        r = self.request(idx, 2, [sensor])
        return ((r[1] << 8) | r[2]) if r else None

    def set_dpi(self, idx, dpi, sensor=0):
        return self.request(idx, 3, [sensor, dpi >> 8, dpi & 0xFF])

    # --- SmartShift (0x2110 / 0x2111) ---
    def smartshift_get(self):
        idx = self.feature(F_SMARTSHIFT)
        r = self.request(idx, 0) if idx else None
        if not r:
            idx = self.feature(F_SMARTSHIFT_ENH)
            r = self.request(idx, 1) if idx else None
            if r:
                return {"mode": r[0], "threshold": r[1], "torque": r[2]}
        return {"mode": r[0], "threshold": r[1]} if r else None      # mode: 1 = swobodne, 2 = skokowe

    def smartshift_set(self, mode, threshold, torque=None):
        """mode: 'auto' (skokowe, samo puszcza przy szybkim ruchu), 'ratchet' (zawsze skokowe), 'free' (swobodne).

        torque (1-100): opor kolka, tylko urzadzenia z 0x2111 (np. MX Master 3/3S); 0/None = bez zmiany."""
        m, a = {"auto": (2, max(1, min(254, int(threshold)))), "ratchet": (2, 255), "free": (1, 0)}[mode]
        idx = self.feature(F_SMARTSHIFT)
        if idx:
            return self.request(idx, 1, [m, a, 0])
        idx = self.feature(F_SMARTSHIFT_ENH)
        if idx:
            t = max(1, min(100, int(torque))) if torque else 0
            return self.request(idx, 2, [m, a, t])
        return None

    def device_name(self):
        idx = self.get_feature(F_DEVNAME)
        if not idx:
            return None
        r = self.request(idx, 0)
        if not r:
            return None
        n, name = r[0], b""
        while len(name) < n:
            r = self.request(idx, 1, [len(name)])
            if not r:
                break
            name += r
        return name[:n].decode("utf-8", "ignore")

    def battery(self):
        """Zwraca {"percent", "level", "status"} albo None. Obsluguje cechy 0x1004, 0x1000, 0x1001."""
        if self._bat is None:
            self._bat = False
            for fid in (F_BATT_UNIFIED, F_BATT_STATUS, F_BATT_VOLTAGE):
                idx = self.get_feature(fid)
                if idx:
                    self._bat = (fid, idx)
                    break
        if not self._bat:
            return None
        fid, idx = self._bat
        if fid == F_BATT_UNIFIED:
            r = self.request(idx, 1)
            if not r:
                return None
            soc, lvl, chg = r[0], r[1], r[2]
            level = "Full" if lvl & 8 else "Normal" if lvl & 4 else "Low" if lvl & 2 else "Critical" if lvl & 1 else ""
            return {"percent": soc if soc > 0 else None, "level": level,
                    "status": {0: "Discharging", 1: "Charging", 2: "Charging", 3: "Full"}.get(chg, "")}
        if fid == F_BATT_STATUS:
            r = self.request(idx, 0)
            if not r:
                return None
            return {"percent": r[0], "level": "",
                    "status": {0: "Discharging", 1: "Charging", 2: "Charging", 3: "Full", 4: "Charging"}.get(r[2], "")}
        r = self.request(idx, 0)
        if not r:
            return None
        mv = (r[0] << 8) | r[1]
        pct = next(p for v, p in VOLT_TABLE if mv >= v)
        return {"percent": pct, "level": "", "status": "Charging" if r[2] & 0x80 else "Discharging"}

    def controls(self, idx):
        r = self.request(idx, 0)
        out = {}
        for i in range(r[0] if r else 0):
            r = self.request(idx, 1, [i])
            if r:
                out[(r[0] << 8) | r[1]] = bool(r[4] & DIVERTABLE)
        return out

    def set_divert(self, idx, cid, on):
        # flagi: 0x01 = diverted, 0x02 = "zmien diverted"; reszta 0 (bez remapowania CID)
        return self.request(idx, 3, [cid >> 8, cid & 0xFF, 0x03 if on else 0x02, 0, 0])


def hidraw_info(node):
    """(bus, vendor, product, deskryptor) dla /sys/class/hidraw/hidrawN albo None."""
    try:
        with open(node + "/device/uevent") as f:
            props = dict(l.split("=", 1) for l in f.read().splitlines() if "=" in l)
        with open(node + "/device/report_descriptor", "rb") as f:
            desc = f.read()
        bus, vendor, product = [int(x, 16) for x in props["HID_ID"].split(":")]
    except (OSError, KeyError, ValueError):
        return None
    return bus, vendor, product, desc


_perm_warned = False


def open_hidpp(name_part):
    """Szuka wezla hidraw z HID++ nalezacego do myszy o danej nazwie."""
    global _perm_warned
    for node in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        info = hidraw_info(node)
        if not info or info[1] != 0x046D or b"\x85\x11" not in info[3]:   # tylko Logitech z raportem HID++ 0x11
            continue
        bus = info[0]
        try:
            h = Hidpp("/dev/" + os.path.basename(node))
        except PermissionError:
            if not _perm_warned:
                print("HID++: brak uprawnien do /dev/hidraw* - uruchom ponownie install.sh", flush=True)
                _perm_warned = True
            continue
        except OSError:
            continue
        for idx in ([0xFF] if bus == 5 else range(1, 7)):   # Bluetooth: 0xFF, odbiornik: 1..6
            h.devidx = idx
            try:
                name = h.device_name()
            except OSError:
                break
            if name and name_part.lower() in name.lower():
                return h
        h.close()
    return None


class HidppWorker(threading.Thread):
    """Watek: przelacza wskazane przyciski w tryb zgloszen, wykonuje akcje i czyta baterie."""

    def __init__(self, name_part, wanted, do, settings=None, dev_name=None):
        super().__init__(daemon=True)
        self.dev_name, self.discovered = dev_name or name_part, False
        self.name_part, self.wanted, self.do = name_part, dict(wanted), do
        self.settings = settings or {}              # {"dpi": N, "smartshift": {"mode", "threshold"}}
        self.stop_evt = threading.Event()
        self.h = None
        self.feat = None
        self.pressed = set()
        self.need_apply = False
        self.retries = 0
        self.pending = None          # nowa konfiguracja czekajaca na zastosowanie (bez restartu)
        self.logged = False
        self.last_batt = 0
        self.warned = set()

    def reconfigure(self, wanted, settings=None):
        self.pending = (dict(wanted), settings or {})

    def apply_settings(self):
        """Ustawia DPI i SmartShift (mysz nie pamieta ich po wylaczeniu) i zapisuje stan dla GUI."""
        h, s = self.h, self.settings
        if s.get("dpi"):
            idx = h.feature(F_DPI)
            if idx:
                lst = h.dpi_list(idx)
                want = min(lst, key=lambda x: abs(x - s["dpi"])) if lst else int(s["dpi"])
                if h.set_dpi(idx, want) is not None:
                    print(f"Ustawiono DPI: {want}", flush=True)
                else:
                    print("HID++: nie udalo sie ustawic DPI", flush=True)
            else:
                print("HID++: mysz nie obsluguje regulacji DPI", flush=True)
        ss = s.get("smartshift")
        if ss:
            try:
                ok = h.smartshift_set(ss.get("mode", "auto"), ss.get("threshold", 25), ss.get("torque")) is not None
            except KeyError:
                ok = False
            print("Ustawiono SmartShift" if ok else "HID++: nie udalo sie ustawic SmartShift", flush=True)
        info = {}
        idx = h.feature(F_DPI)
        if idx:
            info["dpi_list"], info["dpi"] = h.dpi_list(idx), h.get_dpi(idx)
        cur = h.smartshift_get()
        if cur:
            info["smartshift"] = cur
        if info:
            write_state(MOUSE_FILE, info)

    def apply(self):
        ok = True
        if not self.feat:
            return ok
        for cid in self.wanted:
            if self.h.set_divert(self.feat, cid, True) is None:
                ok = False
                if cid not in self.warned:
                    print(f"HID++: nie udalo sie przekierowac przycisku {cid:04X}", flush=True)
                    self.warned.add(cid)
        return ok

    def poll_battery(self):
        self.last_batt = time.time()
        b = self.h.battery()
        if b:
            write_battery(b)
            ALERTS.check(b)

    def connect(self):
        h = open_hidpp(self.name_part)
        if h is None:
            return False
        h.on_report = self.on_report
        self.h, self.pressed = h, set()
        self.feat = h.get_feature(F_REPROG_V4)
        if not self.logged:
            print("HID++: podlaczono" if self.feat else
                  "HID++: podlaczono, ale mysz nie obsluguje przekierowania przyciskow", flush=True)
        self.logged = True
        self.apply()                      # najpierw przyciski (liczy sie czas reakcji)...
        self.apply_settings()             # ...potem DPI i SmartShift...
        self.poll_battery()               # ...i bateria
        if not self.discovered:           # na koncu (w tle): jakie przyciski ma ta mysz
            self.discover()
        return True

    def discover(self):
        """Zapisuje liste przyciskow HID++ i mozliwosci myszy, zeby interfejs dopasowal sie do modelu."""
        self.discovered = True
        info = {"smartshift_torque": bool(self.h.feature(F_SMARTSHIFT_ENH)), "dpi": bool(self.h.feature(F_DPI))}
        if self.feat:
            ctrls = self.h.controls(self.feat)
            info["controls"] = [{"cid": c, "divertable": d} for c, d in sorted(ctrls.items())]
        update_device_file(self.dev_name, **info)

    def on_report(self, rep):
        h = self.h
        if len(rep) < 5 or rep[1] != h.devidx:
            return
        if rep[0] == 0x10 and rep[2] == 0x41:                 # mysz sie (ponownie) polaczyla
            self.need_apply = True
        elif rep[0] == HIDPP_LONG and self.feat and rep[2] == self.feat and (rep[3] >> 4) == 0:
            data, cids = rep[4:20], set()
            for i in range(0, len(data) - 1, 2):
                c = (data[i] << 8) | data[i + 1]
                if c == 0:
                    break
                cids.add(c)
            for c in cids - self.pressed:
                if c in self.wanted: self.do(self.wanted[c], True)
            for c in self.pressed - cids:
                if c in self.wanted: self.do(self.wanted[c], False)
            self.pressed = cids

    def drop(self):
        for c in list(self.pressed):                          # zwolnij ewentualnie "przytrzymane"
            if c in self.wanted:
                try: self.do(self.wanted[c], False)
                except Exception: pass
        self.pressed = set()
        if self.h:
            self.h.close(); self.h = None

    def run(self):
        while not self.stop_evt.is_set():
            try:
                if self.pending is not None:                  # przeladowanie konfiguracji "w locie"
                    (new, sett), self.pending = self.pending, None
                    if self.h and self.feat:
                        for cid in set(self.wanted) - set(new):
                            self.h.set_divert(self.feat, cid, False)
                    self.wanted, self.warned, self.settings = new, set(), sett
                    if self.h:
                        self.apply()
                        self.apply_settings()
                if self.h is None:
                    if not self.connect():
                        self.stop_evt.wait(5); continue
                if self.need_apply:
                    time.sleep(1.0); self.need_apply = False
                    if not self.apply() and self.retries < 3:
                        self.retries += 1; self.need_apply = True
                    else:
                        self.retries = 0
                    self.apply_settings()
                    self.last_batt = 0
                rep = self.h.read(0.5)
                if rep:
                    self.on_report(rep)
                if time.time() - self.last_batt > 60:
                    self.poll_battery()
            except OSError:
                self.drop(); self.stop_evt.wait(2)
            except Exception as ex:                       # nieoczekiwany blad - nie zabijaj watku
                print("HID++: nieoczekiwany blad:", repr(ex), flush=True)
                traceback.print_exc()
                self.drop(); self.stop_evt.wait(2)
        # sprzatanie: przywroc normalne dzialanie przyciskow
        if self.h and self.feat:
            try:
                for cid in self.wanted:
                    self.h.set_divert(self.feat, cid, False)
            except OSError:
                pass
        self.drop()


# =====================================================================
#  evdev
# =====================================================================
LOGITECH_VENDOR = 0x046D
VIRTUAL_PREFIX = "mxremap virtual"              # wirtualne urzadzenia silnika (kopiuja vendor myszy!)
KNOWN_BUTTONS = ["BTN_LEFT", "BTN_RIGHT", "BTN_MIDDLE", "BTN_SIDE", "BTN_EXTRA", "BTN_FORWARD", "BTN_BACK", "BTN_TASK"]
DEVICE_FILE = os.path.expanduser("~/.config/mxremap/device.json")
# nazwy przyciskow HID++ (CID); 0x50-0x56 to zwykle przyciski widoczne tez dla systemu
CID_NAMES = {0x50: "Lewy przycisk", 0x51: "Prawy przycisk", 0x52: "Klik kółka", 0x53: "Wstecz", 0x56: "Dalej",
             0xC3: "Przycisk gestów", 0xC4: "Przycisk trybu kółka", 0xD7: "Przycisk DPI"}
STANDARD_CIDS = {0x50, 0x51, 0x52, 0x53, 0x56}
FALLBACK_HIDDEN = [0xC3, 0xC4]                  # gdy mysz jeszcze nie zglosila listy przyciskow


def short_name(name):
    n = (name or "").strip()
    for prefix in ("Logitech ", "Logi "):
        if n.startswith(prefix):
            n = n[len(prefix):]
    return n.strip()


def is_auto(name_part):
    return not name_part or name_part.strip().lower() == "auto"


def cid_name(cid):
    return CID_NAMES.get(cid, f"Przycisk HID++ 0x{cid:04X}")


def _is_mouse(d, caps):
    return (e.EV_REL in caps and e.BTN_LEFT in caps.get(e.EV_KEY, [])
            and not d.name.startswith(VIRTUAL_PREFIX))


def find_device(name_part=""):
    """Mysz o danej nazwie (fragment) albo - gdy nazwa pusta/"auto" - pierwsza mysz Logitecha.

    Zwraca otwarte urzadzenie albo None. Moze rzucic PermissionError."""
    found = []
    for path in sorted(evdev.list_devices()):
        d = evdev.InputDevice(path)
        caps = d.capabilities()
        if _is_mouse(d, caps) and (d.info.vendor == LOGITECH_VENDOR if is_auto(name_part)
                                   else name_part.lower() in d.name.lower()):
            found.append(d)
        else:
            d.close()
    found.sort(key=lambda d: "MX" not in d.name.upper())          # modele MX maja pierwszenstwo
    for d in found[1:]:
        d.close()
    return found[0] if found else None


def detect_mice():
    """Lista podlaczonych myszy Logitecha: [{"name", "label", "bus"}]."""
    out, seen = [], set()
    for path in sorted(evdev.list_devices()):
        try:
            d = evdev.InputDevice(path)
        except OSError:
            continue
        try:
            if d.info.vendor == LOGITECH_VENDOR and _is_mouse(d, d.capabilities()) and d.name not in seen:
                seen.add(d.name)
                out.append({"name": d.name, "label": short_name(d.name), "bus": d.info.bustype})
        finally:
            d.close()
    return out


def device_profile(dev):
    """Mozliwosci myszy widoczne dla systemu (do dopasowania interfejsu)."""
    caps = dev.capabilities()
    keys, rel = caps.get(e.EV_KEY, []), caps.get(e.EV_REL, [])
    return {"label": short_name(dev.name),
            "buttons": [n for n in KNOWN_BUTTONS if getattr(e, n) in keys],
            "hwheel": e.REL_HWHEEL in rel or e.REL_HWHEEL_HI_RES in rel}


def read_device(path=None):
    try:
        with open(path or DEVICE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def update_device_file(name, path=None, **fields):
    """Zapisuje (trwale) opis myszy; dla innej myszy niz poprzednio zaczyna od zera."""
    path = path or DEVICE_FILE
    cur = read_device(path)
    if cur.get("name") != name:
        cur = {"name": name}
    cur.update(fields)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cur, f)
        os.replace(tmp, path)
    except OSError:
        pass


def hidden_buttons(device):
    """[(CID, nazwa)] przyciskow dostepnych tylko przez HID++ dla danej myszy."""
    ctrls = device.get("controls")
    if not ctrls:
        return [(c, cid_name(c)) for c in FALLBACK_HIDDEN]
    return [(c["cid"], cid_name(c["cid"])) for c in ctrls
            if c.get("divertable") and c["cid"] not in STANDARD_CIDS]


def code(name):
    return getattr(e, name) if isinstance(name, str) else name


RELOAD = {"fn": None}      # ustawiane przez aktywna sesje; wywolywane przez SIGHUP


def resolve_gesture(action, dx, dy):
    """Wybiera akcje gestu na podstawie sumarycznego przesuniecia (dx, dy) podczas przytrzymania."""
    if max(abs(dx), abs(dy)) < action.get("threshold", 100):
        direction = "click"
    elif abs(dx) > abs(dy):
        direction = "right" if dx > 0 else "left"
    else:
        direction = "down" if dy > 0 else "up"
    sub = action.get(direction)
    return sub if sub and sub.get("type") != "gesture" else None


def parse_cfg(cfg):
    buttons = cfg.get("buttons", {})
    maps = {code(k): v for k, v in buttons.items() if not k.startswith("HIDPP_")}
    hid = {int(k[6:], 16): v for k, v in buttons.items() if k.startswith("HIDPP_")}
    return maps, hid, cfg.get("thumb_wheel", {})


def session(dev, load_cfg):
    cfg = load_cfg()
    maps, hid_maps, wheel = parse_cfg(cfg)
    ALERTS.configure(cfg)
    ALERTS.name = short_name(dev.name)
    update_device_file(dev.name, **device_profile(dev))
    st = {"maps": maps, "wheel": wheel}
    ui = UInput.from_device(dev, name="mxremap virtual mouse")
    kb = UInput({e.EV_KEY: list(range(1, 249))}, name="mxremap virtual keyboard")
    lock = threading.RLock()
    gest = {}                  # {"cur": {"a": akcja, "dx": .., "dy": ..}} gdy trwa gest

    def do(action, pressed):
        """pressed: True/False dla przyciskow, None dla zdarzen jednorazowych (kolko)."""
        t = action.get("type")
        with lock:
            if t == "key":
                keys = [code(k) for k in action["keys"]]
                if pressed is None or pressed:
                    for k in keys: kb.write(e.EV_KEY, k, 1)
                if pressed is None or not pressed:
                    for k in reversed(keys): kb.write(e.EV_KEY, k, 0)
                kb.syn()
            elif t == "button":
                b = code(action["button"])
                if pressed is None:
                    ui.write(e.EV_KEY, b, 1); ui.write(e.EV_KEY, b, 0)
                else:
                    ui.write(e.EV_KEY, b, 1 if pressed else 0)
                ui.syn()
            elif t == "command":
                if pressed is None or pressed:
                    subprocess.Popen(action["command"], shell=True)
            elif t in ("app", "url"):
                if pressed is None or pressed:
                    open_target(action)
            elif t in SLOW:                               # tekst, makro, dzwiek, pauza - w osobnym watku
                if pressed is None or pressed:
                    threading.Thread(target=slow_main, args=(action,), daemon=True).start()
            elif t == "gesture":                          # przytrzymaj i przesun; akcja po puszczeniu
                if pressed:
                    gest["cur"] = {"a": action, "dx": 0, "dy": 0}
                elif pressed is False:
                    cur, gest["cur"] = gest.get("cur"), None
                    sub = resolve_gesture(cur["a"], cur["dx"], cur["dy"]) if cur else None
                    if sub:
                        do(sub, None)
            # "none" -> nic nie robimy

    slow_lock = threading.Lock()                           # makra/teksty wykonuja sie po kolei

    def run_slow(action):
        t = action.get("type")
        if t == "wait":
            time.sleep(action.get("ms", 200) / 1000)
        elif t == "audio_next":
            cycle_audio_output()
        elif t == "text":
            paste_text(action.get("text", ""),
                       lambda: do({"type": "key", "keys": ["KEY_LEFTCTRL", "KEY_V"]}, None))
        elif t == "macro":
            delay = action.get("delay", 150) / 1000
            for step in action.get("steps", []):
                if step.get("type") in SLOW:
                    run_slow(step)
                else:
                    do(step, None)
                time.sleep(delay)

    def slow_main(action):
        with slow_lock:
            try:
                run_slow(action)
            except Exception as ex:
                print("Blad akcji:", ex, flush=True)

    worker = HidppWorker(short_name(dev.name), hid_maps, do, cfg.get("mouse", {}), dev_name=dev.name)
    worker.start()

    def reload():
        try:
            c = load_cfg()
            m, h, w = parse_cfg(c)
            ALERTS.configure(c)
        except Exception as ex:
            print("Blad konfiguracji:", ex, flush=True)
            return
        st["maps"], st["wheel"] = m, w
        worker.reconfigure(h, c.get("mouse", {}))
        print("Konfiguracja przeladowana", flush=True)

    RELOAD["fn"] = reload
    dev.grab()
    print(f"Przechwycono: {dev.name}", flush=True)
    try:
        for ev in dev.read_loop():
            maps, wheel = st["maps"], st["wheel"]
            if ev.type == e.EV_REL and ev.code in (e.REL_X, e.REL_Y):
                cur = gest.get("cur")
                if cur is not None:                       # trwa gest: licz ruch, kursor stoi w miejscu
                    cur["dx" if ev.code == e.REL_X else "dy"] += ev.value
                    continue
            if ev.type == e.EV_KEY and ev.code in maps and ev.value in (0, 1):
                do(maps[ev.code], bool(ev.value))
                continue
            if ev.type == e.EV_REL and ev.code in (e.REL_HWHEEL, e.REL_HWHEEL_HI_RES) and wheel:
                side = "right" if ev.value > 0 else "left"
                if side in wheel:
                    if ev.code == e.REL_HWHEEL:          # jedna akcja na "zabek"
                        for _ in range(abs(ev.value)):
                            do(wheel[side], None)
                    continue                              # tlumi oryginalny ruch
            with lock:
                ui.write_event(ev)
    finally:
        RELOAD["fn"] = None
        worker.stop_evt.set(); worker.join(3)
        try: dev.ungrab()
        except OSError: pass
        ui.close(); kb.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default="config.json")
    ap.add_argument("--wait", action="store_true", help="czekaj na mysz i wznawiaj po rozlaczeniu")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--hidpp-info", action="store_true", help="diagnostyka: pokaz przyciski HID++")
    ap.add_argument("--log", help="zapisuj komunikaty do pliku (przydatne w autostarcie)")
    ap.add_argument("--udev-rules", action="store_true", help="wypisz reguly udev dla podlaczonej myszy")
    ap.add_argument("--device", default="", help="fragment nazwy myszy (domyslnie: pierwsza mysz Logitecha)")
    ap.add_argument("--list-mice", action="store_true", help="pokaz wykryte myszy Logitecha")
    ap.add_argument("--install-service", action="store_true", help="zainstaluj i wlacz usluge systemd uzytkownika")
    ap.add_argument("--uninstall-service", action="store_true", help="wylacz usluge systemd uzytkownika")
    ap.add_argument("--install-tray", action="store_true", help="wlacz ikone baterii w zasobniku systemowym")
    ap.add_argument("--uninstall-tray", action="store_true", help="wylacz ikone baterii w zasobniku")
    a = ap.parse_args()
    if a.udev_rules:
        sys.stdout.write(udev_rules())
        return
    if a.list_mice:
        for m_ in detect_mice():
            print(f"{m_['name']}  (magistrala {m_['bus']})")
        return
    if a.install_service:
        ok = install_service(enable=True, now=True)
        print("Usluga systemd zainstalowana i uruchomiona." if ok else
              "Nie udalo sie zainstalowac uslugi (brak sesji systemd?). Zostanie uzyty zwykly autostart.")
        sys.exit(0 if ok else 1)
    if a.install_tray:
        ok = install_tray(enable=True, now=True)
        print("Ikona w zasobniku wlaczona." if ok else "Nie udalo sie wlaczyc ikony (brak sesji systemd?).")
        sys.exit(0 if ok else 1)
    if a.uninstall_tray:
        systemctl("disable", "--now", TRAY_SERVICE_NAME)
        print("Ikona w zasobniku wylaczona.")
        return
    if a.uninstall_service:
        systemctl("disable", "--now", SERVICE_NAME)
        print("Usluga wylaczona.")
        return
    if a.log:
        os.makedirs(os.path.dirname(a.log), exist_ok=True)
        f = open(a.log, "w", buffering=1)
        os.dup2(f.fileno(), 1); os.dup2(f.fileno(), 2)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)          # bez zombie po uruchomionych komendach
    signal.signal(signal.SIGHUP, lambda *_: RELOAD["fn"] and RELOAD["fn"]())   # przeladuj konfiguracje

    if a.list:
        for p in evdev.list_devices():
            print(p, evdev.InputDevice(p).name)
        return

    if a.hidpp_info:
        d_ = find_device(a.device)
        if d_ is None:
            sys.exit("Nie znaleziono myszy Logitecha (uprawnienia? mysz niepodlaczona?)")
        name_ = a.device if not is_auto(a.device) else short_name(d_.name)
        d_.close()
        h = open_hidpp(name_)
        if not h:
            sys.exit(f"Nie znaleziono myszy '{name_}' przez HID++ (uprawnienia? mysz uspiona?)")
        idx = h.get_feature(F_REPROG_V4)
        print(f"Wezel: {h.path}, indeks urzadzenia: {h.devidx}, feature REPROG_V4: {idx}")
        for cid, div in (h.controls(idx).items() if idx else []):
            print(f"  CID 0x{cid:04X}  dywertowalny: {'tak' if div else 'nie'}")
        print("Bateria:", h.battery())
        d = h.feature(F_DPI)
        print("DPI:", (h.get_dpi(d), h.dpi_list(d)) if d else "brak cechy 0x2201")
        print("SmartShift:", h.smartshift_get() or "brak cechy 0x2110/0x2111")
        print("Opor kolka (0x2111):", "tak" if h.feature(F_SMARTSHIFT_ENH) else "nie")
        return

    def load_cfg():
        try:
            return json.load(open(a.config))
        except FileNotFoundError:
            return {}                              # brak konfiguracji - dziala tylko odczyt baterii

    lock = acquire_lock()                                  # tylko jeden silnik naraz
    if lock is None:
        print("Silnik juz dziala - koncze.", flush=True)
        return
    cfg = load_cfg()
    register_engine()
    while True:
        try:
            dev = find_device(cfg.get("device_name", ""))
        except PermissionError:
            if not a.wait:
                sys.exit("Brak uprawnien do /dev/input. Uruchom install.sh (raz) i zaloguj sie ponownie.")
            print("Brak uprawnien do /dev/input - ponawiam za 5 s (uruchom install.sh, jesli sie nie zmienia)", flush=True)
            time.sleep(5); continue
        if dev is None:
            if not a.wait:
                sys.exit("Nie znaleziono myszy. Sprawdz --list.")
            time.sleep(3); continue
        try:
            session(dev, load_cfg)
        except PermissionError:
            dev.close()
            if not a.wait:
                sys.exit("Brak uprawnien do /dev/uinput. Uruchom install.sh (raz).")
            print("Brak uprawnien do /dev/uinput - ponawiam za 5 s (uruchom install.sh, jesli sie nie zmienia)", flush=True)
            time.sleep(5); continue
        except OSError as ex:
            print("Mysz rozlaczona:", ex, flush=True)
        except Exception as ex:                            # nieoczekiwany blad: w trybie --wait dzialaj dalej
            traceback.print_exc()
            if not a.wait:
                raise
            print("Nieoczekiwany blad - ponawiam za 3 s:", repr(ex), flush=True)
            try: dev.close()
            except Exception: pass
            time.sleep(3); continue
        if not a.wait:
            break
        time.sleep(2)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
