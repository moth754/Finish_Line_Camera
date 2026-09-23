"""
settings.py
===========

A tiny, thread-safe, persisted key/value store for everything the Settings tab
can change (recording mode, quality, storage cap, preview size, OCR options
for extraction ...).

Defaults come from config.DEFAULT_SETTINGS; anything the user changes is written
to data/settings.json and survives a restart.  Values are validated against
the ranges in `_RULES` so a stray browser request can't leave the recorder
with an impossible configuration.
"""

from __future__ import annotations

import json
import threading

from . import config

# (type, minimum, maximum) or (type, allowed-values) per key.  None = no bound.
_RULES = {
    # The set of valid cameras and USB modes depends on what is plugged in,
    # so these are free-form strings checked for shape rather than membership;
    # camera_manager falls back gracefully if the value no longer resolves.
    "camera_id":           ("str", 64),
    "usb_mode":            ("str", 64),
    "record_mode":         ("choice", tuple(config.RECORD_MODES.keys())),
    "record_crf":          ("int", 14, 30),
    "segment_seconds":     ("int", 10, 600),
    "storage_cap_gb":      ("float", 1.0, 100000.0),
    "min_free_gb":         ("float", 0.5, 1000.0),
    "preview_width":       ("choice", (640, 960, 1280, 1920)),
    "preview_fps":         ("int", 1, 30),
    "preview_quality":     ("int", 40, 95),
    "ocr_while_recording": ("bool",),
    "extract_motion_only": ("bool",),
    "extract_max_fps":     ("float", 0.1, 120.0),
    "extract_dedup":       ("float", 0.0, 0.5),
}


class Settings:
    def __init__(self, path=None):
        self._path = path or config.SETTINGS_PATH
        self._lock = threading.Lock()
        self._values = dict(config.DEFAULT_SETTINGS)
        self._load()

    # -- persistence --------------------------------------------------------

    def _load(self):
        try:
            if self._path.exists():
                with open(self._path) as fh:
                    data = json.load(fh)
                for k, v in data.items():
                    if k in _RULES:
                        try:
                            self._values[k] = self._coerce(k, v)
                        except ValueError:
                            pass    # keep the default for a bad stored value
        except Exception as exc:
            print(f"[settings] could not load {self._path.name} ({exc}); using defaults")

    def _save(self):
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            with open(tmp, "w") as fh:
                json.dump(self._values, fh, indent=2)
            tmp.replace(self._path)
        except Exception as exc:
            print(f"[settings] could not save ({exc})")

    # -- validation ---------------------------------------------------------

    @staticmethod
    def _coerce(key, value):
        rule = _RULES[key]
        kind = rule[0]
        if kind == "bool":
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if kind == "str":
            v = str(value).strip()
            if not v or len(v) > rule[1]:
                raise ValueError(f"{key} must be 1-{rule[1]} characters")
            # Keep it to the character set the ids and mode keys actually use,
            # so nothing odd can reach a device path or a settings file.
            if not all(c.isalnum() or c in "_-.:/@x" for c in v):
                raise ValueError(f"{key} contains unexpected characters")
            # A camera id carries a device path; never let one walk the tree.
            # (camera_manager only ever uses ids it enumerated itself, so this
            # is a second line of defence rather than the only one.)
            if ".." in v:
                raise ValueError(f"{key} must not contain '..'")
            return v
        if kind == "choice":
            allowed = rule[1]
            # Allow "1280" for an int choice, etc.
            for a in allowed:
                if value == a or str(value) == str(a):
                    return a
            raise ValueError(f"{key} must be one of {allowed}")
        if kind == "int":
            v = int(round(float(value)))
        elif kind == "float":
            v = float(value)
        else:
            raise ValueError(kind)
        lo, hi = rule[1], rule[2]
        if lo is not None and v < lo:
            v = lo
        if hi is not None and v > hi:
            v = hi
        return v

    # -- public API ---------------------------------------------------------

    def get(self, key):
        with self._lock:
            return self._values[key]

    def all(self) -> dict:
        with self._lock:
            return dict(self._values)

    def update(self, changes: dict) -> dict:
        """Validate + apply a dict of changes; unknown keys are rejected."""
        cleaned = {}
        for k, v in changes.items():
            if k not in _RULES:
                raise ValueError(f"unknown setting '{k}'")
            cleaned[k] = self._coerce(k, v)
        with self._lock:
            self._values.update(cleaned)
            self._save()
            return dict(self._values)

    # -- derived helpers ----------------------------------------------------

    def record_mode(self) -> dict:
        """The active RECORD_MODES entry (with its key added)."""
        key = self.get("record_mode")
        mode = dict(config.RECORD_MODES.get(key) or config.RECORD_MODES["2k50"])
        mode["key"] = key
        return mode

    def preview_size(self) -> tuple[int, int]:
        w = int(self.get("preview_width"))
        return (w, w * 9 // 16)
