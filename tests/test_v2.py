"""Tests for 2.0: models, indicators, registry, player order, launch detection, web API."""
import json
import os
import struct
import sys
import tempfile
import time
import unittest
import urllib.request
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import padnamer as p  # noqa: E402
from test_padnamer import FakeSys  # noqa: E402

N64_KEYS = set(p.NSO_LAYOUTS["n64"])
NES_KEYS = set(p.NSO_LAYOUTS["nes"])
HAT = {p.ABS_HAT0X, p.ABS_HAT0Y}
PAD_KEYS = {0x130, 0x131, 0x133, 0x134, 0x136, 0x137, 0x13a, 0x13b, 0x13c}
MAC_N64, MAC_NES, MAC_DS, MAC_XB = ("98:b6:e9:00:00:01", "98:b6:e9:00:00:02",
                                    "a0:5a:5c:00:00:03", "44:16:22:00:00:04")


class FakeBridge:
    def __init__(self, pad, node, name, layout=None):
        self.pad, self.node, self.name, self.layout = pad, node, name, layout
        self.rfd, self.vfd = os.pipe()

    def close(self):
        os.close(self.rfd)
        os.close(self.vfd)


class Env(unittest.TestCase):
    """Fake home + sysfs + daemon in registry mode."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.ra = os.path.join(self.home, ".var/app/org.libretro.RetroArch/config/retroarch")
        os.makedirs(self.ra)
        self.cfg_path = os.path.join(self.home, ".config", "padnamer", "config.toml")
        os.makedirs(os.path.dirname(self.cfg_path))
        with open(self.cfg_path, "w") as f:
            f.write("[settings]\n")
        self.fs = FakeSys()
        self.patches = [mock.patch.object(p.pwd, "getpwuid", lambda uid: mock.Mock(pw_dir=self.home)),
                        self.fs.patch(), mock.patch.object(p, "Bridge", FakeBridge),
                        mock.patch.object(p, "hide_node", lambda path: None),
                        mock.patch.object(p, "_udevadm", lambda *a: None)]
        for x in self.patches:
            x.start()
        self.addCleanup(lambda: [x.stop() for x in reversed(self.patches)])

    def daemon(self, **kw):
        d = p.Daemon(self.cfg_path, manage_udev=False, status_path=None, watch_launches=False,
                     web=False, **kw)
        d.load_config(initial=True)
        return d

    def names_in_order(self, d):
        return [d.pad_by_cid(c).name for c in d.creation_order()]


# --------------------------------------------------------------------------
class ModelTests(unittest.TestCase):
    def setUp(self):
        self.fs = FakeSys()

    def node(self, **kw):
        with self.fs.patch():
            return p.NodeInfo(self.fs.add(**kw))

    def test_detection(self):
        cases = [
            (dict(name="Xbox Wireless Controller", vendor=0x045E, product=0x0B13), "xbox_series"),
            (dict(name="DualSense Wireless Controller", vendor=0x054C, product=0x0CE6), "dualsense"),
            (dict(name="Wireless Controller", vendor=0x054C, product=0x09CC), "dualshock4"),
            (dict(name="Nintendo Switch Pro Controller", vendor=0x057E, product=0x2009), "switch_pro"),
            (dict(name="Joy-Con (L)", vendor=0x057E, product=0x2006), "joycon_l"),
            # NES pads report Joy-Con IDs - the button set decides
            (dict(name="NES Controller (L)", vendor=0x057E, product=0x2006, keys=NES_KEYS, abs_=HAT), "nso_nes"),
            (dict(name="N64 Controller", vendor=0x057E, product=0x2019, keys=N64_KEYS), "nso_n64"),
            (dict(name="Nintendo Wii Remote", vendor=0x057E, product=0x0306), "wii_remote"),
            (dict(name="8BitDo Ultimate", vendor=0x2DC8, product=0x3106), "8bitdo"),
            (dict(name="Some Pad", vendor=0x1234, product=0x5678), "generic"),
        ]
        for kw, want in cases:
            with self.subTest(want):
                self.assertEqual(p.detect_model(self.node(**kw)), want)

    def test_every_model_has_valid_home_systems(self):
        for mid, (label, short, family, home) in p.MODELS.items():
            for h in home:
                self.assertIn(h, p.SYSTEM_LABELS, f"{mid}: {h}")


class IndicatorTests(unittest.TestCase):
    def make(self, names, multicolor=()):
        d = tempfile.mkdtemp()
        for n in names:
            os.makedirs(os.path.join(d, "leds", n))
            with open(os.path.join(d, "leds", n, "brightness"), "w") as f:
                f.write("0\n")
        for n in multicolor:
            os.makedirs(os.path.join(d, "leds", n))
            for fn, v in (("brightness", "0"), ("multi_intensity", "0 0 0"), ("max_brightness", "255")):
                with open(os.path.join(d, "leds", n, fn), "w") as f:
                    f.write(v + "\n")
        return d

    def test_dualsense_five_leds_and_lightbar(self):
        leds = [f"input42:white:player-{i}" for i in range(1, 6)]
        d = self.make(leds, ["input42:rgb:indicator"])
        self.assertEqual(p.describe_indicators(d), {"player_leds": 5, "led_kind": "playstation", "lightbar": True})
        out = p.apply_indicators(d, 2, "switch")
        self.assertEqual(out, {"pattern": "○●○●○", "colour": "#ff0000"})
        bar = os.path.join(d, "leds", "input42:rgb:indicator")
        self.assertEqual(open(os.path.join(bar, "multi_intensity")).read(), "255 0 0")
        self.assertEqual(open(os.path.join(bar, "brightness")).read(), "64")
        self.assertEqual(p.apply_indicators(d, 1, "switch")["pattern"], "○○●○○")

    def test_ds4_lightbar_only(self):
        d = self.make([], ["input7:rgb:indicator"])
        self.assertEqual(p.apply_indicators(d, 4, "switch"), {"pattern": "", "colour": "#ff00ff"})
        self.assertEqual(p.describe_indicators(d)["player_leds"], 0)

    def test_off(self):
        d = self.make([], ["input7:rgb:indicator"])
        self.assertEqual(p.apply_indicators(d, 1, "off"), {"pattern": "", "colour": ""})


# --------------------------------------------------------------------------
class RegistryTests(Env):
    def reg(self):
        return p.Registry(p.UserFiles(self.cfg_path))

    def node(self, **kw):
        return p.NodeInfo(self.fs.add(**kw))

    def test_auto_register_bluetooth_names_and_homes(self):
        r = self.reg()
        a = r.observe(self.node(name="N64 Controller", mac=MAC_N64, keys=N64_KEYS, product=0x2019))
        b = r.observe(self.node(name="N64 Controller", mac="98:b6:e9:00:00:09", keys=N64_KEYS, product=0x2019))
        self.assertEqual((a["name"], b["name"]), ("N64 A", "N64 B"))
        self.assertEqual(r.home_of(a), ["n64"])
        self.assertTrue(a["manage"])
        self.assertEqual(r.order, [MAC_N64, "98:b6:e9:00:00:09"])
        # seen again: same entry, nothing new
        self.assertIs(r.observe(self.node(name="N64 Controller", mac=MAC_N64, keys=N64_KEYS)), a)
        self.assertEqual(len(r.controllers), 2)

    def test_usb_pads_are_remembered_but_not_managed(self):
        r = self.reg()
        e = r.observe(self.node(name="Steam Deck", vendor=0x28DE, product=0x1205, bus=3, hid=False,
                                phys="usb-0000:04:00.4-3/input2"))
        self.assertEqual(e["id"], "phys:usb-0000:04:00.4-3/input2")
        self.assertEqual(e["detected_model"], "steam_deck")
        self.assertFalse(e["manage"])

    def test_identical_no_mac_pads_get_slots(self):
        r = self.reg()
        n1 = self.node(name="Nintendo GameCube Controller", hid=False, product=0x2073, bus=3)
        a = r.observe(n1)
        b = r.observe(n1, in_use={a["id"]})
        self.assertEqual((a["id"], b["id"]), ("name:Nintendo GameCube Controller#1",
                                              "name:Nintendo GameCube Controller#2"))
        self.assertIs(r.observe(n1), a)      # #1 free again -> reused

    def test_update_validation_and_persistence(self):
        r = self.reg()
        e = r.observe(self.node(name="DualSense Wireless Controller", mac=MAC_DS, vendor=0x054C, product=0x0CE6))
        r.observe(self.node(name="N64 Controller", mac=MAC_N64, keys=N64_KEYS))
        r.update(MAC_DS, {"name": "Blue pad", "home": ["genesis", "psx"], "model": "dualshock4"})
        self.assertEqual(e["home"], ["megadrive", "psx"])
        self.assertEqual(r.model_of(e), "dualshock4")
        for bad in ({"name": "N64 A"}, {"name": ""}, {"model": "atari"}, {"home": ["vectrex"]},
                    {"present_as": "xbox"}, {"leds": "rainbow"}):
            with self.subTest(bad), self.assertRaises(ValueError):
                r.update(MAC_DS, bad)
        r.update(MAC_DS, {"model": "auto", "home": None})
        self.assertEqual(r.home_of(e), p.MODELS["dualsense"][3])
        r.save()
        again = self.reg()
        self.assertEqual(again.controllers[MAC_DS]["name"], "Blue pad")
        again.forget(MAC_DS)
        self.assertNotIn(MAC_DS, again.order)

    def test_pin_and_legacy_import(self):
        with open(self.cfg_path, "w") as f:
            f.write(f'[[pad]]\nname="Pad Two"\nmac="{MAC_NES}"\nplayer=2\nsystems=["nes"]\n'
                    f'[[pad]]\nname="Pad One"\nmac="{MAC_N64}"\nplayer=1\n')
        r = self.reg()
        r.import_legacy(p.Config(self.cfg_path))
        self.assertEqual(r.order, [MAC_N64, MAC_NES])           # sorted by old player numbers
        self.assertEqual(r.controllers[MAC_NES]["home"], ["nes"])
        r.import_legacy(p.Config(self.cfg_path))                # only once
        self.assertEqual(len(r.controllers), 2)
        pin = r.pin()
        self.assertRegex(pin, r"^\d{4}$")
        self.assertEqual(r.pin(), pin)


# --------------------------------------------------------------------------
class OrderTests(unittest.TestCase):
    def engine(self, scope="session"):
        reg = mock.Mock()
        reg.order = ["xbox", "n64a", "nes", "n64b"]
        reg.controllers = {k: {} for k in reg.order}
        reg.data = {}
        return p.OrderEngine(reg, scope)

    HOMES = {"xbox": ["xbox"], "n64a": ["n64"], "nes": ["nes"], "n64b": ["n64"]}

    def test_default_and_own_system_first(self):
        e = self.engine()
        conn = {"xbox", "n64a", "nes", "n64b"}
        self.assertEqual(e.active(conn, self.HOMES), ["xbox", "n64a", "nes", "n64b"])
        e.start_session("RMG", ["n64"], 1)
        self.assertEqual(e.active(conn, self.HOMES), ["n64a", "n64b", "xbox", "nes"])
        self.assertEqual(e.active({"xbox", "nes"}, self.HOMES), ["xbox", "nes"])   # no N64 pad: fallback
        e.end_session()
        self.assertEqual(e.active(conn, self.HOMES), ["xbox", "n64a", "nes", "n64b"])

    def test_manual_set_between_games_lasts_for_the_next_game(self):
        e = self.engine()
        conn = {"xbox", "n64a", "nes"}
        e.set_manual(["nes", "xbox"])
        self.assertTrue(e.manual["pending"])
        self.assertEqual(e.active(conn, self.HOMES), ["nes", "xbox", "n64a"])
        e.start_session("RMG", ["n64"], 1)
        self.assertEqual(e.active(conn, self.HOMES), ["nes", "xbox", "n64a"])   # manual beats own-first
        e.end_session()
        self.assertIsNone(e.manual)
        self.assertEqual(e.active(conn, self.HOMES), ["xbox", "n64a", "nes"])

    def test_manual_set_during_game_ends_with_it(self):
        e = self.engine()
        e.start_session("RetroArch", ["nes"], 1)
        e.set_manual(["n64a"])
        self.assertFalse(e.manual["pending"])
        e.end_session()
        self.assertIsNone(e.manual)

    def test_persistent_scope(self):
        e = self.engine("persistent")
        e.set_manual(["nes"])
        e.start_session("x", [], 1)
        e.end_session()
        self.assertEqual(e.manual["ids"], ["nes"])
        self.assertEqual(e.registry.data["manual"], {"ids": ["nes"]})
        e.clear_manual()
        self.assertNotIn("manual", e.registry.data)


# --------------------------------------------------------------------------
class LaunchDetectionTests(unittest.TestCase):
    def proc(self, procs):
        d = tempfile.mkdtemp()
        for pid, (comm, argv) in procs.items():
            os.makedirs(os.path.join(d, str(pid)))
            with open(os.path.join(d, str(pid), "comm"), "w") as f:
                f.write(comm + "\n")
            with open(os.path.join(d, str(pid), "cmdline"), "wb") as f:
                f.write(b"\0".join(a.encode() for a in argv) + b"\0")
        return d

    def test_classify(self):
        d = self.proc({
            1: ("retroarch", ["/app/bin/retroarch", "-L",
                              "/home/deck/.var/app/org.libretro.RetroArch/config/retroarch/cores/mupen64plus_next_libretro.so",
                              "/run/media/mmcblk0p1/Emulation/roms/n64/mario.z64"]),
            2: ("retroarch", ["retroarch"]),                                    # menu, no game
            3: ("dolphin", ["/usr/bin/dolphin", "/home/deck"]),                  # KDE file manager
            4: ("dolphin-emu", ["/app/bin/dolphin-emu", "-b", "-e", "/roms/wii/game.wbfs"]),
            5: ("AppRun", ["/home/deck/Applications/Cemu.AppImage", "-g", "game.wux"]),
            6: ("ares", ["/usr/bin/ares", "--system", "x", "/roms/snes/zelda.sfc"]),
            7: ("pcsx2-qt", ["/app/bin/pcsx2-qt", "-batch", "game.iso"]),
            8: ("retroarch", ["retroarch", "--libretro=/cores/genesis_plus_gx_libretro.so", "sonic.md"]),
            9: ("bash", ["/bin/bash", "/home/deck/script.sh"]),
        })
        c = lambda pid: p.classify_process(pid, d)
        self.assertEqual(c(1), {"emulator": "RetroArch (Mupen64Plus-Next)", "systems": ["n64"],
                                "core": "Mupen64Plus-Next"})
        self.assertIsNone(c(2))
        self.assertIsNone(c(3))
        self.assertEqual(c(4)["systems"], ["wii"])
        self.assertEqual(c(5)["systems"], ["wiiu"])
        self.assertEqual(c(6)["systems"], ["snes"])
        self.assertEqual(c(7)["systems"], ["ps2"])
        self.assertEqual(c(8)["core"], "Genesis Plus GX")
        self.assertIsNone(c(9))

    def test_unknown_core_reads_info_file(self):
        cores = tempfile.mkdtemp()
        with open(os.path.join(cores, "neocd_libretro.info"), "w") as f:
            f.write('display_name = "SNK - Neo Geo CD (NeoCD)"\ncorename = "NeoCD"\n')
        d = self.proc({1: ("retroarch", ["retroarch", "-L", os.path.join(cores, "neocd_libretro.so"), "x.cue"])})
        self.assertEqual(p.classify_process(1, d)["core"], "NeoCD")

    def test_netlink_parsing(self):
        w = p.ProcWatch.__new__(p.ProcWatch)

        def msg(what, a, b):
            ev = struct.pack("=IIQ", what, 0, 0) + struct.pack("=ii", a, b) + b"\0" * 16
            cn = struct.pack("=IIIIHH", 1, 1, 0, 0, len(ev), 0)
            return struct.pack("=IHHII", 16 + len(cn) + len(ev), 3, 0, 0, 0) + cn + ev

        data = msg(p.ProcWatch.PROC_EVENT_EXEC, 1234, 1234) + msg(p.ProcWatch.PROC_EVENT_EXIT, 1235, 1234) + \
            msg(p.ProcWatch.PROC_EVENT_EXIT, 1234, 1234)
        sock = mock.Mock()
        sock.recv.side_effect = [data, BlockingIOError()]
        w.sock = sock
        self.assertEqual(w.read_events(), [("exec", 1234), ("exit", 1234)])   # thread exit ignored


# --------------------------------------------------------------------------
class DaemonFlowTests(Env):
    def add_pads(self):
        self.ev_xbox = self.fs.add("Xbox Wireless Controller", MAC_XB, vendor=0x045E, product=0x0B13,
                                   keys=PAD_KEYS, ff=[p.FF_RUMBLE])
        self.ev_nes = self.fs.add("NES Controller", MAC_NES, product=0x2006, keys=NES_KEYS, abs_=HAT)
        self.ev_n64 = self.fs.add("N64 Controller", MAC_N64, product=0x2019, keys=N64_KEYS,
                                  abs_={0, 1} | HAT, ff=[p.FF_RUMBLE])

    def test_auto_register_and_default_order(self):
        self.add_pads()
        d = self.daemon()
        d.scan()
        self.assertEqual(self.names_in_order(d), ["Xbox A", "NES A", "N64 A"])
        reg = json.load(open(os.path.join(self.home, ".config/padnamer/controllers.json")))
        self.assertEqual(reg["order"], [MAC_XB, MAC_NES, MAC_N64])
        self.assertEqual(d.players(), {MAC_XB: 1, MAC_NES: 2, MAC_N64: 3})

    def test_game_launch_puts_own_pad_first_then_restores(self):
        self.add_pads()
        d = self.daemon()
        d.scan()
        kills = []
        info = {"emulator": "RetroArch (Mupen64Plus-Next)", "systems": ["n64"], "core": "Mupen64Plus-Next"}
        with mock.patch.object(p, "classify_process", lambda pid: info), \
                mock.patch.object(p.os, "kill", lambda pid, sig: kills.append(sig)):
            d.on_exec(4242)
        self.assertEqual(kills, [p.signal.SIGSTOP, p.signal.SIGCONT])        # paused while reordering
        self.assertEqual(self.names_in_order(d), ["N64 A", "Xbox A", "NES A"])
        core = os.path.join(self.ra, "config/Mupen64Plus-Next/Mupen64Plus-Next.cfg")
        text = open(core).read()
        self.assertIn('input_player1_reserved_device = "N64 A"', text)
        self.assertIn('input_player2_reserved_device = "Xbox A"', text)

        # a pad joining mid-game doesn't shuffle anyone
        before = self.names_in_order(d)
        self.fs.add("DualSense Wireless Controller", MAC_DS, vendor=0x054C, product=0x0CE6, keys=PAD_KEYS)
        d.scan()
        self.assertEqual(self.names_in_order(d), before + ["DualSense A"])

        with mock.patch.object(p, "pid_alive", lambda pid: False):
            d.check_session()
        self.assertIsNone(d.order.session)
        self.assertNotIn("input_player1", open(core).read())
        d.scan()                                                             # between games: default order
        self.assertEqual(self.names_in_order(d), ["Xbox A", "NES A", "N64 A", "DualSense A"])

    def test_manual_order_and_move(self):
        self.add_pads()
        d = self.daemon()
        d.scan()
        d.api_set_order([MAC_N64, MAC_NES], "now")
        self.assertEqual(self.names_in_order(d), ["N64 A", "NES A", "Xbox A"])
        d.api_move(MAC_XB, -1)
        self.assertEqual(self.names_in_order(d), ["N64 A", "Xbox A", "NES A"])
        d.api_reset_order()
        self.assertEqual(self.names_in_order(d), ["Xbox A", "NES A", "N64 A"])
        d.api_set_order([MAC_NES, MAC_N64, MAC_XB], "default")               # new default order
        self.assertEqual(self.names_in_order(d), ["NES A", "N64 A", "Xbox A"])
        st = d.state_dict()
        self.assertEqual(st["players"], [MAC_NES, MAC_N64, MAC_XB])
        self.assertEqual({c["id"]: c["player"] for c in st["controllers"]},
                         {MAC_NES: 1, MAC_N64: 2, MAC_XB: 3})

    def test_editing_a_controller_renames_it(self):
        self.add_pads()
        d = self.daemon()
        d.scan()
        d.api_update_controller(MAC_N64, {"name": "Mario's pad", "present_as": "switch_pro"})
        names = sorted(b.name for b in d.bridges.values())
        self.assertIn("Mario's pad", names)
        n64 = next(b for b in d.bridges.values() if b.pad.cid == MAC_N64)
        self.assertEqual(n64.layout.kind, "n64 -> switch_pro")
        d.api_update_controller(MAC_NES, {"manage": False})
        self.assertNotIn(MAC_NES, d.connected_cids())

    def test_launch_wrapper_session(self):
        self.add_pads()
        d = self.daemon()
        d.scan()
        d.api_session(["genesis"], os.getpid())
        self.assertEqual(d.order.session["systems"], ["megadrive"])

    def test_identify_flashes_then_restores(self):
        self.add_pads()
        d = self.daemon()
        d.scan()
        with mock.patch.object(p, "ioctl", lambda *a: 0), mock.patch.object(p.os, "write", lambda fd, b: len(b)):
            d.identify(MAC_XB)
            self.assertIn(MAC_XB, d.flashes)
            d.flashes[MAC_XB]["until"] = 0
            d.process_flashes()
        self.assertNotIn(MAC_XB, d.flashes)
        with self.assertRaises(ValueError):
            d.identify("aa:aa:aa:aa:aa:aa")


# --------------------------------------------------------------------------
class WebTests(Env):
    def setUp(self):
        super().setUp()
        self.fs.add("Xbox Wireless Controller", MAC_XB, vendor=0x045E, product=0x0B13, keys=PAD_KEYS)
        self.fs.add("N64 Controller", MAC_N64, product=0x2019, keys=N64_KEYS, abs_={0, 1} | HAT)
        self.d = self.daemon()
        self.d.scan()
        self.d.publish()
        web_dir = tempfile.mkdtemp()
        with open(os.path.join(web_dir, "index.html"), "w") as f:
            f.write("<html>ui</html>")
        self.web_patch = mock.patch.object(p, "WEB_DIR", web_dir)
        self.web_patch.start()
        self.addCleanup(self.web_patch.stop)
        self.web = p.WebServer(self.d, "127.0.0.1", 0)
        self.port = self.web.httpd.server_address[1]
        self.web.thread.start()
        self.addCleanup(self.web.stop)
        # the daemon's main loop: run submitted commands
        import threading
        self.stop = threading.Event()
        self.loop = threading.Thread(target=self.pump, daemon=True)
        self.loop.start()
        self.addCleanup(self.stop.set)

    def pump(self):
        while not self.stop.is_set():
            try:
                if os.read(self.d.wake_r, 64):
                    self.d.run_commands()
            except BlockingIOError:
                time.sleep(0.01)

    def call(self, path, body=None, headers=None):
        hdrs = {"Content-Type": "application/json", "X-Padnamer": "1"}
        hdrs.update(headers or {})
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read() or b"{}") if path.startswith("/api") else r.read()
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_state_and_page(self):
        code, page = self.call("/")
        self.assertEqual((code, page), (200, b"<html>ui</html>"))
        code, st = self.call("/api/state")
        self.assertEqual(code, 200)
        self.assertEqual([c["name"] for c in st["controllers"]], ["Xbox A", "N64 A"])
        self.assertRegex(st["web"]["pin"], r"^\d{4}$")             # shown on this machine only

    def test_actions(self):
        code, _ = self.call("/api/order", {"ids": [MAC_N64, MAC_XB], "which": "now"})
        self.assertEqual(code, 200)
        self.assertEqual(self.names_in_order(self.d), ["N64 A", "Xbox A"])
        code, res = self.call("/api/controller", {"id": MAC_XB, "name": "Dad's Xbox", "home": ["xbox"]})
        self.assertEqual((code, res["controller"]["name"]), (200, "Dad's Xbox"))
        code, err = self.call("/api/controller", {"id": MAC_XB, "name": "N64 A"})
        self.assertEqual(code, 400)
        self.assertIn("already used", err["error"])
        code, _ = self.call("/api/order/reset", {})
        self.assertEqual(code, 200)

    def test_csrf_header_required(self):
        code, _ = self.call("/api/order/reset", {}, headers={"X-Padnamer": ""})
        self.assertEqual(code, 403)

    def test_pin_for_lan_clients(self):
        with mock.patch.object(p.WebServer, "is_local", staticmethod(lambda h: False)):
            code, _ = self.call("/api/state")
            self.assertEqual(code, 401)
            code, _ = self.call("/api/order/reset", {})
            self.assertEqual(code, 401)
            code, _ = self.call("/api/login", {"pin": "nope"})
            self.assertEqual(code, 403)
            req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/login",
                                         data=json.dumps({"pin": self.d.registry.pin()}).encode(),
                                         headers={"Content-Type": "application/json", "X-Padnamer": "1"})
            with urllib.request.urlopen(req, timeout=5) as r:
                cookie = r.headers["Set-Cookie"].split(";")[0]
            self.assertIn("SameSite=Strict", r.headers["Set-Cookie"])
            code, st = self.call("/api/state", headers={"Cookie": cookie})
            self.assertEqual(code, 200)
            self.assertNotIn("pin", st["web"])                         # never sent to the LAN
            code, _ = self.call("/api/session", {"systems": ["n64"], "pid": 1}, headers={"Cookie": cookie})
            self.assertEqual(code, 403)                                # launches only from this machine

    def test_lockout_after_wrong_pins(self):
        with mock.patch.object(p.WebServer, "is_local", staticmethod(lambda h: False)):
            for _ in range(5):
                self.call("/api/login", {"pin": "0000x"})
            code, _ = self.call("/api/login", {"pin": self.d.registry.pin()})
            self.assertEqual(code, 429)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class SessionHandoverTests(Env):
    def test_launcher_script_hands_over_to_emulator(self):
        self.fs.add("N64 Controller", MAC_N64, product=0x2019, keys=N64_KEYS)
        d = self.daemon()
        d.scan()
        d.start_session({"emulator": "RMG", "systems": ["n64"], "core": None}, 111)
        with mock.patch.object(p.os, "listdir", lambda path: ["111", "222", "333"] if path == "/proc" else os.listdir(path)), \
                mock.patch.object(p, "classify_process",
                                  lambda pid: {"emulator": "RMG", "systems": ["n64"], "core": None} if pid == 222 else None):
            d.session_gone()
        self.assertEqual(d.order.session["pid"], 222)
        with mock.patch.object(p, "classify_process", lambda pid: None):
            d.session_gone()
        self.assertIsNone(d.order.session)

    def test_hotplug_between_games_does_not_shuffle(self):
        self.fs.add("NES Controller", MAC_NES, product=0x2006, keys=NES_KEYS, abs_=HAT)
        d = self.daemon()
        d.scan()
        first = list(d.bridges.values())[0]
        d.api_set_order([MAC_N64, MAC_NES], "default")       # N64 pad should come first...
        self.fs.add("N64 Controller", MAC_N64, product=0x2019, keys=N64_KEYS)
        d.scan()                                               # ...but it joins without shuffling
        self.assertIn(first, d.bridges.values())
        self.assertEqual(self.names_in_order(d), ["NES A", "N64 A"])
        d.start_session({"emulator": "RMG", "systems": ["n64"], "core": None}, os.getpid())
        self.assertEqual(self.names_in_order(d), ["N64 A", "NES A"])   # fixed at the next game


class HardeningTests(Env):
    def test_names_cannot_be_paths(self):
        r = p.Registry(p.UserFiles(self.cfg_path))
        r.observe(p.NodeInfo(self.fs.add("N64 Controller", MAC_N64, keys=N64_KEYS)))
        for bad in ("../../etc/passwd", "a/b", ".hidden"):
            with self.subTest(bad), self.assertRaises(ValueError):
                r.update(MAC_N64, {"name": bad})

    def test_motion_only_devices_not_registered(self):
        r = p.Registry(p.UserFiles(self.cfg_path))
        imu = p.NodeInfo(self.fs.add("Steam Deck Motion Sensors", keys=(), abs_={0, 1, 2}, bus=3, hid=False,
                                     phys="usb-0000:04:00.4-3/input2", props={p.INPUT_PROP_ACCELEROMETER}))
        self.assertIsNone(r.observe(imu))
        self.assertEqual(r.controllers, {})
