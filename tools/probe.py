"""Probe the WingMan Force (046D:C281): print descriptors, pipes, then raw packets.

Usage:  python probe.py            (dump packets for 20 s; move stick, press buttons)
        python probe.py 60         (dump for 60 s)
Output is also written to probe_log.txt next to this script.
"""
import os
import sys
import time

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from winusb import WinUsbDevice, find_device_paths

VID, PID = 0x046D, 0xC281
LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe_log.txt")


def main():
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 20
    log = open(LOG, "w")

    def out(s):
        print(s)
        log.write(s + "\n")
        log.flush()

    paths = find_device_paths(VID, PID)
    if not paths:
        out("WingMan Force (046D:C281) not found. Is it plugged in?")
        return 1
    out(f"device path: {paths[0]}")
    with WinUsbDevice(paths[0]) as dev:
        try:
            out(f"device descriptor: {dev.get_descriptor(1).hex(' ')}")
            out(f"config descriptor: {dev.get_descriptor(2).hex(' ')}")
            for i in (1, 2, 3):
                try:
                    s = dev.get_descriptor(3, i, 0x0409)
                    out(f"string {i}: {s[2:].decode('utf-16-le', 'replace')!r}")
                except OSError:
                    pass
        except OSError as e:
            out(f"descriptor read failed: {e}")
        it = dev.interface
        out(f"interface: class={it.bInterfaceClass:#04x} sub={it.bInterfaceSubClass:#04x} "
            f"proto={it.bInterfaceProtocol:#04x} endpoints={it.bNumEndpoints}")
        for p in dev.pipes:
            out(f"  pipe {p.PipeId:#04x} type={p.PipeType} maxpkt={p.MaximumPacketSize} interval={p.Interval}")

        # I-Force identification queries (vendor control IN requests, as the Linux driver does)
        for q in (b"M", b"P", b"B", b"N", b"V"):
            try:
                r = dev.control_in(0xC1, q[0], 0, 0, 16)
                out(f"query {q.decode()}: {r.hex(' ')}")
            except OSError as e:
                out(f"query {q.decode()}: failed ({e.args[0]})")

        pipe = dev.in_pipe
        dev.set_timeout(pipe, 250)
        out(f"\nReading pipe {pipe:#04x} for {secs:.0f} s - move the stick, throttle, twist, press every button, hat...")
        end = time.time() + secs
        last = None
        while time.time() < end:
            d = dev.read(pipe)
            if d is None or d == last or d[:1] != b"\x01":
                continue
            last = d
            x, y = int.from_bytes(d[1:3], "little"), int.from_bytes(d[3:5], "little")
            out(f"{time.time():.3f}  {d.hex(' ')}   x={x:5d} y={y:5d} thr={d[5]:3d} "
                f"btn={d[6]:08b} b7={d[7]:08b}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
