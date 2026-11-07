#!/usr/bin/env python3
"""MX Remap - ikona baterii myszy w zasobniku systemowym.

Dziala wszedzie tam, gdzie jest obsluga standardu StatusNotifierItem:
KDE Plasma, XFCE, Cinnamon, MATE, LXQt oraz GNOME z rozszerzeniem AppIndicator.
Dane o baterii czyta z pliku zapisywanego przez silnik (mxremap.py) - bez dostepu do myszy.
"""
import json, os, shlex, signal, subprocess, sys, time

import cairo
import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

HERE = os.path.dirname(os.path.abspath(__file__))
RUNTIME = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
BATT_FILE = os.path.join(RUNTIME, "mxremap-battery.json")
DEVICE_FILE = os.path.expanduser("~/.config/mxremap/device.json")
PATH = "/StatusNotifierItem"
IFACE = "org.kde.StatusNotifierItem"
STALE_AFTER = 180                      # sekundy; starsze dane = mysz niedostepna

XML = f"""<node><interface name="{IFACE}">
  <property name="Category" type="s" access="read"/><property name="Id" type="s" access="read"/>
  <property name="Title" type="s" access="read"/><property name="Status" type="s" access="read"/>
  <property name="WindowId" type="u" access="read"/><property name="IconName" type="s" access="read"/>
  <property name="IconPixmap" type="a(iiay)" access="read"/><property name="OverlayIconName" type="s" access="read"/>
  <property name="OverlayIconPixmap" type="a(iiay)" access="read"/>
  <property name="AttentionIconName" type="s" access="read"/>
  <property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
  <property name="AttentionMovieName" type="s" access="read"/>
  <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
  <property name="ItemIsMenu" type="b" access="read"/><property name="Menu" type="o" access="read"/>
  <method name="ContextMenu"><arg type="i" name="x" direction="in"/><arg type="i" name="y" direction="in"/></method>
  <method name="Activate"><arg type="i" name="x" direction="in"/><arg type="i" name="y" direction="in"/></method>
  <method name="SecondaryActivate"><arg type="i" name="x" direction="in"/><arg type="i" name="y" direction="in"/></method>
  <method name="Scroll"><arg type="i" name="delta" direction="in"/><arg type="s" name="orientation" direction="in"/></method>
  <signal name="NewTitle"/><signal name="NewIcon"/><signal name="NewAttentionIcon"/>
  <signal name="NewOverlayIcon"/><signal name="NewToolTip"/><signal name="NewStatus"><arg type="s" name="status"/></signal>
</interface></node>"""

GREEN, ORANGE, RED, BLUE, GREY = ((0.18, 0.66, 0.35), (0.96, 0.62, 0.07), (0.88, 0.11, 0.14),
                                  (0.21, 0.52, 0.89), (0.45, 0.45, 0.45))
STATUS_PL = {"Charging": "ładowanie", "Discharging": "rozładowywanie", "Full": "naładowana", "Not charging": "nie ładuje"}
LEVEL_PL = {"Full": "pełna", "High": "wysoka", "Normal": "normalna", "Low": "niska", "Critical": "krytyczna"}


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def battery_view(batt, label="Mysz Logitech", now=None):
    """Z danych o baterii robi opis ikony: tekst, kolor, status i podpowiedz (czysta funkcja)."""
    now = time.time() if now is None else now
    fresh = batt and now - batt.get("time", 0) < STALE_AFTER and (batt.get("percent") is not None or batt.get("level"))
    if not fresh:
        return {"text": "?", "color": GREY, "status": "Passive", "title": label,
                "tip": "Brak danych o baterii (silnik nie działa albo mysz jest rozłączona)"}
    p, st = batt.get("percent"), batt.get("status", "")
    status_txt = STATUS_PL.get(st, st)
    if p is None:
        lvl = LEVEL_PL.get(batt.get("level"), batt.get("level"))
        color = {"Critical": RED, "Low": ORANGE}.get(batt.get("level"), GREEN)
        return {"text": "!" if batt.get("level") in ("Critical", "Low") else "OK", "color": color, "status": "Active",
                "title": label, "tip": f"Bateria: {lvl}" + (f" · {status_txt}" if status_txt else "")}
    color = BLUE if st == "Charging" else GREEN if p >= 30 else ORANGE if p >= 15 else RED
    return {"text": str(p), "color": color, "status": "Active" if p > 15 or st == "Charging" else "NeedsAttention",
            "title": label, "tip": f"Bateria: {p}%" + (f" · {status_txt}" if status_txt else "")}


def render_icon(size, text, color):
    """Rysuje ikonke (zaokraglony kwadrat z liczba) i zwraca (szer., wys., bajty ARGB32 w kolejnosci sieciowej)."""
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    cr = cairo.Context(surf)
    m, r = size * 0.04, size * 0.24
    cr.new_sub_path()
    cr.arc(size - m - r, m + r, r, -1.5708, 0); cr.arc(size - m - r, size - m - r, r, 0, 1.5708)
    cr.arc(m + r, size - m - r, r, 1.5708, 3.1416); cr.arc(m + r, m + r, r, 3.1416, 4.7124)
    cr.close_path()
    cr.set_source_rgb(*color); cr.fill()
    cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    cr.set_font_size(size * (0.58 if len(text) <= 2 else 0.42))
    ext = cr.text_extents(text)
    cr.move_to((size - ext.width) / 2 - ext.x_bearing, (size - ext.height) / 2 - ext.y_bearing)
    cr.set_source_rgb(1, 1, 1); cr.show_text(text)
    surf.flush()
    raw, out = bytes(surf.get_data()), bytearray()
    for i in range(0, len(raw), 4):                       # cairo: natywna kolejnosc + przemnozone alfa
        px = raw[i:i + 4]
        b, g, rr, a = (px[0], px[1], px[2], px[3]) if sys.byteorder == "little" else (px[3], px[2], px[1], px[0])
        if a:
            rr, g, b = min(255, rr * 255 // a), min(255, g * 255 // a), min(255, b * 255 // a)
        out += bytes((a, rr, g, b))
    return size, size, bytes(out)


class Tray:
    def __init__(self, open_gui=None):
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.node = Gio.DBusNodeInfo.new_for_xml(XML)
        self.open_gui = open_gui or self.launch_gui
        self.view = battery_view({}, "Mysz Logitech")
        self.icons = []
        self.name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self.bus.register_object(PATH, self.node.interfaces[0], self.on_call, self.on_get, None)
        Gio.bus_own_name_on_connection(self.bus, self.name, Gio.BusNameOwnerFlags.NONE, None, None)
        Gio.bus_watch_name_on_connection(self.bus, "org.kde.StatusNotifierWatcher", Gio.BusNameWatcherFlags.NONE,
                                         self.on_watcher, None)
        self.refresh(first=True)
        GLib.timeout_add_seconds(int(os.environ.get("MXREMAP_TRAY_INTERVAL", "10")), lambda: (self.refresh(), True)[1])

    # --- rejestracja w zasobniku (takze gdy panel startuje pozniej niz my) ---
    def on_watcher(self, bus, _name, _owner):
        bus.call("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher", "org.kde.StatusNotifierWatcher",
                 "RegisterStatusNotifierItem", GLib.Variant("(s)", (self.name,)), None,
                 Gio.DBusCallFlags.NONE, -1, None, None)

    # --- dane ---
    def refresh(self, first=False):
        label = read_json(DEVICE_FILE).get("label") or "Mysz Logitech"
        new = battery_view(read_json(BATT_FILE), label)
        old, self.view = self.view, new
        if first or (new["text"], new["color"]) != (old["text"], old["color"]):
            self.icons = [render_icon(s, new["text"], new["color"]) for s in (22, 32, 48)]
        if first:
            return
        for sig in ("NewIcon", "NewToolTip", "NewTitle") if new != old else ():
            self.bus.emit_signal(None, PATH, IFACE, sig, None)
        if new["status"] != old["status"]:
            self.bus.emit_signal(None, PATH, IFACE, "NewStatus", GLib.Variant("(s)", (new["status"],)))

    def on_get(self, _c, _s, _p, _i, prop):
        v = self.view
        pix = GLib.Variant("a(iiay)", [(w, h, d) for w, h, d in self.icons])
        empty = GLib.Variant("a(iiay)", [])
        return {
            "Category": GLib.Variant("s", "Hardware"), "Id": GLib.Variant("s", "mxremap-battery"),
            "Title": GLib.Variant("s", v["title"]), "Status": GLib.Variant("s", v["status"]),
            "WindowId": GLib.Variant("u", 0), "IconName": GLib.Variant("s", ""), "IconPixmap": pix,
            "OverlayIconName": GLib.Variant("s", ""), "OverlayIconPixmap": empty,
            "AttentionIconName": GLib.Variant("s", ""), "AttentionIconPixmap": empty,
            "AttentionMovieName": GLib.Variant("s", ""),
            "ToolTip": GLib.Variant("(sa(iiay)ss)", ("", [], v["title"], v["tip"])),
            "ItemIsMenu": GLib.Variant("b", False), "Menu": GLib.Variant("o", "/NO_DBUSMENU"),
        }.get(prop)

    def on_call(self, _c, _s, _p, _i, method, _params, invocation):
        invocation.return_value(None)
        if method == "Activate":
            self.open_gui()

    @staticmethod
    def launch_gui():
        cmd = (shlex.split(os.environ["MXREMAP_GUI_CMD"]) if os.environ.get("MXREMAP_GUI_CMD")
               else [sys.executable, os.path.join(HERE, "mxremap_gui.py")])
        subprocess.Popen(cmd, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    loop = GLib.MainLoop()
    Tray()
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, lambda: (loop.quit(), False)[1])
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, lambda: (loop.quit(), False)[1])
    loop.run()


if __name__ == "__main__":
    main()
