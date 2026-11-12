"""Force feedback computed directly from Microsoft Flight Simulator (SimConnect).

Replaces XPForce. Everything is played as ONE spring effect on the WingMan Force -
measured on this stick: a second running effect makes the spring buzz, and spring
stiffness above ~60% buzzes too. Every feel is therefore expressed as spring
stiffness plus a moving spring centre, updated ~60 times a second:

  stiffness   grows with airspeed (dynamic pressure) and G-load
  centre      follows elevator / aileron trim (stick rests where trimmed)
              + elevator weight on the ground (stick leans forward when slow)
              + gust bumps from the aircraft's own accelerations
              + stall buffet and runway rumble (small fast centre oscillation)

Nothing here depends on the stick position or control surface deflection, so the
forces cannot form a feedback loop with the pilot's inputs (what made XPForce wobble).
Device axes: X right = +, Y back (towards pilot) = +; centre and stiffness are -1..1 / 0..1.
"""
import ctypes
import math
import os
import random
import threading
import time
from ctypes import wintypes as wt

# Where to look for SimConnect.dll (Microsoft MSFS SDK; not redistributed here).
SIMCONNECT_PATHS = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "SimConnect.dll"),
    r"C:\MSFS SDK\SimConnect SDK\lib\SimConnect.dll",
    r"C:\MSFS 2024 SDK\SimConnect SDK\lib\SimConnect.dll",
    r"C:\Program Files\JK Custom Coding\XPForce for MSFS 2020 and 2024\win_x64\SimConnect.dll",
    os.path.expandvars(r"%APPDATA%\MSFS_AutoFPS\bin\SimConnect.dll"),
    os.path.expandvars(r"%APPDATA%\DynamicLOD\bin\SimConnect.dll"),
    os.path.expandvars(r"%LOCALAPPDATA%\MapEnhancement\current\SimConnect.dll"),
]

FLOAT64 = 4
PERIOD_ONCE, PERIOD_SIM_FRAME = 1, 3
RECV_EXCEPTION, RECV_OPEN, RECV_QUIT, RECV_SIMOBJECT_DATA = 1, 2, 3, 8
UNUSED = 0xFFFFFFFF

# (simvar, units) - one data definition, read every sim frame
FLIGHT_VARS = [
    ("AIRSPEED INDICATED", "knots"),
    ("SIM ON GROUND", "bool"),
    ("GROUND VELOCITY", "knots"),
    ("ELEVATOR TRIM PCT", "percent over 100"),
    ("AILERON TRIM PCT", "percent over 100"),
    ("G FORCE", "gforce"),
    ("ACCELERATION BODY X", "feet per second squared"),
    ("ACCELERATION BODY Y", "feet per second squared"),
    ("ROTATION ACCELERATION BODY X", "radians per second squared"),
    ("ROTATION ACCELERATION BODY Z", "radians per second squared"),
    ("INCIDENCE ALPHA", "radians"),
    ("STALL ALPHA", "radians"),
    ("STALL WARNING", "bool"),
    ("FLAPS HANDLE PERCENT", "percent over 100"),
    ("GENERAL ENG RPM:1", "rpm"),
    ("SIM DISABLED", "bool"),
    ("AIRCRAFT WIND X", "knots"),
    ("AIRCRAFT WIND Y", "knots"),
    ("AMBIENT WIND X", "knots"),          # world frame: east
    ("AMBIENT WIND Y", "knots"),          # world frame: up
    ("AMBIENT WIND Z", "knots"),          # world frame: north
    ("PLANE HEADING DEGREES TRUE", "radians"),
    ("AUTOPILOT MASTER", "bool"),
]
STRING256 = 9
TITLE_REQUEST = 20

# Aircraft profiles. stall/cruise: knots IAS (stiffness grows with (IAS/cruise)^2);
# stiffness: cruise stiffness setting (x0.75, capped at 0.6 - this stick buzzes above);
# air_min: minimum stiffness while airborne (a too-light stick at low speed is twitchy in pitch);
# trim: how far trim moves the resting point (airliner stab trim relieves force, it does not
# move the column much); gusts/buffet: scale; shaker: stick shaker at the stall warning.
PROFILES = {
    "c172": dict(name="small single-engine (C152/C172 type)", stall=48, cruise=120, stiffness=0.8,
                 air_min=0.22, trim=1.0, gusts=1.0, buffet=1.0, shaker=0.0),
    "c208": dict(name="Cessna 208 Caravan", stall=61, cruise=160, stiffness=0.8,
                 air_min=0.30, trim=1.0, gusts=1.0, buffet=1.0, shaker=0.0),
    "b738": dict(name="Boeing 737 (PMDG 737-800 etc.)", stall=115, cruise=250, stiffness=0.8,
                 air_min=0.42, trim=0.25, gusts=2.0, buffet=0.5, shaker=1.0),
}


def profile_for_title(title):
    t = title.lower()
    if "737" in t or "b738" in t or "b737" in t:
        return "b738"
    if "208" in t or "caravan" in t:
        return "c208"
    if "172" in t or "152" in t or "skyhawk" in t or "cessna" in t:
        return "c172"
    return None
# MSFS 2020 rejects the DESIGN SPEED variables, so stall/cruise speeds are settings (--stall-speed/--cruise-speed)
DESIGN_VARS = []


class SimConnect:
    """Minimal SimConnect client (ctypes)."""

    def __init__(self, dll_path=None):
        path = dll_path or next((p for p in SIMCONNECT_PATHS if os.path.exists(p)), None)
        if not path:
            raise RuntimeError("SimConnect.dll not found (pass --simconnect PATH)")
        self.path = path
        self.dll = ctypes.WinDLL(path)
        d = self.dll
        d.SimConnect_Open.argtypes = [ctypes.POINTER(wt.HANDLE), ctypes.c_char_p, wt.HWND, wt.DWORD,
                                      wt.HANDLE, wt.DWORD]
        d.SimConnect_Open.restype = ctypes.c_long
        d.SimConnect_Close.argtypes = [wt.HANDLE]
        d.SimConnect_AddToDataDefinition.argtypes = [wt.HANDLE, wt.DWORD, ctypes.c_char_p, ctypes.c_char_p,
                                                     ctypes.c_int, ctypes.c_float, wt.DWORD]
        d.SimConnect_AddToDataDefinition.restype = ctypes.c_long
        d.SimConnect_RequestDataOnSimObject.argtypes = [wt.HANDLE, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_int,
                                                        wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD]
        d.SimConnect_RequestDataOnSimObject.restype = ctypes.c_long
        d.SimConnect_GetNextDispatch.argtypes = [wt.HANDLE, ctypes.POINTER(ctypes.c_void_p),
                                                 ctypes.POINTER(wt.DWORD)]
        d.SimConnect_GetNextDispatch.restype = ctypes.c_long
        self.h = None
        self.string_requests = set()

    def open(self, name=b"WingMan Force FFB"):
        h = wt.HANDLE()
        if self.dll.SimConnect_Open(ctypes.byref(h), name, None, 0, None, 0) != 0:
            return False
        self.h = h
        return True

    def close(self):
        if self.h:
            self.dll.SimConnect_Close(self.h)
            self.h = None

    def define(self, define_id, variables, datatype=FLOAT64):
        for name, units in variables:
            self.dll.SimConnect_AddToDataDefinition(self.h, define_id, name.encode(),
                                                    units.encode() if units else None,
                                                    datatype, 0.0, UNUSED)
        if datatype == STRING256:
            self.string_requests.add(define_id)

    def request(self, request_id, define_id, period):
        self.dll.SimConnect_RequestDataOnSimObject(self.h, request_id, define_id, 0, period, 0, 0, 0, 0)

    def dispatch(self):
        """Yield (kind, request_id, values) for pending messages."""
        p = ctypes.c_void_p()
        n = wt.DWORD()
        while self.dll.SimConnect_GetNextDispatch(self.h, ctypes.byref(p), ctypes.byref(n)) == 0:
            size, version, recv_id = ctypes.cast(p, ctypes.POINTER(wt.DWORD * 3)).contents
            if recv_id == RECV_SIMOBJECT_DATA:
                hdr = ctypes.cast(p, ctypes.POINTER(wt.DWORD * 10)).contents
                request_id, define_count = hdr[3], hdr[9]
                if request_id in self.string_requests:
                    raw = ctypes.string_at(p.value + 40, min(256, max(0, size - 40)))
                    yield "string", request_id, raw.split(b"\0", 1)[0].decode("utf-8", "replace")
                    continue
                count = (size - 40) // 8
                vals = list((ctypes.c_double * count).from_address(p.value + 40))
                yield "data", request_id, vals
            elif recv_id == RECV_QUIT:
                yield "quit", 0, None
            elif recv_id == RECV_EXCEPTION:
                exc = ctypes.cast(p, ctypes.POINTER(wt.DWORD * 6)).contents
                yield "exception", exc[3], [exc[3], exc[5]]


class HighPass:
    def __init__(self, tau):
        self.tau, self.y, self.x = tau, 0.0, None

    def __call__(self, x, dt):
        if self.x is None or dt <= 0:
            self.x = x
            return 0.0
        a = self.tau / (self.tau + dt)
        self.y = a * (self.y + x - self.x)
        self.x = x
        return self.y


class LowPass:
    def __init__(self, tau, y=0.0):
        self.tau, self.y = tau, y

    def __call__(self, x, dt):
        a = 1.0 - math.exp(-dt / self.tau) if self.tau > 0 else 1.0
        self.y += a * (x - self.y)
        return self.y


class ForceModel:
    """Flight data -> (stiffness x, stiffness y, centre x, centre y) for one spring."""

    def __init__(self, stiffness=None, trim=1.0, gusts=1.0, buffet=1.0, rumble=1.0, weight=1.0,
                 stall_speed=None, cruise_speed=None, profile="c208"):
        # user settings: scale factors, and optional overrides of the profile's speeds/stiffness
        self.user = dict(stiffness=stiffness, trim=trim, gusts=gusts, buffet=buffet,
                         stall=stall_speed, cruise=cruise_speed)
        self.k_cap = 0.6             # this stick buzzes above ~0.6
        self.k_min = 0.08            # stiffness when stopped
        self.hp_trim_x = HighPass(0.4)
        self.hp_trim_y = HighPass(0.4)
        self.rumble_gain = rumble
        self.weight_gain = weight
        self.apply_profile(profile)
        self.hp_wy = HighPass(0.6)     # vertical wind relative to the aircraft
        self.hp_we = HighPass(0.6)     # east wind (world)
        self.hp_wn = HighPass(0.6)     # north wind (world)
        self.lp_roll = LowPass(0.08)
        self.lp_pitch = LowPass(0.08)
        self.lp_k = LowPass(0.3, self.k_min)
        self.phase = 0.0
        self.rumble_x = LowPass(0.04)
        self.rumble_y = LowPass(0.04)
        self.last = {}

    def apply_profile(self, key):
        p = PROFILES[key]
        u = self.user
        self.profile = key
        self.v_stall = u["stall"] or p["stall"]
        self.v_cruise = u["cruise"] or p["cruise"]
        self.k_max = min(self.k_cap, 0.75 * (u["stiffness"] if u["stiffness"] is not None else p["stiffness"]))
        self.air_min = min(self.k_max, p["air_min"])
        self.trim_gain = 2.4 * u["trim"] * p["trim"]        # C208: 10% trim -> 24% of travel
        self.trim_pulse_gain = 1.5 * u["trim"] * max(p["trim"], 0.5)
        self.gust_gain = u["gusts"] * p["gusts"]
        self.buffet_gain = u["buffet"] * p["buffet"]
        self.shaker = p["shaker"]
        return p

    def set_design_speed(self, name, v):
        if name in ("DESIGN SPEED VS0", "DESIGN SPEED VS1") and 20 < v < 300:
            self.v_stall = v if name == "DESIGN SPEED VS1" or self.v_stall == 50.0 else min(self.v_stall, v)
        elif name == "DESIGN SPEED VC" and 40 < v < 700 and v > self.v_stall * 1.3:
            self.v_cruise = v

    def update(self, f, dt):
        """f: dict of FLIGHT_VARS values. Returns (kx, ky, cx, cy, info)."""
        ias = max(0.0, f["AIRSPEED INDICATED"])
        on_ground = f["SIM ON GROUND"] > 0.5
        gs = max(0.0, f["GROUND VELOCITY"])

        # --- stiffness: aerodynamic loads grow with dynamic pressure (~ IAS squared)
        q = min(1.2, (ias / self.v_cruise) ** 2)
        g = f["G FORCE"]
        k = self.k_min + (self.k_max - self.k_min) * min(1.0, q)
        k += 0.15 * max(0.0, min(2.0, g - 1.0))                  # heavier when pulling G
        if not on_ground:
            k = max(k, self.air_min)          # never too light in the air (pitch got twitchy <100 kt)
        autopilot = f["AUTOPILOT MASTER"] > 0.5
        if autopilot:
            k = self.k_cap                    # hold the stick firmly so gusts don't make inputs
        k = self.lp_k(min(self.k_cap, k), dt)

        # --- resting point: trim (nose-up trim -> stick rests further back)
        cx = max(-0.9, min(0.9, f["AILERON TRIM PCT"] * self.trim_gain))
        cy = max(-0.9, min(0.9, f["ELEVATOR TRIM PCT"] * self.trim_gain))
        # a short push/pull while trim is being changed, so each trim click is felt
        pulse_x = self.hp_trim_x(f["AILERON TRIM PCT"], dt) * self.trim_pulse_gain
        pulse_y = self.hp_trim_y(f["ELEVATOR TRIM PCT"], dt) * self.trim_pulse_gain
        # elevator weight: slow on the ground, the stick leans forward
        if on_ground:
            cy -= 0.35 * self.weight_gain * max(0.0, 1.0 - ias / max(1.0, self.v_stall))

        # --- gusts: sudden changes of the wind itself, in world coordinates. Not the aircraft's
        # accelerations, and not the wind relative to the aircraft - both change with the
        # pilot's own inputs and fed back into the stick (flight tests msfs1/msfs2).
        h = f["PLANE HEADING DEGREES TRUE"]
        # Steady wind is filtered out in WORLD coordinates first and only then turned into the
        # aircraft's side axis - otherwise turning in a steady wind looked like a side gust that
        # pushed the stick into the turn (recording msfs4: correlation with stick up to 0.97).
        side = f["AMBIENT WIND X"] * math.cos(h) - f["AMBIENT WIND Z"] * math.sin(h)   # for the record
        ge = self.hp_we(f["AMBIENT WIND X"], dt)
        gn = self.hp_wn(f["AMBIENT WIND Z"], dt)
        wx = ge * math.cos(h) - gn * math.sin(h)                    # knots, side gust
        wy = self.hp_wy(f["AMBIENT WIND Y"], dt)                    # knots, updraft / downdraft
        # Sign chosen so the part of the vertical wind that follows the aircraft's climb/descent
        # (and therefore the pilot's pitch input) damps stick motion instead of reinforcing it.
        gust_y = self.lp_pitch(0.10 * wy, dt)
        gust_x = self.lp_roll(0.14 * wx, dt)                       # side gust -> stick nudged sideways
        # a gust changes the wing's angle of attack by ~ gust / airspeed, so the same gust is
        # felt more when flying slowly (x1 at cruise speed, up to x2.5 near the stall)
        gust_speed = 0.5 if on_ground else max(1.0, min(2.5, self.v_cruise / max(ias, self.v_stall)))
        gust_force_x = max(-0.4, min(0.4, gust_x * self.gust_gain * gust_speed))
        gust_force_y = max(-0.4, min(0.4, gust_y * self.gust_gain * min(gust_speed, 1.5)))

        # --- stall buffet: shake grows from 85% of stall angle (or stall warning)
        alpha, stall_a = f["INCIDENCE ALPHA"], f["STALL ALPHA"]
        near = (alpha / stall_a) if stall_a > 0.05 else 0.0
        buffet = max(0.0, min(1.0, (near - 0.85) / 0.15))
        if f["STALL WARNING"] > 0.5:
            buffet = max(buffet, 0.6)
        # light buffeting when slow: starts at 1.4x stall speed, grows towards the stall
        slow = max(0.0, min(1.0, (1.4 * self.v_stall - ias) / (0.4 * self.v_stall)))
        buffet = max(buffet, 0.7 * slow)
        if on_ground:
            buffet = 0.0
        self.phase += dt * 2 * math.pi * 9.0                         # ~9 Hz shake
        buffet_y = 0.15 * self.buffet_gain * buffet * math.sin(self.phase)
        buffet_x = 0.08 * self.buffet_gain * buffet * math.sin(self.phase * 1.37)
        shaking = self.shaker > 0 and f["STALL WARNING"] > 0.5 and not on_ground
        if shaking:                                   # stick shaker: hard, fast, mostly fore-aft
            self.shaker_phase = getattr(self, "shaker_phase", 0.0) + dt * 2 * math.pi * 15.0
            buffet_y += 0.35 * self.shaker * math.sin(self.shaker_phase)
            buffet_x += 0.10 * self.shaker * math.sin(self.shaker_phase * 1.1)

        # --- runway rumble: random bumps growing with ground speed
        rumble = 0.0
        if on_ground and gs > 2:
            rumble = min(1.0, 0.3 + gs / 50.0) * 0.3 * self.rumble_gain
        rx = self.rumble_x(random.uniform(-1, 1) * rumble, dt)
        ry = self.rumble_y(random.uniform(-1, 1) * rumble, dt)

        # forces become centre offsets: spring force = k * (centre - stick); the moving part
        # is limited so it can never throw the stick across its travel
        # When the spring is too soft to deliver a bump within that limit, the spring is
        # made stiffer for that axis just enough (up to the cap) - otherwise gusts would be
        # felt LESS at low speed, where they should be felt more.
        lim = 0.6
        kx = ky = max(k, 0.05)
        damp = 0.15 if autopilot else 1.0
        fx_dyn = (gust_force_x + rx + pulse_x) * damp + buffet_x
        fy_dyn = (gust_force_y + ry + pulse_y) * damp + buffet_y
        kx = min(self.k_cap, max(kx, abs(fx_dyn) / lim))
        ky = min(self.k_cap, max(ky, abs(fy_dyn) / lim))
        cx += max(-lim, min(lim, fx_dyn / kx))
        cy += max(-lim, min(lim, fy_dyn / ky))
        cx = max(-1.0, min(1.0, cx))
        cy = max(-1.0, min(1.0, cy))
        self.last = dict(ias=round(ias, 1), q=round(q, 3), k=round(k, 3), g=round(g, 2),
                         gust_speed=round(gust_speed, 2), slow_buffet=round(slow, 2),
                         trim_x=round(f["AILERON TRIM PCT"], 3), trim_y=round(f["ELEVATOR TRIM PCT"], 3),
                         gust_x=round(gust_force_x, 3), gust_y=round(gust_force_y, 3),
                         buffet=round(buffet, 2), rumble=round(rumble, 3), ground=int(on_ground),
                         alpha=round(alpha, 3), stall_alpha=round(stall_a, 3), stall_warn=int(f["STALL WARNING"] > 0.5),
                         wind_x=round(f["AIRCRAFT WIND X"], 2), wind_y=round(f["AIRCRAFT WIND Y"], 2),
                         amb_side=round(side, 2), amb_up=round(f["AMBIENT WIND Y"], 2),
                         acc_y=round(f["ACCELERATION BODY Y"] / 32.17, 3), roll_acc=round(f["ROTATION ACCELERATION BODY Z"], 3),
                         cx=round(cx, 3), cy=round(cy, 3), autopilot=int(autopilot),
                         shaker=int(shaking))
        self.last.update(kx=round(kx, 3), ky=round(ky, 3))
        return kx, ky, cx, cy, self.last


class MsfsForceFeedback(threading.Thread):
    """Connects to MSFS, runs the force model and drives one spring on the stick."""

    RATE = 60.0

    def __init__(self, iforce, model, log=print, dll_path=None, on_active=None, on_idle=None,
                 recorder=None, auto_profile=True):
        super().__init__(daemon=True)
        self.auto_profile = auto_profile
        self.ff, self.model, self.log = iforce, model, log
        self.dll_path = dll_path
        self.on_active = on_active or (lambda: None)
        self.on_idle = on_idle or (lambda: None)
        self.recorder = recorder
        self.stop_flag = False
        self.flight = None
        self.flight_time = 0.0
        self.spring = None

    def run(self):
        try:
            sc = SimConnect(self.dll_path)
        except Exception as e:
            self.log(f"MSFS force feedback disabled: {e}")
            return
        self.log(f"MSFS force feedback: using {sc.path}")
        waiting_logged = False
        while not self.stop_flag:
            if not sc.open():
                if not waiting_logged:
                    self.log("MSFS force feedback: waiting for Flight Simulator...")
                    waiting_logged = True
                time.sleep(3)
                continue
            waiting_logged = False
            self.log("MSFS force feedback: connected to Flight Simulator.")
            try:
                self._session(sc)
            except Exception as e:
                self.log(f"MSFS force feedback error: {e!r}")
            finally:
                sc.close()
                self._release()
                self.log("MSFS force feedback: disconnected.")
            time.sleep(2)

    def _session(self, sc):
        sc.define(1, FLIGHT_VARS)
        sc.request(1, 1, PERIOD_SIM_FRAME)
        sc.define(TITLE_REQUEST, [("TITLE", None)], STRING256)
        sc.request(TITLE_REQUEST, TITLE_REQUEST, 4)        # every second; profile follows the aircraft
        self.title = None
        for i, var in enumerate(DESIGN_VARS):
            sc.define(10 + i, [var])
            sc.request(10 + i, 10 + i, PERIOD_ONCE)
        names = [v[0] for v in FLIGHT_VARS]
        last = time.time()
        while not self.stop_flag:
            for kind, req, vals in sc.dispatch():
                if kind == "quit":
                    return
                if kind == "string" and req == TITLE_REQUEST and vals != self.title:
                    self.title = vals
                    key = profile_for_title(vals) if self.auto_profile else None
                    if key and key != self.model.profile:
                        self.model.apply_profile(key)
                    p = PROFILES[self.model.profile]
                    self.log(f"MSFS force feedback: aircraft '{vals}' -> profile {self.model.profile} "
                             f"({p['name']}: stall {self.model.v_stall:.0f} kt, cruise {self.model.v_cruise:.0f} kt)")
                if kind == "exception":
                    self.log(f"MSFS force feedback: SimConnect exception {vals}")
                elif kind == "data" and 10 <= req < 10 + len(DESIGN_VARS) and vals:
                    self.model.set_design_speed(DESIGN_VARS[req - 10][0], vals[0])
                    self.log(f"MSFS force feedback: {DESIGN_VARS[req - 10][0]} = {vals[0]:.0f} kt "
                             f"(stall {self.model.v_stall:.0f}, cruise {self.model.v_cruise:.0f})")
                elif kind == "data" and req == 1 and len(vals) >= len(names):
                    self.flight = dict(zip(names, vals))
                    self.flight_time = time.time()
            now = time.time()
            dt = now - last
            if dt < 1.0 / self.RATE:
                time.sleep(1.0 / self.RATE - dt)
                continue
            last = now
            stale = self.flight is None or now - self.flight_time > 1.0 or self.flight["SIM DISABLED"] > 0.5
            if stale:
                self._release()
                continue
            kx, ky, cx, cy, info = self.model.update(self.flight, dt)
            self._apply(kx, ky, cx, cy)
            if self.recorder:
                sx, sy = self.ff.stick
                self.recorder(dict(info, stick_x=sx, stick_y=sy,
                                   force_x=round(kx * (cx - sx), 3), force_y=round(ky * (cy - sy), 3)))

    def _apply(self, kx, ky, cx, cy):
        from iforce_ffb import T_SPRING
        ff = self.ff
        with ff.lock:
            if self.spring is not None and ff.effects.get(0) is not self.spring:
                self.spring = None           # stick was re-initialised (unplugged / reconnected)
            if self.spring is None:
                self.on_active()
                self.spring = ff.get(0, T_SPRING)
                if self.spring is None:
                    return
                self.spring.gain = 1.0
            e = self.spring
            for c, k, ctr in ((e.cond[0], kx, cx), (e.cond[1], ky, cy)):
                c.update(center=ctr, pos=k, neg=k, psat=1.0, nsat=1.0, db=0.0)
            ff.upload_params(e)
            if not e.core_sent:
                ff.upload_core(e)
            if not e.playing:
                ff.play(e)

    def _release(self):
        if self.spring is not None:
            with self.ff.lock:
                self.ff.free(0)
            self.spring = None
            self.on_idle()
