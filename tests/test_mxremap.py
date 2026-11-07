#!/usr/bin/env python3
"""Testy automatyczne MX Remap.

Uruchomienie (z folderu programu):
    python3 -m unittest discover -s tests -v

Wymagaja tylko python3-evdev. Interfejs (GTK) jest zastepowany atrapa, wiec testy
dzialaja takze bez srodowiska graficznego i bez myszy.
"""
import json, os, sys, tempfile, threading, time, types, unittest
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import mxremap as m                       # noqa: E402
from evdev import ecodes as e            # noqa: E402


def import_gui():
    """Importuje mxremap_gui z atrapa GTK/libadwaita (tylko do testowania danych i logiki)."""
    class Stub(types.ModuleType):
        def __getattr__(self, name):
            if name.startswith("__"):
                raise AttributeError(name)
            cls = type(name, (), {})
            setattr(self, name, cls)
            return cls
    gi = types.ModuleType("gi")
    gi.require_version = lambda *a, **k: None
    repo = types.ModuleType("gi.repository")
    for n in ("Adw", "Gtk", "Gdk", "GLib", "Gio"):
        setattr(repo, n, Stub(n))
    gi.repository = repo
    with mock.patch.dict(sys.modules, {"gi": gi, "gi.repository": repo}):
        import importlib
        sys.modules.pop("mxremap_gui", None)
        return importlib.import_module("mxremap_gui")


class FakeHidpp(m.Hidpp):
    """Atrapa urzadzenia: bez uchwytu hidraw, odpowiedzi podawane przez funkcje."""
    def __init__(self, feats=None, responder=None):
        self.devidx, self._bat, self._fc = 1, None, {}
        self.feats, self.responder, self.sent = feats or {}, responder, []

    def get_feature(self, fid):
        return self.feats.get(fid)

    def request(self, feat, func, params=(), timeout=1.0):
        self.sent.append((feat, func, list(params)))
        r = self.responder
        return r(feat, func, list(params)) if callable(r) else r

    def close(self):
        pass


# ----------------------------------------------------------------- gesty
class GestureTests(unittest.TestCase):
    A = {"type": "gesture", "threshold": 100, "click": {"n": "klik"}, "up": {"n": "gora"},
         "down": {"n": "dol"}, "left": {"n": "lewo"}, "right": {"n": "prawo"}}

    def test_kierunki(self):
        cases = {(0, 0): "klik", (30, -20): "klik", (150, 20): "prawo", (-160, 40): "lewo",
                 (10, -200): "gora", (20, 180): "dol", (-90, -95): "klik", (100, 100): "dol"}
        for (dx, dy), want in cases.items():
            self.assertEqual(m.resolve_gesture(self.A, dx, dy)["n"], want, (dx, dy))

    def test_brak_akcji_w_kierunku(self):
        self.assertIsNone(m.resolve_gesture({"type": "gesture", "up": {"n": "x"}}, 0, 300))

    def test_zagniezdzony_gest_jest_ignorowany(self):
        self.assertIsNone(m.resolve_gesture({"threshold": 10, "up": {"type": "gesture"}}, 0, -50))

    def test_domyslny_prog(self):
        self.assertIsNone(m.resolve_gesture({"right": {"n": "r"}}, 99, 0))
        self.assertEqual(m.resolve_gesture({"right": {"n": "r"}}, 101, 0)["n"], "r")


# ------------------------------------------------------- otwieranie celow
class TargetCommandTests(unittest.TestCase):
    def test_adresy(self):
        t = lambda x: m.target_command({"type": "url", "target": x})
        self.assertEqual(t("wp.pl"), ["xdg-open", "https://wp.pl"])
        self.assertEqual(t("https://example.com/x"), ["xdg-open", "https://example.com/x"])
        self.assertEqual(t("/tmp"), ["xdg-open", "/tmp"])
        self.assertEqual(t("mailto:a@b.pl"), ["xdg-open", "mailto:a@b.pl"])
        self.assertEqual(t("~/Pobrane"), ["xdg-open", os.path.expanduser("~/Pobrane")])
        self.assertEqual(t("moj plik.txt"), ["xdg-open", "moj plik.txt"])      # ze spacja: nie dopisuj https

    def test_aplikacja(self):
        with tempfile.NamedTemporaryFile(suffix=".desktop") as f:
            self.assertEqual(m.target_command({"type": "app", "desktop": f.name, "id": "x.desktop"}),
                             ["gio", "launch", f.name])
        self.assertEqual(m.target_command({"type": "app", "desktop": "/nie/ma", "id": "org.gnome.Nautilus.desktop"}),
                         ["gtk-launch", "org.gnome.Nautilus"])


# ---------------------------------------------------------- protokol HID++
class HidppProtocolTests(unittest.TestCase):
    def test_lista_dpi_zakres_i_dyskretna(self):
        zakres = bytes([0, 0, 200, 0xE0, 100, 0x0F, 0xA0, 0, 0]) + bytes(7)          # 200, krok 100, do 4000
        lst = FakeHidpp({m.F_DPI: 4}, zakres).dpi_list(4)
        self.assertEqual((lst[0], lst[1], lst[-1], len(lst)), (200, 300, 4000, 39))
        dysk = bytes([0, 3, 0x20, 3, 0xE8, 7, 0xD0, 0, 0]) + bytes(7)                # 800, 1000, 2000
        self.assertEqual(FakeHidpp({m.F_DPI: 4}, dysk).dpi_list(4), [800, 1000, 2000])
        self.assertEqual(FakeHidpp({m.F_DPI: 4}, None).dpi_list(4), [])

    def test_dpi_odczyt_zapis(self):
        h = FakeHidpp({m.F_DPI: 4}, bytes([0, 0x03, 0xE8, 0x03, 0xE8]) + bytes(11))
        self.assertEqual(h.get_dpi(4), 1000)
        h.set_dpi(4, 1600)
        self.assertEqual(h.sent[-1], (4, 3, [0, 6, 64]))

    def test_smartshift(self):
        h = FakeHidpp({m.F_SMARTSHIFT: 6}, bytes([2, 25, 10]) + bytes(13))
        self.assertEqual(h.smartshift_get(), {"mode": 2, "threshold": 25})
        for mode, want in (("auto", [2, 30, 0]), ("ratchet", [2, 255, 0]), ("free", [1, 0, 0])):
            h.smartshift_set(mode, 30)
            self.assertEqual(h.sent[-1], (6, 1, want), mode)
        h.smartshift_set("auto", 999)
        self.assertEqual(h.sent[-1][2], [2, 254, 0])                                  # prog przyciety

    def test_smartshift_enhanced_z_oporem(self):
        h = FakeHidpp({m.F_SMARTSHIFT_ENH: 8}, bytes([2, 20, 55]) + bytes(13))
        self.assertEqual(h.smartshift_get(), {"mode": 2, "threshold": 20, "torque": 55})
        h.smartshift_set("free", 0)
        self.assertEqual(h.sent[-1], (8, 2, [1, 0, 0]))                              # bez oporu = bez zmiany
        h.smartshift_set("auto", 20, 40)
        self.assertEqual(h.sent[-1], (8, 2, [2, 20, 40]))
        h.smartshift_set("ratchet", 20, 150)
        self.assertEqual(h.sent[-1], (8, 2, [2, 255, 100]))                          # opor przyciety do 100
        h.smartshift_set("auto", 20, -5)
        self.assertEqual(h.sent[-1][2][2], 1)                                         # ujemny -> minimum 1

    def test_stary_smartshift_ignoruje_opor(self):
        h = FakeHidpp({m.F_SMARTSHIFT: 6}, bytes([2, 25, 10]) + bytes(13))
        h.smartshift_set("auto", 25, 70)
        self.assertEqual(h.sent[-1], (6, 1, [2, 25, 0]))

    def test_bateria_trzy_warianty(self):
        h = FakeHidpp({m.F_BATT_STATUS: 5}, bytes([87, 50, 0]) + bytes(13))
        self.assertEqual(h.battery(), {"percent": 87, "level": "", "status": "Discharging"})
        h = FakeHidpp({m.F_BATT_STATUS: 5}, bytes([40, 30, 1]) + bytes(13))
        self.assertEqual(h.battery()["status"], "Charging")
        h = FakeHidpp({m.F_BATT_UNIFIED: 7}, bytes([64, 4, 1, 0]) + bytes(12))
        self.assertEqual(h.battery(), {"percent": 64, "level": "Normal", "status": "Charging"})
        h = FakeHidpp({m.F_BATT_VOLTAGE: 9}, bytes([0x0F, 0xA0, 0]) + bytes(13))     # 4000 mV
        self.assertEqual(h.battery()["percent"], 80)
        self.assertIsNone(FakeHidpp({}, None).battery())

    def test_dywersja_przycisku_wysyla_poprawne_flagi(self):
        h = FakeHidpp({m.F_REPROG_V4: 5}, b"ok")
        h.set_divert(5, 0xC3, True)
        self.assertEqual(h.sent[-1], (5, 3, [0x00, 0xC3, 0x03, 0, 0]))
        h.set_divert(5, 0xC3, False)
        self.assertEqual(h.sent[-1], (5, 3, [0x00, 0xC3, 0x02, 0, 0]))


# --------------------------------------------------------------- watek HID++
class WorkerTests(unittest.TestCase):
    def make(self, wanted=None, settings=None, do=None):
        w = m.HidppWorker("x", wanted or {}, do or (lambda *a: None), settings)
        w.h = SimpleNamespace(devidx=1)
        w.feat = 5
        return w

    def test_przyciski_wcisniecie_i_puszczenie(self):
        calls = []
        w = self.make({0xC3: {"n": 1}}, do=lambda a, p: calls.append(p))
        def rep(*cids):
            data = b"".join(c.to_bytes(2, "big") for c in cids)
            return bytes([0x11, 1, 5, 0x00]) + data + bytes(16 - len(data))
        w.on_report(rep(0xC3)); w.on_report(rep(0xC3)); w.on_report(rep())
        self.assertEqual(calls, [True, False])                                       # bez dubli

    def test_ignoruje_inne_urzadzenia_i_cechy(self):
        calls = []
        w = self.make({0xC3: {}}, do=lambda a, p: calls.append(p))
        w.on_report(bytes([0x11, 2, 5, 0, 0, 0xC3]) + bytes(14))                     # inny indeks urzadzenia
        w.on_report(bytes([0x11, 1, 9, 0, 0, 0xC3]) + bytes(14))                     # inna cecha
        self.assertEqual(calls, [])

    def test_powiadomienie_o_polaczeniu_wymusza_ponowne_ustawienie(self):
        w = self.make()
        w.on_report(bytes([0x10, 1, 0x41, 0, 0, 0, 0]))
        self.assertTrue(w.need_apply)

    def test_przeladowanie_konfiguracji_bez_restartu(self):
        calls = []
        fake = SimpleNamespace(devidx=1, set_divert=lambda f, c, on: calls.append((c, on)) or b"ok",
                               read=lambda t: time.sleep(0.02), battery=lambda: None, close=lambda: None,
                               feature=lambda fid: None, smartshift_get=lambda: None)
        w = m.HidppWorker("x", {0xC3: {}}, lambda *a: None)
        w.h, w.feat, w.last_batt, w.logged = fake, 5, time.time() + 1000, True
        t = threading.Thread(target=w.run, daemon=True); t.start(); time.sleep(0.1)
        w.reconfigure({0xC4: {}}); time.sleep(0.3)
        self.assertEqual(calls[:2], [(0xC3, False), (0xC4, True)])
        w.stop_evt.set(); t.join(2)
        self.assertEqual(calls[-1], (0xC4, False))                                   # sprzatanie przy zatrzymaniu

    def test_ustawienia_dpi_dopasowane_do_najblizszej_wartosci(self):
        lst = bytes([0, 0, 200, 0xE0, 100, 0x0F, 0xA0, 0, 0]) + bytes(7)
        def resp(feat, func, p):
            if feat == 4 and func == 1: return lst
            if feat == 4 and func == 2: return bytes([0, 0x03, 0xE8]) + bytes(13)
            return bytes([2, 25, 10]) + bytes(13)
        h = FakeHidpp({m.F_DPI: 4, m.F_SMARTSHIFT: 6}, resp)
        w = m.HidppWorker("x", {}, lambda *a: None, {"dpi": 1640, "smartshift": {"mode": "auto", "threshold": 20}})
        w.h = h
        with tempfile.TemporaryDirectory() as d, mock.patch.object(m, "MOUSE_FILE", os.path.join(d, "mouse.json")):
            w.apply_settings()
            self.assertIn((4, 3, [0, 6, 64]), h.sent)                                # 1600 DPI
            self.assertIn((6, 1, [2, 20, 0]), h.sent)
            with open(m.MOUSE_FILE) as f:
                self.assertEqual(json.load(f)["dpi"], 1000)

    def test_wyjatek_w_watku_nie_zabija_go(self):
        calls = []
        class Boom:
            devidx = 1
            def read(self, t): raise ValueError("test")
            def close(self): calls.append("closed")
        w = m.HidppWorker("x", {}, lambda *a: None)
        w.h, w.logged = Boom(), True
        with mock.patch.object(m, "open_hidpp", return_value=None), mock.patch("traceback.print_exc"):
            t = threading.Thread(target=w.run, daemon=True); t.start(); time.sleep(0.3)
            self.assertTrue(t.is_alive())
            w.stop_evt.set(); t.join(3)
        self.assertEqual(calls[:1], ["closed"])


# --------------------------------------------------------------- konfiguracja
class ConfigTests(unittest.TestCase):
    def test_parse_cfg_rozdziela_przyciski(self):
        cfg = {"buttons": {"BTN_SIDE": {"type": "none"}, "HIDPP_C3": {"type": "key", "keys": ["KEY_A"]}},
               "thumb_wheel": {"left": {"type": "none"}}}
        maps, hid, wheel = m.parse_cfg(cfg)
        self.assertEqual(list(maps), [e.BTN_SIDE])
        self.assertEqual(list(hid), [0xC3])
        self.assertEqual(wheel, {"left": {"type": "none"}})

    def test_parse_cfg_pusta(self):
        self.assertEqual(m.parse_cfg({}), ({}, {}, {}))

    def test_zla_nazwa_przycisku_rzuca_wyjatek(self):
        with self.assertRaises(AttributeError):
            m.parse_cfg({"buttons": {"BTN_NIE_MA": {"type": "none"}}})


# ------------------------------------------------------------------- bateria
class BatteryAlertsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(m, "ALERTS_FILE", os.path.join(self.tmp.name, "alerts.json"))
        self.patch.start()
        self.sent = []
        self.n = mock.patch.object(m, "notify", lambda t, b, c=False, icon=None: self.sent.append((b, c)))
        self.n.start()

    def tearDown(self):
        self.n.stop(); self.patch.stop(); self.tmp.cleanup()

    def alerts(self, cfg=None):
        a = m.BatteryAlerts(); a.configure(cfg or {}); return a

    def test_ostrzega_raz_na_prog(self):
        a = self.alerts()
        for p in (40, 20, 16, 15, 14, 12, 8, 5, 4, 3):
            a.check({"percent": p, "status": "Discharging"})
        self.assertEqual([c for _, c in self.sent], [False, True])                    # 15% zwykle, 5% pilne

    def test_ladowanie_zeruje_stan(self):
        a = self.alerts()
        a.check({"percent": 9, "status": "Discharging"}); self.sent.clear()
        a.check({"percent": 20, "status": "Charging"})
        a.check({"percent": 9, "status": "Discharging"})
        self.assertEqual(len(self.sent), 1)

    def test_skok_od_razu_ponizej_obu_progow_to_jedno_powiadomienie(self):
        self.alerts().check({"percent": 4, "status": "Discharging"})
        self.assertEqual(len(self.sent), 1)

    def test_restart_silnika_nie_powtarza(self):
        self.alerts().check({"percent": 4, "status": "Discharging"})
        self.alerts().check({"percent": 4, "status": "Discharging"})
        self.assertEqual(len(self.sent), 1)

    def test_wylaczone_i_wlasne_progi(self):
        self.alerts({"battery_alerts": {"enabled": False}}).check({"percent": 3})
        self.assertEqual(self.sent, [])
        a = self.alerts({"battery_alerts": {"thresholds": [30, 10]}})
        a.check({"percent": 28, "status": "Discharging"})
        self.assertIn("28%", self.sent[0][0])

    def test_brak_procentu_nic_nie_robi(self):
        self.alerts().check({"percent": None, "level": "Low"})
        self.assertEqual(self.sent, [])


# ------------------------------------------------------------ dzwiek i tekst
class AudioAndTextTests(unittest.TestCase):
    SINKS = [{"name": "glosniki", "description": "Głośniki", "ports": [{"name": "p", "availability": "available"}],
              "active_port": "p"},
             {"name": "hdmi", "description": "HDMI", "ports": [{"name": "h", "availability": "not available"}],
              "active_port": "h"},
             {"name": "sluchawki", "description": "Słuchawki", "ports": [], "active_port": None}]

    def run_cycle(self, sinks, default):
        log, notes = [], []
        def fake_run(cmd, **kw):
            a = cmd[1:]; log.append(a)
            out = ""
            if a[:3] == ["-f", "json", "list"]: out = json.dumps(sinks)
            elif a[0] == "get-default-sink": out = default + "\n"
            elif a[:3] == ["list", "short", "sink-inputs"]: out = "57\tx\n58\ty\n"
            return SimpleNamespace(stdout=out)
        with mock.patch.object(m.subprocess, "run", fake_run), \
                mock.patch.object(m, "notify", lambda *a, **k: notes.append(a[:2])):
            m.cycle_audio_output()
        return log, notes

    def test_przelaczanie_pomija_niedostepne_i_przenosi_strumienie(self):
        log, notes = self.run_cycle(self.SINKS, "glosniki")
        self.assertIn(["set-default-sink", "sluchawki"], log)
        self.assertIn(["move-sink-input", "57", "sluchawki"], log)
        self.assertEqual(notes, [("Wyjście dźwięku", "Słuchawki")])

    def test_zapetlenie_na_pierwsze(self):
        log, _ = self.run_cycle(self.SINKS, "sluchawki")
        self.assertIn(["set-default-sink", "glosniki"], log)

    def test_jedno_urzadzenie(self):
        log, notes = self.run_cycle(self.SINKS[:1], "glosniki")
        self.assertFalse([a for a in log if a[0] == "set-default-sink"])
        self.assertIn("jedno", notes[0][1])

    def test_wstawianie_tekstu_przez_schowek(self):
        pressed, calls = [], []
        with mock.patch.object(m.shutil, "which", lambda c: "/usr/bin/wl-copy" if c == "wl-copy" else None), \
                mock.patch.object(m.subprocess, "run", lambda cmd, **kw: calls.append((cmd, kw["input"]))), \
                mock.patch.object(m.time, "sleep", lambda s: None):
            m.paste_text("Zażółć gęślą jaźń", lambda: pressed.append(1))
        self.assertEqual(calls, [(["wl-copy"], "Zażółć gęślą jaźń".encode())])
        self.assertEqual(pressed, [1])

    def test_brak_narzedzia_do_schowka(self):
        notes, pressed = [], []
        with mock.patch.object(m.shutil, "which", lambda c: None), \
                mock.patch.object(m, "notify", lambda *a, **k: notes.append(a)):
            m.paste_text("x", lambda: pressed.append(1))
        self.assertEqual(pressed, [])
        self.assertIn("wl-clipboard", notes[0][1])


# ------------------------------------------------- niezawodnosc: udev/systemd
class UdevRulesTests(unittest.TestCase):
    def make_node(self, root, name, bus, vendor, product, with_hidpp=True, usb_iface=None):
        base = os.path.join(root, "devices", f"{bus:04X}:{vendor:04X}:{product:04X}.{name}")
        if usb_iface:
            base = os.path.join(root, "devices", "usb1", "1-2", usb_iface, f"{bus:04X}:{vendor:04X}:{product:04X}.{name}")
        os.makedirs(base)
        with open(os.path.join(base, "uevent"), "w") as f:
            f.write(f"HID_ID={bus:04X}:{vendor:08X}:{product:08X}\nHID_NAME=x\n")
        with open(os.path.join(base, "report_descriptor"), "wb") as f:
            f.write(b"\x05\x01\x85\x11\x09\x01" if with_hidpp else b"\x05\x01\x09\x06")
        node = os.path.join(root, "class", f"hidraw{name}")
        os.makedirs(node)
        os.symlink(base, os.path.join(node, "device"))
        return os.path.join(root, "class")

    def make_input(self, root, idx, name, vendor=0x046D, rel="143"):
        d = os.path.join(root, "input", f"input{idx}")
        os.makedirs(os.path.join(d, "id")); os.makedirs(os.path.join(d, "capabilities"))
        with open(os.path.join(d, "id", "vendor"), "w") as f: f.write(f"{vendor:04x}\n")
        with open(os.path.join(d, "name"), "w") as f: f.write(name + "\n")
        with open(os.path.join(d, "capabilities", "rel"), "w") as f: f.write(rel + "\n")

    def rules(self, root):
        return m.udev_rules(os.path.join(root, "class"), os.path.join(root, "input"))

    def test_bluetooth(self):
        with tempfile.TemporaryDirectory() as d:
            self.make_node(d, "0", 5, 0x046D, 0xB019)
            r = self.rules(d)
            self.assertIn('KERNELS=="0005:046D:B019.*"', r)
            self.assertNotIn("0003:046D:*", r)                                        # bez reguly szerokiej

    def test_odbiornik_tylko_interfejs_hidpp(self):
        with tempfile.TemporaryDirectory() as d:
            self.make_node(d, "1", 3, 0x046D, 0xC52B, with_hidpp=False, usb_iface="1-2:1.0")   # klawiatura odbiornika
            self.make_node(d, "2", 3, 0x046D, 0xC52B, with_hidpp=True, usb_iface="1-2:1.2")
            r = self.rules(d)
            self.assertIn('ATTRS{idProduct}=="c52b"', r)
            self.assertIn('KERNELS=="*:1.2"', r)
            self.assertNotIn(":1.0", r)

    def test_pomija_inne_firmy(self):
        with tempfile.TemporaryDirectory() as d:
            self.make_node(d, "0", 3, 0x1234, 0x0001, usb_iface="1-2:1.2")
            self.assertIn("regula szeroka", self.rules(d))

    def test_brak_urzadzen_daje_regule_awaryjna(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "class"))
            r = self.rules(d)
            self.assertIn('KERNELS=="0003:046D:*|0005:046D:*"', r)
            self.assertIn('KERNEL=="uinput"', r)

    def test_zawsze_zawiera_uinput_i_zdarzenia_wykrytej_myszy(self):
        with tempfile.TemporaryDirectory() as d:
            self.make_node(d, "0", 5, 0x046D, 0xB019)
            self.make_input(d, 5, "MX Master 2S")
            r = self.rules(d)
            self.assertIn('KERNEL=="uinput"', r)
            self.assertIn('ATTRS{name}=="MX Master 2S"', r)
            self.assertNotIn("ID_INPUT_MOUSE", r)                                     # bez reguly ogolnej

    def test_kilka_myszy_i_pominiecie_cudzych_oraz_wirtualnej(self):
        with tempfile.TemporaryDirectory() as d:
            self.make_input(d, 1, "Logitech MX Master 3S")
            self.make_input(d, 2, "MX Anywhere 3")
            self.make_input(d, 3, "mxremap virtual mouse")                            # nasze wlasne urzadzenie
            self.make_input(d, 4, "Inna mysz", vendor=0x1234)
            self.make_input(d, 5, "Logitech Klawiatura", rel="0")                     # brak osi wzglednych
            os.makedirs(os.path.join(d, "class"))
            r = self.rules(d)
            self.assertIn('ATTRS{name}=="Logitech MX Master 3S"', r)
            self.assertIn('ATTRS{name}=="MX Anywhere 3"', r)
            for zle in ("virtual", "Inna mysz", "Klawiatura"):
                self.assertNotIn(zle, r)

    def test_brak_myszy_regula_ogolna_dla_myszy_logitecha(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "class")); os.makedirs(os.path.join(d, "input"))
            r = self.rules(d)
            self.assertIn('ATTRS{id/vendor}=="046d", ENV{ID_INPUT_MOUSE}=="1"', r)


class ServiceTests(unittest.TestCase):
    def test_jednostka_i_cytowanie(self):
        u = m.service_unit("/usr/bin/python3", "/home/u/moj folder/mxremap.py", "/c.json", "/home/u/100%/log")
        self.assertIn('ExecStart="/usr/bin/python3" "/home/u/moj folder/mxremap.py" -c "/c.json" --wait --log "/home/u/100%%/log"', u)
        for line in ("Restart=on-failure", "WantedBy=graphical-session.target", "After=graphical-session.target"):
            self.assertIn(line, u)

    def test_blokada_pojedynczej_instancji(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(m, "LOCK_FILE", os.path.join(d, "lock")):
            first = m.acquire_lock(wait=0.3)
            self.assertIsNotNone(first)
            self.assertIsNone(m.acquire_lock(wait=0.4))                               # druga instancja odpada
            first.close()
            third = m.acquire_lock(wait=0.3)                                          # po zwolnieniu znow mozna
            self.assertIsNotNone(third)
            third.close()

    def test_systemctl_bez_systemd_nie_rzuca(self):
        with mock.patch.object(m.subprocess, "run", side_effect=FileNotFoundError):
            self.assertFalse(m.systemctl("status", "x"))


# --------------------------------------------------------- dane interfejsu (GUI)
class GuiDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.g = import_gui()

    def test_skroty_tam_i_z_powrotem(self):
        g = self.g
        for txt in ("ctrl+shift+t", "alt+f4", "super", "volumeup", "ctrl+pagedown", "f5"):
            self.assertEqual(g.keys_to_text(g.parse_keys(txt)), txt, txt)
        with self.assertRaises(ValueError):
            g.parse_keys("ctrl+nieistniejacy")
        with self.assertRaises(ValueError):
            g.parse_keys("  ")

    def test_presety_maja_unikalne_id_i_poprawne_klawisze(self):
        g = self.g
        ids = [p[0] for c in g.CATEGORIES + [g.WAIT_CAT] for p in c[2]]
        self.assertEqual(len(ids), len(set(ids)))
        for c in g.CATEGORIES + [g.WAIT_CAT]:
            for pid, _label, a in c[2]:
                if a and a["type"] == "key":
                    for k in a["keys"]:
                        self.assertTrue(hasattr(e, k) and getattr(e, k) <= 248, (pid, k))
                if a and a["type"] == "button":
                    self.assertTrue(hasattr(e, a["button"]), pid)

    def test_typy_akcji_zna_silnik(self):
        g = self.g
        znane = {"key", "button", "app", "url", "text", "macro", "wait", "audio_next", "command", "none", "gesture"}
        for c in g.CATEGORIES + [g.WAIT_CAT]:
            for _pid, _l, a in c[2]:
                if a:
                    self.assertIn(a["type"], znane)

    def test_makro_nie_wystepuje_w_krokach_i_gestach(self):
        g = self.g
        for cats in (g.INNER, g.INNER_STEP):
            self.assertFalse([p for c in cats for p in c[2] if p[0] == "macro"])
        self.assertEqual(g.INNER_STEP[-1][0], "wait")
        self.assertNotIn("gesture", [c[0] for c in g.INNER])

    def test_kazdy_preset_odnajdywany_po_zapisie(self):
        """Zapisana akcja (z polem preset i bez niego) wraca do tej samej kategorii i pozycji."""
        g = self.g
        fake = SimpleNamespace(cats=g.CATEGORIES_G)
        for c in g.CATEGORIES:
            for pid, _l, a in c[2]:
                if not a:
                    continue
                saved = dict(a, preset=pid)
                self.assertEqual(g.Picker.locate(fake, saved), (c[0], pid), pid)
                self.assertEqual(g.Picker.locate(fake, dict(a))[0] in [x[0] for x in g.CATEGORIES], True)

    def test_locate_typy_specjalne(self):
        g = self.g
        fake = SimpleNamespace(cats=g.CATEGORIES_G)
        self.assertEqual(g.Picker.locate(fake, {"type": "app"}), ("open", "open_app"))
        self.assertEqual(g.Picker.locate(fake, {"type": "url", "target": "x"}), ("open", "open_url"))
        self.assertEqual(g.Picker.locate(fake, {"type": "text", "text": "x"}), ("text", "insert_text"))
        self.assertEqual(g.Picker.locate(fake, {"type": "macro", "steps": []}), ("text", "macro"))
        self.assertEqual(g.Picker.locate(fake, {"type": "key", "keys": ["KEY_F9"]}), ("custom", "custom_key"))
        self.assertEqual(g.Picker.locate(fake, {"type": "command", "command": "ls"}), ("custom", "custom_cmd"))
        self.assertEqual(g.Picker.locate(fake, None), ("default", None))
        self.assertEqual(g.Picker.locate(fake, {"type": "none"}), ("none", None))
        inner = SimpleNamespace(cats=g.INNER)                                          # w gescie nie ma makra
        self.assertEqual(g.Picker.locate(inner, {"type": "macro"}), ("default", None))

    def test_nazwy_dodatkowych_przyciskow_po_polsku(self):
        g = self.g
        self.assertIn("forward", g.extra_title("BTN_FORWARD"))
        self.assertIn("BTN_0", g.extra_title("BTN_0"))


# -------------------------------------------------- wiele modeli myszy Logitecha
class FakeEvdev:
    """Atrapa urzadzenia evdev."""
    def __init__(self, path, name, vendor=0x046D, rel=True, left=True, hwheel=True, side=True, bus=5):
        self.path, self.name, self.closed = path, name, False
        self.info = SimpleNamespace(vendor=vendor, bustype=bus, product=1, version=1)
        keys = ([e.BTN_LEFT] if left else []) + ([e.BTN_RIGHT, e.BTN_MIDDLE])
        keys += [e.BTN_SIDE, e.BTN_EXTRA] if side else []
        self._caps = {e.EV_KEY: keys, e.EV_REL: ([e.REL_X, e.REL_Y] + ([e.REL_HWHEEL] if hwheel else [])) if rel else []}
        if not rel:
            del self._caps[e.EV_REL]

    def capabilities(self):
        return self._caps

    def close(self):
        self.closed = True


class MouseModelsTests(unittest.TestCase):
    def patch(self, devs):
        by_path = {d.path: d for d in devs}
        return mock.patch.multiple(m.evdev, list_devices=lambda: sorted(by_path), InputDevice=lambda p: by_path[p])

    def test_nazwy(self):
        self.assertEqual(m.short_name("Logitech MX Master 2S"), "MX Master 2S")
        self.assertEqual(m.short_name("  MX Anywhere 3 "), "MX Anywhere 3")
        self.assertTrue(all(m.is_auto(x) for x in ("", None, "auto", " AUTO ")))
        self.assertFalse(m.is_auto("MX Master"))

    def test_auto_wybiera_mysz_logitecha_i_pomija_wirtualna_i_cudze(self):
        devs = [FakeEvdev("/dev/input/event1", "mxremap virtual mouse"),          # kopiuje vendor myszy!
                FakeEvdev("/dev/input/event2", "Razer Mouse", vendor=0x1532),
                FakeEvdev("/dev/input/event3", "Logitech Keyboard", rel=False),
                FakeEvdev("/dev/input/event4", "MX Anywhere 3")]
        with self.patch(devs):
            d = m.find_device("")
        self.assertEqual(d.name, "MX Anywhere 3")
        self.assertTrue(all(x.closed for x in devs if x is not d))                # reszta zamknieta

    def test_modele_mx_maja_pierwszenstwo(self):
        devs = [FakeEvdev("/dev/input/event1", "Logitech USB Receiver"), FakeEvdev("/dev/input/event2", "MX Master 3S")]
        with self.patch(devs):
            self.assertEqual(m.find_device("auto").name, "MX Master 3S")

    def test_wybor_po_nazwie_gdy_sa_dwie_myszy(self):
        devs = [FakeEvdev("/dev/input/event1", "MX Master 3S"), FakeEvdev("/dev/input/event2", "MX Anywhere 3")]
        with self.patch(devs):
            self.assertEqual(m.find_device("anywhere").name, "MX Anywhere 3")
            self.assertEqual(m.find_device("Master 3S").name, "MX Master 3S")
            self.assertIsNone(m.find_device("nie ma takiej"))

    def test_nazwa_nigdy_nie_wybiera_wirtualnej(self):
        with self.patch([FakeEvdev("/dev/input/event1", "mxremap virtual mouse")]):
            self.assertIsNone(m.find_device("mouse"))
            self.assertIsNone(m.find_device(""))

    def test_wykrywanie_listy_myszy_bez_duplikatow(self):
        devs = [FakeEvdev("/dev/input/event1", "MX Master 3S"), FakeEvdev("/dev/input/event2", "MX Master 3S"),
                FakeEvdev("/dev/input/event3", "mxremap virtual mouse"), FakeEvdev("/dev/input/event4", "X", vendor=1)]
        with self.patch(devs):
            self.assertEqual([x["name"] for x in m.detect_mice()], ["MX Master 3S"])

    def test_brak_uprawnien_daje_pusta_liste(self):
        def deny(path): raise PermissionError(path)
        with mock.patch.multiple(m.evdev, list_devices=lambda: ["/dev/input/event1"], InputDevice=deny):
            self.assertEqual(m.detect_mice(), [])
            with self.assertRaises(PermissionError):
                m.find_device("")                                               # silnik ma to zglosic

    def test_profil_urzadzenia_master_i_anywhere(self):
        master = m.device_profile(FakeEvdev("p", "Logitech MX Master 2S"))
        self.assertEqual((master["label"], master["hwheel"]), ("MX Master 2S", True))
        self.assertIn("BTN_SIDE", master["buttons"])
        compact = m.device_profile(FakeEvdev("p", "MX Anywhere 3", hwheel=False, side=False))
        self.assertFalse(compact["hwheel"])
        self.assertNotIn("BTN_SIDE", compact["buttons"])


class DeviceFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "sub", "device.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_zapis_scalanie_i_reset_dla_innej_myszy(self):
        m.update_device_file("MX Master 3S", self.path, label="MX Master 3S", hwheel=True)
        m.update_device_file("MX Master 3S", self.path, controls=[{"cid": 0xC3, "divertable": True}])
        d = m.read_device(self.path)
        self.assertEqual((d["name"], d["hwheel"], len(d["controls"])), ("MX Master 3S", True, 1))   # scalone
        m.update_device_file("MX Anywhere 3", self.path, label="MX Anywhere 3")
        d = m.read_device(self.path)
        self.assertEqual(d, {"name": "MX Anywhere 3", "label": "MX Anywhere 3"})                   # zaczyna od zera

    def test_odczyt_brakujacego_lub_uszkodzonego_pliku(self):
        self.assertEqual(m.read_device(self.path), {})
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f: f.write("{nie json")
        self.assertEqual(m.read_device(self.path), {})

    def test_ukryte_przyciski_zalezne_od_modelu(self):
        self.assertEqual([c for c, _ in m.hidden_buttons({})], [0xC3, 0xC4])                       # domyslnie
        dev = {"controls": [{"cid": 0x50, "divertable": True}, {"cid": 0x52, "divertable": True},
                            {"cid": 0xC3, "divertable": True}, {"cid": 0xC4, "divertable": True},
                            {"cid": 0xD7, "divertable": True}, {"cid": 0x1234, "divertable": True},
                            {"cid": 0x99, "divertable": False}]}
        got = dict(m.hidden_buttons(dev))
        self.assertEqual(sorted(got), [0xC3, 0xC4, 0xD7, 0x1234])        # bez standardowych i niedywertowalnych
        self.assertEqual(got[0xD7], "Przycisk DPI")
        self.assertEqual(got[0x1234], "Przycisk HID++ 0x1234")
        self.assertEqual(m.hidden_buttons({"controls": [{"cid": 0x50, "divertable": True}]}), [])

    def test_watek_zapisuje_odkryte_przyciski_i_mozliwosci(self):
        def resp(feat, func, p):
            if feat == 5 and func == 0: return bytes([2]) + bytes(15)                                # 2 przyciski
            if feat == 5 and func == 1: return bytes([0, 0xC3 if p == [0] else 0xC4, 0, 0, 0x20]) + bytes(11)
            return b"x"
        h = FakeHidpp({m.F_SMARTSHIFT_ENH: 8, m.F_DPI: 4}, resp)
        w = m.HidppWorker("x", {}, lambda *a: None, dev_name="MX Master 3S")
        w.h, w.feat = h, 5
        with mock.patch.object(m, "DEVICE_FILE", self.path):
            w.discover()
            d = m.read_device()
        self.assertTrue(d["smartshift_torque"] and d["dpi"])
        self.assertEqual([c["cid"] for c in d["controls"]], [0xC3, 0xC4])
        self.assertTrue(w.discovered)


class AlertsNameTests(unittest.TestCase):
    def test_powiadomienie_zawiera_nazwe_wykrytej_myszy(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(m, "ALERTS_FILE", os.path.join(d, "a.json")):
            sent = []
            with mock.patch.object(m, "notify", lambda t, b, c=False, icon=None: sent.append(b)):
                a = m.BatteryAlerts(); a.configure({}); a.name = "MX Anywhere 3"
                a.check({"percent": 12, "status": "Discharging"})
            self.assertEqual(sent, ["MX Anywhere 3: pozostało 12%."])


# ------------------------------------------------------------ ikona w zasobniku

# ------------------------------------------------- GUI: KDE, ukryte przyciski
class DesktopAndKdeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.g = import_gui()

    def test_detect_desktop(self):
        d = self.g.detect_desktop
        self.assertEqual(d({"XDG_CURRENT_DESKTOP": "KDE"}), "kde")
        self.assertEqual(d({"XDG_CURRENT_DESKTOP": "ubuntu:GNOME"}), "gnome")
        self.assertEqual(d({"DESKTOP_SESSION": "plasma"}), "kde")
        self.assertEqual(d({"KDE_FULL_SESSION": "true"}), "kde")
        self.assertEqual(d({"XDG_CURRENT_DESKTOP": "XFCE"}), "other")
        self.assertEqual(d({}), "other")

    def test_localize_nie_psuje_oryginalu_i_podmienia_tylko_wskazane(self):
        g = self.g
        before = repr(g.CATEGORIES)
        out = g.localize(g.CATEGORIES, g.KDE_PRESETS)
        self.assertEqual(repr(g.CATEGORIES), before)
        old = {p[0]: p for c in g.CATEGORIES for p in c[2]}
        new = {p[0]: p for c in out for p in c[2]}
        self.assertEqual(set(old), set(new))
        self.assertEqual(new["screenshot"][1], "Zrzut ekranu – Spectacle")
        self.assertEqual(new["screenshot"][2], old["screenshot"][2])      # etykieta zmieniona, akcja nie
        self.assertNotEqual(new["ws_next"][2], old["ws_next"][2])          # akcja zmieniona
        self.assertEqual(new["ws_next"][1], old["ws_next"][1])             # etykieta bez zmian
        untouched = set(old) - set(g.KDE_PRESETS)
        self.assertTrue(untouched)
        for pid in untouched:
            self.assertEqual(old[pid], new[pid], pid)

    def test_presety_kde_odnosza_sie_do_istniejacych_akcji(self):
        g = self.g
        ids = {p[0] for c in g.CATEGORIES for p in c[2]}
        for pid in list(g.KDE_PRESETS) + list(g.KDE_HINTS):
            self.assertIn(pid, ids, pid)


class HiddenButtonsTests(unittest.TestCase):
    """Wiersze przyciskow HID++ zalezne od tego, co zglasza mysz (sync_hidden na atrapach)."""

    @classmethod
    def setUpClass(cls):
        cls.g = import_gui()

    def make_win(self, device, cfg=None):
        g = self.g

        class Row:
            def __init__(self, win, key, title, icon):
                self.key, self.visible, self.cfg = key, True, None
                self.picker = SimpleNamespace(cat=lambda: ("default", "Domyślnie"))
            def load(self, c):
                self.cfg = c
                if c:
                    self.picker = SimpleNamespace(cat=lambda: ("custom", "Własne"))
            def set_visible(self, v): self.visible = v
            def get_visible(self): return self.visible

        class Group:
            def __init__(self): self.added, self.visible = [], None
            def add(self, row): self.added.append(row)
            def set_visible(self, v): self.visible = v

        win = SimpleNamespace(groups={"Ukryte przyciski (HID++)": Group()}, device=device,
                              _cfg_buttons=cfg or {}, rows={}, diagram=None)
        patcher = mock.patch.multiple(g, ButtonRow=Row, HAVE_DIAGRAM=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        g.Win.sync_hidden(win)
        return win

    def test_mysz_bez_listy_przyciskow_dostaje_domyslne_c3_c4(self):
        win = self.make_win({})
        self.assertEqual(set(win.rows), {self.g.hid_key(0xC3), self.g.hid_key(0xC4)})
        self.assertTrue(win.groups["Ukryte przyciski (HID++)"].visible)

    def test_lista_z_myszy_pomija_przyciski_standardowe(self):
        dev = {"controls": [{"cid": 0xC3, "divertable": True}, {"cid": 0x50, "divertable": True},
                            {"cid": 0xD7, "divertable": True}, {"cid": 0xFF, "divertable": False}]}
        win = self.make_win(dev)
        self.assertEqual(set(win.rows), {self.g.hid_key(0xC3), self.g.hid_key(0xD7)})

    def test_wczesniej_ustawiony_przycisk_zostaje_widoczny(self):
        dev = {"controls": [{"cid": 0xC3, "divertable": True}]}
        key = self.g.hid_key(0xC4)
        win = self.make_win(dev, {key: {"type": "key"}})
        self.assertIn(key, win.rows)
        self.assertTrue(win.rows[key].visible)

    def test_brak_przyciskow_ukrywa_grupe(self):
        win = self.make_win({"controls": [{"cid": 0x50, "divertable": True}]})
        self.assertEqual(win.rows, {})
        self.assertFalse(win.groups["Ukryte przyciski (HID++)"].visible)


def import_tray():
    try:
        import importlib
        return importlib.import_module("mxremap_tray")
    except Exception:                                    # brak gi/cairo - testy zasobnika pomijamy
        return None


class TrayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.T = import_tray()
        if cls.T is None:
            raise unittest.SkipTest("brak python3-gobject/python3-cairo")

    def test_widok_zaleznie_od_stanu(self):
        T, now = self.T, 1000.0
        v = lambda **kw: T.battery_view(dict(kw, time=now), "MX", now=now + 5)
        self.assertEqual((v(percent=87, status="Discharging")["text"], v(percent=87)["color"]), ("87", T.GREEN))
        self.assertEqual(v(percent=20, status="Discharging")["color"], T.ORANGE)
        self.assertEqual(v(percent=9, status="Discharging")["color"], T.RED)
        self.assertEqual(v(percent=9, status="Discharging")["status"], "NeedsAttention")
        self.assertEqual(v(percent=9, status="Charging")["color"], T.BLUE)               # ladowanie wygrywa z niskim
        self.assertEqual(v(percent=9, status="Charging")["status"], "Active")
        self.assertIn("niska", v(level="Low")["tip"])
        self.assertEqual(v(level="Low")["text"], "!")

    def test_brak_swiezych_danych_to_passive(self):
        T = self.T
        self.assertEqual(T.battery_view({}, "MX")["status"], "Passive")
        old = {"percent": 50, "time": 100.0}
        self.assertEqual(T.battery_view(old, "MX", now=100.0 + T.STALE_AFTER + 1)["status"], "Passive")
        self.assertEqual(T.battery_view(old, "MX", now=100.0 + 5)["status"], "Active")

    def test_ikona_ma_poprawny_format_argb(self):
        T = self.T
        for size in (22, 32, 48):
            w, h, data = T.render_icon(size, "87", T.GREEN)
            self.assertEqual((w, h, len(data)), (size, size, size * size * 4))
        w, h, data = T.render_icon(32, "87", T.RED)
        px = lambda x, y: tuple(data[4 * (y * 32 + x):4 * (y * 32 + x) + 4])
        self.assertEqual(px(0, 0)[0], 0)                                                 # naroze: przezroczyste (A=0)
        a, r, g, b = px(4, 16)                                                           # lewy brzeg: kolor tla
        self.assertEqual(a, 255)
        self.assertGreater(r, 200); self.assertLess(g, 60)                               # czerwony, nie zamienione kanaly

    def test_integracja_z_magistrala_dbus(self):
        """Ikona rejestruje sie w (udawanym) panelu, wystawia wlasciwosci, odswieza sie i reaguje na klikniecie."""
        import json, shutil, subprocess
        if not shutil.which("dbus-run-session"):
            self.skipTest("brak dbus-run-session")
        helper = os.path.join(ROOT, "tests", "sni_integration.py")
        r = subprocess.run(["dbus-run-session", "--", sys.executable, helper], capture_output=True, text=True, timeout=60)
        line = next((l for l in r.stdout.splitlines() if l.startswith("RESULT:")), None)
        self.assertIsNotNone(line, r.stdout + r.stderr)
        out = json.loads(line[len("RESULT:"):])
        self.assertNotIn("error", out)
        self.assertTrue(out["name_ok"], out["registered"])                               # zarejestrowana mimo pozniejszego panelu
        f = out["first"]
        self.assertEqual((f["Id"], f["Category"], f["Status"], f["Title"]),
                         ("mxremap-battery", "Hardware", "Active", "MX Anywhere 3"))
        self.assertIn("87%", f["tooltip"])
        self.assertEqual([s[:2] for s in f["sizes"]], [[22, 22], [32, 32], [48, 48]])
        self.assertEqual(out["after_low"]["Status"], "NeedsAttention")
        self.assertIn("12%", out["after_low"]["tooltip"])
        self.assertTrue(out["after_low"]["icon_changed"])
        self.assertEqual(out["stale"]["Status"], "Passive")
        self.assertTrue(out["gui_opened"])                                               # klikniecie otwiera program
        self.assertEqual(out["tray_exit"], 0)                                            # czyste zamkniecie po SIGTERM


class TrayUnitTests(unittest.TestCase):
    def test_jednostka_ikony(self):
        u = m.tray_unit("/usr/bin/python3", "/opt/mój folder/mxremap_tray.py")
        self.assertIn('ExecStart="/usr/bin/python3" "/opt/mój folder/mxremap_tray.py"', u)
        self.assertIn("WantedBy=graphical-session.target", u)
        self.assertIn("Restart=on-failure", u)


if __name__ == "__main__":
    unittest.main(verbosity=2)
