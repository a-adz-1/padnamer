# padnamer

[![tests](https://github.com/a-adz-1/padnamer/actions/workflows/tests.yml/badge.svg)](https://github.com/a-adz-1/padnamer/actions/workflows/tests.yml)
[![licence: GPL v3](https://img.shields.io/badge/licence-GPLv3-blue.svg)](LICENSE)

**Give every controller on SteamOS its own name and player light, and make each game pick its own controller as Player 1.** Start an N64 game and the N64 pad is Player 1; start a Mega Drive game and the Mega Drive pad is. Kids can swap players from the Quick Access menu.

> **Beta.** padnamer is new and has mostly been tested in simulation. It's being tried on real Steam Decks now. Expect rough edges, and please [report how your controllers and emulators behave](https://github.com/a-adz-1/padnamer/issues/new/choose): that's what turns it into something that just works for everyone.

What it does:

- **Remembers every controller** that connects, works out what it is (Xbox
  Series, DualSense, DualShock 4, Switch Pro, Joy-Con, NES / SNES / N64 / Mega
  Drive Switch Online pads, Wii Remote, Wii U Pro, GameCube…) and names it
  ("N64 A", "DualSense B"). Rename it, correct the model and choose its home
  systems on the controller page.
- **Shows the player number on the controller**: Switch pads use Nintendo's
  LED patterns, Wii Remotes one LED per player, DualSense pads their centred
  player LEDs, and PlayStation light bars glow blue, red, green or pink. Xbox
  and most 8BitDo pads have no settable lights, so *Find* buzzes them instead.
- **Watches for games starting** from the Steam menu. It holds the emulator for
  a moment, puts that system's controllers first, writes the order into
  RetroArch or Dolphin, then lets the game run. When the game closes it puts
  everything back. No shortcuts need editing.
- **Lets you reorder**: on the controller page (this machine, or a phone on
  your network with a PIN), the Decky Quick Access panel, or the Controllers
  library tile. A hand-set order lasts for that game, or the next game if
  set between games.
- **Rumble** passes through; Switch Online pads can **act as Switch Pro
  Controllers** while a Switch emulator is open.
- **Disconnect** any Bluetooth controller (or all of them) from the page or
  the Quick Access panel; most pads switch themselves off.
- **Real Wii hardware for Dolphin**: a Wii Balance Board is recognised and
  handed to Dolphin untouched (Dolphin only supports the real board), and any
  Wii Remote or Wii U Pro Controller can be handed over the same way. Their
  input devices are hidden from other games and their raw HID device is opened
  up for Dolphin.

See the **[emulator guide](docs/emulator-guide.md)** for what each emulator needs.

## Install (Desktop Mode, Konsole)

Download the latest `padnamer-*.tar.gz` from [Releases](https://github.com/a-adz-1/padnamer/releases), or clone the repo:

```bash
passwd                    # only if you've never set a sudo password
git clone https://github.com/a-adz-1/padnamer.git
cd padnamer
./install.sh
```

To update later: `git pull && ./install.sh` (or extract a newer release and run `./install.sh`).

The installer:

- puts padnamer in `~/.local/share/padnamer`
- adds the system service
- adds the Decky panel if Decky Loader is installed, and a *Controllers* app
  for the library tile (Steam → *Add a Non-Steam Game* → tick *Controllers*)

It's safe to re-run, and keeps your settings.

Then:

1. Turn off Steam Input for emulator shortcuts. In Steam ROM Manager, set
   each parser's *Steam Input* to disabled and save.
2. Switch on each controller once.
3. Open the controller page and check the names, models, home systems and
   usual order.

```bash
padnamer status            # who's which player right now
journalctl -u padnamer -f  # live log
```

## The controller page

`http://127.0.0.1:8742` on the SteamOS machine (no PIN needed there). On
a phone or PC, use the address shown at the bottom of that page, plus its
PIN. With a controller, the d-pad moves around, **A** selects, **LB / RB**
moves the selected player, and **Y** finds that controller.

## Settings

`~/.config/padnamer/config.toml`, commented. Changes apply within a second.
The main ones:

| Setting | Default | What it does |
|---|---|---|
| `manual_order` | `session` | How long a hand-set order lasts: `session`, `persistent` or `boot` |
| `pause_on_launch` | `true` | Hold a starting emulator while the pads are put in order |
| `led_style` | `switch` | `switch`, `single` or `off` |
| `[web] bind` | `0.0.0.0` | `127.0.0.1` keeps the page on this machine only |
| `[web] require_pin` | `true` | Phones/PCs need the PIN |
| `[retroarch] reservation` | `preferred` | `reserved` makes a port wait for its pad |

The controllers themselves live in `~/.config/padnamer/controllers.json`,
managed from the page. From 1.x, `[[pad]]` entries are imported once,
ordered by their old `player` numbers.

## If a game isn't recognised

padnamer knows RetroArch (and which system from its core), Dolphin, PrimeHack,
DuckStation, PCSX2, PPSSPP, RMG, ares, Azahar, melonDS, Cemu, xemu, Xenia,
Eden/Ryujinx and the other Switch emulators, Flycast, Vita3K, RPCS3, mGBA and
MAME. For anything else, put this in front of the launch command:

```
~/.local/bin/padnamer launch n64 -- %command%
```

## Surviving SteamOS updates

| Where | What | After an update |
|---|---|---|
| `~/.local/share/padnamer/` | program and page | kept |
| `~/.config/padnamer/` | settings and controllers | kept |
| `~/.local/state/padnamer/` | which emulator settings padnamer changed | kept |
| `/etc/systemd/system/padnamer.service` | service | kept (SteamOS preserves `/etc`) |
| `/etc/udev/rules.d/72-padnamer.rules` | hides the original devices | rewritten at every start |
| `~/homebrew/plugins/padnamer` | Quick Access panel | kept with Decky |

If a big update ever resets `/etc`, run `./install.sh` again.

## Uninstall

`./uninstall.sh`. This keeps `~/.config/padnamer`.

## How it works

- **Taking over pads.** For each controller, padnamer takes exclusive control
  of its input device and creates a virtual copy with the new name through
  `/dev/uinput`. Events pass through, and rumble effects are re-uploaded to the
  real pad. A udev rule hides the originals.
- **Player order.** Player order is the order the virtual pads are created in.
  SDL numbers controllers by device number, so for emulators that take the
  first pad they find, creating them in order *is* the player order. RetroArch
  gets name-based reservations at launch, and Dolphin gets profile swaps.
- **Launch detection.** Launches are seen through the kernel's process-events
  connector, so padnamer hears about a game the instant it starts.
- **No dependencies.** padnamer is standard-library Python 3.11+.

Tests: `python3 -m unittest discover tests` (87 tests). They cover:

- the kernel interface, checked against the real headers
- rumble and LED patterns
- the registry and ordering
- launch detection, including the netlink message format
- RetroArch and Dolphin file handling
- the web API with its PIN and cross-site protection
- the Decky backend

The Decky panel's source is in `decky/` (`npm ci && npm run build`).

## Helping out

- **Reports are the most useful thing right now.** Use the [controller](https://github.com/a-adz-1/padnamer/issues/new?template=controller.yml) and [emulator](https://github.com/a-adz-1/padnamer/issues/new?template=emulator.yml) report forms. They ask for exactly what's needed, including `padnamer list` output.
- **Pull requests are welcome.** Run `python3 -m unittest discover tests` first. New controller models usually need just a line in `MODELS` / `VIDPID_MODELS`. New emulators need a line in `EMULATORS` (plus `RETROARCH_CORES` for RetroArch cores).

## Licence

padnamer is free software under the [GNU General Public License v3.0 or later](LICENSE). You can use, share and change it. If you distribute a modified version, you must share its source under the same licence.

The Quick Access panel in `decky/` started from Decky Loader's [plugin template](https://github.com/SteamDeckHomebrew/decky-plugin-template) (BSD-3-Clause; its notice is kept in `decky/LICENSE.template-BSD-3-Clause`).
