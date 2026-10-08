"""Tests for padnamer using a fake sysfs tree and a fake kernel (no /dev/uinput needed)."""
import ctypes
import shutil
import os
import struct
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import padnamer as p  # noqa: E402


def caps_hex(bits):
    """Encode a set of bit numbers the way sysfs does."""
    if not bits:
        return "0"
    words = [0] * (max(bits) // p.LONG_BITS + 1)
    for b in bits:
        words[b // p.LONG_BITS] |= 1 << (b % p.LONG_BITS)
    return " ".join("%x" % w for w in reversed(words))


GAMEPAD_KEYS = {0x130, 0x131, 0x133, 0x134, 0x136, 0x137, 0x13a, 0x13b}  # BTN_SOUTH..START
STICKS = {0, 1, 3, 4, 0x10, 0x11}


class FakeSys:
    """Builds /sys/class/input + /sys/devices + /dev/input under a temp dir."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.cls = os.path.join(self.root, "class", "input")
        self.dev = os.path.join(self.root, "dev", "input")
        os.makedirs(self.cls)
        os.makedirs(self.dev)
        self.n = 0

    def add(self, name, mac="", uniq_on_input=True, keys=GAMEPAD_KEYS, abs_=STICKS,
            ff=(), rel=(), props=(), phys="", vendor=0x057E, product=0x2019,
            bus=5, hid=True, hidraw=None):
        self.n += 1
        ev = f"event{self.n + 10}"
        parent = os.path.join(self.root, "devices", f"bt{self.n}")
        if hid:
            parent = os.path.join(parent, f"0005:{vendor:04X}:{product:04X}.{self.n:04X}")
            os.makedirs(parent)
            with open(os.path.join(parent, "uevent"), "w") as f:
                f.write(f"DRIVER=nintendo\nHID_ID=0005:0000{vendor:04X}:0000{product:04X}\n"
                        f"HID_NAME={name}\nHID_UNIQ={mac}\n")
            if hidraw:
                os.makedirs(os.path.join(parent, "hidraw", hidraw))
        inp = os.path.join(parent, "input", f"input{self.n + 40}")
        os.makedirs(os.path.join(inp, "id"))
        os.makedirs(os.path.join(inp, "capabilities"))
        def w(rel_path, val):
            with open(os.path.join(inp, rel_path), "w") as fh:
                fh.write(val + "\n")
        w("name", name)
        w("phys", phys)
        w("uniq", mac.upper() if uniq_on_input else "")
        w("id/bustype", "%04x" % bus)
        w("id/vendor", "%04x" % vendor)
        w("id/product", "%04x" % product)
        w("id/version", "8001")
        evs = {p.EV_SYN}
        if keys: evs.add(p.EV_KEY)
        if abs_: evs.add(p.EV_ABS)
        if ff: evs.add(p.EV_FF)
        if rel: evs.add(p.EV_REL)
        w("capabilities/ev", caps_hex(evs))
        w("capabilities/key", caps_hex(set(keys)))
        w("capabilities/abs", caps_hex(set(abs_)))
        w("capabilities/ff", caps_hex(set(ff)))
        w("capabilities/rel", caps_hex(set(rel)))
        for c in ("msc", "led", "snd", "sw"):
            w("capabilities/" + c, "0")
        w("properties", caps_hex(set(props)))
        os.makedirs(os.path.join(self.cls, ev))
        os.symlink(inp, os.path.join(self.cls, ev, "device"))
        open(os.path.join(self.dev, ev), "w").close()
        return ev

    def remove(self, ev):
        os.unlink(os.path.join(self.dev, ev))
        os.unlink(os.path.join(self.cls, ev, "device"))
        os.rmdir(os.path.join(self.cls, ev))

    def patch(self):
        return mock.patch.multiple(p, SYS_INPUT=self.cls, DEV_INPUT=self.dev)


def write_config(text):
    f = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
    f.write(textwrap.dedent(text))
    f.close()
    return f.name


MAC_N64 = "98:b6:e9:00:00:01"
MAC_WII = "00:1f:32:00:00:02"
MAC_MD = "98:b6:e9:00:00:03"

CONFIG = f"""
[settings]
hide_hidraw = true

[[pad]]
name = "N64 P1"
mac = "{MAC_N64.upper()}"

[[pad]]
name = "Wii P1"
mac = "{MAC_WII}"
hide_hidraw = false

[[pad]]
name = "GC P1"
match_name = "Nintendo GameCube Controller"

[[pad]]
name = "GC P2"
match_name = "Nintendo GameCube Controller"
product = "0xbeef"
"""


class BitmapTests(unittest.TestCase):
    def test_roundtrip(self):
        bits = {0, 5, 63, 64, 0x130, 0x2ff}
        self.assertEqual(p.parse_bitmap(caps_hex(bits)), bits)

    def test_empty(self):
        self.assertEqual(p.parse_bitmap("0"), set())
        self.assertEqual(p.parse_bitmap(""), set())


class ConfigTests(unittest.TestCase):
    def test_parse(self):
        cfg = p.Config(write_config(CONFIG))
        self.assertEqual([x.name for x in cfg.pads], ["N64 P1", "Wii P1", "GC P1", "GC P2"])
        self.assertEqual(cfg.pads[0].mac, MAC_N64)                 # normalised to lower case
        self.assertTrue(cfg.pads[0].hide_hidraw)
        self.assertFalse(cfg.pads[1].hide_hidraw)
        self.assertEqual(cfg.pads[3].product, 0xBEEF)
        self.assertEqual(cfg.pads[2].phys, "padnamer/gc-p1")

    def test_errors(self):
        bad = {
            'no name': '[[pad]]\nmac="00:11:22:33:44:55"',
            'bad mac': '[[pad]]\nname="x"\nmac="00:11:22"',
            'no match': '[[pad]]\nname="x"',
            'dup name': '[[pad]]\nname="x"\nmac="00:11:22:33:44:55"\n[[pad]]\nname="x"\nmac="00:11:22:33:44:56"',
            'dup mac': '[[pad]]\nname="x"\nmac="00:11:22:33:44:55"\n[[pad]]\nname="y"\nmac="00:11:22:33:44:55"',
        }
        for label, text in bad.items():
            with self.subTest(label), self.assertRaises(ValueError):
                p.Config(write_config(text))


class NodeInfoTests(unittest.TestCase):
    def setUp(self):
        self.fs = FakeSys()

    def test_mac_from_input_uniq(self):
        ev = self.fs.add("Nintendo N64 Controller", MAC_N64, ff=[p.FF_RUMBLE], hidraw="hidraw4")
        with self.fs.patch():
            n = p.NodeInfo(ev)
        self.assertEqual(n.mac, MAC_N64)
        self.assertTrue(n.is_controllerish)
        self.assertEqual(n.hidraw_nodes(), ["/dev/hidraw4"])

    def test_mac_from_hid_parent_when_input_uniq_empty(self):
        # hid-wiimote extension nodes don't set uniq on the input device
        ev = self.fs.add("Nintendo Wii Remote Nunchuk", MAC_WII, uniq_on_input=False)
        with self.fs.patch():
            self.assertEqual(p.NodeInfo(ev).mac, MAC_WII)

    def test_not_controllers(self):
        with self.fs.patch():
            kb = p.NodeInfo(self.fs.add("Consumer Control", keys={115, 114}, abs_=()))
            mouse = p.NodeInfo(self.fs.add("Mouse", keys={0x110}, abs_=(), rel={0, 1}))
            accel = p.NodeInfo(self.fs.add("Wii Remote Accelerometer", keys=(), abs_=(),
                                           props={p.INPUT_PROP_ACCELEROMETER}))
        self.assertFalse(kb.is_controllerish)
        self.assertFalse(mouse.is_controllerish)
        self.assertTrue(accel.is_controllerish)


class NamingTests(unittest.TestCase):
    def node(self, name, props=()):
        n = mock.Mock()
        n.name, n.props = name, set(props)
        return n

    def test_suffixes(self):
        cases = {
            "Nintendo Wii Remote": "Wii P1",
            "Nintendo Wii Remote Accelerometer": "Wii P1 Accelerometer",
            "Nintendo Wii Remote IR": "Wii P1 IR",
            "Nintendo Wii Remote Classic Controller": "Wii P1 Classic Controller",
            "Nintendo Switch Pro Controller (IMU)": "Wii P1 (IMU)",
        }
        for orig, want in cases.items():
            self.assertEqual(p.virtual_name("Wii P1", self.node(orig), set()), want)

    def test_unknown_motion_node_and_collisions(self):
        self.assertEqual(p.virtual_name("Pad", self.node("X", {p.INPUT_PROP_ACCELEROMETER}), set()), "Pad Motion")
        self.assertEqual(p.virtual_name("Pad", self.node("X"), {"Pad", "Pad 2"}), "Pad 3")


class UdevRuleTests(unittest.TestCase):
    def test_rules(self):
        rules = p.build_udev_rules(p.Config(write_config(CONFIG)))
        self.assertIn(f'ATTRS{{uniq}}=="{MAC_N64}"', rules)
        self.assertIn(f'ATTRS{{uniq}}=="{MAC_N64.upper()}"', rules)
        self.assertIn(f'SUBSYSTEM=="hidraw", ENV{{HID_UNIQ}}=="{MAC_N64}"', rules)
        self.assertNotIn(f'SUBSYSTEM=="hidraw", ENV{{HID_UNIQ}}=="{MAC_WII}"', rules)  # hide_hidraw=false
        self.assertIn('ATTRS{name}=="Nintendo GameCube Controller"', rules)
        self.assertIn('ATTRS{phys}=="padnamer/*", ENV{PADNAMER_HIDE}=""', rules)
        # the un-hide exception must come before the action line
        self.assertLess(rules.index('ENV{PADNAMER_HIDE}=""'), rules.index('MODE="0600"'))
        self.assertTrue(rules.rstrip().endswith('LABEL="padnamer_end"'))


ABS_REQS = {p.EVIOCGABS(a) for a in range(0x40)}


class FakeKernel:
    """Stands in for ioctl() on the real device and on /dev/uinput."""
    REAL_FD, VIRT_FD = 1001, 1002

    def __init__(self, max_effects=16):
        self.calls = []
        self.next_real_id = 7
        self.real_effects = {}
        self.pending_upload = None
        self.pending_erase = None
        self.ended = []
        self.max_effects = max_effects
        self.fail_upload = None

    def ioctl(self, fd, req, arg=0):
        self.calls.append((fd, req, arg if isinstance(arg, (int, bytes)) else type(arg).__name__))
        if req == p.EVIOCGEFFECTS:
            arg.value = self.max_effects
        elif req in ABS_REQS:
            arg.minimum, arg.maximum, arg.flat = -32768, 32767, 128
        elif req == p.UI_BEGIN_FF_UPLOAD:
            vid, etype, strong = self.pending_upload
            arg.effect.id = vid
            arg.effect.type = etype
            arg.effect.u.rumble.strong_magnitude = strong
        elif req == p.EVIOCSFF:
            if self.fail_upload:
                raise OSError(self.fail_upload, os.strerror(self.fail_upload))
            if arg.id == -1:
                arg.id = self.next_real_id
                self.next_real_id += 1
            self.real_effects[arg.id] = arg.u.rumble.strong_magnitude
        elif req == p.UI_END_FF_UPLOAD:
            self.ended.append(("upload", arg.request_id, arg.retval))
        elif req == p.UI_BEGIN_FF_ERASE:
            arg.effect_id = self.pending_erase
        elif req == p.EVIOCRMFF:
            self.real_effects.pop(arg, None)
        elif req == p.UI_END_FF_ERASE:
            self.ended.append(("erase", arg.request_id, arg.retval))
        return 0


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.fs = FakeSys()
        self.kernel = FakeKernel()
        self.cfg = p.Config(write_config(CONFIG))
        ev = self.fs.add("Nintendo N64 Controller", MAC_N64, ff=[p.FF_RUMBLE, p.FF_GAIN],
                         props={p.INPUT_PROP_ACCELEROMETER})
        with self.fs.patch():
            self.node = p.NodeInfo(ev)
        self.written = []

        real_open = os.open

        def fake_open(path, flags, *a):
            if path == "/dev/uinput":
                return FakeKernel.VIRT_FD
            if path == self.node.devnode:
                return FakeKernel.REAL_FD
            return real_open(path, flags, *a)

        def fake_write(fd, data):
            self.written.append((fd, data))
            return len(data)

        self.patches = [mock.patch.object(p, "ioctl", self.kernel.ioctl),
                        mock.patch.object(p.os, "open", fake_open),
                        mock.patch.object(p.os, "write", fake_write),
                        mock.patch.object(p.os, "close", lambda fd: None)]
        for x in self.patches:
            x.start()
        self.bridge = p.Bridge(self.cfg.pads[0], self.node, "N64 P1")

    def tearDown(self):
        for x in self.patches:
            x.stop()

    def reqs(self, fd):
        return [c for c in self.kernel.calls if c[0] == fd]

    def test_virtual_device_setup(self):
        virt = self.reqs(FakeKernel.VIRT_FD)
        reqs = [c[1] for c in virt]
        # grab happened on the real device before the virtual one was built
        self.assertEqual(self.kernel.calls[0], (FakeKernel.REAL_FD, p.EVIOCGRAB, 1))
        self.assertIn((FakeKernel.VIRT_FD, p.UI_SET_EVBIT, p.EV_FF), virt)
        self.assertIn((FakeKernel.VIRT_FD, p.UI_SET_BIT(107), p.FF_RUMBLE), virt)
        self.assertIn((FakeKernel.VIRT_FD, p.UI_SET_BIT(101), 0x130), virt)
        self.assertIn((FakeKernel.VIRT_FD, p.UI_SET_PROPBIT, p.INPUT_PROP_ACCELEROMETER), virt)
        self.assertIn((FakeKernel.VIRT_FD, p.UI_SET_PHYS, b"padnamer/" + MAC_N64.encode() + b"\0"), virt)
        self.assertEqual(reqs.count(p.UI_ABS_SETUP), len(STICKS))
        # ordering the kernel requires: setup, abs setup, then create last
        self.assertLess(reqs.index(p.UI_DEV_SETUP), reqs.index(p.UI_ABS_SETUP))
        self.assertEqual(reqs[-1], p.UI_DEV_CREATE)

    def test_setup_struct_contents(self):
        captured = {}
        orig = self.kernel.ioctl

        def spy(fd, req, arg=0):
            if req == p.UI_DEV_SETUP:
                captured["setup"] = p.uinput_setup.from_buffer_copy(arg)
            return orig(fd, req, arg)

        with mock.patch.object(p, "ioctl", spy):
            p.Bridge(self.cfg.pads[3], self.node, "GC P2")
        s = captured["setup"]
        self.assertEqual(s.name, b"GC P2")
        self.assertEqual(s.id.vendor, 0x057E)
        self.assertEqual(s.id.product, 0xBEEF)          # override from config
        self.assertEqual(s.id.bustype, 5)
        self.assertEqual(s.ff_effects_max, 16)

    def test_rumble_upload_play_update_erase(self):
        b, k = self.bridge, self.kernel
        # game uploads effect -> virtual id 0
        k.pending_upload = (0, p.FF_RUMBLE, 0x8000)
        b._handle_virtual_event(p.EV_UINPUT, p.UI_FF_UPLOAD, 42)
        self.assertEqual(k.ended[-1], ("upload", 42, 0))
        self.assertEqual(b.effects, {0: 7})
        self.assertEqual(k.real_effects, {7: 0x8000})

        # game plays virtual effect 0 -> real effect 7 is played
        self.written.clear()
        b._handle_virtual_event(p.EV_FF, 0, 1)
        fd, data = self.written[-1]
        self.assertEqual(fd, FakeKernel.REAL_FD)
        self.assertEqual(struct.unpack(p.EVENT_FMT, data)[2:], (p.EV_FF, 7, 1))

        # game updates the same effect: must reuse real id 7, not allocate a new one
        k.pending_upload = (0, p.FF_RUMBLE, 0xFFFF)
        b._handle_virtual_event(p.EV_UINPUT, p.UI_FF_UPLOAD, 43)
        self.assertEqual(b.effects, {0: 7})
        self.assertEqual(k.real_effects, {7: 0xFFFF})

        # gain passes straight through
        b._handle_virtual_event(p.EV_FF, p.FF_GAIN, 0x6000)
        self.assertEqual(struct.unpack(p.EVENT_FMT, self.written[-1][1])[2:], (p.EV_FF, p.FF_GAIN, 0x6000))

        # erase
        k.pending_erase = 0
        b._handle_virtual_event(p.EV_UINPUT, p.UI_FF_ERASE, 44)
        self.assertEqual(k.ended[-1], ("erase", 44, 0))
        self.assertEqual(b.effects, {})
        self.assertEqual(k.real_effects, {})

        # playing an unknown effect is ignored, not forwarded
        self.written.clear()
        b._handle_virtual_event(p.EV_FF, 5, 1)
        self.assertEqual(self.written, [])

    def test_rumble_upload_failure_reports_errno(self):
        self.kernel.fail_upload = 28  # ENOSPC
        self.kernel.pending_upload = (3, p.FF_RUMBLE, 1)
        self.bridge._handle_virtual_event(p.EV_UINPUT, p.UI_FF_UPLOAD, 9)
        self.assertEqual(self.kernel.ended[-1], ("upload", 9, -28))
        self.assertEqual(self.bridge.effects, {})

    def test_two_independent_effects(self):
        b, k = self.bridge, self.kernel
        for vid, req in ((0, 1), (1, 2)):
            k.pending_upload = (vid, p.FF_RUMBLE, 100 + vid)
            b._handle_virtual_event(p.EV_UINPUT, p.UI_FF_UPLOAD, req)
        self.assertEqual(b.effects, {0: 7, 1: 8})


class PumpTests(unittest.TestCase):
    def test_events_pass_through_unchanged(self):
        b = object.__new__(p.Bridge)
        rr, rw = os.pipe()
        vr, vw = os.pipe()
        os.set_blocking(rr, False)
        b.rfd, b.vfd, b.name, b.layout = rr, vw, "t", None
        events = b"".join(struct.pack(p.EVENT_FMT, 1, 2, p.EV_KEY, 0x130, v) for v in (1, 0)) + \
            struct.pack(p.EVENT_FMT, 1, 2, p.EV_SYN, 0, 0)
        os.write(rw, events)
        self.assertTrue(b.pump_real())
        self.assertEqual(os.read(vr, 4096), events)
        os.close(rw)
        self.assertFalse(b.pump_real())       # EOF == device gone


class DaemonScanTests(unittest.TestCase):
    def setUp(self):
        self.fs = FakeSys()
        self.cfg_path = write_config(CONFIG)
        self.created = []
        test = self

        class FakeBridge:
            def __init__(self, pad, node, name, layout=None):
                self.pad, self.node, self.name, self.layout = pad, node, name, layout
                self.rfd, self.vfd = os.pipe()
                test.created.append(self)

            def close(self):
                os.close(self.rfd)
                os.close(self.vfd)
                self.closed = True

        self.patches = [self.fs.patch(), mock.patch.object(p, "Bridge", FakeBridge),
                        mock.patch.object(p, "hide_node", lambda path: None)]
        for x in self.patches:
            x.start()
        self.d = p.Daemon(self.cfg_path, manage_udev=False, manage_emulators=False, status_path=None, use_registry=False, watch_launches=False, web=False)
        self.d.load_config(initial=True)

    def tearDown(self):
        for x in self.patches:
            x.stop()

    def names(self):
        return sorted(b.name for b in self.d.bridges.values())

    def test_matching_naming_and_hotplug(self):
        self.fs.add("Nintendo N64 Controller", MAC_N64, ff=[p.FF_RUMBLE])
        self.fs.add("Nintendo N64 Controller Consumer Keys", MAC_N64, keys={115}, abs_=())  # not a pad node
        wii = [self.fs.add("Nintendo Wii Remote", MAC_WII, abs_=(), ff=[p.FF_RUMBLE]),
               self.fs.add("Nintendo Wii Remote Accelerometer", MAC_WII, uniq_on_input=False,
                           keys=(), abs_={0, 1, 2}, props={p.INPUT_PROP_ACCELEROMETER}),
               self.fs.add("Nintendo Wii Remote IR", MAC_WII, uniq_on_input=False, keys=(), abs_={0x10, 0x11})]
        self.fs.add("Unrelated Pad", "11:22:33:44:55:66")
        self.fs.add("N64 P1", phys="padnamer/" + MAC_N64, hid=False)          # our own output: ignored
        self.d.scan()
        self.assertEqual(self.names(), ["N64 P1", "Wii P1", "Wii P1 Accelerometer", "Wii P1 IR"])

        # scanning again doesn't duplicate
        self.d.scan()
        self.assertEqual(len(self.d.bridges), 4)

        # Wii Remote disconnects -> its three virtual nodes go away
        for ev in wii:
            self.fs.remove(ev)
        self.d.scan()
        self.assertEqual(self.names(), ["N64 P1"])

    def test_name_matched_pads_fill_in_order(self):
        self.fs.add("Nintendo GameCube Controller", hid=False, product=0x2073)
        self.d.scan()
        self.assertEqual(self.names(), ["GC P1"])
        self.fs.add("Nintendo GameCube Controller", hid=False, product=0x2073)
        self.d.scan()
        self.assertEqual(self.names(), ["GC P1", "GC P2"])
        self.fs.add("Nintendo GameCube Controller", hid=False, product=0x2073)
        self.d.scan()
        self.assertEqual(len(self.d.bridges), 2)   # no third entry configured -> left alone

    def test_config_reload_rebuilds(self):
        self.fs.add("Nintendo N64 Controller", MAC_N64)
        self.d.scan()
        with open(self.cfg_path, "w") as f:
            f.write(CONFIG.replace('name = "N64 P1"', 'name = "Mario Pad"'))
        os.utime(self.cfg_path, (1, 1))
        self.d.load_config()
        self.d.scan()
        self.assertEqual(self.names(), ["Mario Pad"])

    def test_broken_config_keeps_old(self):
        with open(self.cfg_path, "w") as f:
            f.write("[[pad]]\nname = \n")
        os.utime(self.cfg_path, (2, 2))
        self.d.load_config()
        self.assertEqual(len(self.d.config.pads), 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class KernelAbiTests(unittest.TestCase):
    """Compare struct sizes and ioctl numbers with the real kernel headers."""

    @unittest.skipUnless(shutil.which("cc") and os.path.exists("/usr/include/linux/uinput.h"),
                         "needs a C compiler and kernel headers")
    def test_matches_kernel_headers(self):
        import subprocess
        here = os.path.dirname(__file__)
        with tempfile.TemporaryDirectory() as tmp:
            exe = os.path.join(tmp, "abi")
            subprocess.run(["cc", os.path.join(here, "abi.c"), "-o", exe], check=True)
            want = dict(line.rsplit(" ", 1) for line in subprocess.check_output([exe], text=True).splitlines())
        S = ctypes.sizeof
        have = {
            "sizeof(struct ff_effect)": S(p.ff_effect), "sizeof(struct uinput_ff_upload)": S(p.uinput_ff_upload),
            "sizeof(struct uinput_ff_erase)": S(p.uinput_ff_erase), "sizeof(struct uinput_setup)": S(p.uinput_setup),
            "sizeof(struct uinput_abs_setup)": S(p.uinput_abs_setup), "sizeof(struct input_event)": p.EVENT_SIZE,
            "sizeof(struct input_absinfo)": S(p.input_absinfo), "offsetof(struct ff_effect,u)": p.ff_effect.u.offset,
            "offsetof(struct uinput_ff_upload,effect)": p.uinput_ff_upload.effect.offset,
            "EVIOCGABS(3)": p.EVIOCGABS(3), "EVIOCSFF": p.EVIOCSFF, "EVIOCRMFF": p.EVIOCRMFF,
            "EVIOCGEFFECTS": p.EVIOCGEFFECTS, "EVIOCGRAB": p.EVIOCGRAB, "UI_DEV_CREATE": p.UI_DEV_CREATE,
            "UI_DEV_DESTROY": p.UI_DEV_DESTROY, "UI_DEV_SETUP": p.UI_DEV_SETUP, "UI_ABS_SETUP": p.UI_ABS_SETUP,
            "UI_SET_EVBIT": p.UI_SET_EVBIT, "UI_SET_KEYBIT": p.UI_SET_BIT(101), "UI_SET_FFBIT": p.UI_SET_BIT(107),
            "UI_SET_PHYS": p.UI_SET_PHYS, "UI_SET_PROPBIT": p.UI_SET_PROPBIT, "UI_SET_SWBIT": p.UI_SET_BIT(109),
            "UI_BEGIN_FF_UPLOAD": p.UI_BEGIN_FF_UPLOAD, "UI_END_FF_UPLOAD": p.UI_END_FF_UPLOAD,
            "UI_BEGIN_FF_ERASE": p.UI_BEGIN_FF_ERASE, "UI_END_FF_ERASE": p.UI_END_FF_ERASE,
            "EV_UINPUT": p.EV_UINPUT, "FF_RUMBLE": p.FF_RUMBLE, "FF_GAIN": p.FF_GAIN,
            "FF_AUTOCENTER": p.FF_AUTOCENTER, "INPUT_PROP_ACCELEROMETER": p.INPUT_PROP_ACCELEROMETER,
            "BTN_MISC": p.BTN_MISC, "KEY_MAX": p.KEY_MAX, "EV_FF": p.EV_FF,
        }
        self.assertEqual(set(want), set(have))
        for k, v in have.items():
            self.assertEqual(int(want[k]), v, k)
