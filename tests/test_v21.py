"""2.1: disconnecting controllers and handing Wii hardware (Balance Board) to Dolphin."""
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import padnamer as p  # noqa: E402
from test_v2 import Env, PAD_KEYS, N64_KEYS, MAC_N64, MAC_XB  # noqa: E402

MAC_BOARD = "00:22:4c:00:00:0b"
MAC_WII = "00:1f:32:00:00:0c"
BOARD_ABS = {p.ABS_HAT0X, p.ABS_HAT0Y, 0x12, 0x13}       # four weight sensors


class BalanceBoardTests(Env):
    def add_board(self, core_first=False):
        if core_first:      # hid-wiimote's generic node can show up before the board's own
            self.fs.add("Nintendo Wii Remote", MAC_BOARD, product=0x0306, keys={0x130}, abs_=())
        return self.fs.add("Nintendo Wii Remote Balance Board", MAC_BOARD, product=0x0306,
                           keys={0x130}, abs_=BOARD_ABS)

    def test_board_detected_and_left_for_dolphin(self):
        self.add_board()
        self.fs.add("N64 Controller", MAC_N64, product=0x2019, keys=N64_KEYS)
        d = self.daemon()
        d.scan()
        board = d.registry.controllers[MAC_BOARD]
        self.assertEqual(board["detected_model"], "wii_balance_board")
        self.assertEqual(p.Registry.mode_of(board), "dolphin")
        self.assertNotIn(MAC_BOARD, d.connected_cids())          # never grabbed or renamed
        self.assertEqual(self.names_in_order(d), ["N64 A"])
        st = {c["id"]: c for c in d.state_dict()["controllers"]}
        self.assertTrue(st[MAC_BOARD]["connected"])
        self.assertIsNone(st[MAC_BOARD]["player"])
        self.assertEqual(st[MAC_BOARD]["mode"], "dolphin")
        self.assertEqual(d.registry.passthrough_macs(), [MAC_BOARD])

    def test_board_recognised_when_generic_node_comes_first(self):
        self.add_board(core_first=True)
        d = self.daemon()
        d.scan()
        self.assertEqual(p.Registry.mode_of(d.registry.controllers[MAC_BOARD]), "dolphin")
        self.assertEqual(d.connected_cids(), set())

    def test_wii_remote_can_be_handed_over_and_back(self):
        self.fs.add("Nintendo Wii Remote", MAC_WII, product=0x0306, keys=PAD_KEYS, ff=[p.FF_RUMBLE])
        d = self.daemon()
        d.scan()
        self.assertIn(MAC_WII, d.connected_cids())
        d.api_update_controller(MAC_WII, {"mode": "dolphin"})
        self.assertNotIn(MAC_WII, d.connected_cids())
        self.assertEqual(d.registry.passthrough_macs(), [MAC_WII])
        d.api_update_controller(MAC_WII, {"mode": "managed"})
        self.assertIn(MAC_WII, d.connected_cids())
        self.assertEqual(d.registry.passthrough_macs(), [])

    def test_only_wii_hardware_can_go_to_dolphin(self):
        self.fs.add("Xbox Wireless Controller", MAC_XB, vendor=0x045E, product=0x0B13, keys=PAD_KEYS)
        d = self.daemon()
        d.scan()
        with self.assertRaises(ValueError):
            d.registry.update(MAC_XB, {"mode": "dolphin"})
        with self.assertRaises(ValueError):
            d.registry.update(MAC_XB, {"mode": "sideways"})

    def test_user_choice_beats_auto_detection(self):
        self.add_board(core_first=True)
        d = self.daemon()
        d.scan()
        d.registry.update(MAC_BOARD, {"mode": "ignore"})
        d.registry.observe(p.NodeInfo(self.add_board()))
        self.assertEqual(p.Registry.mode_of(d.registry.controllers[MAC_BOARD]), "ignore")


class UdevPassthroughTests(unittest.TestCase):
    def test_rules(self):
        path = tempfile.mktemp(suffix=".toml")
        with open(path, "w") as f:
            f.write("[settings]\n")
        rules = p.build_udev_rules(p.Config(path), [], [MAC_BOARD])
        self.assertIn(f'SUBSYSTEM=="hidraw", ENV{{HID_UNIQ}}=="{MAC_BOARD}", ENV{{PADNAMER_REAL}}="1"', rules)
        self.assertIn(f'ENV{{HID_UNIQ}}=="{MAC_BOARD.upper()}", ENV{{PADNAMER_HIDE}}="1"', rules)
        self.assertIn('ENV{PADNAMER_REAL}=="1", MODE="0660", TAG+="uaccess"', rules)
        if subprocess.run(["which", "udevadm"], capture_output=True).returncode == 0:
            out = tempfile.mktemp(suffix=".rules")
            with open(out, "w") as f:
                f.write(rules)
            r = subprocess.run(["udevadm", "verify", out], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class DisconnectTests(Env):
    def setUp(self):
        super().setUp()
        self.fs.add("Xbox Wireless Controller", MAC_XB, vendor=0x045E, product=0x0B13, keys=PAD_KEYS)
        self.fs.add("Nintendo Wii Remote Balance Board", MAC_BOARD, product=0x0306, keys={0x130}, abs_=BOARD_ABS)
        self.fs.add("Nintendo GameCube Controller", hid=False, product=0x2073, bus=3, keys=PAD_KEYS)
        self.d = self.daemon()
        self.d.scan()
        self.calls = []
        self.bt = mock.patch.object(p, "bt_disconnect", lambda mac, label="": self.calls.append(mac))
        self.bt.start()
        self.addCleanup(self.bt.stop)

    def test_disconnect_one(self):
        self.d.api_disconnect(MAC_XB)
        self.assertEqual(self.calls, [MAC_XB])
        self.d.api_disconnect(MAC_BOARD)                     # a Dolphin device can be dropped too
        self.assertEqual(self.calls, [MAC_XB, MAC_BOARD])

    def test_refusals(self):
        gc = next(c for c in self.d.registry.controllers if c.startswith("name:"))
        with self.assertRaises(ValueError) as e:
            self.d.api_disconnect(gc)
        self.assertIn("Bluetooth", str(e.exception))
        with self.assertRaises(ValueError):
            self.d.api_disconnect("aa:bb:cc:dd:ee:ff")
        self.d.registry.controllers[MAC_N64] = {"id": MAC_N64, "kind": "mac", "mac": MAC_N64, "name": "N64 A"}
        with self.assertRaises(ValueError) as e:
            self.d.api_disconnect(MAC_N64)
        self.assertIn("isn't connected", str(e.exception))
        self.assertEqual(self.calls, [])

    def test_disconnect_all_bluetooth_only(self):
        self.assertEqual(self.d.api_disconnect_all(), 2)
        self.assertEqual(sorted(self.calls), sorted([MAC_XB, MAC_BOARD]))


class BtDisconnectHelperTests(unittest.TestCase):
    def run_sync(self):
        threads = []

        class Now:
            def __init__(self, target, **kw):
                self.target = target

            def start(self):
                self.target()
        return mock.patch("threading.Thread", Now)

    def test_bluetoothctl_first(self):
        runs = []

        def fake(cmd, **kw):
            runs.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "Attempting to disconnect\nSuccessful disconnected", "")
        with self.run_sync(), mock.patch.object(p.subprocess, "run", fake):
            p.bt_disconnect("aa:bb:cc:dd:ee:ff", "Pad")
        self.assertEqual(runs, [["bluetoothctl", "disconnect", "AA:BB:CC:DD:EE:FF"]])

    def test_falls_back_to_dbus(self):
        runs = []

        def fake(cmd, **kw):
            runs.append(cmd)
            if cmd[0] == "bluetoothctl":
                raise FileNotFoundError
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        with self.run_sync(), mock.patch.object(p.subprocess, "run", fake), \
                mock.patch.object(p.glob, "glob", lambda pat: ["/sys/class/bluetooth/hci0"]):
            p.bt_disconnect("aa:bb:cc:dd:ee:ff")
        self.assertEqual(runs[-1], ["busctl", "call", "org.bluez", "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF",
                                    "org.bluez.Device1", "Disconnect"])


if __name__ == "__main__":
    unittest.main()
