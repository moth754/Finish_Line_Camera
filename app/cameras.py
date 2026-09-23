"""
cameras.py
==========

The list of cameras this machine can record from, and the rules for choosing
between them.

Three kinds of source are supported:

    csi:<n>       a Raspberry Pi camera on the CSI ribbon, driven by Picamera2.
    usb:<device>  any V4L2 camera - a USB webcam, a capture card, a virtual
                  device - driven through OpenCV.
    mock          the built-in synthetic scene, so the app always starts.

A camera is identified by a short string ("csi:0", "usb:/dev/video2", "mock")
which is what the Settings tab stores.  "auto" means "pick the best available",
which is the default and what every existing install keeps doing: a Pi camera
if one is attached, otherwise a USB camera, otherwise the mock.

Enumeration is deliberately cheap and failure-tolerant.  It runs whenever the
Settings tab is opened, and a camera that has been unplugged must not raise -
it simply stops appearing in the list.
"""

from __future__ import annotations

import time

from . import v4l2

AUTO = "auto"
MOCK = "mock"


# ---------------------------------------------------------------------------
# Enumeration
# ---------------------------------------------------------------------------

def list_csi_cameras() -> list[dict]:
    """Raspberry Pi CSI cameras, via Picamera2.  Empty everywhere else."""
    try:
        from picamera2 import Picamera2
        info = Picamera2.global_camera_info()
    except Exception:
        return []
    out = []
    for cam in info or []:
        index = cam.get("Num", len(out))
        model = cam.get("Model") or "Pi camera"
        out.append({
            "id": f"csi:{index}",
            "kind": "csi",
            "name": model,
            "detail": cam.get("Id", ""),
            "device": None,
        })
    return out


def list_usb_cameras() -> list[dict]:
    """V4L2 capture devices (USB webcams and the like)."""
    out = []
    for dev in v4l2.list_capture_devices():
        out.append({
            "id": f"usb:{dev['device']}",
            "kind": "usb",
            "name": dev["name"],
            "detail": f"{dev['device']} · {dev['driver']}",
            "device": dev["device"],
        })
    return out


# Scanning means opening every /dev/video* node and running a handful of
# ioctls on each, so the result is held briefly.  Anything that needs to see a
# camera plugged in a moment ago passes force=True (the Settings tab's rescan
# button does exactly that).
_CACHE_SECONDS = 3.0
_cache: list[dict] | None = None
_cache_at = 0.0


def list_cameras(force: bool = False) -> list[dict]:
    """
    Everything available, in preference order, with the mock last.

    Each entry: {id, kind, name, detail, device}.
    """
    global _cache, _cache_at
    now = time.monotonic()
    if not force and _cache is not None and (now - _cache_at) < _CACHE_SECONDS:
        return list(_cache)

    cams = list_csi_cameras() + list_usb_cameras()
    cams.append({"id": MOCK, "kind": "mock", "name": "Mock camera (no hardware)",
                 "detail": "synthetic scene for testing and demos", "device": None})
    _cache, _cache_at = cams, now
    return list(cams)


def modes_for(camera_id: str) -> list[dict] | None:
    """
    The record modes a given camera can offer, or None to mean "use the
    built-in Raspberry Pi presets from config.RECORD_MODES".

    Only USB cameras report their own modes: a webcam cannot be asked for
    2304x1296 at 50 fps just because the Pi camera can manage it.
    """
    kind, _, rest = camera_id.partition(":")
    if kind == "usb" and rest:
        return v4l2.list_modes(rest)
    return None


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def resolve(camera_id: str) -> tuple[str, dict | None]:
    """
    Turn a stored camera id into the one actually to be used.

    Returns (resolved_id, entry) where entry is the matching list_cameras()
    record, or None for the mock.  A camera that has been unplugged since it
    was chosen falls back to whatever is available rather than refusing to
    start - a race must never be lost to a missing USB device.
    """
    cams = list_cameras()
    by_id = {c["id"]: c for c in cams}

    if camera_id and camera_id != AUTO and camera_id in by_id:
        entry = by_id[camera_id]
        return (camera_id, None if entry["kind"] == "mock" else entry)

    # "auto", or a camera that is no longer present.
    for cam in cams:
        if cam["kind"] in ("csi", "usb"):
            return (cam["id"], cam)
    return (MOCK, None)
