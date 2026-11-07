#!/usr/bin/env python3
"""Pomocniczy skrypt testu zasobnika: uruchamiany przez `dbus-run-session`, wypisuje wynik jako JSON.

Udaje panel (StatusNotifierWatcher), uruchamia ikone i sprawdza rejestracje, wlasciwosci, odswiezanie i klikniecie."""
import json, os, subprocess, sys, tempfile, time
import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
tmp = tempfile.mkdtemp()
env = dict(os.environ, XDG_RUNTIME_DIR=tmp, HOME=tmp, MXREMAP_TRAY_INTERVAL="1",
           MXREMAP_GUI_CMD=f"touch {tmp}/gui-opened")
wlog = os.path.join(tmp, "watcher.log")
WATCHER = r'''
import sys, gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib
XML = '<node><interface name="org.kde.StatusNotifierWatcher"><method name="RegisterStatusNotifierItem"><arg type="s" name="service" direction="in"/></method></interface></node>'
bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
def call(conn, sender, path, iface, method, params, inv):
    open(sys.argv[1], "a").write(params.unpack()[0] + "\n"); inv.return_value(None)
bus.register_object("/StatusNotifierWatcher", Gio.DBusNodeInfo.new_for_xml(XML).interfaces[0], call, None, None)
Gio.bus_own_name_on_connection(bus, "org.kde.StatusNotifierWatcher", Gio.BusNameOwnerFlags.NONE, None, None)
GLib.MainLoop().run()
'''


def write_batt(**kw):
    with open(os.path.join(tmp, "mxremap-battery.json"), "w") as f:
        json.dump(dict(kw, time=int(time.time())), f)


out = {}
bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
write_batt(percent=87, status="Discharging")
os.makedirs(os.path.join(tmp, ".config/mxremap"))
with open(os.path.join(tmp, ".config/mxremap/device.json"), "w") as f:
    json.dump({"name": "MX Anywhere 3", "label": "MX Anywhere 3"}, f)
tray = subprocess.Popen([sys.executable, os.path.join(ROOT, "mxremap_tray.py")], env=env)
time.sleep(1.2)                                              # ikona wstaje PRZED panelem...
watcher = subprocess.Popen([sys.executable, "-c", WATCHER, wlog], env=env)
time.sleep(2.0)                                              # ...a panel pojawia sie pozniej
try:
    out["registered"] = open(wlog).read().split() if os.path.exists(wlog) else []
    name = out["registered"][0] if out["registered"] else None
    out["name_ok"] = bool(name) and name.startswith("org.kde.StatusNotifierItem-%d-" % tray.pid)

    def props():
        r = bus.call_sync(name, "/StatusNotifierItem", "org.freedesktop.DBus.Properties", "GetAll",
                          GLib.Variant("(s)", ("org.kde.StatusNotifierItem",)), None, Gio.DBusCallFlags.NONE, 5000, None)
        return r.unpack()[0]

    p = props()
    out["first"] = {"Id": p["Id"], "Category": p["Category"], "Status": p["Status"], "Title": p["Title"],
                    "tooltip": p["ToolTip"][3], "sizes": [(w, h, len(d)) for w, h, d in p["IconPixmap"]],
                    "menu": p["Menu"], "is_menu": p["ItemIsMenu"]}
    first_icon = p["IconPixmap"][1][2]
    write_batt(percent=12, status="Discharging")
    time.sleep(2.5)                                          # odswiezanie co 1 s
    p = props()
    out["after_low"] = {"Status": p["Status"], "tooltip": p["ToolTip"][3], "icon_changed": p["IconPixmap"][1][2] != first_icon}
    os.remove(os.path.join(tmp, "mxremap-battery.json"))     # silnik przestal dzialac -> brak swiezych danych
    with open(os.path.join(tmp, "mxremap-battery.json"), "w") as f:
        json.dump({"percent": 40, "status": "Discharging", "time": int(time.time()) - 600}, f)
    time.sleep(2.5)
    out["stale"] = {"Status": props()["Status"]}
    bus.call_sync(name, "/StatusNotifierItem", "org.kde.StatusNotifierItem", "Activate",
                  GLib.Variant("(ii)", (0, 0)), None, Gio.DBusCallFlags.NONE, 5000, None)
    time.sleep(0.8)
    out["gui_opened"] = os.path.exists(os.path.join(tmp, "gui-opened"))
except Exception as ex:                                       # noqa: BLE001
    out["error"] = repr(ex)
finally:
    tray.terminate(); watcher.terminate()
    tray.wait(5); watcher.wait(5)
    out["tray_exit"] = tray.returncode
print("RESULT:" + json.dumps(out))
