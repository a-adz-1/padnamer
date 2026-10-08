"""Tests for player LEDs, Switch Pro layout, RetroArch/Dolphin management and status."""
import os
import struct
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import padnamer as p  # noqa: E402
from test_padnamer import FakeSys, FakeKernel, write_config  # noqa: E402

N64_KEYS = set(p.NSO_LAYOUTS["n64"])
N64_ABS = {p.ABS_X, p.ABS_Y, p.ABS_HAT0X, p.ABS_HAT0Y}
MD_KEYS = set(p.NSO_LAYOUTS["megadrive"])
MAC1, MAC2 = "98:b6:e9:00:00:01", "98:b6:e9:00:00:02"


def pad(**kw):
    raw = {"name": "Pad", "mac": MAC1}
    raw.update(kw)
    return p.PadConfig(raw, {}, 0, p.BUILTIN_SYSTEMS)


# --------------------------------------------------------------------------
class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.fs = FakeSys()

    def node(self, **kw):
        with self.fs.patch():
            return p.NodeInfo(self.fs.add(**kw))

    def test_detects_every_nso_type(self):
        for kind, table in p.NSO_LAYOUTS.items():
            n = self.node(name=kind, keys=set(table), abs_={p.ABS_HAT0X, p.ABS_HAT0Y})
            self.assertEqual(p.detect_nso_type(n), kind)
        pro = self.node(name="Pro", keys=set(p.PRO_BUTTONS.values()),
                        abs_={0, 1, 3, 4, 0x10, 0x11})
        self.assertEqual(p.detect_nso_type(pro), "pro")
        other = self.node(name="8BitDo", keys=N64_KEYS, vendor=0x2DC8)
        self.assertIsNone(p.detect_nso_type(other))

    def test_n64_as_switch_pro_matches_what_the_switch_sees(self):
        n = self.node(name="Nintendo N64 Controller", keys=N64_KEYS, abs_=N64_ABS, ff=[p.FF_RUMBLE])
        lay = p.build_layout(pad(present_as="switch_pro"), n)
        self.assertEqual((lay.vendor, lay.product), (0x057E, 0x2009))
        self.assertEqual(lay.keys, set(p.PRO_BUTTONS.values()))
        self.assertEqual(set(lay.abs), {0, 1, 3, 4, 0x10, 0x11})
        m = lay.keymap
        self.assertEqual(m[p.BTN_SOUTH], p.BTN_EAST)      # N64 A  -> Switch A
        self.assertEqual(m[p.BTN_EAST], p.BTN_SOUTH)      # N64 B  -> Switch B
        self.assertEqual(m[p.BTN_TL2], p.BTN_TL2)         # Z      -> ZL
        self.assertEqual(m[p.BTN_TR2], p.BTN_THUMBL)      # ZR     -> left stick click
        self.assertEqual(m[p.BTN_SELECT], p.BTN_WEST)     # C-up   -> Y
        self.assertEqual(m[p.BTN_NORTH], p.BTN_TR2)       # C-down -> ZR
        self.assertEqual(m[p.BTN_WEST], p.BTN_NORTH)      # C-left -> X
        self.assertEqual(m[p.BTN_C], p.BTN_SELECT)        # C-right-> Minus
        self.assertEqual(len(set(m.values())), len(m))   # no two buttons collide

    def test_megadrive_as_switch_pro(self):
        n = self.node(name="MD", keys=MD_KEYS, abs_={0x10, 0x11})
        m = p.build_layout(pad(present_as="switch_pro"), n).keymap
        self.assertEqual(m[p.BTN_WEST], p.BTN_EAST)       # MD A -> Switch A
        self.assertEqual(m[p.BTN_EAST], p.BTN_TR)         # MD C -> R

    def test_no_layout_cases(self):
        n = self.node(name="N64", keys=N64_KEYS, abs_=N64_ABS)
        self.assertIsNone(p.build_layout(pad(), n))                     # original
        imu = self.node(name="Pro (IMU)", keys=(), abs_={0, 1, 2}, props={p.INPUT_PROP_ACCELEROMETER})
        self.assertIsNone(p.build_layout(pad(present_as="switch_pro"), imu))
        other = self.node(name="8BitDo", keys={0x130, 0x131}, vendor=0x2DC8)
        with self.assertLogs("padnamer", "WARNING"):
            self.assertIsNone(p.build_layout(pad(present_as="switch_pro"), other))

    def test_bridge_translates_events_and_builds_pro_device(self):
        n = self.node(name="Nintendo N64 Controller", keys=N64_KEYS, abs_=N64_ABS, ff=[p.FF_RUMBLE])
        kernel, written, setups = FakeKernel(), [], {}

        def spy(fd, req, arg=0):
            if req == p.UI_DEV_SETUP:
                setups["s"] = p.uinput_setup.from_buffer_copy(arg)
            return kernel.ioctl(fd, req, arg)

        opens = {"/dev/uinput": FakeKernel.VIRT_FD, n.devnode: FakeKernel.REAL_FD}
        with mock.patch.object(p, "ioctl", spy), \
                mock.patch.object(p.os, "open", lambda path, *a: opens[path]), \
                mock.patch.object(p.os, "close", lambda fd: None):
            b = p.Bridge(pad(name="N64 P1", present_as="switch_pro"), n, "N64 P1",
                         p.build_layout(pad(present_as="switch_pro"), n))
        self.assertEqual((setups["s"].id.vendor, setups["s"].id.product), (0x057E, 0x2009))
        virt = [c for c in kernel.calls if c[0] == FakeKernel.VIRT_FD]
        self.assertEqual([c[1] for c in virt].count(p.UI_ABS_SETUP), 6)
        self.assertIn((FakeKernel.VIRT_FD, p.UI_SET_BIT(101), p.BTN_THUMBR), virt)   # Pro-only button
        self.assertNotIn((FakeKernel.VIRT_FD, p.UI_SET_BIT(101), p.BTN_C), virt)     # N64-only code gone
        # layout axes come from the Pro spec, not the real device
        self.assertFalse(any(c[0] == FakeKernel.REAL_FD and c[1] in {p.EVIOCGABS(a) for a in range(64)}
                             for c in kernel.calls))

        ev = lambda t, c, v: struct.pack(p.EVENT_FMT, 5, 6, t, c, v)
        raw = ev(p.EV_MSC, 4, 99) + ev(p.EV_KEY, p.BTN_SOUTH, 1) + ev(p.EV_KEY, p.BTN_C, 1) + \
            ev(p.EV_ABS, p.ABS_X, -1200) + ev(p.EV_SYN, 0, 0)
        out = b._translate(raw)
        got = [struct.unpack_from(p.EVENT_FMT, out, o)[2:] for o in range(0, len(out), p.EVENT_SIZE)]
        self.assertEqual(got, [(p.EV_KEY, p.BTN_EAST, 1), (p.EV_KEY, p.BTN_SELECT, 1),
                               (p.EV_ABS, p.ABS_X, -1200), (p.EV_SYN, 0, 0)])


# --------------------------------------------------------------------------
class LedTests(unittest.TestCase):
    def make(self, names):
        d = tempfile.mkdtemp()
        for n in names:
            os.makedirs(os.path.join(d, "leds", n))
            with open(os.path.join(d, "leds", n, "brightness"), "w") as f:
                f.write("0\n")
        return d

    def state(self, d, names):
        return [open(os.path.join(d, "leds", n, "brightness")).read().strip() for n in names]

    def test_switch_patterns(self):
        names = [f"0005:057E:2019.0001:green:player-{i}" for i in range(1, 5)]
        d = self.make(names + ["0005:057E:2019.0001:blue:home"])
        self.assertEqual(p.apply_player_leds(d, 2, "switch"), "●●○○")
        self.assertEqual(self.state(d, names), ["1", "1", "0", "0"])
        self.assertEqual(p.apply_player_leds(d, 2, "single"), "○●○○")
        self.assertEqual(self.state(d, names), ["0", "1", "0", "0"])
        self.assertEqual(p.apply_player_leds(d, 5, "switch"), "●○○●")
        self.assertEqual(p.apply_player_leds(d, 3, "off"), "")
        self.assertEqual(self.state(d, names), ["1", "0", "0", "1"])   # untouched

    def test_wii_remote_uses_single_led(self):
        names = [f"0005:057E:0306.0002:blue:p{i}" for i in range(4)]
        d = self.make(names)
        self.assertEqual(p.apply_player_leds(d, 3, "switch"), "○○●○")
        self.assertEqual(self.state(d, names), ["0", "0", "1", "0"])

    def test_no_leds(self):
        self.assertEqual(p.apply_player_leds(tempfile.mkdtemp(), 1, "switch"), "")
        self.assertEqual(p.apply_player_leds(None, 1, "switch"), "")


# --------------------------------------------------------------------------
class ConfigFeatureTests(unittest.TestCase):
    def test_systems_players_aliases(self):
        cfg = p.Config(write_config(f"""
            [[pad]]
            name = "MD"
            mac = "{MAC1}"
            player = 1
            systems = ["Genesis", "sms"]
            present_as = "switch_pro"
            leds = "single"
        """))
        pd = cfg.pads[0]
        self.assertEqual(pd.systems, ["megadrive", "mastersystem"])
        self.assertEqual((pd.player, pd.present_as, pd.leds), (1, "switch_pro", "single"))

    def test_custom_system(self):
        cfg = p.Config(write_config(f"""
            [systems.arcade]
            retroarch_cores = ["FinalBurn Neo", "MAME"]
            [systems.n64]
            retroarch_cores = ["Mupen64Plus-Next"]
            [[pad]]
            name = "Stick"
            mac = "{MAC1}"
            player = 1
            systems = ["arcade", "n64"]
        """))
        self.assertEqual(cfg.systems["arcade"]["retroarch"], ["FinalBurn Neo", "MAME"])
        self.assertEqual(cfg.systems["n64"]["retroarch"], ["Mupen64Plus-Next"])

    def test_errors(self):
        bad = {
            "unknown system": f'[[pad]]\nname="a"\nmac="{MAC1}"\nplayer=1\nsystems=["dreamcastt"]',
            "player range": f'[[pad]]\nname="a"\nmac="{MAC1}"\nplayer=9',
            "present_as": f'[[pad]]\nname="a"\nmac="{MAC1}"\npresent_as="xbox"',
            "seat clash": f'[[pad]]\nname="a"\nmac="{MAC1}"\nplayer=1\nsystems=["n64"]\n'
                          f'[[pad]]\nname="b"\nmac="{MAC2}"\nplayer=1\nsystems=["n64"]',
        }
        for label, text in bad.items():
            with self.subTest(label), self.assertRaises(ValueError):
                p.Config(write_config(text))

    def test_same_player_different_systems_is_fine(self):
        p.Config(write_config(f'[[pad]]\nname="a"\nmac="{MAC1}"\nplayer=1\nsystems=["n64"]\n'
                              f'[[pad]]\nname="b"\nmac="{MAC2}"\nplayer=1\nsystems=["nes"]'))


# --------------------------------------------------------------------------
class HomeFixture(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.ra = os.path.join(self.home, ".var/app/org.libretro.RetroArch/config/retroarch")
        self.dol = os.path.join(self.home, ".var/app/org.DolphinEmu.dolphin-emu/config/dolphin-emu")
        os.makedirs(self.ra)
        os.makedirs(self.dol)
        self.pw = mock.patch.object(p.pwd, "getpwuid", lambda uid: mock.Mock(pw_dir=self.home))
        self.pw.start()
        self.addCleanup(self.pw.stop)

    def config(self, text):
        path = os.path.join(self.home, "config.toml")
        with open(path, "w") as f:
            f.write(textwrap.dedent(text))
        return path

    def read(self, *parts):
        with open(os.path.join(*parts)) as f:
            return f.read()


TWO_N64 = f"""
[[pad]]
name = "N64 P1"
mac = "{MAC1}"
player = 1
systems = ["n64"]

[[pad]]
name = "N64 P2"
mac = "{MAC2}"
player = 2
systems = ["n64"]
"""


class RetroArchTests(HomeFixture):
    def test_session_reservations_written_and_removed(self):
        cfg_path = self.config("[settings]\n")
        core_cfg = os.path.join(self.ra, "config", "Mupen64Plus-Next", "Mupen64Plus-Next.cfg")
        os.makedirs(os.path.dirname(core_cfg))
        with open(core_cfg, "w") as f:
            f.write('video_shader_enable = "true"\ninput_player3_reserved_device = "My Own Pad"\n')
        files, state, cfg = p.UserFiles(cfg_path), {}, p.Config(cfg_path)
        ra = p.RetroArchManager(files)
        ra.apply(ra.session_desired(cfg, "Mupen64Plus-Next", ["N64 A", "DualSense A"]), state, "s")
        text = self.read(core_cfg)
        self.assertIn('video_shader_enable = "true"', text)                 # user's lines kept
        self.assertIn('input_player3_reserved_device = "My Own Pad"', text)
        self.assertIn('input_player1_reserved_device = "N64 A"', text)      # name only, no vid:pid
        self.assertIn('input_player1_device_reservation_type = "1"', text)  # preferred
        self.assertIn('input_player2_reserved_device = "DualSense A"', text)
        ra.apply({}, state, "s")                                             # game closes
        text = self.read(core_cfg)
        self.assertNotIn("input_player1", text)
        self.assertIn('input_player3_reserved_device = "My Own Pad"', text)
        self.assertEqual(state, {})

    def test_reserved_mode_and_no_retroarch_installed(self):
        cfg_path = self.config('[retroarch]\nreservation = "reserved"\n')
        ra = p.RetroArchManager(p.UserFiles(cfg_path))
        want = ra.session_desired(p.Config(cfg_path), "Snes9x", ["SNES A"])
        self.assertEqual(list(want.values())[0]["input_player1_device_reservation_type"], "2")
        os.rename(self.ra, self.ra + ".gone")
        self.assertEqual(ra.session_desired(p.Config(cfg_path), "Snes9x", ["SNES A"]), {})


class DolphinTests(HomeFixture):
    def setUp(self):
        super().setUp()
        with open(os.path.join(self.dol, "GCPadNew.ini"), "w") as f:
            f.write("[GCPad1]\nDevice = SDL/0/Steam Deck\nButtons/A = `Button S`\n\n"
                    "[GCPad2]\nDevice = SDL/1/Other\n")
        os.makedirs(os.path.join(self.dol, "Profiles", "GCPad"))
        for name in ("GameCube A", "N64 A"):
            with open(os.path.join(self.dol, "Profiles", "GCPad", name + ".ini"), "w") as f:
                f.write(f"[Profile]\nDevice = evdev/0/{name}\nButtons/A = `SOUTH`\nMain Stick/Up = `Axis 1-`\n")
        self.cfg = p.Config(self.config("[settings]\n"))
        self.mgr = p.DolphinManager(p.UserFiles(os.path.join(self.home, "config.toml")))

    def gcpad(self):
        return p._read_ini(os.path.join(self.dol, "GCPadNew.ini"))

    def test_profiles_follow_player_order_and_restore(self):
        state = {}
        status = self.mgr.apply_seats(self.cfg, [("GameCube A", 1)], ["gcpad"], state)
        ini = self.gcpad()
        self.assertEqual(ini.get("GCPad1", "Device"), "evdev/0/GameCube A")
        self.assertEqual(ini.get("GCPad2", "Device"), "SDL/1/Other")       # other port untouched
        self.assertEqual(status, {"GameCube A": ["GCPadNew.ini [GCPad1]"]})

        # N64 pad becomes P1, GameCube P2 - the port's original settings must still be remembered
        self.mgr.apply_seats(self.cfg, [("N64 A", 1), ("GameCube A", 2)], ["gcpad"], state)
        ini = self.gcpad()
        self.assertEqual(ini.get("GCPad1", "Device"), "evdev/0/N64 A")
        self.assertEqual(ini.get("GCPad2", "Device"), "evdev/0/GameCube A")

        self.mgr.apply_seats(self.cfg, [], [], state)                      # game closed
        ini = self.gcpad()
        self.assertEqual(ini.get("GCPad1", "Device"), "SDL/0/Steam Deck")
        self.assertEqual(ini.get("GCPad1", "Buttons/A"), "`Button S`")
        self.assertFalse(ini.has_option("GCPad1", "Main Stick/Up"))
        self.assertEqual(ini.get("GCPad2", "Device"), "SDL/1/Other")
        self.assertEqual(state["dolphin"], {})

    def test_missing_profile_leaves_port_alone(self):
        state = {}
        with self.assertLogs("padnamer", "INFO") as logs:
            self.mgr.apply_seats(self.cfg, [("Wii Remote A", 1)], ["wiimote"], state)
        self.assertTrue(any("Wii Remote A" in m for m in logs.output))
        self.assertFalse(os.path.exists(os.path.join(self.dol, "WiimoteNew.ini")))

    def test_wiimote_profile_sets_emulated_source(self):
        os.makedirs(os.path.join(self.dol, "Profiles", "Wiimote"))
        with open(os.path.join(self.dol, "Profiles", "Wiimote", "Wii Remote A.ini"), "w") as f:
            f.write("[Profile]\nDevice = evdev/0/Wii Remote A\nButtons/A = `BTN_A`\n")
        state = {}
        self.mgr.apply_seats(self.cfg, [("Wii Remote A", 2)], ["wiimote"], state)
        ini = p._read_ini(os.path.join(self.dol, "WiimoteNew.ini"))
        self.assertEqual(ini.get("Wiimote2", "Device"), "evdev/0/Wii Remote A")
        self.assertEqual(ini.get("Wiimote2", "Source"), "1")
        self.mgr.apply_seats(self.cfg, [], [], state)
        self.assertFalse(p._read_ini(os.path.join(self.dol, "WiimoteNew.ini")).has_section("Wiimote2"))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class SwitchAutoTests(unittest.TestCase):
    def fake_proc(self, procs):
        d = tempfile.mkdtemp()
        for pid, (comm, argv0) in enumerate(procs, 100):
            os.makedirs(os.path.join(d, str(pid)))
            with open(os.path.join(d, str(pid), "comm"), "w") as f:
                f.write(comm + "\n")
            with open(os.path.join(d, str(pid), "cmdline"), "wb") as f:
                f.write(argv0.encode() + b"\0--flag\0")
        return d

    def test_process_detection(self):
        names = p.DEFAULT_SWITCH_EMULATORS
        self.assertFalse(p.switch_emulator_running(names, self.fake_proc(
            [("steam", "/usr/bin/steam"), ("retroarch", "retroarch"), ("kwin_wayland", "kwin")])))
        self.assertTrue(p.switch_emulator_running(names, self.fake_proc(
            [("AppRun", "/home/deck/Applications/Eden.AppImage")])))
        self.assertTrue(p.switch_emulator_running(names, self.fake_proc([("Ryujinx", "/app/bin/Ryujinx")])))

    def test_auto_layout_follows_emulator(self):
        fs = FakeSys()
        with fs.patch():
            n = p.NodeInfo(fs.add("N64", keys=N64_KEYS, abs_=N64_ABS))
        auto = pad(present_as="auto")
        self.assertIsNone(p.build_layout(auto, n, switch_active=False))
        self.assertEqual(p.build_layout(auto, n, switch_active=True).product, 0x2009)

    def test_daemon_rebuilds_auto_pads(self):
        fs = FakeSys()
        home = tempfile.mkdtemp()
        cfg = os.path.join(home, "c.toml")
        with open(cfg, "w") as f:
            f.write(f'[[pad]]\nname="N64 P1"\nmac="{MAC1}"\npresent_as="auto"\n'
                    f'[[pad]]\nname="NES P1"\nmac="{MAC2}"\n')
        fs.add("N64", MAC1, keys=N64_KEYS, abs_=N64_ABS)
        fs.add("NES", MAC2, keys=set(p.NSO_LAYOUTS["nes"]), abs_={0x10, 0x11})

        class FB:
            def __init__(self, pad_, node, name, layout=None):
                self.pad, self.node, self.name, self.layout = pad_, node, name, layout
                self.rfd, self.vfd = os.pipe()

            def close(self):
                os.close(self.rfd)
                os.close(self.vfd)

        running = [False]
        with fs.patch(), mock.patch.object(p, "Bridge", FB), \
                mock.patch.object(p, "hide_node", lambda x: None), \
                mock.patch.object(p, "switch_emulator_running", lambda names: running[0]):
            d = p.Daemon(cfg, manage_udev=False, manage_emulators=False, status_path=None, use_registry=False, watch_launches=False, web=False)
            d.load_config(initial=True)
            layouts = lambda: {b.name: (b.layout.kind if b.layout else None) for b in d.bridges.values()}
            d.check_switch_emulators(); d.scan()
            self.assertEqual(layouts(), {"N64 P1": None, "NES P1": None})
            nes_bridge = next(b for b in d.bridges.values() if b.name == "NES P1")
            running[0] = True
            d.check_switch_emulators(); d.scan()
            self.assertEqual(layouts(), {"N64 P1": "n64 -> switch_pro", "NES P1": None})
            names = [d.pad_by_cid(c).name for c in d.creation_order()]
            self.assertEqual(names, ["N64 P1", "NES P1"])        # player order kept after rebuild
            running[0] = False
            d.check_switch_emulators(); d.scan()
            self.assertEqual(layouts(), {"N64 P1": None, "NES P1": None})
