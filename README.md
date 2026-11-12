# Logitech WingMan Force (J-UA9) on Windows 10/11 64-bit

A user-mode driver that brings the original **Logitech WingMan Force** USB joystick
(model **J-UA9**, USB ID `046D:C281`) back to life on 64-bit Windows 10 and 11 -
including **force feedback** for Microsoft Flight Simulator 2020.

> This is **not** for the WingMan Force 3D or WingMan Strike Force 3D (those are standard
> HID devices and still work with Logitech's later drivers).

## Why this exists

Logitech only ever shipped a **32-bit** kernel driver (`WmUsbHid.sys`) for the J-UA9. The
stick is not a standard HID device, so 64-bit Windows has no driver for it at all and
it shows up as an unknown "Wingman Force" device.

This project replaces that driver with a small Python program:

```
WingMan Force ──USB──> WinUSB (built into Windows)
                          │
                    wingman_force.py ──> vJoy virtual joystick ──> games / MSFS
                          │
              force feedback  <── MSFS flight data (SimConnect)   [--msfs, default]
                              <── or DirectInput effects via vJoy (e.g. XPForce)
```

No unsigned or self-signed kernel code is involved: WinUSB is Microsoft's signed
driver, and vJoy is a signed virtual-joystick driver.

## What works

- Stick X/Y, throttle, 9 buttons, hat switch - as a normal DirectInput joystick ("vJoy Device")
- Built-in centring spring when no force-feedback application is running
- **Force feedback computed from MSFS 2020 flight data** (recommended), with aircraft
  profiles picked automatically from the loaded aircraft (small Cessna, Cessna 208 Caravan,
  Boeing 737):
  - stick stiffness grows with airspeed and G-load
  - the stick rests where you trimmed it (a short push/pull while trimming)
  - gust bumps from the wind (stronger at low speed), stall buffet, 737 stick shaker, runway rumble
- Alternatively, force feedback from any DirectInput game/tool through vJoy (e.g. XPForce)

## Requirements

- Windows 10 or 11, 64-bit
- **Python 3.10+** ([python.org](https://www.python.org/downloads/)) - no extra packages needed
- **vJoy 2.2.2.0** - included in `third_party/vJoy` (MIT licence). Older vJoy 2.1.x has a
  force-feedback bug (every effect gets the same ID) and will not work properly.
- For MSFS force feedback: **`SimConnect.dll`** from the MSFS SDK (not redistributed here, see below)
- The WingMan Force **power adapter** for force feedback

## Installation

### 1. Give the stick the WinUSB driver
1. Plug in the stick. Open **Device Manager** (`Win+R` → `devmgmt.msc`).
2. Under *Other devices*, right-click **Wingman Force** → **Update driver**.
3. **Browse my computer for drivers** → **Let me pick from a list of available drivers on my computer**.
4. Choose **Universal Serial Bus devices** → Next → Manufacturer **WinUsb Device**, Model **WinUsb Device** → Next → accept the warning.

The stick now appears under *Universal Serial Bus devices*. (Undo: right-click → *Uninstall device*.)

### 2. Install vJoy
1. Run `third_party/vJoy/vJoySetup_v2.2.2.0_Win10_Win11.exe` (if an older vJoy is installed,
   uninstall it first via *Settings → Apps*).
2. Run `tools/setup_vjoy.bat` **as administrator**. It configures vJoy device 1 to match the
   stick (X, Y, Z/throttle, 9 buttons, 1 hat, all force-feedback effects) and names it
   "WingMan Force J-UA9" (XPForce refuses devices called "vJoy Device").

### 3. SimConnect.dll (only for MSFS force feedback)
`SimConnect.dll` comes with the **MSFS SDK** (*Options → General → Developers* in MSFS, or the
SDK installer). Copy it next to `wingman_force.py`, or pass `--simconnect PATH`. Many MSFS
add-ons also ship a copy; the program checks the SDK's default location automatically.

### 4. Check the stick
```
python tools/probe.py
```
Move the stick and press buttons - you should see raw packets. Then run the driver once with
`python wingman_force.py --buttons` to confirm all 9 buttons.

## Usage

Double-click **`Start WingMan Force.bat`** and keep its window open while playing.

In MSFS, bind the **WingMan Force J-UA9** (vJoy) device: *Ailerons (Axis)* → X axis,
*Elevator (Axis)* → Y axis, throttle → Z axis, sensitivity 0, small dead zone. Do **not** bind
the axes to the "Aileron left/right" or "Elevator up/down" keys.

**Do not run XPForce** in the default (`--msfs`) mode - two programs would drive the motors.

The stick has an optical **hand sensor**: forces only play while a hand is on the grip.

### Options

| Option | Meaning |
|---|---|
| `--msfs` | force feedback from MSFS flight data (used by the .bat) |
| `--aircraft auto\|c172\|c208\|b738` | aircraft profile (default: detected from MSFS) |
| `--trim`, `--gusts`, `--buffet`, `--rumble` | effect strength in percent (default 100) |
| `--msfs-stiffness N` | stiffness at cruise speed, percent |
| `--stall-speed KT`, `--cruise-speed KT` | override the profile's speeds |
| `--center N` | built-in centring spring when idle, percent (default 40) |
| `--no-ffb` | joystick only |
| `--record FILE.csv` | record stick and force values for tuning |
| `--test`, `--buttons` | show live values / identify buttons (no vJoy needed) |

For XPForce or other DirectInput force-feedback software instead of `--msfs`:
`python wingman_force.py --invert-spring-y` (pitch spring direction needed flipping with
XPForce in MSFS). See `python wingman_force.py --help` for all XPForce-mode options.

## How the force feedback works (technical notes)

The J-UA9 is an **Immersion I-Force 2.0** device. The protocol is documented in the Linux
kernel (`Documentation/input/devices/iforce-protocol.rst`); this project is an independent
implementation. Findings that are **not** in that documentation, measured on a real J-UA9 or
taken from the behaviour of Logitech's original driver:

- Start-up needs `40 05 00 04` (axis saturation threshold) - otherwise effects are near zero.
- An effect's envelope block with zero levels silences it; use address `FFFF` (no envelope).
- Effect duration `0` means *zero length*; infinite is `0xFFFF`.
- Direction byte: `0` pushes back, `64` left, `128` forward, `192` right.
- Spring coefficient `+` pulls to the centre; centre offset `+0.5` moves the stick to `+0.5`.
- Byte 1 bit 1 of the `0x02` status report is the hand sensor; nothing plays without a hand.
- **Only one effect may run at a time** without the stick buzzing at the centre, and spring
  stiffness above ~60 % buzzes. All MSFS feel is therefore expressed as one spring whose
  stiffness and centre are updated ~60 times per second; constant forces are folded into the
  spring centre.
- Forces must not depend on the aircraft's own accelerations or on the wind *relative to the
  aircraft*: both follow the pilot's inputs and create a feedback loop (pitch wobble). Gusts
  are taken from the world-frame wind, with the steady wind filtered out *before* projecting
  onto the aircraft axes (otherwise turning in a steady wind feels like a gust).

## Uninstall
Device Manager → right-click the stick → *Uninstall device*; uninstall vJoy via *Settings → Apps*.

## Credits and licences
- This project: MIT licence (see `LICENSE`).
- vJoy © Shaul Eizikovich, BrunnerInnovation fork 2.2.2.0 - MIT licence (`third_party/vJoy/LICENSE.txt`).
- I-Force protocol description: Linux kernel documentation (Johann Deneux, Vojtech Pavlik).
- Not affiliated with or endorsed by Logitech, Immersion or Microsoft. WingMan, I-Force and
  Microsoft Flight Simulator are trademarks of their respective owners.
- Use at your own risk; force feedback can move the stick unexpectedly.
