"""I-Force force-feedback engine for the WingMan Force, plus a bridge from vJoy FFB.

I-Force protocol (USB): each OUT packet is [command id, data...].
  0x01 effect core (14 bytes), 0x02 envelope, 0x03 magnitude, 0x04 period,
  0x05 condition, 0x40 autocentre, 0x41 play/stop, 0x42 enable, 0x43 gain.
Effects live in a small device memory (200 bytes on this stick): each effect
references up to two "modifier" blocks (magnitude/period/condition + envelope).
Protocol facts follow the public Linux iforce driver; this is an independent
implementation.
"""
import ctypes
import math
import threading
import time

# command ids
CMD_EFFECT, CMD_ENVELOPE, CMD_MAGNITUDE, CMD_PERIOD, CMD_CONDITION = 0x01, 0x02, 0x03, 0x04, 0x05
CMD_AUTOCENTER, CMD_PLAY, CMD_ENABLE, CMD_GAIN = 0x40, 0x41, 0x42, 0x43

# effect type codes
T_CONSTANT = 0x00
T_SQUARE, T_TRIANGLE, T_SINE, T_SAW_UP, T_SAW_DOWN = 0x20, 0x21, 0x22, 0x23, 0x24
T_SPRING, T_DAMPER = 0x40, 0x41

MOD1_SIZE = {T_CONSTANT: 2, T_SQUARE: 0x0C, T_TRIANGLE: 0x0C, T_SINE: 0x0C, T_SAW_UP: 0x0C,
             T_SAW_DOWN: 0x0C, T_SPRING: 8, T_DAMPER: 8}


def _s8(v):
    """Clamp to a signed byte and return it as 0-255."""
    return max(-128, min(127, int(v))) & 0xFF


def _u16(v):
    v = max(0, min(0xFFFF, int(v)))
    return [v & 0xFF, v >> 8]


def _s16(v):
    v = max(-0x8000, min(0x7FFF, int(v))) & 0xFFFF
    return [v & 0xFF, v >> 8]


class MemoryPool:
    """First-fit allocator for the stick's effect memory (2-byte aligned)."""

    def __init__(self, size):
        self.size = size
        self.used = {}  # start -> length

    def alloc(self, n):
        n = (n + 1) & ~1
        pos = 0
        for start in sorted(self.used):
            if start - pos >= n:
                break
            pos = start + self.used[start]
        if pos + n > self.size:
            return None
        self.used[pos] = n
        return pos

    def free(self, start):
        self.used.pop(start, None)


class Effect:
    def __init__(self, slot):
        self.slot = slot
        self.type = None
        self.mod1 = None
        self.mod2 = None
        self.playing = False
        self.core_sent = False
        self.yslot = None          # constant force: second channel for the forward/back part
        self.ymod = None
        self.core_dir = None       # direction last written to a periodic effect's core
        # parameters (normalised: magnitudes -1..1, times in ms)
        self.duration = 0          # 0 = infinite
        self.direction = 0         # 0-255 = 0-360 deg, I-Force convention
        self.gain = 1.0
        self.magnitude = 0.0
        self.offset = 0.0
        self.phase = 0             # 0-35999 (hundredths of a degree)
        self.period = 100
        self.env = (0, 0.0, 0, 0.0)  # attack ms, attack level, fade ms, fade level
        self.cond = [dict(center=0.0, pos=0.0, neg=0.0, psat=1.0, nsat=1.0, db=0.0),
                     dict(center=0.0, pos=0.0, neg=0.0, psat=1.0, nsat=1.0, db=0.0)]


class IForce:
    """Effect manager talking to the stick through a write(bytes) function."""

    def __init__(self, write, memory=200, max_effects=10, log=None):
        self._write = write
        self.pool = MemoryPool(memory)
        self.max_effects = max_effects
        self.effects = {}
        self.lock = threading.RLock()
        self.log = log or (lambda s: None)
        self.strength = 1.0       # user master strength 0-1
        self.direction_offset = 0  # 0-255, fix-up if forces come out rotated
        self.invert_spring_y = False  # flip forward/back of game spring centres
        self.invert_push_y = False    # flip forward/back of game pushes and vibrations
        self.vibration_scale = 1.0    # scales periodic (vibration) effects from games
        self.push_scale = 1.0         # scales constant (push) forces from games (pitch, and roll
        self.push_roll_scale = None   #   unless push_roll_scale is set; may be > 1 to boost gusts)
        self.push_roll_smooth = 0.0   # seconds; low-pass on the roll part of pushes
        self.push_smooth = 0.3        # seconds; low-pass on pushes merged into the spring
        self._push_f = (0.0, 0.0, None)
        self.recorder = None          # callable(dict) for --record
        self.stick = (0, 0)           # latest stick position, set by the main loop
        self.spring_scale = 1.0   # scales spring (centring) effects from games
        self.damper_scale = 0.0   # scales damper/friction/inertia effects from games
        self.spring_deadzone = 0.0  # minimum dead zone for game springs (0-1)
        self._sent = {}           # last bytes per parameter block / effect core, to skip repeats
        # A constant force is played as two fixed-direction channels (left/right and
        # forward/back) whose strengths change smoothly. Rewriting an effect's direction
        # requires restarting it, which this stick feels as a jolt - XPForce changes the
        # direction of its constant force slightly on every update, which felt like a
        # constantly rough engine. The last 3 channels are reserved for this.
        self.split_reserved = 3
        self.user_slots = max_effects - self.split_reserved
        self._split_free = list(range(self.user_slots, max_effects))
        # Channels actually running on the stick. An effect the app has started but whose
        # force is currently zero is kept stopped: every running effect - even at zero -
        # slows the stick's force loop, and with a spring running that made it buzz at centre.
        self._on = set()
        self.idle_threshold = 2     # force below this (of 127 / 100) counts as zero
        # While a spring is playing, constant forces are not played as separate effects (a
        # second running effect makes the spring chatter): the spring centre is moved instead.
        # Spring force ~ stiffness * (centre - position), so a force F == centre shift F/stiffness.
        self.merge_constant = True

    def send(self, *data):
        with self.lock:
            self._write(bytes(data))

    def send_once(self, key, *data):
        """Send only if different from what this memory block / channel already holds.
        Apps such as XPForce re-send unchanged effects 20-30 times a second; repeating them
        makes this stick twitch."""
        with self.lock:
            b = bytes(data)
            if self._sent.get(key) == b:
                return False
            self._write(b)
            self._sent[key] = b
            return True

    # -- device-wide ------------------------------------------------------
    def init(self):
        """Start-up sequence as done by Logitech's WmUsbHid.sys."""
        with self.lock:
            self.send(CMD_AUTOCENTER, 0x05, 0x00, 0x04)   # axis saturation threshold 1024
            for ch in range(self.max_effects):            # clear all effect channels
                self.send(CMD_EFFECT, ch, 0, 0, 0, 0, 0, 0, 0, 0xFF, 0xFF, 0xFF, 0xFF, 0, 0)
            self.effects.clear()
            self._sent.clear()
            self._split_free = list(range(self.user_slots, self.max_effects))
            self._on = set()
            self.pool = MemoryPool(self.pool.size)
            self.send(CMD_ENABLE, 0x05)                   # enable FF + stop all
            self.set_gain(1.0)

    def enable(self, on=True):
        self.send(CMD_ENABLE, 0x04 if on else 0x01)

    def set_gain(self, g):
        self.send(CMD_GAIN, max(0, min(0x80, int(g * self.strength * 0x80))))  # 0x80 = 100%

    def autocenter(self, strength):
        """Built-in centring spring, strength 0-1 (device scale 0-100)."""
        self.send(CMD_AUTOCENTER, 0x03, max(0, min(100, int(strength * 100))))
        self.send(CMD_AUTOCENTER, 0x04, 0x01)  # always 01 (00 appears to disable force output)

    def stop_all(self):
        with self.lock:
            for e in self.effects.values():
                self.stop(e)

    def reset(self):
        with self.lock:
            self.stop_all()
            for slot in list(self.effects):
                self.free(slot)

    # -- effect lifetime ----------------------------------------------------
    def get(self, slot, etype=None):
        with self.lock:
            if slot >= self.max_effects:
                return None
            e = self.effects.get(slot)
            if e is None:
                e = self.effects[slot] = Effect(slot)
            if etype is not None and etype != e.type:
                self._release_mem(e)
                e.type = etype
            if e.type is not None and e.mod1 is None:
                e.mod1 = self.pool.alloc(MOD1_SIZE[e.type])
                e.mod2 = self.pool.alloc(8 if e.type in (T_SPRING, T_DAMPER) else 0x0E)
                if e.type == T_CONSTANT and self._split_free:
                    e.ymod = self.pool.alloc(2)
                    if e.ymod is not None:
                        e.yslot = self._split_free.pop(0)
                if e.mod1 is None or e.mod2 is None:
                    self.log(f"effect memory full, dropping effect {slot}")
                    self._release_mem(e)
                    return None
            return e

    def _release_mem(self, e):
        for m in (e.mod1, e.mod2, e.ymod):
            if m is not None:
                self.pool.free(m)
        e.mod1 = e.mod2 = e.ymod = None
        if e.yslot is not None:
            self._split_free.append(e.yslot)
            e.yslot = None
        e.core_sent = False
        e.core_dir = None

    def free(self, slot):
        with self.lock:
            e = self.effects.pop(slot, None)
            if e:
                if e.playing:
                    self.stop(e)
                self._release_mem(e)

    def _active_spring(self):
        for e in self.effects.values():
            if e.type == T_SPRING and e.playing and e.core_sent:
                return e
        return None

    def _const_force(self):
        """Playing constant forces in device axes (x right+, y back+), low-pass filtered.
        A flight recording showed XPForce's pitch push follows the stick within ~0.1 s; at full
        strength that loop made the stick wobble. Smoothing keeps sustained loads (trim
        pressure, weight, wind) and damps the fast back-and-forth."""
        fx, fy = self._raw_const_force()
        px, py, t0 = self._push_f
        now = time.time()
        if t0 is None or self.push_smooth <= 0:
            self._push_f = (fx, fy, now)
            return fx, fy
        dt = max(0.0, now - t0)
        ay = 1.0 - math.exp(-dt / self.push_smooth)
        ax = 1.0 - math.exp(-dt / self.push_roll_smooth) if self.push_roll_smooth > 0 else 1.0
        px, py = px + ax * (fx - px), py + ay * (fy - py)
        self._push_f = (px, py, now)
        return px, py

    def _raw_const_force(self):
        fx = fy = 0.0
        for e in self.effects.values():
            if e.type == T_CONSTANT and e.playing:
                a = math.radians(self._direction(e) * 360 / 256)
                m = e.magnitude * e.gain
                rx = self.push_scale if self.push_roll_scale is None else self.push_roll_scale
                fx += -m * math.sin(a) * rx      # direction 64 pushes left
                fy += m * math.cos(a) * self.push_scale   # direction 0 pushes back
        return max(-1.0, min(1.0, fx)), max(-1.0, min(1.0, fy))

    def _merging(self):
        return self.merge_constant and self._active_spring() is not None

    def _refresh_merge(self):
        """Re-send the spring (merged forces changed) and start/stop constant channels."""
        sp = self._active_spring()
        if sp is not None:
            self.upload_params(sp)
        for e in list(self.effects.values()):
            if e.type == T_CONSTANT and e.core_sent:
                self._sync(e)

    def _direction(self, e):
        d = (e.direction + self.direction_offset) & 0xFF
        if self.invert_push_y:
            d = (0x80 - d) & 0xFF   # mirror forward/back, keep left/right
        return d

    # -- uploads ------------------------------------------------------------
    def upload_params(self, e):
        """Send the type-specific modifier(s) of an effect."""
        g = e.gain
        if e.type == T_CONSTANT:
            g = g * self.push_scale
            if e.yslot is not None:
                # split: channel slot pushes along direction 64 (left), yslot along 0 (back);
                # a negative strength pushes the opposite way.
                a = math.radians(self._direction(e) * 360 / 256)
                m = e.magnitude * g * 127
                self.send_once(("m", e.mod1), CMD_MAGNITUDE, *_u16(e.mod1), _s8(round(m * math.sin(a))))
                self.send_once(("m", e.ymod), CMD_MAGNITUDE, *_u16(e.ymod), _s8(round(m * math.cos(a))))
            else:
                self.send_once(("m", e.mod1), CMD_MAGNITUDE, *_u16(e.mod1), _s8(e.magnitude * g * 127))
            if self._merging():
                sp = self._active_spring()
                self.upload_params(sp)
        elif e.type in (T_SPRING, T_DAMPER):
            # damper/friction resists stick speed; on this stick strong values make the motors
            # chatter on sensor noise, so it has its own scale (default off).
            g = g * (self.damper_scale if e.type == T_DAMPER else self.spring_scale)
            rec = {}
            merge = (e.type == T_SPRING and self.merge_constant and e.playing
                     and self._active_spring() is e)
            force = self._const_force() if merge else (0.0, 0.0)
            for i, addr in enumerate((e.mod1, e.mod2)):
                c = dict(e.cond[i])
                if e.type == T_SPRING:
                    c["db"] = max(c["db"], self.spring_deadzone)
                sign = -1 if (i == 1 and self.invert_spring_y) else 1
                kp, kn = c["pos"] * g, c["neg"] * g
                centre = sign * c["center"]           # device axis
                if merge and abs(force[i]) > 0.005:
                    k = max(0.0, (abs(kp) + abs(kn)) / 2)
                    want = k * centre + force[i]      # spring force wanted with the stick centred
                    if k > 0.01 and abs(want / k) <= 1.0:
                        centre = want / k
                    else:                             # spring too soft: stiffen just enough
                        k = min(max(abs(want), k), self.spring_scale)
                        centre = 1.0 if want > 0 else -1.0
                        kp = kn = k
                if e.type == T_SPRING:
                    ax = "xy"[i]
                    rec[f"app_centre_{ax}"] = round(c["center"], 4)
                    rec[f"app_k_{ax}"] = round(c["pos"], 4)
                    rec[f"push_{ax}"] = round(force[i], 4)
                    rec[f"sent_centre_{ax}"] = round(centre, 4)
                    rec[f"sent_k_{ax}"] = round(kp, 4)
                self.send_once(("m", addr), CMD_CONDITION, *_u16(addr),
                          _s8(kp * 100), _s8(kn * 100),
                          *_s16(centre * 500), *_u16(c["db"] * 1000),
                          max(0, min(100, int(c["psat"] * 100))), max(0, min(100, int(c["nsat"] * 100))))
            if e.playing:
                self._sync(e)
            if rec and self.recorder:
                rec["stick_x"], rec["stick_y"] = self.stick
                self.recorder(rec)
            return
        else:
            g = g * self.vibration_scale
            self.send_once(("m", e.mod1), CMD_PERIOD, *_u16(e.mod1), _s8(e.magnitude * g * 127), _s8(e.offset * g * 127),
                      int(e.phase * 256 / 36000) & 0xFF, *_u16(max(1, e.period)))
        if e.playing:
            self._sync(e)
        if self._has_envelope(e):
            # An envelope level of 0 silences the effect, so a side with no ramp gets full level.
            a_ms, a_lvl, f_ms, f_lvl = e.env
            a = 127 if a_ms == 0 else max(0, min(127, int(a_lvl * 127)))
            f = 127 if f_ms == 0 else max(0, min(127, int(f_lvl * 127)))
            self.send_once(("m", e.mod2), CMD_ENVELOPE, *_u16(e.mod2), *_u16(a_ms), a, *_u16(f_ms), f)

    @staticmethod
    def _has_envelope(e):
        return e.env[0] > 0 or e.env[2] > 0

    def upload_core(self, e):
        cond = e.type in (T_SPRING, T_DAMPER)
        split = e.type == T_CONSTANT and e.yslot is not None
        axes = 0xC0 if cond else 0x20
        if cond:
            direction = 0x60
        elif split:
            direction = 0x40                  # fixed: strengths carry the direction
            self.upload_params(e)
        else:
            direction = self._direction(e)
            # periodic: ignore direction jitter of a couple of units (avoids restarts)
            if e.core_dir is not None and min((direction - e.core_dir) & 0xFF,
                                              (e.core_dir - direction) & 0xFF) <= 2:
                direction = e.core_dir
            e.core_dir = direction
        mod2 = e.mod2 if (cond or self._has_envelope(e)) else 0xFFFF
        duration = 0xFFFF if e.duration == 0 else e.duration   # 0 would mean "zero length"
        changed = self.send_once(("c", e.slot), CMD_EFFECT, e.slot, e.type, axes, *_u16(duration),
                                 direction, 0, 0, *_u16(e.mod1), *_u16(mod2), 0, 0)
        if split:
            changed |= self.send_once(("c", e.yslot), CMD_EFFECT, e.yslot, e.type, axes, *_u16(duration),
                                      0x00, 0, 0, *_u16(e.ymod), *_u16(mod2), 0, 0)
        e.core_sent = True
        if changed:   # a rewritten core must be restarted
            for slot in (e.slot, e.yslot):
                if slot is not None and slot in self._on:
                    self.send(CMD_PLAY, slot, 0x01, 0x01)

    def _channels(self, e):
        """[(device channel, has force now)] for an effect."""
        g = e.gain
        t = self.idle_threshold
        if e.type == T_CONSTANT:
            m = e.magnitude * g * self.push_scale * 127
            if self._merging():
                return [(s, False) for s in (e.slot, e.yslot) if s is not None]
            if e.yslot is not None:
                a = math.radians(self._direction(e) * 360 / 256)
                return [(e.slot, abs(m * math.sin(a)) >= t), (e.yslot, abs(m * math.cos(a)) >= t)]
            return [(e.slot, abs(m) >= t)]
        if e.type in (T_SPRING, T_DAMPER):
            s = g * (self.damper_scale if e.type == T_DAMPER else self.spring_scale) * 100
            return [(e.slot, any(abs(c["pos"] * s) >= 1 or abs(c["neg"] * s) >= 1 for c in e.cond))]
        g = g * self.vibration_scale
        return [(e.slot, abs(e.magnitude * g * 127) >= t or abs(e.offset * g * 127) >= t)]

    def _sync(self, e):
        """Start/stop device channels so only effects with actual force are running."""
        if not e.core_sent:
            return
        for slot, has_force in self._channels(e):
            on = e.playing and has_force
            if on and slot not in self._on:
                self.send(CMD_PLAY, slot, 0x01, 0x01)
                self._on.add(slot)
            elif not on and slot in self._on:
                self.send(CMD_PLAY, slot, 0, 0)
                self._on.discard(slot)

    def play(self, e, loops=1):
        if not e.core_sent:          # e.g. driver restarted while the app kept its effects
            self.upload_params(e)
            self.upload_core(e)
        if loops > 1:                # a counted repeat is played as asked
            self.send(CMD_PLAY, e.slot, 0x41, max(1, min(255, loops)))
            self._on.add(e.slot)
            e.playing = True
            return
        was = e.playing
        e.playing = True
        self._sync(e)                # already running: nothing is sent (no restart jolt)
        if not was and e.type in (T_SPRING, T_CONSTANT):
            self._refresh_merge()

    def stop(self, e):
        was = e.playing
        e.playing = False
        for slot in (e.slot, e.yslot):
            if slot is not None and slot in self._on:
                self.send(CMD_PLAY, slot, 0, 0)
                self._on.discard(slot)
        if was and e.type in (T_SPRING, T_CONSTANT):
            self._refresh_merge()


# ---------------------------------------------------------------------------
# vJoy FFB bridge
# ---------------------------------------------------------------------------

class FFB_DATA(ctypes.Structure):
    _fields_ = [("size", ctypes.c_ulong), ("cmd", ctypes.c_ulong), ("data", ctypes.c_void_p)]


class FFB_EFF_REPORT(ctypes.Structure):
    _fields_ = [("EffectBlockIndex", ctypes.c_ubyte), ("EffectType", ctypes.c_int),
                ("Duration", ctypes.c_ushort), ("TrigerRpt", ctypes.c_ushort),
                ("SamplePrd", ctypes.c_ushort), ("Gain", ctypes.c_ubyte), ("TrigerBtn", ctypes.c_ubyte),
                ("Polar", ctypes.c_int), ("Direction", ctypes.c_ubyte), ("DirY", ctypes.c_ubyte)]


class FFB_EFF_REPORT_22(ctypes.Structure):
    """vJoy 2.2.x layout (extra fields, 16-bit direction 0-0x7FFF = 0-360 deg)."""
    _fields_ = [("EffectBlockIndex", ctypes.c_ubyte), ("EffectType", ctypes.c_int),
                ("Duration", ctypes.c_ushort), ("TrigerRpt", ctypes.c_ushort),
                ("SamplePrd", ctypes.c_ushort), ("StartDelay", ctypes.c_ushort),
                ("Gain", ctypes.c_ubyte), ("TrigerBtn", ctypes.c_ubyte),
                ("AxesEnabledDirection", ctypes.c_ubyte), ("Polar", ctypes.c_int),
                ("Direction", ctypes.c_ushort), ("DirY", ctypes.c_ushort)]


class FFB_EFF_OP(ctypes.Structure):
    _fields_ = [("EffectBlockIndex", ctypes.c_ubyte), ("EffectOp", ctypes.c_int), ("LoopCount", ctypes.c_ubyte)]


class FFB_EFF_PERIOD(ctypes.Structure):
    _fields_ = [("EffectBlockIndex", ctypes.c_ubyte), ("Magnitude", ctypes.c_ulong),
                ("Offset", ctypes.c_long), ("Phase", ctypes.c_ulong), ("Period", ctypes.c_ulong)]


class FFB_EFF_COND(ctypes.Structure):
    _fields_ = [("EffectBlockIndex", ctypes.c_ubyte), ("isY", ctypes.c_int),
                ("CenterPointOffset", ctypes.c_long), ("PosCoeff", ctypes.c_long),
                ("NegCoeff", ctypes.c_long), ("PosSatur", ctypes.c_ulong),
                ("NegSatur", ctypes.c_ulong), ("DeadBand", ctypes.c_long)]


class FFB_EFF_ENVLP(ctypes.Structure):
    _fields_ = [("EffectBlockIndex", ctypes.c_ubyte), ("AttackLevel", ctypes.c_ulong),
                ("FadeLevel", ctypes.c_ulong), ("AttackTime", ctypes.c_ulong), ("FadeTime", ctypes.c_ulong)]


class FFB_EFF_CONSTANT(ctypes.Structure):
    _fields_ = [("EffectBlockIndex", ctypes.c_ubyte), ("Magnitude", ctypes.c_long)]


class FFB_EFF_RAMP(ctypes.Structure):
    _fields_ = [("EffectBlockIndex", ctypes.c_ubyte), ("Start", ctypes.c_long), ("End", ctypes.c_long)]


# vJoy packet types
PT_EFFREP, PT_ENVREP, PT_CONDREP, PT_PRIDREP, PT_CONSTREP, PT_RAMPREP = 0x01, 0x02, 0x03, 0x04, 0x05, 0x06
PT_EFOPREP, PT_BLKFRREP, PT_CTRLREP, PT_GAINREP = 0x0A, 0x0B, 0x0C, 0x0D
PT_NEWEFREP, PT_BLKLDREP, PT_POOLREP = 0x11, 0x12, 0x13

# vJoy effect type -> I-Force type
VJ_TYPES = {1: T_CONSTANT, 2: T_CONSTANT, 3: T_SQUARE, 4: T_SINE, 5: T_TRIANGLE, 6: T_SAW_UP,
            7: T_SAW_DOWN, 8: T_SPRING, 9: T_DAMPER, 10: T_DAMPER, 11: T_DAMPER}
VJ_NAMES = {1: "constant", 2: "ramp", 3: "square", 4: "sine", 5: "triangle", 6: "saw-up",
            7: "saw-down", 8: "spring", 9: "damper", 10: "inertia", 11: "friction", 12: "custom"}

FfbGenCB = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)


class VJoyFfbBridge:
    """Receives vJoy FFB packets (DirectInput effects from games/XPForce) and drives IForce."""

    def __init__(self, vjoy_dll, vjoy_id, iforce, log=print, verbose=False, on_activity=None, on_idle=None):
        self.v = vjoy_dll
        self.id = vjoy_id
        self.ff = iforce
        self.log = log
        self.verbose = verbose
        self.on_activity = on_activity or (lambda: None)
        self.on_idle = on_idle or (lambda: None)
        self.chan = {}       # (block index, effect class) -> I-Force channel
        self.last = {}       # block index -> class of the last reported effect
        self.last_cond = {}  # block index -> last condition class (spring/damper...)
        dll_ver, drv_ver = ctypes.c_ushort(), ctypes.c_ushort()
        self.v.DriverMatch(ctypes.byref(dll_ver), ctypes.byref(drv_ver))
        self.vjoy_version = drv_ver.value or dll_ver.value
        # vJoy 2.2.x fixed the effect block index bug and changed the effect report layout
        self.v22 = self.vjoy_version >= 0x220
        self.log(f"vJoy driver version {self.vjoy_version:#x} "
                 f"({'2.2.x effect reports' if self.v22 else '2.1.x effect reports - one shared effect id'})")
        self._cb = FfbGenCB(self._callback)  # keep a reference
        self.v.FfbRegisterGenCB(self._cb, None)

    def _get(self, fn, packet, struct):
        s = struct()
        if getattr(self.v, fn)(packet, ctypes.byref(s)) != 0:
            return None
        return s

    def _callback(self, data, _user):
        try:
            self._handle(ctypes.c_void_p(data))
        except Exception as e:  # never let an exception escape into vJoy's thread
            self.log(f"FFB error: {e!r}")

    # vJoy 2.1.9/2.2.1 hands every DirectInput effect the same block index, so a spring and a
    # constant force from one app (e.g. XPForce) arrive with the same id. Effects are therefore
    # keyed by (block index, effect class), and an operation (start/stop) applies to the effect
    # whose parameters were reported last for that block index.
    def _slot_for(self, ebi, cls):
        key = (ebi, cls)
        if key not in self.chan:
            used = set(self.chan.values())
            free = [s for s in range(self.ff.user_slots) if s not in used]
            if not free:
                return None
            self.chan[key] = free[0]
        self.last[ebi] = cls
        return self.chan[key]

    @staticmethod
    def _cls(vj_type):
        if vj_type in (1, 2):
            return "const"
        if 3 <= vj_type <= 7:
            return "periodic"
        return f"cond{vj_type}"

    @staticmethod
    def _s16(v):
        """vJoy delivers some signed 16-bit fields zero-extended (e.g. -10000 as 55536)."""
        v &= 0xFFFFFFFF
        if v >= 0x80000000:
            v -= 0x100000000
        if 0x7FFF < v <= 0xFFFF:
            v -= 0x10000
        return v

    def _handle(self, p):
        dev = ctypes.c_int()
        if self.v.Ffb_h_DeviceID(p, ctypes.byref(dev)) != 0 or dev.value != self.id:
            return
        t = ctypes.c_int()
        if self.v.Ffb_h_Type(p, ctypes.byref(t)) != 0:
            return
        t = t.value
        ebi_c = ctypes.c_int()
        has_ebi = self.v.Ffb_h_EBI(p, ctypes.byref(ebi_c)) == 0
        ebi = ebi_c.value
        ff = self.ff
        if t in (PT_NEWEFREP, PT_EFFREP):
            self.on_activity()   # an app is creating effects

        if t == PT_CTRLREP:
            c = ctypes.c_int()
            self.v.Ffb_h_DevCtrl(p, ctypes.byref(c))
            if self.verbose:
                self.log(f"FFB device control {c.value}")
            if c.value == 1:        # enable actuators
                ff.enable(True)
            elif c.value in (2, 3):  # disable actuators / stop all
                ff.stop_all()
            elif c.value == 4:      # reset
                ff.reset()
                self.chan.clear()
                self.last.clear()
                ff.enable(True)
        elif t == PT_GAINREP:
            g = ctypes.c_ubyte()
            self.v.Ffb_h_DevGain(p, ctypes.byref(g))
            ff.set_gain(g.value / 255)
        elif t == PT_NEWEFREP:
            if self.verbose:
                et = ctypes.c_int()
                self.v.Ffb_h_EffNew(p, ctypes.byref(et))
                self.log(f"FFB new effect: {VJ_NAMES.get(et.value, et.value)}")
        elif t == PT_BLKFRREP and has_ebi:
            for key in [k for k in self.chan if k[0] == ebi]:
                ff.free(self.chan.pop(key))
            self.last.pop(ebi, None)
            if not ff.effects:
                self.on_idle()
        elif t == PT_EFFREP:
            r = self._get("Ffb_h_Eff_Report", p, FFB_EFF_REPORT_22 if self.v22 else FFB_EFF_REPORT)
            if not r:
                return
            itype = VJ_TYPES.get(r.EffectType)
            if itype is None:
                return
            ebi = r.EffectBlockIndex
            slot = self._slot_for(ebi, self._cls(r.EffectType))
            if slot is None:
                return
            if itype in (T_SPRING, T_DAMPER):
                self.last_cond[ebi] = self._cls(r.EffectType)
            e = ff.get(slot, itype)
            if not e:
                return
            e.vj_type = r.EffectType
            e.duration = 0 if r.Duration in (0, 0xFFFF) else r.Duration
            e.gain = r.Gain / 255 if r.Gain else 1.0
            # Measured on the stick: I-Force direction 0 pushes back, 64 left, 128 forward,
            # 192 right - the same convention as DirectInput polar (0 = force from north).
            if r.Polar:
                e.direction = (r.Direction * 256 // 0x8000) & 0xFF if self.v22 else r.Direction & 0xFF
            else:
                import math
                if self.v22:
                    dx, dy = ctypes.c_short(r.Direction).value, ctypes.c_short(r.DirY).value
                else:
                    dx, dy = ctypes.c_byte(r.Direction).value, ctypes.c_byte(r.DirY).value
                ang = math.degrees(math.atan2(dx, -dy)) % 360 if (dx or dy) else 0
                e.direction = int(ang * 256 / 360) & 0xFF
            if self.verbose:
                self.log(f"FFB effect {ebi}/{slot}: {VJ_NAMES.get(r.EffectType)} dur={r.Duration} "
                         f"gain={r.Gain} polar={r.Polar} dir={r.Direction},{r.DirY}")
            ff.upload_params(e)
            ff.upload_core(e)
        elif t == PT_CONSTREP:
            c = self._get("Ffb_h_Eff_Constant", p, FFB_EFF_CONSTANT)
            if c:
                slot = self._slot_for(c.EffectBlockIndex, "const")
                e = ff.get(slot, T_CONSTANT) if slot is not None else None
                if e:
                    e.magnitude = self._s16(c.Magnitude) / 10000
                    if self.verbose:
                        self.log(f"FFB const {c.EffectBlockIndex}/{slot}: magnitude={self._s16(c.Magnitude)}")
                    ff.upload_params(e)
        elif t == PT_RAMPREP:
            r = self._get("Ffb_h_Eff_Ramp", p, FFB_EFF_RAMP)
            if r:
                slot = self._slot_for(r.EffectBlockIndex, "const")
                e = ff.get(slot, T_CONSTANT) if slot is not None else None
                if e:   # I-Force has no ramp: use the average level
                    e.magnitude = (self._s16(r.Start) + self._s16(r.End)) / 20000
                    ff.upload_params(e)
        elif t == PT_PRIDREP:
            r = self._get("Ffb_h_Eff_Period", p, FFB_EFF_PERIOD)
            if r:
                slot = self._slot_for(r.EffectBlockIndex, "periodic")
                e = ff.get(slot) if slot is not None else None
                if e:
                    if e.type is None:
                        e = ff.get(slot, T_SINE)
                    e.magnitude = r.Magnitude / 10000
                    e.offset = self._s16(r.Offset) / 10000
                    e.phase = r.Phase
                    e.period = r.Period
                    if self.verbose:
                        self.log(f"FFB periodic {r.EffectBlockIndex}/{slot}: mag={r.Magnitude} "
                                 f"offset={self._s16(r.Offset)} period={r.Period}ms")
                    ff.upload_params(e)
        elif t == PT_CONDREP:
            r = self._get("Ffb_h_Eff_Cond", p, FFB_EFF_COND)
            if r:
                ebi = r.EffectBlockIndex
                cls = self.last_cond.get(ebi, "cond8")
                slot = self._slot_for(ebi, cls)
                e = ff.get(slot) if slot is not None else None
                if e:
                    if e.type is None:
                        e = ff.get(slot, VJ_TYPES.get(int(cls[4:]), T_SPRING))
                    centre = self._s16(r.CenterPointOffset)
                    pos, neg = self._s16(r.PosCoeff), self._s16(r.NegCoeff)
                    e.cond[1 if r.isY else 0] = dict(
                        center=centre / 10000, pos=pos / 10000, neg=neg / 10000,
                        psat=r.PosSatur / 10000, nsat=r.NegSatur / 10000,
                        db=self._s16(r.DeadBand) / 10000)
                    if self.verbose:
                        self.log(f"FFB cond {ebi}/{slot} {'Y' if r.isY else 'X'}: centre={centre} "
                                 f"k+={pos} k-={neg} sat={r.PosSatur}/{r.NegSatur} db={r.DeadBand}")
                    ff.upload_params(e)
        elif t == PT_ENVREP:
            r = self._get("Ffb_h_Eff_Envlp", p, FFB_EFF_ENVLP)
            if r:
                cls = self.last.get(r.EffectBlockIndex)
                slot = self.chan.get((r.EffectBlockIndex, cls))
                e = ff.effects.get(slot) if slot is not None else None
                if e and e.type is not None:
                    e.env = (r.AttackTime, r.AttackLevel / 10000, r.FadeTime, r.FadeLevel / 10000)
                    ff.upload_params(e)
                    ff.upload_core(e)   # envelope on/off changes the core's second address
        elif t == PT_EFOPREP:
            r = self._get("Ffb_h_EffOp", p, FFB_EFF_OP)
            if r:
                cls = self.last.get(r.EffectBlockIndex)
                slot = self.chan.get((r.EffectBlockIndex, cls))
                e = ff.effects.get(slot) if slot is not None else None
                if self.verbose:
                    self.log(f"FFB op {r.EffectBlockIndex}/{slot} ({cls}): {r.EffectOp} loops={r.LoopCount}")
                if not e or e.type is None:
                    return
                if r.EffectOp in (1, 2):
                    if r.EffectOp == 2:
                        for other in list(ff.effects.values()):
                            if other is not e and other.playing:
                                ff.stop(other)
                    ff.play(e, 1 if r.LoopCount in (0, 1, 0xFF) else r.LoopCount)
                elif r.EffectOp == 3:
                    ff.stop(e)
