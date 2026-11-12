"""User-mode driver for the Logitech WingMan Force USB (J-UA9, 046D:C281) on 64-bit Windows 10.

Reads the stick through Windows' built-in WinUSB driver and feeds a vJoy virtual
joystick, so games see a normal DirectInput joystick.

    python wingman_force.py                 run, feeding vJoy device 1
    python wingman_force.py --test          just print live values (no vJoy needed)
    python wingman_force.py --center 50     motor centring spring strength 0-100 (default 40, 0 = off)
    python wingman_force.py --vjoy 2        use vJoy device 2
    python wingman_force.py --strength 70   overall force-feedback strength 0-100 (default 100)
    python wingman_force.py --ffb-log       log the force effects games/XPForce send

Input report (interrupt IN endpoint 0x82), 8 bytes:
    [0]=0x01  [1:3]=X s16 LE (+-2048)  [3:5]=Y s16 LE  [5]=throttle 0-255
    [6]=buttons 1-8 bits  [7] high nibble = hat (0-7 clockwise from up, 0xF centre),
    [7] low nibble = extra buttons.
Reports starting with 0x02 are force-feedback status (byte 1 bit 1 = hand on the grip).
Output packets (interrupt OUT endpoint 0x01) are [command id, data...], I-Force protocol.
"""
import argparse
import ctypes
import os
import sys
import time

from iforce_ffb import IForce, VJoyFfbBridge
from winusb import WinUsbDevice, find_device_paths

VID, PID = 0x046D, 0xC281
AXIS_RANGE = 2048

# Physical button N (1-9) -> raw bit in (d[6] | d[7] low nibble << 8), measured on a J-UA9.
BUTTON_BITS = [0, 1, 3, 2, 4, 6, 8, 7, 5]
N_BUTTONS = len(BUTTON_BITS)


def decode(d):
    x = int.from_bytes(d[1:3], "little", signed=True)
    y = int.from_bytes(d[3:5], "little", signed=True)
    thr = d[5]
    raw = d[6] | ((d[7] & 0x0F) << 8)
    buttons = 0
    for i, bit in enumerate(BUTTON_BITS):
        if raw >> bit & 1:
            buttons |= 1 << i
    hat = d[7] >> 4
    return x, y, thr, buttons, (None if hat > 7 else hat)


class VJoy:
    HID_X, HID_Y, HID_Z = 0x30, 0x31, 0x32
    VJD_STAT_OWN, VJD_STAT_FREE = 0, 1

    def __init__(self, dev_id):
        dll = None
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), r"C:\Program Files"):
            p = os.path.join(base, "vJoy", "x64", "vJoyInterface.dll")
            if os.path.exists(p):
                dll = p
                break
        if not dll:
            raise RuntimeError("vJoy is not installed (vJoyInterface.dll not found). "
                               "Install vJoy 2.2.x, or run with --test.")
        self.v = ctypes.WinDLL(dll)
        self.id = dev_id
        if not self.v.vJoyEnabled():
            raise RuntimeError("vJoy is installed but not enabled (check 'Configure vJoy').")
        status = self.v.GetVJDStatus(dev_id)
        if status not in (self.VJD_STAT_OWN, self.VJD_STAT_FREE):
            raise RuntimeError(f"vJoy device {dev_id} is missing or in use by another program "
                               f"(status {status}). Enable it in 'Configure vJoy'.")
        if not self.v.AcquireVJD(dev_id):
            raise RuntimeError(f"Could not acquire vJoy device {dev_id}.")
        self.v.ResetVJD(dev_id)
        self.n_buttons = self.v.GetVJDButtonNumber(dev_id)
        self.n_cpov = self.v.GetVJDContPovNumber(dev_id)
        self.n_dpov = self.v.GetVJDDiscPovNumber(dev_id)

    def update(self, x, y, thr, buttons, hat):
        scale = lambda v: int((v + AXIS_RANGE) * 0x7FFF / (2 * AXIS_RANGE)) + 1
        self.v.SetAxis(scale(max(-AXIS_RANGE, min(AXIS_RANGE, x))), self.id, self.HID_X)
        self.v.SetAxis(scale(max(-AXIS_RANGE, min(AXIS_RANGE, y))), self.id, self.HID_Y)
        self.v.SetAxis(int(thr * 0x7FFF / 255) + 1, self.id, self.HID_Z)
        for i in range(min(N_BUTTONS, self.n_buttons)):
            self.v.SetBtn(bool(buttons >> i & 1), self.id, i + 1)
        if self.n_cpov:
            self.v.SetContPov(-1 if hat is None else hat * 4500, self.id, 1)
        elif self.n_dpov:
            # discrete POV: 0=up 1=right 2=down 3=left, -1 centre (diagonals rounded)
            self.v.SetDiscPov(-1 if hat is None else ((hat + 1) // 2) % 4, self.id, 1)

    def close(self):
        self.v.ResetVJD(self.id)
        self.v.RelinquishVJD(self.id)


HAT_NAMES = ["up", "up-right", "right", "down-right", "down", "down-left", "left", "up-left"]


def run(args):
    vjoy = None if args.test else VJoy(args.vjoy)
    if vjoy:
        print(f"vJoy device {args.vjoy}: {vjoy.n_buttons} buttons, "
              f"{vjoy.n_cpov} continuous / {vjoy.n_dpov} discrete POV hats")

    current = [None]          # the connected WinUsbDevice, shared with the FFB thread

    def usb_write(data):
        dev = current[0]
        if dev is not None:
            dev.write(dev.out_pipe, data)

    ff = IForce(usb_write, log=print)
    ff.strength = max(0, min(100, args.strength)) / 100
    ff.invert_spring_y = args.invert_ffb_y or args.invert_spring_y
    ff.invert_push_y = args.invert_ffb_y or args.invert_push_y
    ff.vibration_scale = max(0, min(100, args.vibration)) / 100
    ff.push_scale = max(0, min(300, args.push)) / 100
    ff.push_roll_scale = max(0, min(400, args.push_roll)) / 100
    ff.push_smooth = max(0.0, args.push_smooth)
    if args.record:
        import csv
        rec_file = open(args.record, "w", newline="")
        rec_writer = [None]
        t0 = time.time()

        def record(row):
            row = {"t": round(time.time() - t0, 3), **row}
            if rec_writer[0] is None:
                rec_writer[0] = csv.DictWriter(rec_file, fieldnames=list(row))
                rec_writer[0].writeheader()
            rec_writer[0].writerow(row)
            rec_file.flush()
        ff.recorder = record
        print(f"Recording force feedback to {args.record}")
    ff.spring_scale = max(0, min(100, args.spring)) / 100
    ff.damper_scale = max(0, min(100, args.friction)) / 100
    ff.spring_deadzone = max(0, min(100, args.spring_deadzone)) / 100
    print(f"Force feedback: spring {args.spring}% (dead zone {args.spring_deadzone}%), "
          f"friction/damper {args.friction}%")
    print(f"Force feedback: pitch flip - spring centre {'on' if ff.invert_spring_y else 'off'}, "
          f"pushes {'on' if ff.invert_push_y else 'off'}; pushes pitch {args.push}% roll {args.push_roll}%, "
          f"vibrations {args.vibration}%")
    ffb_state = {"active": False}

    def ffb_activity():       # a game / XPForce started sending effects: built-in spring off
        if not ffb_state["active"]:
            ffb_state["active"] = True
            print("\nForce-feedback app connected - built-in centring spring off.")
            ff.autocenter(0)

    def ffb_idle():           # all effects released: built-in spring back on
        if ffb_state["active"]:
            ffb_state["active"] = False
            print("\nForce-feedback app released the stick - built-in centring spring on.")
            ff.autocenter(args.center / 100)

    if args.msfs and not args.no_ffb:
        # Forces computed from MSFS flight data (SimConnect) - XPForce must NOT be running.
        from msfs_ffb import ForceModel, MsfsForceFeedback
        ff.spring_scale = 1.0          # the model already limits stiffness
        ff.spring_deadzone = 0.0
        ff.invert_spring_y = ff.invert_push_y = False
        msfs_recorder = ff.recorder
        ff.recorder = None
        model = ForceModel(stiffness=None if args.msfs_stiffness is None else max(0, min(100, args.msfs_stiffness)) / 100,
                           trim=args.trim / 100, gusts=args.gusts / 100,
                           buffet=args.buffet / 100, rumble=args.rumble / 100,
                           stall_speed=args.stall_speed, cruise_speed=args.cruise_speed,
                           profile="c208" if args.aircraft == "auto" else args.aircraft)
        msfs = MsfsForceFeedback(ff, model, log=print, dll_path=args.simconnect,
                                 on_active=ffb_activity, on_idle=ffb_idle, recorder=msfs_recorder,
                                 auto_profile=args.aircraft == "auto")
        msfs.start()
        print(f"Force feedback: from MSFS flight data - aircraft profile {args.aircraft}, trim {args.trim}%, "
              f"gusts {args.gusts}%, buffet {args.buffet}%, rumble {args.rumble}%  (do not run XPForce)")
    elif vjoy and not args.no_ffb:
        if vjoy.v.IsDeviceFfb(args.vjoy):
            vjoy.ffb_bridge = VJoyFfbBridge(vjoy.v, args.vjoy, ff, log=print, verbose=args.ffb_log,
                                            on_activity=ffb_activity, on_idle=ffb_idle)
            print("Force feedback: enabled (vJoy FFB -> WingMan Force motors)")
        else:
            print("Force feedback: vJoy device has no FFB effects enabled (run vJoyConfig with -e all)")

    try:
        while True:
            paths = find_device_paths(VID, PID)
            if not paths:
                print("Waiting for WingMan Force (046D:C281)...", end="\r")
                time.sleep(1)
                continue
            try:
                with WinUsbDevice(paths[0]) as dev:
                    print("WingMan Force connected.                    ")
                    current[0] = dev
                    try:
                        ff.init()
                        ff.autocenter(0 if ffb_state["active"] else args.center / 100)
                    except OSError as e:
                        print(f"(could not initialise force feedback: {e})")
                    pipe = dev.in_pipe
                    dev.set_timeout(pipe, 500)
                    last = None
                    last_raw = (0, 0)
                    hand = None
                    if args.buttons:
                        print("Press each button one at a time (Ctrl+C to stop):")
                    while True:
                        d = dev.read(pipe)
                        if d is None or len(d) < 2:
                            continue
                        if d[0] == 0x02:
                            h = bool(d[1] & 0x02)
                            if h != hand and (args.verbose or args.ffb_log):
                                print(f"\nHand sensor: {'hand on stick' if h else 'no hand (forces off)'}")
                            hand = h
                            continue
                        if len(d) < 8 or d[0] != 0x01 or d == last:
                            continue
                        last = d
                        state = decode(d)
                        ff.stick = (round(state[0] / AXIS_RANGE, 4), round(state[1] / AXIS_RANGE, 4))
                        if vjoy:
                            vjoy.update(*state)
                        raw_btn = (d[6], d[7] & 0x0F)
                        if args.buttons:
                            if raw_btn != last_raw:
                                last_raw = raw_btn
                                b = state[3]
                                names = " ".join(str(i + 1) for i in range(N_BUTTONS) if b >> i & 1)
                                if raw_btn != (0, 0):
                                    print(f"button {names or '(unmapped!)':8s}  raw byte6={d[6]:08b} byte7={d[7] & 0x0F:04b}")
                        elif (args.test or args.verbose) and not args.ffb_log:
                            x, y, thr, b, hat = state
                            btns = " ".join(str(i + 1) for i in range(N_BUTTONS) if b >> i & 1) or "-"
                            print(f"X {x:+5d}  Y {y:+5d}  throttle {thr:3d}  "
                                  f"hat {HAT_NAMES[hat] if hat is not None else 'centre':10s}  "
                                  f"buttons {btns:20s}", end="\r")
            except OSError as e:
                current[0] = None
                print(f"\nDevice lost ({e}); retrying...")
                time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        dev = current[0]
        current[0] = None
        if dev is not None:
            try:
                dev.write(dev.out_pipe, bytes([0x42, 0x05]))
                dev.write(dev.out_pipe, bytes([0x40, 0x03, max(0, min(100, args.center))]))
                dev.write(dev.out_pipe, bytes([0x40, 0x04, 0x01]))
            except OSError:
                pass
        if vjoy:
            vjoy.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--test", action="store_true", help="print live values only, no vJoy")
    ap.add_argument("--vjoy", type=int, default=1, help="vJoy device number (default 1)")
    ap.add_argument("--center", type=int, default=40, help="centring spring strength 0-100 (default 40)")
    ap.add_argument("--strength", type=int, default=100, help="overall force-feedback strength 0-100 (default 100)")
    ap.add_argument("--spring", type=int, default=60,
                    help="stiffness of game spring effects 0-100 (default 60; above ~70 this stick buzzes)")
    ap.add_argument("--spring-deadzone", type=int, default=0,
                    help="minimum dead zone around the spring centre in percent (5 also stops the buzz)")
    ap.add_argument("--friction", type=int, default=0, help="strength of game friction/damper effects 0-100 (default 0)")
    ap.add_argument("--invert-ffb-y", action="store_true", help="flip forward/back of all game forces (pitch)")
    ap.add_argument("--invert-spring-y", action="store_true", help="flip forward/back of game spring centres only")
    ap.add_argument("--invert-push-y", action="store_true", help="flip forward/back of game pushes/vibrations only")
    ap.add_argument("--push", type=int, default=25,
                    help="strength of game pitch push forces (aerodynamic loads, weight), percent (default 25)")
    ap.add_argument("--push-roll", type=int, default=200,
                    help="strength of game roll push forces (gusts), percent, may exceed 100 (default 200)")
    ap.add_argument("--push-smooth", type=float, default=0.3,
                    help="smoothing of game pushes in seconds (default 0.3; 0 = off)")
    ap.add_argument("--vibration", type=int, default=100, help="strength of game vibration effects 0-100 (default 100)")
    ap.add_argument("--no-ffb", action="store_true", help="disable force feedback")
    ap.add_argument("--msfs", action="store_true",
                    help="compute force feedback from MSFS flight data (SimConnect) instead of XPForce")
    ap.add_argument("--aircraft", default="auto", choices=["auto", "c172", "c208", "b738"],
                    help="--msfs: aircraft profile (default auto: chosen from the aircraft loaded in MSFS)")
    ap.add_argument("--msfs-stiffness", type=int, default=None, help="--msfs: stick stiffness at cruise, percent (overrides profile)")
    ap.add_argument("--trim", type=int, default=100, help="--msfs: how far trim moves the stick's resting point, percent")
    ap.add_argument("--gusts", type=int, default=100, help="--msfs: strength of gust/turbulence bumps, percent")
    ap.add_argument("--buffet", type=int, default=100, help="--msfs: strength of stall buffet, percent")
    ap.add_argument("--rumble", type=int, default=100, help="--msfs: strength of runway rumble, percent")
    ap.add_argument("--stall-speed", type=float, default=None, help="--msfs: stall speed in knots (overrides profile)")
    ap.add_argument("--cruise-speed", type=float, default=None,
                    help="--msfs: speed in knots where the stick reaches full stiffness (overrides profile)")
    ap.add_argument("--simconnect", metavar="PATH", help="--msfs: path to SimConnect.dll")
    ap.add_argument("--record", metavar="FILE.csv", help="record stick position and game spring/push values")
    ap.add_argument("--ffb-log", action="store_true", help="log force-feedback effects received from games")
    ap.add_argument("--buttons", action="store_true", help="button identification mode (implies --test)")
    ap.add_argument("-v", "--verbose", action="store_true", help="print live values while feeding vJoy")
    args = ap.parse_args()
    args.test = args.test or args.buttons
    run(args)


if __name__ == "__main__":
    main()
