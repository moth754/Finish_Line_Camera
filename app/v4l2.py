"""
v4l2.py
=======

Enumerate Video4Linux2 capture devices and the modes they support, using the
kernel's ioctl interface directly through `ctypes` + `fcntl`.

Why not shell out to `v4l2-ctl`?  Because it is not installed by default on
most distributions, and this is the one piece of information the app cannot do
without when a USB camera is in use.  The ioctls below are stable kernel ABI
and need no third-party package.

What this module is for:
    * finding the USB webcams actually plugged into a machine, and
    * asking each one which resolutions and frame rates it can really deliver,

so the Settings tab can offer a truthful dropdown instead of the Raspberry Pi
presets, which a webcam cannot honour.

Everything here is best-effort: a device that answers an ioctl with an error is
skipped rather than being allowed to break the camera list.
"""

from __future__ import annotations

import ctypes
import fcntl
import glob
import os

# ---------------------------------------------------------------------------
# ioctl request-number encoding (linux/ioctl.h)
# ---------------------------------------------------------------------------

_IOC_NRBITS, _IOC_TYPEBITS, _IOC_SIZEBITS = 8, 8, 14
_IOC_NRSHIFT = 0
_IOC_TYPESHIFT = _IOC_NRSHIFT + _IOC_NRBITS
_IOC_SIZESHIFT = _IOC_TYPESHIFT + _IOC_TYPEBITS
_IOC_DIRSHIFT = _IOC_SIZESHIFT + _IOC_SIZEBITS
_IOC_NONE, _IOC_WRITE, _IOC_READ = 0, 1, 2


def _IOC(direction: int, typ: str, nr: int, size: int) -> int:
    return ((direction << _IOC_DIRSHIFT) | (ord(typ) << _IOC_TYPESHIFT)
            | (nr << _IOC_NRSHIFT) | (size << _IOC_SIZESHIFT))


def _IOR(typ, nr, struct):   return _IOC(_IOC_READ, typ, nr, ctypes.sizeof(struct))
def _IOWR(typ, nr, struct):  return _IOC(_IOC_READ | _IOC_WRITE, typ, nr, ctypes.sizeof(struct))


# ---------------------------------------------------------------------------
# Structures (linux/videodev2.h)
# ---------------------------------------------------------------------------

class v4l2_capability(ctypes.Structure):
    _fields_ = [("driver", ctypes.c_char * 16),
                ("card", ctypes.c_char * 32),
                ("bus_info", ctypes.c_char * 32),
                ("version", ctypes.c_uint32),
                ("capabilities", ctypes.c_uint32),
                ("device_caps", ctypes.c_uint32),
                ("reserved", ctypes.c_uint32 * 3)]


class v4l2_fmtdesc(ctypes.Structure):
    _fields_ = [("index", ctypes.c_uint32),
                ("type", ctypes.c_uint32),
                ("flags", ctypes.c_uint32),
                ("description", ctypes.c_char * 32),
                ("pixelformat", ctypes.c_uint32),
                ("reserved", ctypes.c_uint32 * 4)]


class _frmsize_discrete(ctypes.Structure):
    _fields_ = [("width", ctypes.c_uint32), ("height", ctypes.c_uint32)]


class _frmsize_stepwise(ctypes.Structure):
    _fields_ = [("min_width", ctypes.c_uint32), ("max_width", ctypes.c_uint32),
                ("step_width", ctypes.c_uint32), ("min_height", ctypes.c_uint32),
                ("max_height", ctypes.c_uint32), ("step_height", ctypes.c_uint32)]


class _frmsize_union(ctypes.Union):
    _fields_ = [("discrete", _frmsize_discrete), ("stepwise", _frmsize_stepwise)]


class v4l2_frmsizeenum(ctypes.Structure):
    _fields_ = [("index", ctypes.c_uint32),
                ("pixel_format", ctypes.c_uint32),
                ("type", ctypes.c_uint32),
                ("u", _frmsize_union),
                ("reserved", ctypes.c_uint32 * 2)]


class v4l2_fract(ctypes.Structure):
    _fields_ = [("numerator", ctypes.c_uint32), ("denominator", ctypes.c_uint32)]


class _frmival_stepwise(ctypes.Structure):
    _fields_ = [("min", v4l2_fract), ("max", v4l2_fract), ("step", v4l2_fract)]


class _frmival_union(ctypes.Union):
    _fields_ = [("discrete", v4l2_fract), ("stepwise", _frmival_stepwise)]


class v4l2_frmivalenum(ctypes.Structure):
    _fields_ = [("index", ctypes.c_uint32),
                ("pixel_format", ctypes.c_uint32),
                ("width", ctypes.c_uint32),
                ("height", ctypes.c_uint32),
                ("type", ctypes.c_uint32),
                ("u", _frmival_union),
                ("reserved", ctypes.c_uint32 * 2)]


VIDIOC_QUERYCAP = _IOR("V", 0, v4l2_capability)
VIDIOC_ENUM_FMT = _IOWR("V", 2, v4l2_fmtdesc)
VIDIOC_ENUM_FRAMESIZES = _IOWR("V", 74, v4l2_frmsizeenum)
VIDIOC_ENUM_FRAMEINTERVALS = _IOWR("V", 75, v4l2_frmivalenum)

V4L2_BUF_TYPE_VIDEO_CAPTURE = 1
V4L2_CAP_VIDEO_CAPTURE = 0x00000001
V4L2_CAP_DEVICE_CAPS = 0x80000000
V4L2_FRMSIZE_TYPE_DISCRETE = 1
V4L2_FRMIVAL_TYPE_DISCRETE = 1

# Raspberry Pi platform video nodes.  A Pi exposes a dozen /dev/video* entries
# for its ISP, encoder and CSI front-end; they advertise a capture capability
# but are not cameras, and listing them would make the dropdown useless.  The
# Pi's actual camera is offered separately through Picamera2.
_PLATFORM_DRIVERS = {
    "bcm2835-isp", "bcm2835-codec", "pispbe", "rp1-cfe", "unicam",
    "rpivid", "rpi-hevc-dec", "rpi-codec", "bcm2835_isp",
}

# Pixel formats worth capturing, best first.  MJPEG is what lets a USB 2.0
# webcam reach 1080p at a usable frame rate; YUYV is uncompressed and is
# usually capped to a few frames per second at high resolutions.
_PREFERRED_FORMATS = ("MJPG", "YUYV", "NV12", "YU12", "UYVY", "H264")


def _fourcc(value: int) -> str:
    return "".join(chr((value >> (8 * i)) & 0xFF) for i in range(4)).strip("\x00 ")


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", "replace").strip("\x00 ").strip()


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def query_capability(path: str) -> dict | None:
    """Return {driver, card, bus_info, is_capture} for a device, or None."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        cap = v4l2_capability()
        fcntl.ioctl(fd, VIDIOC_QUERYCAP, cap)
        # device_caps describes THIS node; capabilities describes the whole
        # physical device, which on a multi-node card over-reports.
        caps = cap.device_caps if (cap.capabilities & V4L2_CAP_DEVICE_CAPS) else cap.capabilities
        return {"driver": _decode(cap.driver), "card": _decode(cap.card),
                "bus_info": _decode(cap.bus_info),
                "is_capture": bool(caps & V4L2_CAP_VIDEO_CAPTURE)}
    except OSError:
        return None
    finally:
        os.close(fd)


def _enum_pixel_formats(fd) -> list[tuple[int, str, str]]:
    out = []
    for index in range(64):
        desc = v4l2_fmtdesc(index=index, type=V4L2_BUF_TYPE_VIDEO_CAPTURE)
        try:
            fcntl.ioctl(fd, VIDIOC_ENUM_FMT, desc)
        except OSError:
            break
        out.append((desc.pixelformat, _fourcc(desc.pixelformat), _decode(desc.description)))
    return out


def _enum_frame_sizes(fd, pixfmt: int) -> list[tuple[int, int]]:
    sizes = []
    for index in range(64):
        fs = v4l2_frmsizeenum(index=index, pixel_format=pixfmt)
        try:
            fcntl.ioctl(fd, VIDIOC_ENUM_FRAMESIZES, fs)
        except OSError:
            break
        if fs.type == V4L2_FRMSIZE_TYPE_DISCRETE:
            sizes.append((fs.u.discrete.width, fs.u.discrete.height))
        else:
            # A stepwise/continuous device (rare for webcams): offer a few
            # sensible 16:9 sizes that fall inside its range.
            sw = fs.u.stepwise
            for w, h in ((1920, 1080), (1280, 720), (960, 540), (640, 360)):
                if sw.min_width <= w <= sw.max_width and sw.min_height <= h <= sw.max_height:
                    sizes.append((w, h))
            break
    return sizes


def _enum_frame_rates(fd, pixfmt: int, width: int, height: int) -> list[float]:
    rates = []
    for index in range(64):
        fi = v4l2_frmivalenum(index=index, pixel_format=pixfmt,
                              width=width, height=height)
        try:
            fcntl.ioctl(fd, VIDIOC_ENUM_FRAMEINTERVALS, fi)
        except OSError:
            break
        if fi.type == V4L2_FRMIVAL_TYPE_DISCRETE:
            num, den = fi.u.discrete.numerator, fi.u.discrete.denominator
            if num:
                rates.append(round(den / num, 3))
        else:
            sw = fi.u.stepwise
            if sw.min.numerator:
                rates.append(round(sw.min.denominator / sw.min.numerator, 3))
            break
    return sorted({r for r in rates if r > 0}, reverse=True)


def list_modes(path: str) -> list[dict]:
    """
    Every (format, size, frame-rate) this device can actually produce.

    Returned newest-useful-first: preferred pixel formats, then largest frames,
    then fastest rate.  Each entry is
    {key, label, fourcc, width, height, fps}.
    """
    try:
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    except OSError:
        return []
    modes: list[dict] = []
    try:
        for pixfmt, fourcc, _desc in _enum_pixel_formats(fd):
            if fourcc not in _PREFERRED_FORMATS:
                continue
            for (w, h) in _enum_frame_sizes(fd, pixfmt):
                rates = _enum_frame_rates(fd, pixfmt, w, h) or [30.0]
                for fps in rates:
                    ifps = int(round(fps))
                    if ifps < 1:
                        continue
                    modes.append({
                        "key": f"{fourcc}:{w}x{h}@{ifps}",
                        "label": f"{w}×{h} @ {ifps} fps ({fourcc})",
                        "fourcc": fourcc, "width": w, "height": h, "fps": ifps,
                    })
    except Exception:
        pass
    finally:
        os.close(fd)

    def rank(m):
        try:
            fmt_rank = _PREFERRED_FORMATS.index(m["fourcc"])
        except ValueError:
            fmt_rank = len(_PREFERRED_FORMATS)
        return (fmt_rank, -(m["width"] * m["height"]), -m["fps"])

    modes.sort(key=rank)
    # Drop duplicate size/rate pairs that several formats can both do; the
    # first one kept is the preferred format for that combination.
    seen, unique = set(), []
    for m in modes:
        sig = (m["width"], m["height"], m["fps"])
        if sig in seen:
            continue
        seen.add(sig)
        unique.append(m)
    return unique


def list_capture_devices() -> list[dict]:
    """
    Real video-capture devices on this machine, Pi platform nodes excluded.

    Each entry: {device, name, driver, bus_info}.  Devices are returned in
    /dev/video number order so the list is stable between calls.
    """
    found = []
    paths = sorted(glob.glob("/dev/video*"),
                   key=lambda p: int("".join(c for c in os.path.basename(p) if c.isdigit()) or 0))
    seen_bus = set()
    for path in paths:
        cap = query_capability(path)
        if not cap or not cap["is_capture"]:
            continue
        if cap["driver"].lower() in _PLATFORM_DRIVERS:
            continue
        # A UVC webcam registers several nodes (capture + metadata); only the
        # first node of a given bus address can actually stream video.
        bus = cap["bus_info"] or path
        if bus in seen_bus:
            continue
        if not list_modes(path):
            continue        # no usable capture formats: not a camera we can use
        seen_bus.add(bus)
        found.append({"device": path, "name": cap["card"] or os.path.basename(path),
                      "driver": cap["driver"], "bus_info": cap["bus_info"]})
    return found
