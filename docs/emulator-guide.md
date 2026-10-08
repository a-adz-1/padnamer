# Controller guide for every emulator

With padnamer running, each game puts its own controllers first: start an N64 game and the N64 pad is Player 1; start a Mega Drive game and the Mega Drive pad is. Most emulators follow along on their own; a few need a one-time setup, listed below.

## How padnamer picks Player 1

Every controller has **home systems** (an N64 pad's is Nintendo 64, a DualSense's is the PlayStations). You can change them on the controller page. When you start a game from the Steam menu:

1. padnamer sees the emulator start and works out the system (RetroArch's core gives it away: Mupen64Plus-Next means N64).
2. It holds the emulator for a moment.
3. It puts the controllers whose home includes that system first, then everyone else in the usual order.
4. It lights each controller's player number, writes the order into RetroArch or Dolphin if that's what's starting, and lets the game carry on.
5. When the game closes, everything goes back to the usual order.

If nobody's home controller is connected, the usual order is used, so any controller still works. A controller that's switched on mid-game joins at the end without shuffling anyone else.

**Setting the order by hand** (controller page or Quick Access panel) beats all of this. Set between games, it's used for the next game; set during a game, it lasts until that game closes. Then it's back to automatic.

## Once, before anything else

1. **Install padnamer.** In Desktop Mode run `./install.sh` from the padnamer folder. It also installs the Quick Access panel if Decky Loader is there.
2. **Turn Steam Input off for emulator shortcuts.** With it on, Steam re-wraps every controller as one generic Xbox pad and the names and order are lost. In Steam ROM Manager, set each parser's *Steam Input* option to disabled and save the shortcuts again. For a single shortcut: Properties → Controller → Disable Steam Input. Leave it on for normal Steam games.
3. **Connect every controller once** so padnamer remembers it. Then open the controller page (the Controllers tile, the panel's *Edit controllers*, or `http://<deck address>:8742` on a phone with the PIN), and check each one's name, model and home systems.
4. **Set the usual order**: choose *Always* on the controller page and arrange the line-up. That's the order used between games and after each game's own controllers.

## Every emulator at a glance

Three kinds:

- **Automatic**: the emulator takes controllers in the order padnamer creates them, or padnamer writes the order into its settings.
- **One profile per pad**: map each pad once, then padnamer swaps it in.
- **Bind once**: the emulator remembers each pad by its own identity. padnamer's unique names keep that working, but the emulator won't fall back to another pad.

| Emulator | Systems | Own pad becomes P1 | What you do once |
| --- | --- | --- | --- |
| RetroArch | NES, SNES, Mega Drive, Master System, Game Boy, GBA, PC Engine, PS1, Saturn, arcade | Automatic | Nothing |
| RMG | N64 | Automatic | Set each player's device to *Automatic* |
| PCSX2 | PS2 | Automatic | Nothing (keep the default SDL-0, SDL-1 bindings) |
| DuckStation | PS1 | Automatic | Nothing (keep the default bindings) |
| melonDS | DS | Automatic | Leave the controller set to the first one |
| PPSSPP, mGBA, Vita3K | PSP, GBA, Vita | Automatic (one player: uses the first pad) | Nothing |
| MAME | Arcade | Automatic | Nothing |
| Dolphin, PrimeHack | GameCube, Wii | One profile per pad | Save a profile named exactly like each pad |
| Dolphin with a Balance Board or real Wii Remote | Wii | Handed to Dolphin untouched | Set the slot to *Real Balance Board* / *Real Wii Remote* |
| Eden, Ryujinx | Switch | Bind once | Pick each player's pad by name, type *Pro Controller* |
| xemu | Xbox | Bind once (remembers each pad's port) | Connect pads in player order the first time |
| Cemu | Wii U | Bind once | Pick each player's pad by name, save a profile |
| Azahar | 3DS | Bind once | Pick the pad by name |
| ares, RPCS3, Flycast, Xenia | Various | Not checked yet: treat as bind once | Pick each player's pad by name |

## Emulator by emulator

### RetroArch

Nothing to set up. When a game starts, padnamer writes the player order into that core's override file (for example `config/Mupen64Plus-Next/Mupen64Plus-Next.cfg`) as *Preferred* device reservations, and removes them when the game closes. Your own settings in those files are left alone.

- If you've set a reservation yourself in Settings → Input → Port 1 Controls, clear it, or it fights padnamer.
- A *game* override with its own reservation still wins over padnamer's.
- Changing the order mid-game doesn't move players in RetroArch until the next launch. Use Quick Menu → Controls → Port 1 for that game.
- New controllers may need a one-off bind: Settings → Input → Port 1 Controls → *Save Controller Profile*. Profiles are saved under the controller's padnamer name, so each pad keeps its own.

### RMG (N64)

In RMG: Settings → Input. For Players 1 to 4, set the input device to **Automatic** and tick *Plugged in*. Automatic takes controllers in the order padnamer created them, so the N64 pad is Player 1. If a player is set to a specific pad instead, that pad stays on that player whatever padnamer does.

### PCSX2 (PS2) and DuckStation (PS1)

Keep the default bindings: Controller Port 1 uses *SDL-0*, Port 2 *SDL-1*. Those numbers follow the order padnamer creates the pads in, so they track the player order. If you remapped buttons, map them on SDL-0 rather than on a pad's name.

### Dolphin and PrimeHack (GameCube, Wii)

Dolphin binds each port to one named device and has no fallback, so padnamer swaps profiles in:

1. Connect the pad. In Dolphin → Controllers, set Port 1 to *Standard Controller* (GameCube) or *Emulated Wii Remote* (Wii), then *Configure*.
2. Pick the pad from the device list (it shows as `evdev/0/<its name>`), map the buttons.
3. Type the pad's exact padnamer name into the profile box, for example `GameCube A`, and press *Save*.
4. Repeat for every pad you'd use in Dolphin, including non-GameCube ones like a DualSense.

From then on, when a GameCube or Wii game starts, padnamer puts each player's profile into GCPad1–4 or Wiimote1–4, and restores the ports when the game closes. Connect pads before starting the game; Dolphin reads these at launch. Real Wii hardware (a Balance Board, or a Wii Remote you want Dolphin to drive directly) is covered next.

### Wii Balance Board (and real Wii Remotes) in Dolphin

Dolphin only supports a *real* Balance Board, talking to it directly. padnamer recognises the board and leaves it untouched for Dolphin: it isn't renamed or put in the player order, games other than Dolphin don't see it, and its raw device is opened up so Dolphin can reach it. On the controller page it shows as *Handed to Dolphin as a real Wii device*.

1. Pair the board with SteamOS once: Settings → Bluetooth, then press the red sync button in its battery compartment.
2. In Dolphin → Controllers, under Wii Remotes, set the **Balance Board** slot to *Real Balance Board* and tick *Continuous Scanning*.
3. Keep Wii Remote 1 as an emulated Wii Remote with that player's profile, so the menus still work from a controller.
4. Start Wii Fit with the board switched on.

If Dolphin doesn't find it, press **Disconnect** on the board's row, then press its red sync button with the game open, so Dolphin pairs with it directly.

Any Wii Remote or Wii U Pro Controller can go the same way: Edit → *How padnamer handles it* → *Hand it to Dolphin as a real Wii device*, then set that Dolphin slot to *Real Wii Remote*. You get its speaker and exact motion, but it leaves padnamer's player order.

### Eden and Ryujinx (Switch)

These remember each pad by its identity, so assign them once:

1. Open the emulator's controller settings. For Player 1, choose controller type **Pro Controller** and pick the pad by its padnamer name.
2. Do the same for Players 2–4 with the pads you'd normally use.

For Nintendo retro pads, set *In Switch emulators* on the controller page to **Act as a Switch Pro Controller while a Switch emulator is open**. The pad then has the Switch's own layout in Eden or Ryujinx and its normal layout everywhere else. Bind it while the emulator is open, so it's in that mode when you pick it. If the assigned pad is off, that player has no controller; switch it on or reassign.

### xemu (Xbox)

xemu remembers which port each pad was in and puts new pads in the first free port. Connect the pads once in player order with the emulator open; after that each goes back to its own port.

### Cemu (Wii U) and Azahar (3DS)

Pick the controller per player by name and save a controller profile. Like the Switch emulators, they don't fall back to another pad.

### PPSSPP, mGBA, Vita3K, MAME, melonDS

One-player systems, or ones that take the first controller: whichever pad is Player 1 is the one they use. Nothing to set.

### ares, RPCS3, Flycast, Xenia

Not checked against padnamer yet. Pick each player's controller by name in the emulator's settings, and please [file an emulator report](https://github.com/a-adz-1/padnamer/issues/new?template=emulator.yml) saying how it behaves.

## Everyday use

**Who's who.** Each controller shows its player number on its own lights:

- Switch-style pads light 1–4 LEDs the way a Switch does.
- Wii Remotes light one LED per player.
- DualSense pads use their centred player LEDs.
- PlayStation light bars glow blue, red, green or pink for Players 1–4.

On the controller page the same colours are on the player numbers. Xbox and most 8BitDo pads have no lights padnamer can set: press **Find** and that pad buzzes.

**Swapping players.** In Game Mode press the … button, open the plug icon, then *padnamer*:

- ▲ / ▼ moves a controller up or down the line-up. Set before starting a game, it's used for that next game; during a game, it lasts until the game closes.
- *Find* flashes and buzzes that controller.
- *Off* disconnects it.
- *Save as the usual order* makes moves permanent instead.
- *Back to automatic order* undoes a hand-set order straight away.

**Turning controllers off.** Each Bluetooth controller in the line-up has a **Disconnect** button (*Off* in the Quick Access panel). *Disconnect all controllers* drops every one at once, which is handy at bedtime. Most controllers switch themselves off when disconnected; press their home or sync button to bring them back. Controllers on a cable, and the Switch 2 GameCube pad, can't be disconnected this way.

The same controls are on the Controllers tile and on a phone at `http://<deck address>:8742` (PIN on the controller page). On that page, a controller selected with the d-pad moves with **LB / RB**, and **Y** finds it.

**Mid-game moves.** Emulators that take pads in order notice straight away. RetroArch and Dolphin pick up a new order at the next game start.

## When something's off

| What you see | Likely cause | Fix |
| --- | --- | --- |
| A controller shows up twice in an emulator | Steam Input is on for that shortcut | Disable Steam Input for it (step 2 of *Once, before anything else*) |
| Every controller is called Xbox 360 Controller | Same: Steam Input | Same fix |
| The wrong pad is Player 1 in one emulator | That emulator binds a specific pad | Check its row in the table above; for RMG set *Automatic* |
| The order didn't change when a game started | The emulator wasn't recognised | Check the controller page says *Now playing*; if not, add `~/.local/bin/padnamer launch <system> --` in front of the shortcut's launch command |
| A new controller isn't renamed | It's on USB, or set to be left alone | On the controller page: Edit → *How padnamer handles it* → *Rename it and put it in the player order* |
| No controllers at all | The padnamer service stopped | `sudo systemctl status padnamer`; stopping it gives every controller back its original name |
| A game freezes for a moment at start, or misbehaves | padnamer holds the emulator while it sorts the pads | Set `pause_on_launch = false` in `~/.config/padnamer/config.toml` |
| Dolphin doesn't see the Balance Board | Dolphin can't reach it while SteamOS holds the connection | Press Disconnect on the board, then its red sync button with the game open |

The live log is in Konsole with `journalctl -u padnamer -f`.

## Sources

- [RetroArch: device reservation matching](https://github.com/libretro/RetroArch/blob/master/tasks/task_autodetect.c): a reserved name with no `vid:pid` prefix matches by device name
- [RMG input plugin](https://github.com/Rosalie241/RMG/blob/master/Source/RMG-Input/main.cpp): the *Automatic* device takes controllers in order
- [PCSX2 SDL input](https://github.com/PCSX2/pcsx2/blob/master/pcsx2/Input/SDLInputSource.cpp): SDL-n numbers are handed out lowest-free in connection order (DuckStation shares this design)
- [xemu input](https://github.com/xemu-project/xemu/blob/master/ui/xemu-input.c): remembers each pad's port, auto-binds new pads to the first free port
- [Dolphin evdev backend](https://github.com/dolphin-emu/dolphin/blob/master/Source/Core/InputCommon/ControllerInterface/evdev/evdev.cpp): devices bound by name
- [Dolphin real Wii Remote support](https://github.com/dolphin-emu/dolphin/blob/master/Source/Core/Core/HW/WiimoteReal/WiimoteReal.cpp): Balance Board is real-hardware only
- [SDL Linux joysticks](https://github.com/libsdl-org/SDL/blob/main/src/joystick/linux/SDL_sysjoystick.c): controllers sorted by device number
- [Linux hid-nintendo](https://github.com/torvalds/linux/blob/master/drivers/hid/hid-nintendo.c) and [hid-playstation](https://github.com/torvalds/linux/blob/master/drivers/hid/hid-playstation.c): player LED patterns and light bar colours
