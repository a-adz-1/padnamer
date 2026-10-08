#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the padnamer authors
"""
padnamer - give every Bluetooth controller its own stable, custom name.

Each configured controller is matched by Bluetooth MAC (or by device name),
grabbed exclusively, and re-created as a virtual uinput device carrying the
name you chose ("N64 P1", "Mega Drive", ...). Buttons, sticks, triggers,
motion sensors and rumble (force feedback) are passed through in both
directions. The original device nodes are hidden from normal users so games
and emulators only ever see the renamed copy.

It can also:
  * light the controller's player LEDs to match its configured player number
  * present a Nintendo Switch Online retro pad (NES/SNES/N64/Mega Drive) as
    the Switch Pro Controller it really is on the wire
  * assign each pad to its systems: RetroArch core overrides (device
    reservation per player) and Dolphin controller ports (profile swap on
    connect/disconnect) are managed for you

Pure Python standard library (3.11+), so nothing has to be pip-installed or
compiled and SteamOS updates can't break it.

Commands
  padnamer.py run    --config FILE   run the daemon (needs root)
  padnamer.py list                   show controllers, MACs, IDs, capabilities
  padnamer.py status                 what the running daemon is doing
  padnamer.py rules  --config FILE   print the udev rules the daemon installs
  padnamer.py check  --config FILE   validate the config file
  padnamer.py rumble NAME            play a test rumble on a device by name
"""

import argparse
import configparser
import ctypes
import errno
import fcntl
import glob
import json
import logging
import os
import pwd
import re
import select
import signal
import struct
import subprocess
import sys
import time

try:
    import tomllib
except ImportError:  # Python < 3.11
    tomllib = None

log = logging.getLogger("padnamer")

VERSION = "2.1.0"
STATUS_PATH = "/run/padnamer/status.json"
PHYS_PREFIX = "padnamer/"
UDEV_RULES_PATH = "/etc/udev/rules.d/72-padnamer.rules"

# --------------------------------------------------------------------------
# Kernel ABI: event codes
# --------------------------------------------------------------------------

EV_SYN, EV_KEY, EV_REL, EV_ABS, EV_MSC, EV_SW = 0x00, 0x01, 0x02, 0x03, 0x04, 0x05
EV_LED, EV_SND, EV_REP, EV_FF, EV_PWR, EV_FF_STATUS = 0x11, 0x12, 0x14, 0x15, 0x16, 0x17
EV_UINPUT = 0x0101
UI_FF_UPLOAD, UI_FF_ERASE = 1, 2

FF_RUMBLE = 0x50
FF_GAIN, FF_AUTOCENTER = 0x60, 0x61

BTN_MISC = 0x100
KEY_MAX = 0x2FF
INPUT_PROP_ACCELEROMETER = 0x06
BUS_BLUETOOTH = 0x05

# sysfs capability file -> (event type, uinput "set bit" ioctl nr)
CAP_FILES = {
    "key": (EV_KEY, 101),
    "rel": (EV_REL, 102),
    "abs": (EV_ABS, 103),
    "msc": (EV_MSC, 104),
    "led": (EV_LED, 105),
    "snd": (EV_SND, 106),
    "ff": (EV_FF, 107),
    "sw": (EV_SW, 109),
}

# --------------------------------------------------------------------------
# Kernel ABI: structures (ctypes gives the right padding for this arch)
# --------------------------------------------------------------------------

u16, s16, u32, s32 = ctypes.c_uint16, ctypes.c_int16, ctypes.c_uint32, ctypes.c_int32


class input_id(ctypes.Structure):
    _fields_ = [("bustype", u16), ("vendor", u16), ("product", u16), ("version", u16)]


class input_absinfo(ctypes.Structure):
    _fields_ = [("value", s32), ("minimum", s32), ("maximum", s32),
                ("fuzz", s32), ("flat", s32), ("resolution", s32)]


class ff_envelope(ctypes.Structure):
    _fields_ = [("attack_length", u16), ("attack_level", u16),
                ("fade_length", u16), ("fade_level", u16)]


class ff_constant_effect(ctypes.Structure):
    _fields_ = [("level", s16), ("envelope", ff_envelope)]


class ff_ramp_effect(ctypes.Structure):
    _fields_ = [("start_level", s16), ("end_level", s16), ("envelope", ff_envelope)]


class ff_condition_effect(ctypes.Structure):
    _fields_ = [("right_saturation", u16), ("left_saturation", u16),
                ("right_coeff", s16), ("left_coeff", s16),
                ("deadband", u16), ("center", s16)]


class ff_periodic_effect(ctypes.Structure):
    _fields_ = [("waveform", u16), ("period", u16), ("magnitude", s16),
                ("offset", s16), ("phase", u16), ("envelope", ff_envelope),
                ("custom_len", u32), ("custom_data", ctypes.c_void_p)]


class ff_rumble_effect(ctypes.Structure):
    _fields_ = [("strong_magnitude", u16), ("weak_magnitude", u16)]


class ff_effect_union(ctypes.Union):
    _fields_ = [("constant", ff_constant_effect), ("ramp", ff_ramp_effect),
                ("periodic", ff_periodic_effect),
                ("condition", ff_condition_effect * 2),
                ("rumble", ff_rumble_effect)]


class ff_trigger(ctypes.Structure):
    _fields_ = [("button", u16), ("interval", u16)]


class ff_replay(ctypes.Structure):
    _fields_ = [("length", u16), ("delay", u16)]


class ff_effect(ctypes.Structure):
    _fields_ = [("type", u16), ("id", s16), ("direction", u16),
                ("trigger", ff_trigger), ("replay", ff_replay),
                ("u", ff_effect_union)]


class uinput_setup(ctypes.Structure):
    _fields_ = [("id", input_id), ("name", ctypes.c_char * 80), ("ff_effects_max", u32)]


class uinput_abs_setup(ctypes.Structure):
    _fields_ = [("code", u16), ("absinfo", input_absinfo)]


class uinput_ff_upload(ctypes.Structure):
    _fields_ = [("request_id", u32), ("retval", s32),
                ("effect", ff_effect), ("old", ff_effect)]


class uinput_ff_erase(ctypes.Structure):
    _fields_ = [("request_id", u32), ("retval", s32), ("effect_id", u32)]


# struct input_event { struct timeval time; __u16 type; __u16 code; __s32 value; }
EVENT_FMT = "@llHHi"
EVENT_SIZE = struct.calcsize(EVENT_FMT)

# --------------------------------------------------------------------------
# Kernel ABI: ioctl numbers
# --------------------------------------------------------------------------

_IOC_NONE, _IOC_WRITE, _IOC_READ = 0, 1, 2


def _IOC(direction, typ, nr, size):
    return (direction << 30) | (size << 16) | (ord(typ) << 8) | nr


def _IO(t, nr): return _IOC(_IOC_NONE, t, nr, 0)
def _IOR(t, nr, size): return _IOC(_IOC_READ, t, nr, size)
def _IOW(t, nr, size): return _IOC(_IOC_WRITE, t, nr, size)
def _IOWR(t, nr, size): return _IOC(_IOC_READ | _IOC_WRITE, t, nr, size)


INT = ctypes.sizeof(ctypes.c_int)
PTR = ctypes.sizeof(ctypes.c_void_p)

def EVIOCGABS(axis): return _IOR("E", 0x40 + axis, ctypes.sizeof(input_absinfo))
EVIOCSFF = _IOW("E", 0x80, ctypes.sizeof(ff_effect))
EVIOCRMFF = _IOW("E", 0x81, INT)
EVIOCGEFFECTS = _IOR("E", 0x84, INT)
EVIOCGRAB = _IOW("E", 0x90, INT)

UI_DEV_CREATE = _IO("U", 1)
UI_DEV_DESTROY = _IO("U", 2)
UI_DEV_SETUP = _IOW("U", 3, ctypes.sizeof(uinput_setup))
UI_ABS_SETUP = _IOW("U", 4, ctypes.sizeof(uinput_abs_setup))
UI_SET_EVBIT = _IOW("U", 100, INT)
UI_SET_PHYS = _IOW("U", 108, PTR)
UI_SET_PROPBIT = _IOW("U", 110, INT)
UI_BEGIN_FF_UPLOAD = _IOWR("U", 200, ctypes.sizeof(uinput_ff_upload))
UI_END_FF_UPLOAD = _IOW("U", 201, ctypes.sizeof(uinput_ff_upload))
UI_BEGIN_FF_ERASE = _IOWR("U", 202, ctypes.sizeof(uinput_ff_erase))
UI_END_FF_ERASE = _IOW("U", 203, ctypes.sizeof(uinput_ff_erase))


def UI_SET_BIT(nr): return _IOW("U", nr, INT)


def ioctl(fd, request, arg=0):
    """Thin wrapper so tests can swap the kernel out."""
    return fcntl.ioctl(fd, request, arg)


# --------------------------------------------------------------------------
# sysfs probing (no need to open devices we don't care about)
# --------------------------------------------------------------------------

LONG_BITS = ctypes.sizeof(ctypes.c_long) * 8
SYS_INPUT = "/sys/class/input"
DEV_INPUT = "/dev/input"


def _read(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def parse_bitmap(text):
    """sysfs capability bitmaps: hex longs, most significant word first."""
    bits = set()
    for i, word in enumerate(reversed(text.split())):
        value = int(word, 16)
        base = i * LONG_BITS
        while value:
            low = value & -value
            bits.add(base + low.bit_length() - 1)
            value ^= low
    return bits


def normalise_mac(mac):
    return mac.strip().lower()


MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


class NodeInfo:
    """Everything we know about /dev/input/eventN from sysfs."""

    def __init__(self, event):
        self.event = event                        # "event12"
        self.devnode = os.path.join(DEV_INPUT, event)
        base = os.path.realpath(os.path.join(SYS_INPUT, event, "device"))
        self.sysdir = base
        self.name = _read(os.path.join(base, "name"))
        self.phys = _read(os.path.join(base, "phys"))
        self.uniq = _read(os.path.join(base, "uniq"))
        ids = {}
        for k in ("bustype", "vendor", "product", "version"):
            try:
                ids[k] = int(_read(os.path.join(base, "id", k), "0"), 16)
            except ValueError:
                ids[k] = 0
        self.bustype, self.vendor = ids["bustype"], ids["vendor"]
        self.product, self.version = ids["product"], ids["version"]
        self.caps = {}
        for cap in ("ev",) + tuple(CAP_FILES):
            self.caps[cap] = parse_bitmap(_read(os.path.join(base, "capabilities", cap)))
        self.props = parse_bitmap(_read(os.path.join(base, "properties")))
        self.hid_dir, self.hid_uniq, self.hid_name = self._find_hid_parent(base)

    @staticmethod
    def _find_hid_parent(start):
        path = start
        for _ in range(6):
            path = os.path.dirname(path)
            if not path or path == "/":
                break
            uevent = _read(os.path.join(path, "uevent"))
            if "HID_ID=" in uevent:
                uniq = name = ""
                for line in uevent.splitlines():
                    if line.startswith("HID_UNIQ="):
                        uniq = line.split("=", 1)[1]
                    elif line.startswith("HID_NAME="):
                        name = line.split("=", 1)[1]
                return path, uniq, name
        return None, "", ""

    @property
    def is_balance_board(self):
        return "Balance Board" in self.name or "RVL-WBC" in self.hid_name

    @property
    def mac(self):
        for candidate in (self.uniq, self.hid_uniq):
            m = normalise_mac(candidate)
            if MAC_RE.match(m):
                return m
        return ""

    @property
    def ours(self):
        return self.phys.startswith(PHYS_PREFIX)

    @property
    def is_gamepad(self):
        """Has joystick/gamepad buttons (not just motion sensors or consumer keys)."""
        return any(0x120 <= k <= 0x13F for k in self.caps["key"])

    @property
    def is_controllerish(self):
        """Gamepads, wiimote cores, motion sensors. Not keyboards/mice/consumer keys."""
        keys = self.caps["key"]
        has_buttons = any(BTN_MISC <= k <= KEY_MAX for k in keys)
        has_abs = bool(self.caps["abs"])
        has_ff = bool(self.caps["ff"])
        accel = INPUT_PROP_ACCELEROMETER in self.props
        mouse = bool(self.caps["rel"]) and not has_abs
        return (has_buttons or has_abs or has_ff or accel) and not mouse

    def hidraw_nodes(self):
        if not self.hid_dir:
            return []
        return ["/dev/" + os.path.basename(p)
                for p in glob.glob(os.path.join(self.hid_dir, "hidraw", "hidraw*"))]


def list_event_nodes():
    names = [n for n in os.listdir(SYS_INPUT) if n.startswith("event")] if os.path.isdir(SYS_INPUT) else []
    return sorted(names, key=lambda n: int(n[5:]) if n[5:].isdigit() else 0)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

# Built-in systems. RetroArch entries are core *library names* (the folder
# name RetroArch uses under config/ for core overrides). Add or override
# systems in the config file with a [systems.<name>] table.
BUILTIN_SYSTEMS = {
    "nes":          {"retroarch": ["Mesen", "FCEUmm", "Nestopia", "QuickNES"]},
    "snes":         {"retroarch": ["Snes9x", "bsnes", "bsnes-hd beta", "Mesen-S"]},
    "n64":          {"retroarch": ["Mupen64Plus-Next", "ParaLLEl N64"]},
    "megadrive":    {"retroarch": ["Genesis Plus GX", "Genesis Plus GX Wide", "PicoDrive", "BlastEm"]},
    "mastersystem": {"retroarch": ["Genesis Plus GX", "PicoDrive", "Gearsystem"]},
    "segacd":       {"retroarch": ["Genesis Plus GX", "PicoDrive"]},
    "32x":          {"retroarch": ["PicoDrive"]},
    "gb":           {"retroarch": ["Gambatte", "SameBoy", "mGBA", "Gearboy"]},
    "gbc":          {"retroarch": ["Gambatte", "SameBoy", "mGBA", "Gearboy"]},
    "gba":          {"retroarch": ["mGBA", "gpSP", "VBA-M"]},
    "pcengine":     {"retroarch": ["Beetle PCE", "Beetle PCE Fast"]},
    "psx":          {"retroarch": ["Beetle PSX HW", "Beetle PSX", "SwanStation", "PCSX-ReARMed"]},
    "saturn":       {"retroarch": ["Beetle Saturn", "Kronos", "YabaSanshiro"]},
    "gamecube":     {"dolphin": "gcpad"},
    "wii":          {"dolphin": "wiimote"},
}
# Systems without RetroArch/Dolphin handling still count as "home" systems for ordering.
SYSTEM_ALIASES = {"genesis": "megadrive", "md": "megadrive", "sms": "mastersystem",
                  "gc": "gamecube", "ngc": "gamecube", "famicom": "nes", "sfc": "snes",
                  "superfamicom": "snes", "pce": "pcengine", "tg16": "pcengine", "ps1": "psx"}
ALL_SYSTEMS = None   # filled in after SYSTEM_LABELS is defined
PRESENT_AS = ("original", "switch_pro", "auto")
DEFAULT_SWITCH_EMULATORS = ["eden", "ryujinx", "yuzu", "citron", "sudachi", "suyu", "torzu"]
LED_STYLES = ("switch", "single", "off")


class PadConfig:
    def __init__(self, raw, defaults, index, systems):
        where = f"[[pad]] #{index + 1}"
        self.name = str(raw.get("name", "")).strip()
        if not self.name:
            raise ValueError(f"{where}: 'name' is required")
        if len(self.name.encode()) > 60:
            raise ValueError(f"{where}: name '{self.name}' is too long (max 60 bytes)")
        where = f"{where} ({self.name})"
        self.mac = normalise_mac(str(raw.get("mac", "")))
        self.match_name = str(raw.get("match_name", "")).strip()
        self.match_phys = str(raw.get("match_phys", "")).strip()
        self.cid = None
        self.model = None
        if self.mac and not MAC_RE.match(self.mac):
            raise ValueError(f"{where}: mac '{raw.get('mac')}' is not AA:BB:CC:DD:EE:FF")
        if not self.mac and not self.match_name and not self.match_phys:
            raise ValueError(f"{where}: give either 'mac' or 'match_name'")
        self.hide_hidraw = bool(raw.get("hide_hidraw", defaults.get("hide_hidraw", True)))
        self.all_nodes = bool(raw.get("all_nodes", False))
        self.vendor = self._id(raw, "vendor", where)
        self.product = self._id(raw, "product", where)

        self.player = raw.get("player")
        if self.player is not None:
            if not isinstance(self.player, int) or not 1 <= self.player <= 8:
                raise ValueError(f"{where}: player must be a number 1-8")
        self.present_as = str(raw.get("present_as", "original")).lower()
        if self.present_as not in PRESENT_AS:
            raise ValueError(f"{where}: present_as must be one of {', '.join(PRESENT_AS)}")
        self.leds = str(raw.get("leds", defaults.get("led_style", "switch"))).lower()
        if self.leds not in LED_STYLES:
            raise ValueError(f"{where}: leds must be one of {', '.join(LED_STYLES)}")

        wanted = raw.get("systems", [])
        if isinstance(wanted, str):
            wanted = [wanted]
        self.systems = []
        for sysname in wanted:
            key = SYSTEM_ALIASES.get(str(sysname).lower(), str(sysname).lower())
            if key not in systems:
                raise ValueError(f"{where}: unknown system '{sysname}'. Known: {', '.join(sorted(systems))}")
            if key not in self.systems:
                self.systems.append(key)
        self.ident = self.mac or ("name:" + self.match_name if self.match_name else "phys:" + self.match_phys)
        self.phys = PHYS_PREFIX + (self.mac or re.sub(r"[^A-Za-z0-9]+", "-", self.name).lower())

    @staticmethod
    def _id(raw, key, where):
        v = raw.get(key)
        if v is None:
            return None
        if isinstance(v, str):
            v = int(v, 16)
        if not 0 <= v <= 0xFFFF:
            raise ValueError(f"{where}: {key} must be 0x0000-0xFFFF")
        return v

    def matches(self, node):
        if node.ours:
            return False
        if self.mac:
            return node.mac == self.mac
        if self.match_name:
            return node.name == self.match_name
        return bool(self.match_phys) and node.phys == self.match_phys


class Config:
    def __init__(self, path):
        self.path = path
        if tomllib is None:
            raise RuntimeError("Python 3.11+ is needed to read TOML config")
        with open(path, "rb") as f:
            raw = tomllib.load(f)
        settings = raw.get("settings", {})
        self.log_level = str(settings.get("log_level", "info")).upper()
        self.switch_emulators = [str(x).lower() for x in
                                 settings.get("switch_emulators", DEFAULT_SWITCH_EMULATORS)]
        self.led_style = str(settings.get("led_style", "switch")).lower()
        if self.led_style not in LED_STYLES:
            raise ValueError(f"[settings] led_style must be one of {', '.join(LED_STYLES)}")
        self.hide_hidraw = bool(settings.get("hide_hidraw", True))
        self.pause_on_launch = bool(settings.get("pause_on_launch", True))
        self.manual_scope = str(settings.get("manual_order", "session")).lower()
        if self.manual_scope not in ("session", "persistent", "boot"):
            raise ValueError("[settings] manual_order must be 'session', 'persistent' or 'boot'")
        web = raw.get("web", {})
        self.web_enabled = bool(web.get("enabled", True))
        self.web_bind = str(web.get("bind", "0.0.0.0"))
        self.web_port = int(web.get("port", 8742))
        self.web_pin_required = bool(web.get("require_pin", True))

        self.systems = {k: dict(v) for k, v in ALL_SYSTEMS.items()}
        for name, spec in raw.get("systems", {}).items():
            key = name.lower()
            entry = self.systems.setdefault(key, {})
            if "retroarch_cores" in spec:
                entry["retroarch"] = [str(c) for c in spec["retroarch_cores"]]
            if "dolphin" in spec:
                if spec["dolphin"] not in ("gcpad", "wiimote"):
                    raise ValueError(f"[systems.{name}] dolphin must be 'gcpad' or 'wiimote'")
                entry["dolphin"] = spec["dolphin"]

        ra = raw.get("retroarch", {})
        self.ra_enabled = bool(ra.get("manage", True))
        self.ra_reservation = str(ra.get("reservation", "preferred")).lower()
        if self.ra_reservation not in ("preferred", "reserved"):
            raise ValueError("[retroarch] reservation must be 'preferred' or 'reserved'")
        self.ra_dirs = ra.get("config_dirs")
        dol = raw.get("dolphin", {})
        self.dolphin_enabled = bool(dol.get("manage", True))
        self.dolphin_dirs = dol.get("config_dirs")

        self.pads = [PadConfig(p, settings, i, self.systems) for i, p in enumerate(raw.get("pad", []))]
        seen_names, seen_macs, seats = set(), set(), {}
        for p in self.pads:
            if p.name in seen_names:
                raise ValueError(f"pad name '{p.name}' is used twice")
            seen_names.add(p.name)
            if p.mac:
                if p.mac in seen_macs:
                    raise ValueError(f"mac {p.mac} is listed twice")
                seen_macs.add(p.mac)
            for s in (p.systems if p.player else []):
                other = seats.setdefault((s, p.player), p.name)
                if other != p.name:
                    raise ValueError(f"'{other}' and '{p.name}' both claim {s} player {p.player}")


# --------------------------------------------------------------------------
# udev rules: hide originals from users (and from SDL/Steam enumeration)
# --------------------------------------------------------------------------

def _udev_str(s):
    return s.replace('"', "").replace("\\", "")


def build_udev_rules(config, pads=None, passthrough=()):
    """pads: renamed controllers to hide. passthrough: MACs handed to Dolphin as real
    Wii devices - their input nodes are hidden from games, their raw HID node opened up."""
    pads = config.pads if pads is None else pads
    out = [
        "# Generated by padnamer - edit the padnamer config instead and restart the service.",
        "# Hides the original device nodes of renamed controllers so only the renamed",
        "# copies are visible to games, emulators and Steam.",
        'ACTION=="remove", GOTO="padnamer_end"',
        'SUBSYSTEM=="input", KERNEL=="input*", IMPORT{parent}="HID_UNIQ"',
        'SUBSYSTEM=="input", KERNEL=="event*", IMPORT{parent}="HID_UNIQ"',
        'SUBSYSTEM=="hidraw", IMPORT{parent}="HID_UNIQ"',
    ]
    for pad in pads:
        out.append(f"# {_udev_str(pad.name)}")
        if pad.mac:
            for mac in sorted({pad.mac, pad.mac.upper()}):
                out.append(f'SUBSYSTEM=="input", KERNEL=="event*", ATTRS{{uniq}}=="{mac}", ENV{{PADNAMER_HIDE}}="1"')
                out.append(f'SUBSYSTEM=="input", KERNEL=="event*", ENV{{HID_UNIQ}}=="{mac}", ENV{{PADNAMER_HIDE}}="1"')
                if pad.hide_hidraw:
                    out.append(f'SUBSYSTEM=="hidraw", ENV{{HID_UNIQ}}=="{mac}", ENV{{PADNAMER_HIDE}}="1"')
        elif pad.match_name:
            out.append(f'SUBSYSTEM=="input", KERNEL=="event*", ATTRS{{name}}=="{_udev_str(pad.match_name)}", ENV{{PADNAMER_HIDE}}="1"')
        elif pad.match_phys:
            out.append(f'SUBSYSTEM=="input", KERNEL=="event*", ATTRS{{phys}}=="{_udev_str(pad.match_phys)}", ENV{{PADNAMER_HIDE}}="1"')
    for mac in passthrough:
        out.append(f"# real Wii device for Dolphin: {mac}")
        for m in sorted({mac.lower(), mac.upper()}):
            out.append(f'SUBSYSTEM=="input", KERNEL=="event*", ATTRS{{uniq}}=="{m}", ENV{{PADNAMER_HIDE}}="1"')
            out.append(f'SUBSYSTEM=="input", KERNEL=="event*", ENV{{HID_UNIQ}}=="{m}", ENV{{PADNAMER_HIDE}}="1"')
            out.append(f'SUBSYSTEM=="hidraw", ENV{{HID_UNIQ}}=="{m}", ENV{{PADNAMER_REAL}}="1"')
    out += [
        'ENV{PADNAMER_REAL}=="1", MODE="0660", TAG+="uaccess"',
        "# never hide our own renamed devices",
        f'SUBSYSTEM=="input", KERNEL=="event*", ATTRS{{phys}}=="{PHYS_PREFIX}*", ENV{{PADNAMER_HIDE}}=""',
        'ENV{PADNAMER_HIDE}=="1", MODE="0600", OWNER="root", GROUP="root", TAG-="uaccess", '
        'ENV{ID_INPUT_JOYSTICK}="", ENV{ID_INPUT_ACCELEROMETER}=""',
        'LABEL="padnamer_end"',
        "",
    ]
    return "\n".join(out)


def _udevadm(*args):
    try:
        subprocess.run(["udevadm", *args], check=False, timeout=30,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("udevadm %s failed: %s", " ".join(args), e)


def install_udev_rules(config, pads=None, passthrough=()):
    text = build_udev_rules(config, pads, passthrough)
    if _read(UDEV_RULES_PATH) == text.strip():
        return
    tmp = UDEV_RULES_PATH + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, UDEV_RULES_PATH)
    log.info("wrote %s", UDEV_RULES_PATH)
    _udevadm("control", "--reload")
    _udevadm("trigger", "--action=change", "--subsystem-match=input", "--subsystem-match=hidraw")


def remove_udev_rules():
    try:
        os.unlink(UDEV_RULES_PATH)
    except FileNotFoundError:
        return
    log.info("removed %s, un-hiding original devices", UDEV_RULES_PATH)
    _udevadm("control", "--reload")
    _udevadm("trigger", "--action=change", "--subsystem-match=input", "--subsystem-match=hidraw")


def hide_node(path):
    """Belt and braces for the udev rule: root-only, no user ACL."""
    try:
        st = os.stat(path)
        if st.st_mode & 0o077:
            os.chmod(path, 0o600)
        try:
            os.removexattr(path, "system.posix_acl_access")
        except OSError:
            pass
    except OSError as e:
        log.debug("could not hide %s: %s", path, e)


# --------------------------------------------------------------------------
# Naming of multi-node controllers (Wii Remote, Joy-Con IMU, ...)
# --------------------------------------------------------------------------

SUFFIXES = sorted([
    "(IMU)", "IMU", "Accelerometer", "IR", "Nunchuk", "Classic Controller",
    "Pro Controller", "Balance Board", "Motion Plus", "Motion Sensors",
    "Drums", "Guitar", "Turntable", "Touchpad", "Motion", "Sensors",
], key=len, reverse=True)


def virtual_name(pad_name, node, taken):
    suffix = ""
    for s in SUFFIXES:
        if node.name.endswith(" " + s):
            suffix = " " + s
            break
    if not suffix and INPUT_PROP_ACCELEROMETER in node.props:
        suffix = " Motion"
    name = pad_name + suffix
    n = 2
    while name in taken:
        name = f"{pad_name}{suffix} {n}"
        n += 1
    return name


# --------------------------------------------------------------------------
# Layouts: present a Switch Online retro pad as a Switch Pro Controller
# --------------------------------------------------------------------------

BTN_SOUTH, BTN_EAST, BTN_C, BTN_NORTH, BTN_WEST, BTN_Z = 0x130, 0x131, 0x132, 0x133, 0x134, 0x135
BTN_TL, BTN_TR, BTN_TL2, BTN_TR2 = 0x136, 0x137, 0x138, 0x139
BTN_SELECT, BTN_START, BTN_MODE, BTN_THUMBL, BTN_THUMBR = 0x13A, 0x13B, 0x13C, 0x13D, 0x13E
ABS_X, ABS_Y, ABS_RX, ABS_RY, ABS_HAT0X, ABS_HAT0Y = 0x00, 0x01, 0x03, 0x04, 0x10, 0x11

NINTENDO_VID = 0x057E
PROCON_PID = 0x2009

# Straight from drivers/hid/hid-nintendo.c: for each NSO pad, which evdev code
# the kernel reports for each button, and which Switch button that is in the
# controller's own HID report (i.e. what a real Switch sees).
NSO_LAYOUTS = {
    "nes": {BTN_SOUTH: "A", BTN_EAST: "B", BTN_TL: "L", BTN_TR: "R",
            BTN_SELECT: "MINUS", BTN_START: "PLUS"},
    "snes": {BTN_EAST: "A", BTN_SOUTH: "B", BTN_NORTH: "X", BTN_WEST: "Y",
             BTN_TL: "L", BTN_TR: "R", BTN_TL2: "ZL", BTN_TR2: "ZR",
             BTN_SELECT: "MINUS", BTN_START: "PLUS"},
    "megadrive": {BTN_WEST: "A", BTN_SOUTH: "B", BTN_EAST: "R", BTN_TL: "X",
                  BTN_NORTH: "Y", BTN_TR: "L", BTN_SELECT: "ZR", BTN_START: "PLUS",
                  BTN_MODE: "HOME", BTN_Z: "CAPTURE"},
    "n64": {BTN_SOUTH: "A", BTN_EAST: "B", BTN_TL2: "ZL", BTN_TL: "L", BTN_TR: "R",
            BTN_TR2: "LSTICK", BTN_START: "PLUS", BTN_SELECT: "Y", BTN_NORTH: "ZR",
            BTN_WEST: "X", BTN_C: "MINUS", BTN_MODE: "HOME", BTN_Z: "CAPTURE"},
}
# hid-nintendo's procon_button_mappings: Switch button -> evdev code
PRO_BUTTONS = {"A": BTN_EAST, "B": BTN_SOUTH, "X": BTN_NORTH, "Y": BTN_WEST,
               "L": BTN_TL, "R": BTN_TR, "ZL": BTN_TL2, "ZR": BTN_TR2,
               "MINUS": BTN_SELECT, "PLUS": BTN_START, "LSTICK": BTN_THUMBL,
               "RSTICK": BTN_THUMBR, "HOME": BTN_MODE, "CAPTURE": BTN_Z}
_STICK = (-32767, 32767, 250, 500)      # JC_MAX_STICK_MAG, JC_STICK_FUZZ, JC_STICK_FLAT
_HAT = (-1, 1, 0, 0)
PRO_ABS = {ABS_X: _STICK, ABS_Y: _STICK, ABS_RX: _STICK, ABS_RY: _STICK,
           ABS_HAT0X: _HAT, ABS_HAT0Y: _HAT}


class Layout:
    """How the virtual device differs from the real one."""

    def __init__(self, kind, keys, abs_axes, keymap, vendor, product):
        self.kind = kind              # e.g. "n64 -> switch_pro"
        self.keys = keys              # set of key codes to advertise
        self.abs = abs_axes           # {axis: (min, max, fuzz, flat)}
        self.keymap = keymap          # real key code -> virtual key code
        self.vendor, self.product = vendor, product


def detect_nso_type(node):
    if node.vendor != NINTENDO_VID:
        return None
    keys = node.caps["key"]
    if keys == set(PRO_BUTTONS.values()) and {ABS_RX, ABS_RY} <= node.caps["abs"]:
        return "pro"
    for kind, table in NSO_LAYOUTS.items():
        if keys == set(table):
            return kind
    return None


def switch_emulator_running(names, proc="/proc"):
    """True if a process whose name or argv[0] starts with one of `names` is running."""
    if not names:
        return False
    try:
        pids = [d for d in os.listdir(proc) if d.isdigit()]
    except OSError:
        return False
    for pid in pids:
        comm = _read(os.path.join(proc, pid, "comm")).lower()
        try:
            with open(os.path.join(proc, pid, "cmdline"), "rb") as f:
                argv0 = f.read(512).split(b"\0", 1)[0].decode(errors="replace")
        except OSError:
            argv0 = ""
        base = os.path.basename(argv0).lower()
        if any(comm.startswith(n) or base.startswith(n) for n in names):
            return True
    return False


def effective_present_as(pad, switch_active):
    if pad.present_as == "auto":
        return "switch_pro" if switch_active else "original"
    return pad.present_as


def build_layout(pad, node, switch_active=False):
    """Returns a Layout, or None to clone the device as-is."""
    if effective_present_as(pad, switch_active) != "switch_pro" or not node.is_controllerish:
        return None
    if INPUT_PROP_ACCELEROMETER in node.props:
        return None                    # motion node of a Pro/Joy-Con: leave alone
    kind = detect_nso_type(node)
    if kind is None:
        log.warning("%s: %s isn't a Switch Online / Pro pad hid-nintendo knows; "
                    "keeping its own layout", pad.name, node.name)
        return None
    if kind == "pro":
        keymap = {c: c for c in PRO_BUTTONS.values()}
    else:
        keymap = {code: PRO_BUTTONS[button] for code, button in NSO_LAYOUTS[kind].items()}
    return Layout(f"{kind} -> switch_pro", set(PRO_BUTTONS.values()), dict(PRO_ABS),
                  keymap, NINTENDO_VID, PROCON_PID)


# --------------------------------------------------------------------------
# Player LEDs
# --------------------------------------------------------------------------

# hid-nintendo / Nintendo's official player patterns (players 1-8)
SWITCH_LED_PATTERNS = [(1, 0, 0, 0), (1, 1, 0, 0), (1, 1, 1, 0), (1, 1, 1, 1),
                       (1, 0, 0, 1), (1, 0, 1, 0), (1, 0, 1, 1), (0, 1, 1, 0)]


def find_player_leds(hid_dir):
    """Returns ([brightness paths in LED order], kind) or ([], None).

    kind: 'switch' (hid-nintendo, 4 LEDs), 'wii' (hid-wiimote, 4 LEDs) or
    'playstation' (hid-playstation DualSense, 5 LEDs).
    """
    if not hid_dir:
        return [], None
    found, kind = {}, None
    for path in glob.glob(os.path.join(hid_dir, "leds", "*")):
        name = os.path.basename(path)
        m = re.search(r":white:player-([1-5])$", name)    # hid-playstation (DualSense)
        if m:
            found[int(m.group(1)) - 1], kind = path, "playstation"
            continue
        m = re.search(r":player-([1-4])$", name)          # hid-nintendo
        if m:
            found[int(m.group(1)) - 1], kind = path, "switch"
            continue
        m = re.search(r":p([0-3])$", name)                 # hid-wiimote
        if m:
            found[int(m.group(1))], kind = path, "wii"
    want = 5 if kind == "playstation" else 4
    if len(found) != want:
        return [], None
    return [os.path.join(found[i], "brightness") for i in range(want)], kind


def find_lightbar(hid_dir):
    """The RGB light bar of a DualShock 4 / DualSense (multicolor LED class dir)."""
    if not hid_dir:
        return None
    for path in glob.glob(os.path.join(hid_dir, "leds", "*:rgb:indicator")):
        if os.path.exists(os.path.join(path, "multi_intensity")):
            return path
    return None


# hid-playstation's dualsense_set_player_leds: centred like a PS5 (players 1-5)
PS_LED_PATTERNS = [(0, 0, 1, 0, 0), (0, 1, 0, 1, 0), (1, 0, 1, 0, 1), (1, 1, 0, 1, 1), (1, 1, 1, 1, 1)]
# PS4 order (blue, red, green, pink) then extras for 5-8
PLAYER_COLOURS = [(0, 0, 255), (255, 0, 0), (0, 255, 0), (255, 0, 255),
                  (255, 128, 0), (0, 255, 255), (255, 255, 0), (255, 255, 255)]
LIGHTBAR_BRIGHTNESS = 64            # of 255: the kernel's own player colours are this dim


def led_pattern(player, style, kind):
    if kind == "playstation":
        return PS_LED_PATTERNS[(player - 1) % 5]
    if style == "single" or kind == "wii":
        # Wii convention (and 'single'): one LED per player, 5-8 reuse the Switch extras
        if player <= 4:
            return tuple(int(i == player - 1) for i in range(4))
    return SWITCH_LED_PATTERNS[(player - 1) % 8]


def _write_leds(paths, pattern, force=False):
    for path, want in zip(paths, pattern):
        cur = _read(path)
        if not force and cur.isdigit() and (int(cur) > 0) == bool(want):
            continue
        try:
            with open(path, "w") as f:
                f.write("1" if want else "0")
        except OSError as e:
            log.debug("LED write %s failed: %s", path, e)
            return False
    return True


def set_lightbar(path, rgb, brightness=LIGHTBAR_BRIGHTNESS):
    if not path:
        return False
    want = " ".join(str(c) for c in rgb)
    try:
        if _read(os.path.join(path, "multi_intensity")) != want:
            with open(os.path.join(path, "multi_intensity"), "w") as f:
                f.write(want)
        if _read(os.path.join(path, "brightness")) != str(brightness):
            with open(os.path.join(path, "brightness"), "w") as f:
                f.write(str(brightness))
        return True
    except OSError as e:
        log.debug("light bar write %s failed: %s", path, e)
        return False


def apply_player_leds(hid_dir, player, style):
    """Set the LEDs; returns the pattern string shown, or '' if not possible."""
    if style == "off" or not player:
        return ""
    paths, kind = find_player_leds(hid_dir)
    if not paths:
        return ""
    pattern = led_pattern(player, style, kind)
    if not _write_leds(paths, pattern):
        return ""
    return "".join("●" if x else "○" for x in pattern)


def describe_indicators(hid_dir):
    paths, kind = find_player_leds(hid_dir)
    return {"player_leds": len(paths), "led_kind": kind, "lightbar": bool(find_lightbar(hid_dir))}


def apply_indicators(hid_dir, player, style):
    """Player LEDs and light bar colour. Returns {'pattern': str, 'colour': '#rrggbb' or ''}."""
    out = {"pattern": "", "colour": ""}
    if style == "off" or not player:
        return out
    out["pattern"] = apply_player_leds(hid_dir, player, style)
    rgb = PLAYER_COLOURS[(player - 1) % len(PLAYER_COLOURS)]
    if set_lightbar(find_lightbar(hid_dir), rgb):
        out["colour"] = "#%02x%02x%02x" % rgb
    return out


# --------------------------------------------------------------------------
# Controller models
# --------------------------------------------------------------------------

SYSTEM_LABELS = {
    "nes": "NES", "snes": "SNES", "n64": "Nintendo 64", "gamecube": "GameCube",
    "wii": "Wii", "wiiu": "Wii U", "switch": "Switch", "gb": "Game Boy",
    "gbc": "Game Boy Color", "gba": "Game Boy Advance", "nds": "Nintendo DS",
    "3ds": "Nintendo 3DS", "megadrive": "Mega Drive / Genesis", "mastersystem": "Master System",
    "segacd": "Mega-CD", "32x": "32X", "saturn": "Saturn", "dreamcast": "Dreamcast",
    "psx": "PlayStation", "ps2": "PlayStation 2", "ps3": "PlayStation 3", "psp": "PSP",
    "vita": "PS Vita", "xbox": "Xbox", "xbox360": "Xbox 360", "pcengine": "PC Engine",
    "arcade": "Arcade",
}

ALL_SYSTEMS = dict(BUILTIN_SYSTEMS)
for _s in SYSTEM_LABELS:
    ALL_SYSTEMS.setdefault(_s, {})

# id: (label, short name for auto-naming, family, default home systems)
MODELS = {
    "xbox_series":    ("Xbox Series X|S controller", "Xbox", "xbox", ["xbox", "xbox360"]),
    "xbox_one":       ("Xbox One controller", "Xbox One", "xbox", ["xbox", "xbox360"]),
    "xbox_360":       ("Xbox 360 controller", "Xbox 360", "xbox", ["xbox360"]),
    "dualsense":      ("PS5 DualSense", "DualSense", "playstation", ["psx", "ps2", "ps3", "psp", "vita"]),
    "dualsense_edge": ("PS5 DualSense Edge", "DualSense Edge", "playstation", ["psx", "ps2", "ps3", "psp", "vita"]),
    "dualshock4":     ("PS4 DualShock 4", "DualShock 4", "playstation", ["psx", "ps2", "ps3", "psp", "vita"]),
    "dualshock3":     ("PS3 DualShock 3", "DualShock 3", "playstation", ["psx", "ps2", "ps3"]),
    "switch_pro":     ("Switch Pro Controller", "Switch Pro", "nintendo", ["switch"]),
    "joycon_l":       ("Joy-Con (L)", "Joy-Con L", "nintendo", ["switch"]),
    "joycon_r":       ("Joy-Con (R)", "Joy-Con R", "nintendo", ["switch"]),
    "joycon_pair":    ("Joy-Con pair (combined)", "Joy-Cons", "nintendo", ["switch"]),
    "nso_nes":        ("NES controller (Switch Online)", "NES", "nintendo", ["nes"]),
    "nso_snes":       ("SNES controller (Switch Online)", "SNES", "nintendo", ["snes"]),
    "nso_n64":        ("N64 controller (Switch Online)", "N64", "nintendo", ["n64"]),
    "nso_megadrive":  ("Mega Drive controller (Switch Online)", "Mega Drive", "nintendo",
                       ["megadrive", "mastersystem", "segacd", "32x"]),
    "switch2_gc":     ("GameCube controller (Switch 2)", "GameCube", "nintendo", ["gamecube"]),
    "wii_remote":     ("Wii Remote", "Wii Remote", "nintendo", ["wii"]),
    "wiiu_pro":       ("Wii U Pro Controller", "Wii U Pro", "nintendo", ["wiiu", "wii"]),
    "wii_balance_board": ("Wii Balance Board", "Balance Board", "nintendo", ["wii"]),
    "8bitdo":         ("8BitDo controller", "8BitDo", "other", []),
    "steam_deck":     ("Steam Deck controls", "Steam Deck", "valve", []),
    "generic":        ("Other controller", "Controller", "other", []),
}

VIDPID_MODELS = {
    (0x045E, 0x0B12): "xbox_series", (0x045E, 0x0B13): "xbox_series",
    (0x045E, 0x0B20): "xbox_series", (0x045E, 0x0B21): "xbox_series", (0x045E, 0x0B22): "xbox_series",
    (0x045E, 0x02E0): "xbox_one", (0x045E, 0x02EA): "xbox_one", (0x045E, 0x02FD): "xbox_one",
    (0x045E, 0x0B00): "xbox_one", (0x045E, 0x0B05): "xbox_one",
    (0x045E, 0x028E): "xbox_360", (0x045E, 0x0719): "xbox_360",
    (0x054C, 0x0CE6): "dualsense", (0x054C, 0x0DF2): "dualsense_edge",
    (0x054C, 0x05C4): "dualshock4", (0x054C, 0x09CC): "dualshock4", (0x054C, 0x0BA0): "dualshock4",
    (0x054C, 0x0268): "dualshock3",
    (0x057E, 0x2009): "switch_pro", (0x057E, 0x2006): "joycon_l", (0x057E, 0x2007): "joycon_r",
    (0x057E, 0x2008): "joycon_pair", (0x057E, 0x2017): "nso_snes", (0x057E, 0x201E): "nso_megadrive",
    (0x057E, 0x2019): "nso_n64", (0x057E, 0x0306): "wii_remote", (0x057E, 0x0330): "wiiu_pro",
    (0x057E, 0x2073): "switch2_gc",
    (0x28DE, 0x1205): "steam_deck",
}
NSO_KIND_MODELS = {"nes": "nso_nes", "snes": "nso_snes", "n64": "nso_n64",
                   "megadrive": "nso_megadrive", "pro": "switch_pro"}


# Devices Dolphin can drive directly as real Wii hardware (it opens their raw HID node)
DOLPHIN_REAL_MODELS = {"wii_remote", "wiiu_pro", "wii_balance_board"}


def detect_model(node):
    if node.vendor == NINTENDO_VID and node.is_balance_board:
        return "wii_balance_board"
    kind = detect_nso_type(node)
    if kind:                          # caps beat IDs: NES pads report Joy-Con IDs
        return NSO_KIND_MODELS[kind]
    model = VIDPID_MODELS.get((node.vendor, node.product))
    if model:
        return model
    if node.vendor == 0x2DC8:
        return "8bitdo"
    name = node.name.lower()
    for needle, model in (("xbox", "xbox_series"), ("dualsense", "dualsense"),
                          ("wireless controller", "dualshock4"), ("pro controller", "switch_pro"),
                          ("wii remote", "wii_remote"), ("joy-con", "joycon_pair"),
                          ("gamecube", "switch2_gc"), ("8bitdo", "8bitdo")):
        if needle in name:
            return model
    return "generic"


def node_identity(node):
    """(id, kind) a controller is remembered by, or (None, None)."""
    if node.mac:
        return node.mac, "mac"
    if (node.vendor, node.product) == (NINTENDO_VID, 0x2073) or "joy-cons" in node.name.lower():
        return "name:" + node.name, "name"          # nsogcd / joycond: no MAC
    if node.phys:
        return "phys:" + node.phys, "phys"
    return None, None


# --------------------------------------------------------------------------
# Registry: every controller ever seen, editable from the web UI
# --------------------------------------------------------------------------

class Registry:
    """~/.config/padnamer/controllers.json - controllers, default order, manual order, PIN."""

    def __init__(self, files, path=None):
        self.files = files
        self.path = path or os.path.join(files.home, ".config", "padnamer", "controllers.json")
        self.data = {"version": 1, "controllers": {}, "order": []}
        self.dirty = False
        try:
            with open(self.path) as f:
                loaded = json.load(f)
            if isinstance(loaded, dict) and isinstance(loaded.get("controllers"), dict):
                self.data.update(loaded)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            log.error("controllers.json unreadable (%s); starting a fresh list", e)
            try:
                os.replace(self.path, self.path + ".broken")
            except OSError:
                pass
        self.data.setdefault("order", [])
        for cid in self.controllers:
            if cid not in self.data["order"]:
                self.data["order"].append(cid)

    @property
    def controllers(self):
        return self.data["controllers"]

    @property
    def order(self):
        return self.data["order"]

    def save(self, force=False):
        if not (self.dirty or force):
            return
        self.files.write(self.path, json.dumps(self.data, indent=2, sort_keys=True) + "\n")
        self.dirty = False

    def pin(self):
        web = self.data.setdefault("web", {})
        if not web.get("pin"):
            import secrets
            web["pin"] = "%04d" % secrets.randbelow(10000)
            self.dirty = True
        return web["pin"]

    def _unique_name(self, base):
        taken = {c.get("name") for c in self.controllers.values()}
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            name = f"{base} {letter}"
            if name not in taken:
                return name
        n = 27
        while f"{base} {n}" in taken:
            n += 1
        return f"{base} {n}"

    def import_legacy(self, config):
        """Bring [[pad]] entries from the TOML config in (once)."""
        if self.data.get("imported_toml"):
            return
        pads = sorted(config.pads, key=lambda p: (p.player or 99))
        for p in pads:
            cid = p.mac or "name:" + p.match_name
            if p.match_name and any(x.match_name == p.match_name for x in pads if x is not p):
                cid += "#%d" % (pads.index(p) + 1)
            if cid in self.controllers:
                continue
            self.controllers[cid] = {
                "id": cid, "kind": "mac" if p.mac else "name", "mac": p.mac,
                "match_name": p.match_name, "name": p.name, "model": None,
                "detected_model": None, "home": list(p.systems), "present_as": p.present_as,
                "leds": "off" if p.leds == "off" else "auto", "manage": True,
                "first_seen": None, "last_seen": None, "original_name": "",
            }
            self.order.append(cid)
        self.data["imported_toml"] = True
        self.dirty = True

    def observe(self, node, auto_add=True, in_use=()):
        """Record a sighting; register new Bluetooth gamepads. Returns entry or None.

        in_use: ids already bound to another device right now (for identical
        no-MAC pads, which fill name#1, name#2... in connection order).
        """
        cid, kind = node_identity(node)
        if cid is None:
            return None
        if kind == "name":
            entry = next((c for c in self.name_slots(node.name) if c["id"] not in in_use), None)
        else:
            entry = self.controllers.get(cid)
        now = int(time.time())
        if entry is None:
            if not auto_add or not node.is_gamepad or node.ours:
                return None
            model = detect_model(node)
            # Auto-manage Bluetooth pads and no-MAC Nintendo helpers; USB/internal stay opt-in.
            # A Balance Board goes to Dolphin untouched: Dolphin only supports the real thing.
            passthrough = model == "wii_balance_board"
            manage = (kind == "name" or node.bustype == BUS_BLUETOOTH) and not passthrough
            if kind == "name":
                cid = self._next_name_slot_id(node.name)
            entry = {
                "id": cid, "kind": kind, "mac": node.mac if kind == "mac" else "",
                "match_name": node.name if kind == "name" else "",
                "phys": node.phys if kind == "phys" else "",
                "name": self._unique_name(MODELS[model][1]), "model": None,
                "detected_model": model, "home": None, "present_as": "original",
                "leds": "auto", "manage": manage, "passthrough": passthrough,
                "first_seen": now, "last_seen": now,
                "original_name": node.name, "vid": node.vendor, "pid": node.product,
            }
            self.controllers[cid] = entry
            self.order.append(cid)
            self.dirty = True
            log.info("new controller: %s (%s) -> '%s'%s", node.name, cid, entry["name"],
                     "" if manage else " (not managed until enabled in the web UI)")
            return entry
        changed = False
        model = detect_model(node)
        if model == "wii_balance_board" and entry.get("detected_model") != model:
            # first saw the board's generic Wii Remote node; now we know what it is
            entry["detected_model"], changed = model, True
            if not entry.get("mode_set_by_user"):
                entry["manage"], entry["passthrough"] = False, True
        elif INPUT_PROP_ACCELEROMETER not in node.props and \
                entry.get("detected_model") in (None, "generic") and model != "generic":
            entry["detected_model"], changed = model, True
        if not entry.get("original_name") and node.name:
            entry["original_name"], changed = node.name, True
        if now - (entry.get("last_seen") or 0) > 60:
            entry["last_seen"], changed = now, True
        if changed:
            self.dirty = True
        return entry

    def _next_name_slot_id(self, name):
        n = 1
        while f"name:{name}#{n}" in self.controllers:
            n += 1
        return f"name:{name}#{n}"

    def name_slots(self, name):
        return sorted((c for c in self.controllers.values()
                       if c.get("kind") == "name" and c.get("match_name") == name),
                      key=lambda c: c["id"])

    def model_of(self, entry):
        return entry.get("model") or entry.get("detected_model") or "generic"

    def home_of(self, entry):
        if entry.get("home") is not None:
            return list(entry["home"])
        return list(MODELS[self.model_of(entry)][3])

    def update(self, cid, changes):
        entry = self.controllers.get(cid)
        if entry is None:
            raise KeyError(cid)
        if "name" in changes:
            name = re.sub(r"[\x00-\x1f\"\\]", "", str(changes["name"])).strip()
            if not name or len(name.encode()) > 60:
                raise ValueError("name must be 1-60 characters")
            if "/" in name or name.startswith("."):
                raise ValueError("names can't contain / or start with a dot")
            if any(c.get("name") == name for k, c in self.controllers.items() if k != cid):
                raise ValueError(f"'{name}' is already used by another controller")
            entry["name"] = name
        if "model" in changes:
            m = changes["model"]
            if m not in (None, "", "auto") and m not in MODELS:
                raise ValueError("unknown model")
            entry["model"] = None if m in (None, "", "auto") else m
        if "home" in changes:
            home = changes["home"]
            if home is not None:
                home = [SYSTEM_ALIASES.get(h, h) for h in home]
                bad = [h for h in home if h not in SYSTEM_LABELS]
                if bad:
                    raise ValueError(f"unknown system {bad[0]}")
            entry["home"] = home
        if "present_as" in changes:
            if changes["present_as"] not in PRESENT_AS:
                raise ValueError("present_as must be original, switch_pro or auto")
            entry["present_as"] = changes["present_as"]
        if "leds" in changes:
            if changes["leds"] not in ("auto", "off", "single", "switch"):
                raise ValueError("leds must be auto, off, single or switch")
            entry["leds"] = changes["leds"]
        if "manage" in changes:
            entry["manage"] = bool(changes["manage"])
            if entry["manage"]:
                entry["passthrough"] = False
            entry["mode_set_by_user"] = True
        if "mode" in changes:
            mode = changes["mode"]
            if mode not in ("managed", "dolphin", "ignore"):
                raise ValueError("mode must be managed, dolphin or ignore")
            if mode == "dolphin":
                if entry.get("kind") != "mac" or self.model_of(entry) not in DOLPHIN_REAL_MODELS:
                    raise ValueError("only Wii Remotes, Wii U Pro Controllers and Balance Boards "
                                     "can be handed to Dolphin as real devices")
            entry["manage"] = mode == "managed"
            entry["passthrough"] = mode == "dolphin"
            entry["mode_set_by_user"] = True
        self.dirty = True
        return entry

    @staticmethod
    def mode_of(entry):
        if entry.get("passthrough"):
            return "dolphin"
        return "managed" if entry.get("manage") else "ignore"

    def passthrough_macs(self):
        return sorted(c["mac"] for c in self.controllers.values()
                      if c.get("passthrough") and c.get("kind") == "mac" and c.get("mac"))

    def forget(self, cid):
        if self.controllers.pop(cid, None) is None:
            raise KeyError(cid)
        if cid in self.order:
            self.order.remove(cid)
        manual = self.data.get("manual")
        if manual and cid in manual.get("ids", []):
            manual["ids"].remove(cid)
        self.dirty = True

    def set_order(self, ids):
        known = [i for i in ids if i in self.controllers]
        rest = [i for i in self.order if i not in known]
        self.data["order"] = known + rest
        self.dirty = True

    def pad_configs(self, settings):
        """PadConfig objects for managed controllers, in default order."""
        pads = []
        for cid in self.order:
            e = self.controllers.get(cid)
            if not e or not e.get("manage"):
                continue
            raw = {"name": e["name"], "present_as": e.get("present_as", "original"),
                   "systems": [h for h in self.home_of(e) if h in SYSTEM_LABELS]}
            if e.get("kind") == "mac":
                raw["mac"] = e["mac"]
            elif e.get("kind") == "name":
                raw["match_name"] = e["match_name"]
            else:
                raw["match_phys"] = e.get("phys", "")
            leds = e.get("leds", "auto")
            raw["leds"] = settings.get("led_style", "switch") if leds == "auto" else leds
            try:
                pad = PadConfig(raw, settings, len(pads), ALL_SYSTEMS)
            except ValueError as err:
                log.warning("controller %s skipped: %s", cid, err)
                continue
            pad.cid = cid
            pad.model = self.model_of(e)
            pads.append(pad)
        return pads


# --------------------------------------------------------------------------
# Player order
# --------------------------------------------------------------------------

class OrderEngine:
    """Decides who is Player 1, 2, ...

    Default: the global order (web UI), except that while a game is running
    the pads whose home systems include that game's system go first.
    A manual order (web UI / Decky panel) overrides that. With scope
    'session' it lasts until the game it was set for (or the next game
    started, if set between games) closes; 'persistent' until reset;
    'boot' until the service restarts.
    """

    def __init__(self, registry, scope="session"):
        self.registry = registry
        self.scope = scope
        self.session = None          # {"emulator", "systems", "pid", "since"}
        self.manual = None           # {"ids": [...], "pending": bool}
        saved = registry.data.get("manual")
        if scope == "persistent" and saved and saved.get("ids"):
            self.manual = {"ids": list(saved["ids"]), "pending": False}

    def _persist(self):
        if self.scope == "persistent":
            if self.manual:
                self.registry.data["manual"] = {"ids": self.manual["ids"]}
            else:
                self.registry.data.pop("manual", None)
            self.registry.dirty = True

    def set_manual(self, ids):
        self.manual = {"ids": [i for i in ids if i in self.registry.controllers],
                       "pending": self.session is None}
        self._persist()

    def clear_manual(self):
        self.manual = None
        self._persist()

    def start_session(self, emulator, systems, pid):
        self.session = {"emulator": emulator, "systems": list(systems), "pid": pid,
                        "since": int(time.time())}
        if self.manual and self.manual.get("pending"):
            self.manual["pending"] = False            # this game is the one it was set for

    def end_session(self):
        self.session = None
        if self.manual and self.scope == "session" and not self.manual.get("pending"):
            self.manual = None

    def active(self, connected, homes):
        """connected: ids currently connected; homes: {id: [systems]} -> ids in player order."""
        connected = [i for i in self.registry.order if i in connected] + \
                    [i for i in connected if i not in self.registry.order]
        if self.manual:
            first = [i for i in self.manual["ids"] if i in connected]
            return first + [i for i in connected if i not in first]
        if self.session and self.session["systems"]:
            systems = set(self.session["systems"])
            own = [i for i in connected if systems & set(homes.get(i, []))]
            return own + [i for i in connected if i not in own]
        return connected


# --------------------------------------------------------------------------
# The bridge: one real node <-> one virtual node, with rumble passthrough
# --------------------------------------------------------------------------

class Bridge:
    def __init__(self, pad, node, name, layout=None):
        self.pad = pad
        self.node = node
        self.name = name
        self.layout = layout
        self.effects = {}           # virtual effect id -> real effect id
        self.caps = dict(node.caps)
        if layout:
            self.caps["key"] = set(layout.keys)
            self.caps["abs"] = set(layout.abs)
            self.caps["ev"] = set(node.caps["ev"]) | {EV_KEY, EV_ABS}
            self.caps["msc"] = set()
        self.rfd = self.vfd = None
        self.rfd = os.open(node.devnode, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            ioctl(self.rfd, EVIOCGRAB, 1)
            self.vfd = self._create_virtual()
        except Exception:
            os.close(self.rfd)
            raise
        log.info("%s: %s (%s) -> '%s'%s", pad.name, node.devnode, node.name, name,
                 f" as {layout.kind}" if layout else "")

    # ---- virtual device -------------------------------------------------
    def _create_virtual(self):
        node, pad, caps, layout = self.node, self.pad, self.caps, self.layout
        vfd = os.open("/dev/uinput", os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            for ev in sorted(caps["ev"]):
                # EV_REP left out: kernel autorepeat on the copy would double up
                # the repeat events already forwarded from the real device.
                if ev not in (EV_UINPUT, EV_FF_STATUS, EV_PWR, EV_REP):
                    ioctl(vfd, UI_SET_EVBIT, ev)
            for cap, (ev, nr) in CAP_FILES.items():
                if ev in caps["ev"]:
                    for code in sorted(caps[cap]):
                        ioctl(vfd, UI_SET_BIT(nr), code)
            for prop in sorted(node.props):
                ioctl(vfd, UI_SET_PROPBIT, prop)
            ioctl(vfd, UI_SET_PHYS, pad.phys.encode() + b"\0")

            setup = uinput_setup()
            setup.id.bustype = node.bustype
            vendor = layout.vendor if layout else node.vendor
            product = layout.product if layout else node.product
            setup.id.vendor = vendor if pad.vendor is None else pad.vendor
            setup.id.product = product if pad.product is None else pad.product
            setup.id.version = node.version
            setup.name = self.name.encode()[:79]
            if EV_FF in node.caps["ev"]:
                count = ctypes.c_int(0)
                try:
                    ioctl(self.rfd, EVIOCGEFFECTS, count)
                except OSError:
                    pass
                setup.ff_effects_max = max(count.value, 1)
            ioctl(vfd, UI_DEV_SETUP, setup)

            if EV_ABS in caps["ev"]:
                for axis in sorted(caps["abs"]):
                    info = input_absinfo()
                    if layout and axis in layout.abs:
                        info.minimum, info.maximum, info.fuzz, info.flat = layout.abs[axis]
                    else:
                        ioctl(self.rfd, EVIOCGABS(axis), info)
                    ab = uinput_abs_setup()
                    ab.code = axis
                    ab.absinfo = info
                    ioctl(vfd, UI_ABS_SETUP, ab)

            ioctl(vfd, UI_DEV_CREATE)
            return vfd
        except Exception:
            os.close(vfd)
            raise

    # ---- real -> virtual: input events ------------------------------------
    def pump_real(self):
        """Returns False when the controller has gone away."""
        while True:
            try:
                data = os.read(self.rfd, EVENT_SIZE * 64)
            except BlockingIOError:
                return True
            except OSError as e:
                if e.errno in (errno.ENODEV, errno.EIO, errno.EBADF):
                    return False
                raise
            if not data:
                return False
            if self.layout is not None:
                data = self._translate(data)
                if not data:
                    continue
            os.write(self.vfd, data)

    def _translate(self, data):
        keymap, axes = self.layout.keymap, self.layout.abs
        out = bytearray()
        for off in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
            sec, usec, etype, code, value = struct.unpack_from(EVENT_FMT, data, off)
            if etype == EV_KEY:
                code = keymap.get(code)
                if code is None:
                    continue
            elif etype == EV_ABS and code not in axes:
                continue
            elif etype == EV_MSC:
                continue                       # scancodes would describe the old layout
            out += struct.pack(EVENT_FMT, sec, usec, etype, code, value)
        return bytes(out)

    # ---- virtual -> real: rumble / LEDs -----------------------------------
    def pump_virtual(self):
        while True:
            try:
                data = os.read(self.vfd, EVENT_SIZE * 64)
            except BlockingIOError:
                return True
            except OSError as e:
                if e.errno in (errno.ENODEV, errno.EIO, errno.EBADF):
                    return False
                raise
            if not data:
                return True
            for off in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
                _, _, etype, code, value = struct.unpack_from(EVENT_FMT, data, off)
                self._handle_virtual_event(etype, code, value)

    def _handle_virtual_event(self, etype, code, value):
        if etype == EV_UINPUT:
            if code == UI_FF_UPLOAD:
                self._ff_upload(value)
            elif code == UI_FF_ERASE:
                self._ff_erase(value)
        elif etype == EV_FF:
            if code in (FF_GAIN, FF_AUTOCENTER):
                self._write_real(EV_FF, code, value)
            elif code in self.effects:
                self._write_real(EV_FF, self.effects[code], value)
        elif etype == EV_LED:
            self._write_real(EV_LED, code, value)
            self._write_real(EV_SYN, 0, 0)

    def _write_real(self, etype, code, value):
        try:
            os.write(self.rfd, struct.pack(EVENT_FMT, 0, 0, etype, code, value))
        except OSError as e:
            log.debug("%s: write to real device failed: %s", self.name, e)

    def _ff_upload(self, request_id):
        up = uinput_ff_upload()
        up.request_id = request_id
        ioctl(self.vfd, UI_BEGIN_FF_UPLOAD, up)
        effect = ff_effect.from_buffer_copy(up.effect)
        virtual_id = effect.id
        effect.id = self.effects.get(virtual_id, -1)
        try:
            ioctl(self.rfd, EVIOCSFF, effect)
            self.effects[virtual_id] = effect.id
            up.retval = 0
            log.debug("%s: effect type 0x%x virtual %d -> real %d",
                      self.name, effect.type, virtual_id, effect.id)
        except OSError as e:
            up.retval = -e.errno
            log.debug("%s: effect upload refused: %s", self.name, e)
        ioctl(self.vfd, UI_END_FF_UPLOAD, up)

    def _ff_erase(self, request_id):
        er = uinput_ff_erase()
        er.request_id = request_id
        ioctl(self.vfd, UI_BEGIN_FF_ERASE, er)
        real_id = self.effects.pop(er.effect_id, None)
        er.retval = 0
        if real_id is not None:
            try:
                ioctl(self.rfd, EVIOCRMFF, real_id)
            except OSError:
                pass
        ioctl(self.vfd, UI_END_FF_ERASE, er)

    def close(self):
        for fd, destroy in ((self.vfd, True), (self.rfd, False)):
            if fd is None:
                continue
            try:
                if destroy:
                    ioctl(fd, UI_DEV_DESTROY)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
        self.vfd = self.rfd = None
        log.info("%s: '%s' removed", self.pad.name, self.name)


# --------------------------------------------------------------------------
# Emulator integration: RetroArch + Dolphin
# --------------------------------------------------------------------------

class UserFiles:
    """Reads/writes files in the config owner's home, keeping them owned by that user."""

    def __init__(self, config_path):
        st = os.stat(config_path)
        self.uid, self.gid = st.st_uid, st.st_gid
        try:
            self.home = pwd.getpwuid(self.uid).pw_dir
        except KeyError:
            self.home = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(config_path))))
        self.state_path = os.path.join(self.home, ".local", "state", "padnamer", "state.json")

    def path(self, p):
        return os.path.join(self.home, p[2:]) if p.startswith("~/") else p

    def _chown(self, path):
        if os.geteuid() == 0:
            try:
                os.chown(path, self.uid, self.gid)
            except OSError:
                pass

    def makedirs(self, d):
        missing = []
        while d and not os.path.isdir(d):
            missing.append(d)
            d = os.path.dirname(d)
        for m in reversed(missing):
            os.mkdir(m)
            self._chown(m)

    def write(self, path, text):
        self.makedirs(os.path.dirname(path))
        tmp = path + ".padnamer-tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        self._chown(tmp)
        os.replace(tmp, path)

    def load_state(self):
        try:
            with open(self.state_path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save_state(self, state):
        self.write(self.state_path, json.dumps(state, indent=2, sort_keys=True) + "\n")


RA_DIRS = ["~/.var/app/org.libretro.RetroArch/config/retroarch", "~/.config/retroarch"]
DOLPHIN_DIRS = ["~/.var/app/org.DolphinEmu.dolphin-emu/config/dolphin-emu", "~/.config/dolphin-emu"]
_CFG_LINE = re.compile(r'^\s*([A-Za-z0-9_]+)\s*=')


def _existing_dirs(files, dirs):
    return [d for d in (files.path(x) for x in dirs) if os.path.isdir(d)]


class RetroArchManager:
    """Writes input_playerN_reserved_device into a core's override file.

    Used per game launch: when padnamer sees RetroArch start with (say) the
    Mupen64Plus-Next core, it writes the current player order into that
    core's override before letting RetroArch run, and removes it again when
    the game closes. RetroArch treats a plain name (no 'vid:pid ' prefix) as
    a match on the device name, so identical models are told apart.
    """

    def __init__(self, files):
        self.files = files

    def session_desired(self, config, library_name, names):
        if not config.ra_enabled or not library_name:
            return {}
        rtype = "1" if config.ra_reservation == "preferred" else "2"
        out = {}
        for root in _existing_dirs(self.files, config.ra_dirs or RA_DIRS):
            path = os.path.join(root, "config", library_name, library_name + ".cfg")
            keys = out.setdefault(path, {})
            for player, name in enumerate(names[:8], 1):
                keys[f"input_player{player}_reserved_device"] = name
                keys[f"input_player{player}_device_reservation_type"] = rtype
        return out

    def apply(self, desired, state, key="retroarch"):
        """Make the files contain exactly `desired` among the keys we manage."""
        previous = state.get(key, {})
        for path in sorted(set(desired) | set(previous)):
            want = desired.get(path, {})
            managed = set(previous.get(path, []))
            try:
                with open(path, encoding="utf-8") as f:
                    lines = f.read().splitlines()
            except FileNotFoundError:
                if not want:
                    continue
                lines = []
            out, seen = [], set()
            for line in lines:
                m = _CFG_LINE.match(line)
                if m and m.group(1) in want:
                    out.append(f'{m.group(1)} = "{want[m.group(1)]}"')
                    seen.add(m.group(1))
                elif m and m.group(1) in managed:
                    continue                          # we set it before, no longer wanted
                else:
                    out.append(line)
            for k in sorted(want):
                if k not in seen:
                    out.append(f'{k} = "{want[k]}"')
            if out != lines:
                self.files.write(path, "\n".join(out) + "\n")
                log.info("RetroArch: updated %s", path)
        if desired:
            state[key] = {p: sorted(k) for p, k in desired.items()}
        else:
            state.pop(key, None)
        return desired


def _read_ini(path):
    cp = configparser.ConfigParser(interpolation=None, strict=False, delimiters=("=",),
                                   comment_prefixes=("#", ";"), empty_lines_in_values=False)
    cp.optionxform = str
    if os.path.exists(path):
        cp.read(path, encoding="utf-8")
    return cp


def _ini_text(cp):
    import io
    buf = io.StringIO()
    cp.write(buf, space_around_delimiters=True)
    return buf.getvalue()


DOLPHIN_KINDS = {
    "gcpad": ("GCPadNew.ini", "GCPad", "GCPad"),
    "wiimote": ("WiimoteNew.ini", "Wiimote", "Wiimote"),
}


class DolphinManager:
    """Puts each player's saved Dolphin profile into their port for a game.

    Map a pad once in Dolphin and save the profile under exactly its padnamer
    name (e.g. 'GameCube A'). When a GameCube/Wii game starts, padnamer copies
    the profiles of players 1-4 into GCPad1-4 / Wiimote1-4. When the game
    closes every port gets its previous settings back.
    """

    def __init__(self, files):
        self.files = files
        self.missing_reported = set()

    def roots(self, config):
        if not config.dolphin_enabled:
            return []
        return _existing_dirs(self.files, config.dolphin_dirs or DOLPHIN_DIRS)

    def profile_path(self, root, kind, pad_name):
        return os.path.join(root, "Profiles", DOLPHIN_KINDS[kind][2], pad_name + ".ini")

    def apply_seats(self, config, assignments, kinds, state):
        """assignments: [(pad_name, player)]. Returns {pad_name: [port labels]}."""
        applied = state.setdefault("dolphin", {})
        desired = {}
        for root in self.roots(config):
            for kind in kinds:
                ini_name, prefix, _ = DOLPHIN_KINDS[kind]
                ini_path = os.path.join(root, ini_name)
                for pad_name, player in assignments:
                    if not 1 <= player <= 4:
                        continue
                    profile = self.profile_path(root, kind, pad_name)
                    if not os.path.exists(profile):
                        if (pad_name, profile) not in self.missing_reported:
                            log.info("Dolphin: no profile '%s' - map %s in Dolphin and save the "
                                     "profile with that exact name to have it used automatically",
                                     profile, pad_name)
                            self.missing_reported.add((pad_name, profile))
                        continue
                    desired[f"{ini_path}|{prefix}{player}"] = (pad_name, profile, kind)

        status = {}
        for key in [k for k in applied if k not in desired]:
            ini_path, section = key.rsplit("|", 1)
            self._restore(ini_path, section, applied.pop(key))
            log.info("Dolphin: restored %s [%s]", os.path.basename(ini_path), section)
        for key, (pad_name, profile, kind) in desired.items():
            ini_path, section = key.rsplit("|", 1)
            label = f"{os.path.basename(ini_path)} [{section}]"
            current = applied.get(key)
            if current and current.get("pad") == pad_name:
                status.setdefault(pad_name, []).append(label)
                continue
            prof = _read_ini(profile)
            if not prof.has_section("Profile"):
                continue
            cp = _read_ini(ini_path)
            saved = current["saved"] if current else (
                dict(cp.items(section)) if cp.has_section(section) else None)
            if cp.has_section(section):
                cp.remove_section(section)
            cp.add_section(section)
            for k, v in prof.items("Profile"):
                cp.set(section, k, v)
            if kind == "wiimote":
                cp.set(section, "Source", "1")        # emulated Wii Remote
            self.files.write(ini_path, _ini_text(cp))
            applied[key] = {"pad": pad_name, "saved": saved}
            status.setdefault(pad_name, []).append(label)
            log.info("Dolphin: %s -> %s", pad_name, label)
        return status

    def _restore(self, ini_path, section, entry):
        cp = _read_ini(ini_path)
        if cp.has_section(section):
            cp.remove_section(section)
        if entry.get("saved") is not None:
            cp.add_section(section)
            for k, v in entry["saved"].items():
                cp.set(section, k, v)
        self.files.write(ini_path, _ini_text(cp))


# --------------------------------------------------------------------------
# Game launch detection
# --------------------------------------------------------------------------

# RetroArch core file name (minus _libretro.so) -> (systems, core library name)
RETROARCH_CORES = {
    "mupen64plus_next": (["n64"], "Mupen64Plus-Next"), "parallel_n64": (["n64"], "ParaLLEl N64"),
    "mesen": (["nes"], "Mesen"), "fceumm": (["nes"], "FCEUmm"), "nestopia": (["nes"], "Nestopia"),
    "quicknes": (["nes"], "QuickNES"),
    "snes9x": (["snes"], "Snes9x"), "bsnes": (["snes"], "bsnes"), "bsnes_hd_beta": (["snes"], "bsnes-hd beta"),
    "mesen-s": (["snes"], "Mesen-S"),
    "genesis_plus_gx": (["megadrive", "mastersystem", "segacd"], "Genesis Plus GX"),
    "genesis_plus_gx_wide": (["megadrive", "mastersystem", "segacd"], "Genesis Plus GX Wide"),
    "picodrive": (["megadrive", "mastersystem", "segacd", "32x"], "PicoDrive"),
    "blastem": (["megadrive"], "BlastEm"), "gearsystem": (["mastersystem"], "Gearsystem"),
    "gambatte": (["gb", "gbc"], "Gambatte"), "sameboy": (["gb", "gbc"], "SameBoy"),
    "gearboy": (["gb", "gbc"], "Gearboy"), "mgba": (["gba", "gb", "gbc"], "mGBA"),
    "gpsp": (["gba"], "gpSP"), "vbam": (["gba"], "VBA-M"),
    "mednafen_pce": (["pcengine"], "Beetle PCE"), "mednafen_pce_fast": (["pcengine"], "Beetle PCE Fast"),
    "mednafen_psx_hw": (["psx"], "Beetle PSX HW"), "mednafen_psx": (["psx"], "Beetle PSX"),
    "swanstation": (["psx"], "SwanStation"), "pcsx_rearmed": (["psx"], "PCSX-ReARMed"),
    "mednafen_saturn": (["saturn"], "Beetle Saturn"), "kronos": (["saturn"], "Kronos"),
    "yabasanshiro": (["saturn"], "YabaSanshiro"), "flycast": (["dreamcast"], "Flycast"),
    "fbneo": (["arcade"], "FinalBurn Neo"), "mame": (["arcade"], "MAME"),
    "mame2003_plus": (["arcade"], "MAME 2003-Plus"), "melonds": (["nds"], "melonDS"),
    "melondsds": (["nds"], "melonDS DS"), "desmume": (["nds"], "DeSmuME"),
    "ppsspp": (["psp"], "PPSSPP"), "dolphin": (["gamecube", "wii"], "dolphin-emu"),
}

ROM_EXT_SYSTEMS = {
    ".nes": "nes", ".fds": "nes", ".sfc": "snes", ".smc": "snes", ".z64": "n64", ".n64": "n64",
    ".v64": "n64", ".md": "megadrive", ".gen": "megadrive", ".smd": "megadrive", ".sms": "mastersystem",
    ".32x": "32x", ".gb": "gb", ".gbc": "gbc", ".gba": "gba", ".pce": "pcengine",
    ".wbfs": "wii", ".wad": "wii", ".gcm": "gamecube", ".gcz": "gamecube", ".ciso": "gamecube",
}

# (process name / file name prefixes, label, systems | "retroarch" | "rom")
EMULATORS = [
    (("retroarch",), "RetroArch", "retroarch"),
    (("dolphin-emu",), "Dolphin", ["gamecube", "wii"]),     # not "dolphin": that's KDE's file manager
    (("primehack",), "PrimeHack", ["wii"]),
    (("duckstation",), "DuckStation", ["psx"]),
    (("pcsx2",), "PCSX2", ["ps2"]),
    (("ppsspp",), "PPSSPP", ["psp"]),
    (("rmg", "mupen64plus"), "RMG", ["n64"]),
    (("ares",), "ares", "rom"),
    (("azahar", "citra", "lime3ds"), "Azahar", ["3ds"]),
    (("melonds",), "melonDS", ["nds"]),
    (("cemu",), "Cemu", ["wiiu"]),
    (("xemu",), "xemu", ["xbox"]),
    (("xenia",), "Xenia", ["xbox360"]),
    (("ryujinx", "eden", "yuzu", "citron", "sudachi", "suyu", "torzu"), "Switch emulator", ["switch"]),
    (("flycast",), "Flycast", ["dreamcast"]),
    (("vita3k",), "Vita3K", ["vita"]),
    (("rpcs3",), "RPCS3", ["ps3"]),
    (("mgba",), "mGBA", ["gba", "gb", "gbc"]),
    (("mame", "supermodel"), "MAME", ["arcade"]),
    (("snes9x", "bsnes"), "Snes9x", ["snes"]),
]


def _proc_argv(pid, proc="/proc"):
    try:
        with open(os.path.join(proc, str(pid), "cmdline"), "rb") as f:
            raw = f.read(8192)
    except OSError:
        return []
    return [a.decode(errors="replace") for a in raw.split(b"\0") if a]


def _systems_from_roms(argv):
    found = []
    for a in argv[1:]:
        s = ROM_EXT_SYSTEMS.get(os.path.splitext(a)[1].lower())
        if s and s not in found:
            found.append(s)
    return found


def _retroarch_core(argv):
    core = None
    for i, a in enumerate(argv):
        if a in ("-L", "--libretro") and i + 1 < len(argv):
            core = argv[i + 1]
        elif a.startswith("--libretro="):
            core = a.split("=", 1)[1]
        elif a.startswith("-L") and len(a) > 2:
            core = a[2:]
    return core


def _core_library_name(core_path, short):
    """Library name from the core's .info file, for cores not in our table."""
    base = os.path.splitext(os.path.basename(core_path))[0]
    candidates = [os.path.splitext(core_path)[0] + ".info",
                  os.path.join(os.path.dirname(os.path.dirname(core_path)), "info", base + ".info"),
                  os.path.join("/app/share/libretro/info", base + ".info")]
    for c in candidates:
        for line in _read(c).splitlines():
            m = re.match(r'\s*corename\s*=\s*"(.*)"', line)
            if m:
                return m.group(1)
    return None


def classify_process(pid, proc="/proc"):
    """Returns {'emulator', 'systems', 'core'} if pid is a known emulator, else None."""
    comm = _read(os.path.join(proc, str(pid), "comm")).lower()
    argv = _proc_argv(pid, proc)
    base = os.path.basename(argv[0]).lower() if argv else ""
    names = [n for n in (comm, base) if n]
    if not names:
        return None
    for prefixes, label, systems in EMULATORS:
        if not any(n.startswith(p) for n in names for p in prefixes):
            continue
        info = {"emulator": label, "systems": [], "core": None}
        if systems == "retroarch":
            core = _retroarch_core(argv)
            if not core:
                return None            # RetroArch menu with no game: don't hijack it
            short = re.sub(r"_libretro(_android)?\.(so|dll|dylib)$", "", os.path.basename(core))
            known = RETROARCH_CORES.get(short)
            if known:
                info["systems"], info["core"] = list(known[0]), known[1]
            else:
                info["core"] = _core_library_name(core, short)
                info["systems"] = _systems_from_roms(argv)
            info["emulator"] = f"RetroArch ({info['core'] or short})"
        elif systems == "rom":
            info["systems"] = _systems_from_roms(argv)
        else:
            info["systems"] = list(systems)
            if label == "Dolphin":
                roms = [s for s in _systems_from_roms(argv) if s in ("gamecube", "wii")]
                if roms:
                    info["systems"] = roms
        return info
    return None


class ProcWatch:
    """Tells the daemon the moment a program starts or exits.

    Uses the kernel's process events connector (instant, needs root); falls
    back to scanning /proc on every tick if that isn't available.
    """
    NETLINK_CONNECTOR, CN_IDX_PROC, CN_VAL_PROC = 11, 1, 1
    PROC_CN_MCAST_LISTEN = 1
    PROC_EVENT_EXEC, PROC_EVENT_EXIT = 0x00000002, 0x80000000

    def __init__(self):
        self.sock = None
        self.known_pids = None
        try:
            import socket
            sock = socket.socket(socket.AF_NETLINK, socket.SOCK_DGRAM, self.NETLINK_CONNECTOR)
            sock.bind((os.getpid(), self.CN_IDX_PROC))
            payload = struct.pack("=I", self.PROC_CN_MCAST_LISTEN)
            cn = struct.pack("=IIIIHH", self.CN_IDX_PROC, self.CN_VAL_PROC, 0, 0, len(payload), 0)
            nl = struct.pack("=IHHII", 16 + len(cn) + len(payload), 3, 0, 0, os.getpid())
            sock.send(nl + cn + payload)
            sock.setblocking(False)
            self.sock = sock
            log.info("watching for game launches (process events)")
        except (OSError, AttributeError) as e:
            log.info("process events unavailable (%s); scanning /proc instead", e)

    def fileno(self):
        return self.sock.fileno() if self.sock else None

    def read_events(self):
        """[(kind, pid)] with kind 'exec' or 'exit'."""
        out = []
        if not self.sock:
            return out
        while True:
            try:
                data = self.sock.recv(65536)
            except (BlockingIOError, InterruptedError):
                return out
            except OSError:
                return out
            off = 0
            while off + 16 <= len(data):
                nl_len = struct.unpack_from("=I", data, off)[0]
                if nl_len < 16:
                    break
                body = off + 16 + 20                       # nlmsghdr + cn_msg
                if body + 16 <= off + nl_len:
                    what = struct.unpack_from("=I", data, body)[0]
                    if what == self.PROC_EVENT_EXEC:
                        _pid, tgid = struct.unpack_from("=ii", data, body + 16)
                        out.append(("exec", tgid))
                    elif what == self.PROC_EVENT_EXIT:
                        pid, tgid = struct.unpack_from("=ii", data, body + 16)
                        if pid == tgid:
                            out.append(("exit", tgid))
                off += (nl_len + 3) & ~3

    def poll_new(self, proc="/proc"):
        """Fallback: pids that appeared since last call."""
        try:
            pids = {int(d) for d in os.listdir(proc) if d.isdigit()}
        except OSError:
            return []
        if self.known_pids is None:
            self.known_pids = pids
            return []
        new = sorted(pids - self.known_pids)
        self.known_pids = pids
        return new


def bt_disconnect(mac, label=""):
    """Ask BlueZ to drop a device, in the background (it can take a couple of seconds).

    Most controllers switch themselves off when the console disconnects them.
    """
    import threading

    def run():
        mac_u = mac.upper()
        try:
            r = subprocess.run(["bluetoothctl", "disconnect", mac_u], capture_output=True,
                               text=True, timeout=20)
            ok = r.returncode == 0 and "fail" not in (r.stdout + r.stderr).lower()
            if ok:
                log.info("disconnected %s (%s)", label or mac_u, mac_u)
                return
            log.debug("bluetoothctl: %s %s", r.stdout.strip(), r.stderr.strip())
        except (OSError, subprocess.TimeoutExpired) as e:
            log.debug("bluetoothctl failed: %s", e)
        # fall back to BlueZ over D-Bus, trying each adapter
        path_mac = mac_u.replace(":", "_")
        for hci in sorted(glob.glob("/sys/class/bluetooth/hci*")):
            name = os.path.basename(hci)
            if ":" in name:
                continue
            try:
                r = subprocess.run(["busctl", "call", "org.bluez", f"/org/bluez/{name}/dev_{path_mac}",
                                    "org.bluez.Device1", "Disconnect"], capture_output=True, timeout=20)
                if r.returncode == 0:
                    log.info("disconnected %s (%s)", label or mac_u, mac_u)
                    return
            except (OSError, subprocess.TimeoutExpired):
                pass
        log.warning("couldn't disconnect %s (%s)", label or mac_u, mac_u)

    threading.Thread(target=run, name="bt-disconnect", daemon=True).start()


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


# --------------------------------------------------------------------------
# Web UI / API
# --------------------------------------------------------------------------

WEB_DIR = os.path.join(os.path.dirname(os.path.realpath(__file__)), "web")
LOCAL_ADDRS = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}


def lan_addresses():
    import socket
    addrs = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))        # TEST-NET: nothing is sent
        addrs.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return addrs


class WebServer:
    """Small JSON API + single-page UI. LAN access needs the PIN; this machine doesn't."""

    MAX_BODY = 64 * 1024

    def __init__(self, daemon, bind, port):
        import http.server
        import threading
        self.daemon = daemon
        self.port = port
        self.tokens = set()
        self.failures = {}           # ip -> (count, locked_until)
        self.lock = threading.Lock()
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "padnamer"

            def log_message(self, fmt, *args):
                log.debug("web %s - " + fmt, self.client_address[0], *args)

            def do_GET(self):
                server.handle(self, "GET")

            def do_POST(self):
                server.handle(self, "POST")

        self.httpd = http.server.ThreadingHTTPServer((bind, port), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="padnamer-web", daemon=True)

    def start(self):
        self.thread.start()
        addrs = ", ".join(f"http://{a}:{self.port}" for a in lan_addresses()) or f"port {self.port}"
        log.info("web UI: %s (PIN %s)", addrs, self.daemon.registry.pin())

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    # ---- helpers ----------------------------------------------------------------
    @staticmethod
    def is_local(h):
        return h.client_address[0] in LOCAL_ADDRS

    def authed(self, h):
        if self.is_local(h) or not self.daemon.config.web_pin_required:
            return True
        cookie = h.headers.get("Cookie", "")
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "pn_token" and v in self.tokens:
                return True
        return False

    def send(self, h, code, body, ctype="application/json", extra=None):
        data = body if isinstance(body, bytes) else body.encode()
        h.send_response(code)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(len(data)))
        h.send_header("Cache-Control", "no-store")
        h.send_header("X-Content-Type-Options", "nosniff")
        h.send_header("X-Frame-Options", "DENY")
        for k, v in (extra or {}).items():
            h.send_header(k, v)
        h.end_headers()
        h.wfile.write(data)

    def json(self, h, code, obj, extra=None):
        self.send(h, code, json.dumps(obj), extra=extra)

    def body(self, h):
        n = int(h.headers.get("Content-Length") or 0)
        if n > self.MAX_BODY:
            raise ValueError("request too large")
        raw = h.rfile.read(n) if n else b"{}"
        data = json.loads(raw or b"{}")
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return data

    # ---- routing -------------------------------------------------------------------
    def handle(self, h, method):
        path = h.path.split("?", 1)[0]
        try:
            if method == "GET" and path in ("/", "/index.html"):
                return self.static(h, "index.html", "text/html; charset=utf-8")
            if method == "GET" and path == "/api/state":
                return self.get_state(h)
            if method == "POST":
                # custom header: browsers can't send it cross-site without a CORS preflight,
                # which we never allow - stops other web pages poking the API
                if h.headers.get("X-Padnamer") != "1":
                    return self.json(h, 403, {"error": "missing X-Padnamer header"})
                if path == "/api/login":
                    return self.login(h)
                if not self.authed(h):
                    return self.json(h, 401, {"error": "PIN needed"})
                return self.post(h, path, self.body(h))
            self.json(h, 404, {"error": "not found"})
        except (ValueError, KeyError) as e:
            self.json(h, 400, {"error": str(e).strip("'")})
        except TimeoutError as e:
            self.json(h, 503, {"error": str(e)})
        except BrokenPipeError:
            pass
        except Exception as e:
            log.exception("web request failed")
            self.json(h, 500, {"error": str(e)})

    def static(self, h, name, ctype):
        try:
            with open(os.path.join(WEB_DIR, name), "rb") as f:
                self.send(h, 200, f.read(), ctype)
        except FileNotFoundError:
            self.send(h, 404, b"web UI files missing - re-run install.sh", "text/plain")

    def get_state(self, h):
        if not self.authed(h):
            return self.json(h, 401, {"error": "PIN needed", "pin_required": True})
        with self.daemon.lock:
            state = json.loads(self.daemon.snapshot or "{}")
        state["web"] = {"port": self.port, "addresses": lan_addresses(), "local": self.is_local(h)}
        if self.is_local(h):
            state["web"]["pin"] = self.daemon.registry.pin()
        self.json(h, 200, state)

    def login(self, h):
        import secrets
        ip = h.client_address[0]
        now = time.monotonic()
        with self.lock:
            count, until = self.failures.get(ip, (0, 0))
            if until > now:
                return self.json(h, 429, {"error": "too many tries, wait a minute"})
        pin = str(self.body(h).get("pin", ""))
        if secrets.compare_digest(pin, self.daemon.registry.pin()):
            token = secrets.token_urlsafe(24)
            with self.lock:
                self.tokens.add(token)
                self.failures.pop(ip, None)
            return self.json(h, 200, {"ok": True}, extra={
                "Set-Cookie": f"pn_token={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000"})
        with self.lock:
            count += 1
            self.failures[ip] = (count, now + 60 if count >= 5 else 0)
        self.json(h, 403, {"error": "wrong PIN"})

    def post(self, h, path, data):
        d = self.daemon
        if path == "/api/controller":
            cid = str(data.get("id", ""))
            changes = {k: data[k] for k in ("name", "model", "home", "present_as", "leds", "manage", "mode")
                       if k in data}
            entry = d.submit(d.api_update_controller, cid, changes)
            return self.json(h, 200, {"ok": True, "controller": entry})
        if path == "/api/forget":
            d.submit(d.api_forget, str(data.get("id", "")))
            return self.json(h, 200, {"ok": True})
        if path == "/api/order":
            ids = [str(i) for i in data.get("ids", [])]
            which = data.get("which", "now")
            if which not in ("now", "default"):
                raise ValueError("which must be 'now' or 'default'")
            d.submit(d.api_set_order, ids, which)
            return self.json(h, 200, {"ok": True})
        if path == "/api/move":
            d.submit(d.api_move, str(data.get("id", "")), int(data.get("delta", 0)))
            return self.json(h, 200, {"ok": True})
        if path == "/api/order/reset":
            d.submit(d.api_reset_order)
            return self.json(h, 200, {"ok": True})
        if path == "/api/disconnect":
            d.submit(d.api_disconnect, str(data.get("id", "")))
            return self.json(h, 200, {"ok": True})
        if path == "/api/disconnect_all":
            n = d.submit(d.api_disconnect_all)
            return self.json(h, 200, {"ok": True, "count": n})
        if path == "/api/identify":
            d.submit(d.identify, str(data.get("id", "")))
            return self.json(h, 200, {"ok": True})
        if path == "/api/session":
            if not self.is_local(h):
                return self.json(h, 403, {"error": "launch notices only from this machine"})
            systems = [str(x) for x in data.get("systems", [])]
            d.submit(d.api_session, systems, int(data.get("pid", 0)), timeout=15)
            return self.json(h, 200, {"ok": True})
        self.json(h, 404, {"error": "not found"})


# --------------------------------------------------------------------------
# Daemon
# --------------------------------------------------------------------------

class Daemon:
    SCAN_INTERVAL = 1.0
    FLASH_STEP = 0.2

    def __init__(self, config_path, manage_udev=True, manage_emulators=True,
                 status_path=STATUS_PATH, use_registry=True, watch_launches=True, web=True):
        self.config_path = config_path
        self.manage_udev = manage_udev
        self.manage_emulators = manage_emulators
        self.use_registry = use_registry
        self.watch_launches = watch_launches
        self.want_web = web
        self.status_path = status_path
        self.config = None
        self.config_mtime = None
        self.files = None
        self.registry = None
        self.order = None
        self.dolphin = None
        self.ra = None
        self.state = {}
        self.pads = []
        self.bridges = {}            # event name -> Bridge
        self.bridge_seq = 0
        self.failed = {}             # event name -> device number we gave up on
        self.fd_map = {}             # fd -> (bridge, "real"|"virt")
        self.poller = select.poll()
        self.stopping = False
        self.dolphin_status = {}
        self.indicator_status = {}
        self.flashes = {}            # cid -> {"until", "next", "on", "effect"}
        self.switch_active = False
        self.seen_now = set()
        self.pads_stale = False
        self.present_cids = set()
        self.last_passthrough = None
        self.procwatch = None
        self.session_meta = {}
        self.enforce_order = True
        self.last_status = None
        self.snapshot = "{}"
        import queue
        import threading
        self.lock = threading.Lock()
        self.commands = queue.Queue()
        self.wake_r, self.wake_w = os.pipe()
        os.set_blocking(self.wake_r, False)
        os.set_blocking(self.wake_w, False)
        self.poller.register(self.wake_r, select.POLLIN)
        self.web = None

    # ---- config / registry ------------------------------------------------------
    def load_config(self, initial=False):
        try:
            mtime = os.stat(self.config_path).st_mtime
        except OSError as e:
            if initial:
                raise
            log.warning("config unreadable: %s", e)
            return
        if not initial and mtime == self.config_mtime:
            return
        try:
            config = Config(self.config_path)
        except Exception as e:
            if initial:
                raise
            log.error("config not reloaded, keeping the old one: %s", e)
            self.config_mtime = mtime
            return
        self.config_mtime = mtime
        self.config = config
        logging.getLogger().setLevel(getattr(logging, config.log_level, logging.INFO))
        if self.files is None and (self.manage_emulators or self.use_registry):
            self.files = UserFiles(self.config_path)
            self.state = self.files.load_state()
            self.dolphin = DolphinManager(self.files)
            self.ra = RetroArchManager(self.files)
        if self.use_registry and self.registry is None:
            self.registry = Registry(self.files)
            self.registry.import_legacy(config)
            self.registry.pin()
            self.registry.save()
        if self.registry is not None:
            scope = config.manual_scope
            if self.order is None:
                self.order = OrderEngine(self.registry, scope)
            self.order.scope = scope
        elif self.order is None:
            self.order = OrderEngine(_LegacyOrder(config), "session")
        if self.manage_emulators and self.ra:
            try:   # tidy reservations left by 1.x or a game that was running when we stopped
                self.ra.apply({}, self.state, "retroarch")
                self.ra.apply({}, self.state, "retroarch_session")
                self.files.save_state(self.state)
            except Exception as e:
                log.error("RetroArch tidy-up failed: %s", e)
        self.refresh_pads(rebuild=not initial)
        log.info("config loaded: %d managed controller(s)", len(self.pads))

    def refresh_pads(self, rebuild=True):
        if self.registry is not None:
            settings = {"led_style": self.config.led_style, "hide_hidraw": self.config.hide_hidraw}
            self.pads = self.registry.pad_configs(settings)
        else:
            self.pads = list(self.config.pads)
            for p in self.pads:
                p.cid = p.cid or p.ident + ("#" + p.name if not p.mac else "")
        if self.manage_udev:
            install_udev_rules(self.config, self.pads,
                               self.registry.passthrough_macs() if self.registry else ())
        if rebuild:
            for ev in list(self.bridges):
                self.release(ev)
            self.failed.clear()
            self.enforce_order = True

    # ---- bridges -----------------------------------------------------------------
    def register(self, ev, bridge):
        self.bridge_seq += 1
        bridge.seq = self.bridge_seq
        self.bridges[ev] = bridge
        for fd, role in ((bridge.rfd, "real"), (bridge.vfd, "virt")):
            self.fd_map[fd] = (bridge, role)
            self.poller.register(fd, select.POLLIN)

    def release(self, ev):
        bridge = self.bridges.pop(ev, None)
        if not bridge:
            return
        for fd in (bridge.rfd, bridge.vfd):
            if fd is not None and fd in self.fd_map:
                del self.fd_map[fd]
                try:
                    self.poller.unregister(fd)
                except KeyError:
                    pass
        bridge.close()

    def connected_cids(self):
        return {b.pad.cid for b in self.bridges.values()}

    def creation_order(self):
        """Pad ids in the order their virtual devices were created (= SDL index order)."""
        seen, out = set(), []
        for b in sorted(self.bridges.values(), key=lambda b: getattr(b, "seq", 0)):
            if b.pad.cid not in seen:
                seen.add(b.pad.cid)
                out.append(b.pad.cid)
        return out

    def homes(self):
        return {p.cid: p.systems for p in self.pads}

    def desired_order(self, extra=()):
        connected = self.connected_cids() | set(extra)
        return self.order.active(connected, self.homes())

    def pad_by_cid(self, cid):
        return next((p for p in self.pads if p.cid == cid), None)

    def match_node(self, node, claimed=()):
        if self.registry is None:
            return self.pad_for(node)
        in_use = self.connected_cids() | set(claimed)
        entry = self.registry.observe(node, auto_add=node.is_controllerish, in_use=in_use)
        if entry is not None:
            self.seen_now.add(entry["id"])
            if not entry.get("manage") and entry["id"] in in_use:
                self.pads_stale = True          # e.g. turned out to be a Balance Board
        if entry is None or not entry.get("manage"):
            return None
        pad = self.pad_by_cid(entry["id"])
        if pad is None:                       # just registered: rebuild the pad list
            self.refresh_pads(rebuild=False)
            pad = self.pad_by_cid(entry["id"])
        return pad

    def pad_for(self, node):
        for pad in self.pads:
            if not pad.matches(node):
                continue
            if not pad.mac and any(b.pad is pad for b in self.bridges.values()):
                continue
            return pad
        return None

    def scan(self):
        self.seen_now = set()
        present = set(list_event_nodes())
        for ev in list(self.bridges):
            if ev not in present:
                self.release(ev)
        for ev in list(self.failed):
            if ev not in present:
                del self.failed[ev]

        candidates = []
        claimed_now = set()
        for ev in sorted(present, key=lambda e: int(e[5:]) if e[5:].isdigit() else 0):
            if ev in self.bridges:
                hide_node(self.bridges[ev].node.devnode)
                continue
            try:
                rdev = os.stat(os.path.join(DEV_INPUT, ev)).st_rdev
            except OSError:
                continue
            if self.failed.get(ev) == rdev:
                continue
            node = NodeInfo(ev)
            if node.ours or not node.name:
                continue
            pad = self.match_node(node, claimed_now)
            if pad is None:
                continue
            if not (pad.all_nodes or node.is_controllerish):
                continue
            if not pad.mac and pad.cid in claimed_now and self.registry is None:
                continue
            claimed_now.add(pad.cid)
            candidates.append((pad, node, ev, rdev))

        if self.pads_stale:
            self.pads_stale = False
            self.refresh_pads(rebuild=False)
            managed = {p.cid for p in self.pads}
            for ev, b in list(self.bridges.items()):
                if b.pad.cid not in managed:
                    log.info("%s: no longer managed, handing the device back", b.pad.name)
                    self.release(ev)
            candidates = [c for c in candidates if c[0].cid in managed]
        if candidates:
            order = self.desired_order(extra={p.cid for p, *_ in candidates})
            rank = {cid: i for i, cid in enumerate(order)}
            candidates.sort(key=lambda c: (rank.get(c[0].cid, 999), int(c[2][5:]) if c[2][5:].isdigit() else 0))
            for pad, node, ev, rdev in candidates:
                self.create_bridge(pad, node, ev, rdev)
        self.present_cids = self.seen_now | self.connected_cids()
        if self.registry is not None:
            real = self.registry.passthrough_macs()
            if real != self.last_passthrough:
                self.last_passthrough = real
                if self.manage_udev:
                    install_udev_rules(self.config, self.pads, real)
            if self.registry.dirty:
                try:
                    self.registry.save()
                except OSError as e:
                    log.error("couldn't save controllers.json: %s", e)
        self.check_order()

    def create_bridge(self, pad, node, ev, rdev):
        taken = {b.name for b in self.bridges.values()}
        name = virtual_name(pad.name, node, taken)
        hide_node(node.devnode)
        if pad.hide_hidraw:
            for raw in node.hidraw_nodes():
                hide_node(raw)
        layout = build_layout(pad, node, self.switch_active)
        try:
            bridge = Bridge(pad, node, name, layout)
        except OSError as e:
            log.error("%s: could not take over %s: %s", pad.name, node.devnode, e)
            self.failed[ev] = rdev
            return None
        self.register(ev, bridge)
        return bridge

    def check_order(self, force=False):
        """Re-create virtual pads if their creation order doesn't match the player order.

        Only done between games, at game start, or when someone reorders by
        hand - never just because a pad joined mid-game.
        """
        if not self.bridges:
            return False
        if not (force or self.enforce_order):
            return False                 # pads that join are added at the end, nobody is shuffled
        want = self.desired_order()
        have = self.creation_order()
        self.enforce_order = False
        if want == have:
            return False
        log.info("player order: %s", ", ".join(self.pad_by_cid(c).name if self.pad_by_cid(c) else c
                                               for c in want))
        self.recreate_all(want)
        return True

    def recreate_all(self, order):
        rank = {cid: i for i, cid in enumerate(order)}
        items = [(b.pad, b.node, ev) for ev, b in self.bridges.items()]
        for ev in list(self.bridges):
            self.release(ev)
        items.sort(key=lambda it: (rank.get(it[0].cid, 999), int(it[2][5:]) if it[2][5:].isdigit() else 0))
        for pad, node, ev in items:
            try:
                rdev = os.stat(node.devnode).st_rdev
            except OSError:
                continue
            fresh = NodeInfo(ev)
            if fresh.name:
                self.create_bridge(pad, fresh, ev, rdev)

    def players(self):
        """cid -> player number (creation order, which is what emulators see)."""
        return {cid: i + 1 for i, cid in enumerate(self.creation_order())}

    # ---- sessions (a game is running) ------------------------------------------------
    def on_exec(self, pid):
        if self.order.session is not None and pid_alive(self.order.session["pid"]):
            return
        info = classify_process(pid)
        if not info:
            return
        stopped = False
        if self.config.pause_on_launch:
            try:
                os.kill(pid, signal.SIGSTOP)        # hold the emulator until the pads are in order
                stopped = True
            except OSError:
                pass
        try:
            self.start_session(info, pid)
        finally:
            if stopped:
                try:
                    os.kill(pid, signal.SIGCONT)
                except OSError:
                    pass

    def start_session(self, info, pid):
        log.info("game started: %s (%s), pid %d", info["emulator"],
                 ", ".join(info["systems"]) or "unknown system", pid)
        self.order.start_session(info["emulator"], info["systems"], pid)
        self.session_meta = info
        if self.check_switch_emulators():
            self.scan()
        changed = self.check_order(force=True)
        names = [self.pad_by_cid(c).name for c in self.creation_order() if self.pad_by_cid(c)]
        if self.manage_emulators and self.files:
            try:
                if info.get("core") and not info["emulator"].startswith("Dolphin"):
                    self.ra.apply(self.ra.session_desired(self.config, info["core"], names),
                                  self.state, "retroarch_session")
                kinds = []
                if "gamecube" in info["systems"]:
                    kinds.append("gcpad")
                if "wii" in info["systems"]:
                    kinds.append("wiimote")
                if kinds:
                    self.dolphin_status = self.dolphin.apply_seats(
                        self.config, [(n, i) for i, n in enumerate(names, 1)], kinds, self.state)
                self.files.save_state(self.state)
            except Exception as e:
                log.error("emulator config update failed: %s", e)
        self.update_indicators()
        if changed:
            _udevadm("settle", "--timeout=3")
        self.publish()

    def end_session(self):
        info = self.order.session
        if not info:
            return
        log.info("game closed: %s", info["emulator"])
        self.order.end_session()
        self.session_meta = {}
        if self.manage_emulators and self.files:
            try:
                self.ra.apply({}, self.state, "retroarch_session")
                self.dolphin_status = self.dolphin.apply_seats(self.config, [], [], self.state)
                self.files.save_state(self.state)
            except Exception as e:
                log.error("emulator config restore failed: %s", e)
        self.check_switch_emulators()
        self.enforce_order = True
        self.publish()

    def session_gone(self):
        """The session's process exited: follow the emulator if a launcher script handed over."""
        s = self.order.session
        if not s:
            return
        try:
            pids = sorted((int(d) for d in os.listdir("/proc") if d.isdigit()), reverse=True)
        except OSError:
            pids = []
        for pid in pids:
            if pid == s["pid"] or pid == os.getpid():
                continue
            info = classify_process(pid)
            if info and (set(info["systems"]) & set(s["systems"]) or not s["systems"]):
                log.info("game continues in pid %d (%s)", pid, info["emulator"])
                s["pid"] = pid
                return
        self.end_session()

    def check_session(self):
        s = self.order.session
        if s and not pid_alive(s["pid"]):
            self.session_gone()

    def handle_proc_events(self):
        for kind, pid in self.procwatch.read_events():
            if kind == "exec":
                self.on_exec(pid)
            elif kind == "exit" and self.order.session and pid == self.order.session["pid"]:
                self.session_gone()

    def check_switch_emulators(self):
        """Pads with present_as=auto: Switch Pro layout while a Switch emulator runs."""
        if not any(p.present_as == "auto" for p in self.pads):
            return False
        s = self.order.session
        if s is not None:
            active = "switch" in s["systems"]
        else:
            active = switch_emulator_running(self.config.switch_emulators)
        if active == self.switch_active:
            return False
        self.switch_active = active
        log.info("Switch emulator %s: %s pads as Switch Pro Controllers",
                 "running" if active else "closed", "presenting" if active else "no longer presenting")
        for ev, b in list(self.bridges.items()):
            if b.pad.present_as == "auto":
                self.release(ev)
        self.failed.clear()
        self.enforce_order = True          # rebuilt pads go back to their places
        return True

    # ---- indicators / identify ------------------------------------------------------------
    def update_indicators(self):
        players = self.players()
        for pad in self.pads:
            if pad.cid in self.flashes:
                continue
            player = players.get(pad.cid)
            if not player:
                self.indicator_status.pop(pad.cid, None)
                continue
            result = {"pattern": "", "colour": ""}
            for b in self.bridges.values():
                if b.pad.cid == pad.cid and b.node.hid_dir:
                    result = apply_indicators(b.node.hid_dir, player, pad.leds)
                    if result["pattern"] or result["colour"]:
                        break
            if result != self.indicator_status.get(pad.cid):
                if result["pattern"] or result["colour"]:
                    log.info("%s: player %d %s%s", pad.name, player, result["pattern"],
                             f" {result['colour']}" if result["colour"] else "")
            self.indicator_status[pad.cid] = result

    def identify(self, cid):
        bridges = [b for b in self.bridges.values() if b.pad.cid == cid]
        if not bridges:
            raise ValueError("that controller isn't connected")
        effect_id = None
        for b in bridges:
            if EV_FF in b.node.caps["ev"] and FF_RUMBLE in b.node.caps["ff"]:
                try:
                    eff = ff_effect()
                    eff.type, eff.id = FF_RUMBLE, -1
                    eff.u.rumble.strong_magnitude = 0xC000
                    eff.u.rumble.weak_magnitude = 0xC000
                    eff.replay.length = 600
                    ioctl(b.rfd, EVIOCSFF, eff)
                    if eff.id >= 0:
                        os.write(b.rfd, struct.pack(EVENT_FMT, 0, 0, EV_FF, eff.id, 1))
                        effect_id = (b, eff.id)
                except OSError as e:
                    log.debug("identify rumble failed: %s", e)
                break
        now = time.monotonic()
        self.flashes[cid] = {"until": now + 2.0, "next": now, "on": False, "effect": effect_id}
        return True

    def process_flashes(self):
        now = time.monotonic()
        for cid, f in list(self.flashes.items()):
            pad = self.pad_by_cid(cid)
            dirs = [b.node.hid_dir for b in self.bridges.values() if b.pad.cid == cid and b.node.hid_dir]
            if now >= f["until"] or not dirs:
                del self.flashes[cid]
                if f.get("effect"):
                    b, eid = f["effect"]
                    try:
                        ioctl(b.rfd, EVIOCRMFF, eid)
                    except (OSError, TypeError):
                        pass
                self.indicator_status.pop(cid, None)
                continue
            if now < f["next"]:
                continue
            f["on"] = not f["on"]
            f["next"] = now + self.FLASH_STEP
            for d in dirs:
                paths, _ = find_player_leds(d)
                _write_leds(paths, [int(f["on"])] * len(paths))
                set_lightbar(find_lightbar(d), (255, 255, 255) if f["on"] else (0, 0, 0), 128)

    # ---- commands from the web UI (run on the main thread) --------------------------------
    def submit(self, fn, *args, timeout=10):
        import threading
        done = threading.Event()
        box = {}

        def run():
            try:
                box["result"] = fn(*args)
            except Exception as e:      # reported back to the caller
                box["error"] = e
            finally:
                done.set()
        self.commands.put(run)
        try:
            os.write(self.wake_w, b"x")
        except BlockingIOError:
            pass
        if not done.wait(timeout):
            raise TimeoutError("padnamer is busy")
        if "error" in box:
            raise box["error"]
        return box.get("result")

    def run_commands(self):
        try:
            while os.read(self.wake_r, 4096):
                pass
        except BlockingIOError:
            pass
        while not self.commands.empty():
            self.commands.get_nowait()()

    # web API actions (main thread)
    def api_update_controller(self, cid, changes):
        before = self.registry.controllers.get(cid, {}).copy()
        entry = self.registry.update(cid, changes)
        self.registry.save()
        relevant = ("name", "model", "home", "present_as", "leds", "manage", "passthrough")
        if any(before.get(k) != entry.get(k) for k in relevant):
            self.refresh_pads(rebuild=True)
            self.scan()
        self.publish()
        return entry

    def api_forget(self, cid):
        self.registry.forget(cid)
        self.registry.save()
        self.refresh_pads(rebuild=True)
        self.scan()
        self.publish()

    def api_set_order(self, ids, which):
        if which == "default":
            self.registry.set_order(ids)
            if self.order.manual and self.order.manual.get("pending"):
                self.order.clear_manual()      # a new default replaces a not-yet-used manual order
            self.registry.save()
        else:
            self.order.set_manual(ids)
            self.registry.save()
        self.check_order(force=True)
        self.update_indicators()
        self.publish()

    def api_reset_order(self):
        self.order.clear_manual()
        self.registry.save()
        self.check_order(force=True)
        self.update_indicators()
        self.publish()

    def api_move(self, cid, delta):
        current = self.creation_order()
        if cid not in current:
            raise ValueError("that controller isn't connected")
        i = current.index(cid)
        j = max(0, min(len(current) - 1, i + delta))
        current.insert(j, current.pop(i))
        self.api_set_order(current, "now")

    def api_disconnect(self, cid):
        entry = self.registry.controllers.get(cid) if self.registry else None
        if entry is None:
            raise ValueError("unknown controller")
        if entry.get("kind") != "mac" or not entry.get("mac"):
            raise ValueError("this controller isn't connected over Bluetooth - unplug or switch it off instead")
        if cid not in self.present_cids:
            raise ValueError(f"{entry['name']} isn't connected")
        bt_disconnect(entry["mac"], entry["name"])
        return True

    def api_disconnect_all(self):
        macs = [(e["mac"], e["name"]) for cid, e in self.registry.controllers.items()
                if cid in self.present_cids and e.get("kind") == "mac" and e.get("mac")]
        for mac, name in macs:
            bt_disconnect(mac, name)
        return len(macs)

    def api_session(self, systems, pid, emulator="launch wrapper"):
        systems = [SYSTEM_ALIASES.get(s, s) for s in systems]
        self.start_session({"emulator": emulator, "systems": systems, "core": None}, pid)

    # ---- status ------------------------------------------------------------------------------
    def state_dict(self):
        players = self.players()
        connected = self.connected_cids()
        controllers = []
        entries = self.registry.controllers if self.registry else {}
        ids = list(self.registry.order) if self.registry else [p.cid for p in self.pads]
        for cid in ids:
            e = entries.get(cid, {})
            pad = self.pad_by_cid(cid)
            bridges = [b for b in self.bridges.values() if b.pad.cid == cid]
            hid = next((b.node.hid_dir for b in bridges if b.node.hid_dir), None)
            model = self.registry.model_of(e) if e else (pad.model if pad else "generic")
            controllers.append({
                "id": cid,
                "name": e.get("name") or (pad.name if pad else cid),
                "model": e.get("model"), "detected_model": e.get("detected_model"),
                "effective_model": model, "model_label": MODELS.get(model, MODELS["generic"])[0],
                "family": MODELS.get(model, MODELS["generic"])[2],
                "home": self.registry.home_of(e) if e else (pad.systems if pad else []),
                "home_is_default": e.get("home") is None if e else False,
                "present_as": e.get("present_as", pad.present_as if pad else "original"),
                "leds": e.get("leds", "auto"), "manage": e.get("manage", True),
                "mode": Registry.mode_of(e) if e else "managed",
                "can_dolphin": e.get("kind") == "mac" and model in DOLPHIN_REAL_MODELS,
                "bluetooth": e.get("kind") == "mac",
                "connected": cid in connected or cid in self.present_cids, "player": players.get(cid),
                "devices": sorted(b.name for b in bridges),
                "layout": sorted({b.layout.kind for b in bridges if b.layout}),
                "indicator": self.indicator_status.get(cid, {"pattern": "", "colour": ""}),
                "capabilities": {**describe_indicators(hid),
                                 "rumble": any(FF_RUMBLE in b.node.caps["ff"] for b in bridges)},
                "original_name": e.get("original_name", ""), "last_seen": e.get("last_seen"),
                "dolphin": self.dolphin_status.get(e.get("name") or "", []),
            })
        s = self.order.session
        manual = self.order.manual
        return {
            "version": VERSION,
            "session": dict(s, core=self.session_meta.get("core")) if s else None,
            "manual": {"ids": manual["ids"], "pending": manual.get("pending", False),
                       "scope": self.order.scope} if manual else None,
            "manual_scope": self.order.scope,
            "players": [cid for cid, _ in sorted(players.items(), key=lambda kv: kv[1])],
            "controllers": controllers,
            "models": [{"id": k, "label": v[0], "family": v[2], "home": v[3]} for k, v in MODELS.items()],
            "systems": [{"id": k, "label": v} for k, v in SYSTEM_LABELS.items()],
            "launch_watch": bool(self.procwatch and self.procwatch.sock),
        }

    def publish(self):
        try:
            text = json.dumps(self.state_dict(), indent=2)
        except Exception as e:
            log.error("status build failed: %s", e)
            return
        with self.lock:
            self.snapshot = text
        if self.registry is not None:
            try:
                self.registry.save()
            except OSError as e:
                log.error("couldn't save controllers.json: %s", e)
        if not self.status_path or text == self.last_status:
            return
        try:
            os.makedirs(os.path.dirname(self.status_path), exist_ok=True)
            with open(self.status_path + ".tmp", "w") as f:
                f.write(text)
            os.chmod(self.status_path + ".tmp", 0o644)
            os.replace(self.status_path + ".tmp", self.status_path)
            self.last_status = text
        except OSError as e:
            log.debug("status write failed: %s", e)

    def after_scan(self):
        self.update_indicators()
        self.publish()

    # ---- main loop -----------------------------------------------------------------------------
    def run(self):
        self.load_config(initial=True)
        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)
        signal.signal(signal.SIGHUP, lambda *_: setattr(self, "config_mtime", None))
        if self.watch_launches:
            self.procwatch = ProcWatch()
            if self.procwatch.fileno() is not None:
                self.poller.register(self.procwatch.fileno(), select.POLLIN)
        if self.want_web and self.registry is not None and self.config.web_enabled:
            try:
                self.web = WebServer(self, self.config.web_bind, self.config.web_port)
                self.web.start()
            except OSError as e:
                log.error("web UI couldn't start on port %d: %s", self.config.web_port, e)
        log.info("padnamer %s running", VERSION)
        next_scan = 0.0
        try:
            while not self.stopping:
                now = time.monotonic()
                if now >= next_scan:
                    self.load_config()
                    if self.procwatch and self.procwatch.fileno() is None:
                        for pid in self.procwatch.poll_new():
                            self.on_exec(pid)
                    self.check_session()
                    self.check_switch_emulators()
                    self.scan()
                    self.after_scan()
                    next_scan = now + self.SCAN_INTERVAL
                if self.flashes:
                    self.process_flashes()
                timeout = max(0, next_scan - time.monotonic())
                if self.flashes:
                    timeout = min(timeout, 0.05)
                try:
                    events = self.poller.poll(timeout * 1000)
                except InterruptedError:
                    continue
                for fd, mask in events:
                    if fd == self.wake_r:
                        self.run_commands()
                        continue
                    if self.procwatch and fd == self.procwatch.fileno():
                        self.handle_proc_events()
                        continue
                    entry = self.fd_map.get(fd)
                    if not entry:
                        continue
                    bridge, role = entry
                    alive = True
                    if mask & (select.POLLERR | select.POLLHUP | select.POLLNVAL):
                        alive = False
                    else:
                        try:
                            alive = bridge.pump_real() if role == "real" else bridge.pump_virtual()
                        except OSError as e:
                            log.warning("%s: %s", bridge.name, e)
                            alive = False
                    if not alive:
                        ev = next((k for k, b in self.bridges.items() if b is bridge), None)
                        if ev:
                            self.release(ev)
        finally:
            if self.web:
                self.web.stop()
            for ev in list(self.bridges):
                self.release(ev)
            if self.manage_emulators and self.files:
                try:
                    self.ra.apply({}, self.state, "retroarch_session")
                    self.dolphin.apply_seats(self.config, [], [], self.state)
                    self.files.save_state(self.state)
                except Exception as e:
                    log.error("restoring emulator config failed: %s", e)
            if self.registry is not None:
                try:
                    self.registry.save()
                except OSError:
                    pass
            if self.status_path:
                try:
                    os.unlink(self.status_path)
                except OSError:
                    pass
            if self.manage_udev:
                remove_udev_rules()
            log.info("stopped")

    def _stop(self, *_):
        self.stopping = True
        try:
            os.write(self.wake_w, b"x")
        except OSError:
            pass


class _LegacyOrder:
    """Stand-in registry for running from the TOML [[pad]] list only (tests, --no-registry)."""

    def __init__(self, config):
        self.data = {}
        self.controllers = {}
        self.dirty = False
        pads = sorted(config.pads, key=lambda p: (p.player or 99))
        self.order = [p.ident + ("#" + p.name if not p.mac else "") for p in pads]


# --------------------------------------------------------------------------
# CLI helpers
# --------------------------------------------------------------------------

def cmd_list(_args):
    rows = []
    for ev in list_event_nodes():
        n = NodeInfo(ev)
        if not n.name:
            continue
        if not (n.is_controllerish or n.bustype == BUS_BLUETOOTH or n.ours):
            continue
        tags = []
        if n.ours:
            tags.append("padnamer")
        if EV_FF in n.caps["ev"]:
            tags.append("rumble")
        if INPUT_PROP_ACCELEROMETER in n.props:
            tags.append("motion")
        if n.bustype == BUS_BLUETOOTH:
            tags.append("bluetooth")
        kind = detect_nso_type(n)
        if kind and not n.ours:
            tags.append(f"switch:{kind}")
        if find_player_leds(n.hid_dir)[0]:
            tags.append("player-leds")
        try:
            hidden = not os.access(n.devnode, os.R_OK) if os.geteuid() != 0 else \
                not (os.stat(n.devnode).st_mode & 0o077)
        except OSError:
            hidden = False
        if hidden and not n.ours:
            tags.append("hidden")
        rows.append((ev, n.name, n.mac or "-", f"{n.vendor:04x}:{n.product:04x}", ",".join(tags)))
    if not rows:
        print("No controllers found.")
        return 0
    widths = [max(len(r[i]) for r in rows + [("NODE", "NAME", "MAC", "VID:PID", "")]) for i in range(5)]
    fmt = "  ".join("{:<%d}" % w for w in widths)
    print(fmt.format("NODE", "NAME", "MAC", "VID:PID", "NOTES"))
    for r in rows:
        print(fmt.format(*r))
    return 0


def cmd_rules(args):
    sys.stdout.write(build_udev_rules(Config(args.config)))
    return 0


def cmd_check(args):
    try:
        cfg = Config(args.config)
    except Exception as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1
    print(f"OK: {len(cfg.pads)} controller(s)")
    for p in cfg.pads:
        how = f"mac {p.mac}" if p.mac else f"name '{p.match_name}'"
        extra = []
        if p.player:
            extra.append(f"player {p.player}")
        if p.present_as != "original":
            extra.append(f"as {p.present_as}")
        if p.systems:
            extra.append("systems: " + ", ".join(p.systems))
        print(f"  {p.name:<20} matched by {how}" + (f"  ({'; '.join(extra)})" if extra else ""))
    print("Controllers are managed in the web UI (padnamer status shows the address).")
    return 0


def cmd_status(_args):
    try:
        with open(STATUS_PATH) as f:
            st = json.load(f)
    except OSError:
        print("padnamer isn't running (no status). Check: systemctl status padnamer")
        return 1
    s = st.get("session")
    if s:
        print(f"Now playing: {s['emulator']} ({', '.join(s['systems']) or 'unknown system'})")
    else:
        print("No game running")
    if st.get("manual"):
        m = st["manual"]
        when = "for the next game" if m["pending"] else ("until reset" if m["scope"] == "persistent"
                                                         else "for this game")
        print(f"Order set by hand {when}")
    by_id = {c["id"]: c for c in st["controllers"]}
    print()
    for cid in st["players"]:
        c = by_id[cid]
        ind = c["indicator"]
        print(f"  P{c['player']}  {c['name']:<18} {c['model_label']:<34} "
              f"{ind.get('pattern', '')} {ind.get('colour', '')}".rstrip())
    idle = [c for c in st["controllers"] if not c["connected"]]
    if idle:
        print("\n  Not connected: " + ", ".join(c["name"] for c in idle))
    return 0


def cmd_launch(args):
    """padnamer launch n64 -- <command>: tell padnamer which system starts, then run it."""
    import urllib.request
    cmd = args.command[1:] if args.command and args.command[0] == "--" else args.command
    if not cmd:
        print("usage: padnamer launch SYSTEM[,SYSTEM] -- command ...", file=sys.stderr)
        return 2
    body = json.dumps({"systems": args.systems.split(","), "pid": os.getpid()}).encode()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{args.port}/api/session", data=body,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=8).read()
    except OSError as e:
        print(f"padnamer: couldn't reach the service ({e}); launching anyway", file=sys.stderr)
    os.execvp(cmd[0], cmd)


def cmd_rumble(args):
    target = None
    for ev in list_event_nodes():
        n = NodeInfo(ev)
        if n.name == args.name and EV_FF in n.caps["ev"]:
            target = n
            if n.ours:
                break
    if not target:
        print(f"No rumble-capable device named '{args.name}'. Try 'list'.", file=sys.stderr)
        return 1
    fd = os.open(target.devnode, os.O_RDWR)
    try:
        effect = ff_effect()
        effect.type = FF_RUMBLE
        effect.id = -1
        strength = int(max(0.0, min(1.0, args.strength)) * 0xFFFF)
        effect.u.rumble.strong_magnitude = strength
        effect.u.rumble.weak_magnitude = strength
        effect.replay.length = int(args.seconds * 1000)
        ioctl(fd, EVIOCSFF, effect)
        os.write(fd, struct.pack(EVENT_FMT, 0, 0, EV_FF, effect.id, 1))
        print(f"Rumbling '{args.name}' ({target.devnode}) for {args.seconds}s")
        time.sleep(args.seconds + 0.1)
        ioctl(fd, EVIOCRMFF, effect.id)
    finally:
        os.close(fd)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="padnamer", description=__doc__.split("\n\n")[0])
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="run the daemon (root)")
    p.add_argument("--config", required=True)
    p.add_argument("--no-udev", action="store_true", help="don't install udev rules")
    p.add_argument("--no-emulators", action="store_true", help="don't touch RetroArch/Dolphin config")
    p.add_argument("-v", "--verbose", action="store_true")
    sub.add_parser("list", help="show controllers and their MACs")
    sub.add_parser("status", help="show what the running daemon is doing")
    p = sub.add_parser("launch", help="run a game, telling padnamer its system first")
    p.add_argument("systems", help="e.g. n64 or gamecube,wii")
    p.add_argument("command", nargs=argparse.REMAINDER)
    p.add_argument("--port", type=int, default=8742)
    p = sub.add_parser("rules", help="print generated udev rules")
    p.add_argument("--config", required=True)
    p = sub.add_parser("check", help="validate config")
    p.add_argument("--config", required=True)
    p = sub.add_parser("rumble", help="test rumble on a device")
    p.add_argument("name")
    p.add_argument("--seconds", type=float, default=1.0)
    p.add_argument("--strength", type=float, default=0.7)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
                        format="%(levelname)s %(message)s")
    if args.cmd == "run":
        if os.geteuid() != 0:
            print("padnamer run needs root (it grabs devices and writes udev rules).", file=sys.stderr)
            return 1
        Daemon(args.config, manage_udev=not args.no_udev,
               manage_emulators=not args.no_emulators).run()
        return 0
    return {"list": cmd_list, "status": cmd_status, "rules": cmd_rules, "check": cmd_check,
            "rumble": cmd_rumble, "launch": cmd_launch}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
