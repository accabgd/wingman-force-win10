"""Minimal WinUSB access via ctypes (no third-party packages).

Finds a USB device by VID/PID through GUID_DEVINTERFACE_USB_DEVICE (every USB
device exposes it) and opens it with WinUSB. The device must have WinUSB bound
as its function driver (Device Manager -> Update driver -> WinUsb Device).
"""
import ctypes
from ctypes import wintypes as wt

setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
winusb = ctypes.WinDLL("winusb", use_last_error=True)


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD),
                ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, s):
        import uuid
        u = uuid.UUID(s)
        g = cls()
        g.Data1, g.Data2, g.Data3 = u.fields[0], u.fields[1], u.fields[2]
        g.Data4[:] = list(u.bytes[8:])
        return g


GUID_DEVINTERFACE_USB_DEVICE = GUID.parse("A5DCBF10-6530-11D2-901F-00C04FB951ED")

DIGCF_PRESENT = 0x02
DIGCF_DEVICEINTERFACE = 0x10
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 1
FILE_SHARE_WRITE = 2
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000
INVALID_HANDLE_VALUE = wt.HANDLE(-1).value
PIPE_TRANSFER_TIMEOUT = 0x03
ERROR_SEM_TIMEOUT = 121


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("InterfaceClassGuid", GUID),
                ("Flags", wt.DWORD), ("Reserved", ctypes.c_void_p)]


class USB_INTERFACE_DESCRIPTOR(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("bLength", ctypes.c_ubyte), ("bDescriptorType", ctypes.c_ubyte),
                ("bInterfaceNumber", ctypes.c_ubyte), ("bAlternateSetting", ctypes.c_ubyte),
                ("bNumEndpoints", ctypes.c_ubyte), ("bInterfaceClass", ctypes.c_ubyte),
                ("bInterfaceSubClass", ctypes.c_ubyte), ("bInterfaceProtocol", ctypes.c_ubyte),
                ("iInterface", ctypes.c_ubyte)]


class WINUSB_PIPE_INFORMATION(ctypes.Structure):
    _fields_ = [("PipeType", ctypes.c_int), ("PipeId", ctypes.c_ubyte),
                ("MaximumPacketSize", ctypes.c_ushort), ("Interval", ctypes.c_ubyte)]


class WINUSB_SETUP_PACKET(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("RequestType", ctypes.c_ubyte), ("Request", ctypes.c_ubyte),
                ("Value", ctypes.c_ushort), ("Index", ctypes.c_ushort),
                ("Length", ctypes.c_ushort)]


setupapi.SetupDiGetClassDevsW.restype = wt.HANDLE
setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), wt.LPCWSTR, wt.HWND, wt.DWORD]
setupapi.SetupDiEnumDeviceInterfaces.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.POINTER(GUID),
                                                 wt.DWORD, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)]
setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [wt.HANDLE, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
                                                      ctypes.c_void_p, wt.DWORD,
                                                      ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wt.HANDLE]
kernel32.CreateFileW.restype = wt.HANDLE
kernel32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD,
                                 wt.DWORD, wt.HANDLE]
kernel32.CloseHandle.argtypes = [wt.HANDLE]
winusb.WinUsb_Initialize.argtypes = [wt.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
winusb.WinUsb_Free.argtypes = [ctypes.c_void_p]
winusb.WinUsb_QueryInterfaceSettings.argtypes = [ctypes.c_void_p, ctypes.c_ubyte,
                                                 ctypes.POINTER(USB_INTERFACE_DESCRIPTOR)]
winusb.WinUsb_QueryPipe.argtypes = [ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_ubyte,
                                    ctypes.POINTER(WINUSB_PIPE_INFORMATION)]
winusb.WinUsb_SetPipePolicy.argtypes = [ctypes.c_void_p, ctypes.c_ubyte, wt.ULONG, wt.ULONG,
                                        ctypes.c_void_p]
winusb.WinUsb_ReadPipe.argtypes = [ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_void_p, wt.ULONG,
                                   ctypes.POINTER(wt.ULONG), ctypes.c_void_p]
winusb.WinUsb_WritePipe.argtypes = winusb.WinUsb_ReadPipe.argtypes
winusb.WinUsb_ControlTransfer.argtypes = [ctypes.c_void_p, WINUSB_SETUP_PACKET, ctypes.c_void_p,
                                          wt.ULONG, ctypes.POINTER(wt.ULONG), ctypes.c_void_p]
winusb.WinUsb_GetDescriptor.argtypes = [ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_ubyte,
                                        ctypes.c_ushort, ctypes.c_void_p, wt.ULONG,
                                        ctypes.POINTER(wt.ULONG)]


def _err(msg):
    return OSError(ctypes.get_last_error(), f"{msg} (Win32 error {ctypes.get_last_error()})")


def find_device_paths(vid, pid):
    tag = f"vid_{vid:04x}&pid_{pid:04x}"
    h = setupapi.SetupDiGetClassDevsW(ctypes.byref(GUID_DEVINTERFACE_USB_DEVICE), None, None,
                                      DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
    if h == INVALID_HANDLE_VALUE:
        raise _err("SetupDiGetClassDevs")
    paths = []
    try:
        i = 0
        while True:
            ifd = SP_DEVICE_INTERFACE_DATA(cbSize=ctypes.sizeof(SP_DEVICE_INTERFACE_DATA))
            if not setupapi.SetupDiEnumDeviceInterfaces(h, None, ctypes.byref(GUID_DEVINTERFACE_USB_DEVICE),
                                                        i, ctypes.byref(ifd)):
                break
            i += 1
            need = wt.DWORD()
            setupapi.SetupDiGetDeviceInterfaceDetailW(h, ctypes.byref(ifd), None, 0, ctypes.byref(need), None)
            buf = ctypes.create_string_buffer(need.value)
            # cbSize of SP_DEVICE_INTERFACE_DETAIL_DATA_W: 8 on x64, 6 on x86
            ctypes.c_uint32.from_buffer(buf).value = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
            if setupapi.SetupDiGetDeviceInterfaceDetailW(h, ctypes.byref(ifd), buf, need, None, None):
                path = ctypes.wstring_at(ctypes.addressof(buf) + 4)
                if tag in path.lower():
                    paths.append(path)
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(h)
    return paths


class WinUsbDevice:
    def __init__(self, path):
        self.path = path
        self.file = kernel32.CreateFileW(path, GENERIC_READ | GENERIC_WRITE,
                                         FILE_SHARE_READ | FILE_SHARE_WRITE, None,
                                         OPEN_EXISTING, FILE_FLAG_OVERLAPPED, None)
        if self.file == INVALID_HANDLE_VALUE:
            raise _err("CreateFile failed - is WinUSB installed for this device?")
        self.h = ctypes.c_void_p()
        if not winusb.WinUsb_Initialize(self.file, ctypes.byref(self.h)):
            e = _err("WinUsb_Initialize failed - is WinUSB the device's driver?")
            kernel32.CloseHandle(self.file)
            raise e
        self.pipes = []
        desc = USB_INTERFACE_DESCRIPTOR()
        if winusb.WinUsb_QueryInterfaceSettings(self.h, 0, ctypes.byref(desc)):
            self.interface = desc
            for n in range(desc.bNumEndpoints):
                p = WINUSB_PIPE_INFORMATION()
                if winusb.WinUsb_QueryPipe(self.h, 0, n, ctypes.byref(p)):
                    self.pipes.append(p)

    @property
    def in_pipe(self):
        return next(p.PipeId for p in self.pipes if p.PipeId & 0x80)

    @property
    def out_pipe(self):
        return next((p.PipeId for p in self.pipes if not p.PipeId & 0x80), None)

    def set_timeout(self, pipe, ms):
        v = wt.ULONG(ms)
        winusb.WinUsb_SetPipePolicy(self.h, pipe, PIPE_TRANSFER_TIMEOUT, 4, ctypes.byref(v))

    def read(self, pipe, size=64):
        """Returns bytes, or None on timeout."""
        buf = (ctypes.c_ubyte * size)()
        n = wt.ULONG()
        if not winusb.WinUsb_ReadPipe(self.h, pipe, buf, size, ctypes.byref(n), None):
            if ctypes.get_last_error() == ERROR_SEM_TIMEOUT:
                return None
            raise _err("WinUsb_ReadPipe")
        return bytes(buf[:n.value])

    def write(self, pipe, data):
        buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(bytes(data))
        n = wt.ULONG()
        if not winusb.WinUsb_WritePipe(self.h, pipe, buf, len(data), ctypes.byref(n), None):
            raise _err("WinUsb_WritePipe")
        return n.value

    def control_in(self, request_type, request, value, index, length):
        sp = WINUSB_SETUP_PACKET(request_type, request, value, index, length)
        buf = (ctypes.c_ubyte * length)()
        n = wt.ULONG()
        if not winusb.WinUsb_ControlTransfer(self.h, sp, buf, length, ctypes.byref(n), None):
            raise _err("WinUsb_ControlTransfer")
        return bytes(buf[:n.value])

    def get_descriptor(self, dtype, index=0, lang=0, size=255):
        buf = (ctypes.c_ubyte * size)()
        n = wt.ULONG()
        if not winusb.WinUsb_GetDescriptor(self.h, dtype, index, lang, buf, size, ctypes.byref(n)):
            raise _err("WinUsb_GetDescriptor")
        return bytes(buf[:n.value])

    def close(self):
        if self.h:
            winusb.WinUsb_Free(self.h)
            self.h = None
        if self.file:
            kernel32.CloseHandle(self.file)
            self.file = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
