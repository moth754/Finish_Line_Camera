"""
motion_detector.py
==================

Detects motion inside a user-defined rectangle (the "region of interest" or
ROI) using nothing but NumPy - no OpenCV required.

How it works
------------
Every new greyscale frame is compared against the previous one *within the ROI
only*.  Pixels whose brightness changed by more than a threshold are counted;
if the fraction of changed pixels exceeds `MOTION_AREA_FRACTION`, we declare
motion.  This is the classic "frame differencing" technique - cheap, fast and
perfectly adequate for spotting a runner crossing a fixed strip of track.

The ROI is stored in *normalised* coordinates (0.0-1.0) so it is independent of
the frame resolution and can be redrawn from the web UI at any time.  It is also
persisted to disk (data/roi.json) so it survives restarts.

Thread-safety: the ROI can be read by the capture loop while simultaneously
being rewritten by a web request, so all access goes through a lock.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass

import numpy as np

from . import config


@dataclass
class MotionResult:
    """Outcome of comparing one frame against the previous one."""
    detected: bool   # did motion exceed the threshold this frame?
    score: float     # fraction (0-1) of ROI pixels that changed
    roi_pixels: tuple[int, int, int, int]  # (x, y, w, h) in lores pixels
    # Where within the whole frame the motion actually happened, as a normalised
    # (x, y, w, h) bounding box of the changed pixels (0-1, resolution
    # independent).  None when nothing changed.  Used by motion-gated OCR.
    motion_bbox: tuple[float, float, float, float] | None = None


class MotionDetector:
    def __init__(self):
        # The previous greyscale frame, kept so we can diff against it.
        self._prev_gray: np.ndarray | None = None
        self._lock = threading.Lock()

        # ROI as normalised floats {x, y, w, h}.  Loaded from disk if present,
        # otherwise seeded from the default in config.
        self._roi = self._load_roi()

        # Motion sensitivity: the fraction (0-1) of ROI pixels that must change
        # to count as motion.  Adjustable live from the web UI and persisted.
        self._area_threshold = self._load_threshold()

        # The most recent motion score, so the UI can show a live readout that
        # makes the threshold easy to tune.
        self._last_score = 0.0

    def reset(self):
        """Forget the reference frame (after a camera reconfigure)."""
        with self._lock:
            self._prev_gray = None
            self._last_score = 0.0

    # -- ROI management -----------------------------------------------------

    def _load_roi(self) -> dict:
        """Load the saved ROI, falling back to the configured default."""
        try:
            if config.ROI_STATE_PATH.exists():
                with open(config.ROI_STATE_PATH, "r") as fh:
                    data = json.load(fh)
                # Validate keys; if anything is off, use the default.
                if all(k in data for k in ("x", "y", "w", "h")):
                    return {k: float(data[k]) for k in ("x", "y", "w", "h")}
        except Exception as exc:
            print(f"[motion] could not load ROI ({exc}); using default")
        return dict(config.DEFAULT_ROI)

    def reload_roi(self):
        """Re-read the persisted ROI (used by the OCR worker process)."""
        roi = self._load_roi()
        with self._lock:
            self._roi = roi

    def get_roi(self) -> dict:
        """Return the current ROI as normalised floats (thread-safe copy)."""
        with self._lock:
            return dict(self._roi)

    # -- motion sensitivity (settable at runtime from the web UI) -----------

    def _load_threshold(self) -> float:
        """Load the saved motion threshold, falling back to the config default."""
        try:
            if config.MOTION_STATE_PATH.exists():
                with open(config.MOTION_STATE_PATH, "r") as fh:
                    data = json.load(fh)
                if "area_threshold" in data:
                    return float(data["area_threshold"])
        except Exception as exc:
            print(f"[motion] could not load threshold ({exc}); using default")
        return float(config.MOTION_AREA_FRACTION)

    def get_motion_settings(self) -> dict:
        """Return the current threshold plus the latest live motion score."""
        with self._lock:
            return {"area_threshold": self._area_threshold,
                    "last_score": self._last_score}

    def set_threshold(self, area_threshold: float):
        """Set the motion sensitivity (fraction 0-1) and persist it."""
        # Clamp to a sensible range: 0.1% .. 50% of the ROI.
        area_threshold = min(max(float(area_threshold), 0.001), 0.5)
        with self._lock:
            self._area_threshold = area_threshold
        try:
            config.MOTION_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(config.MOTION_STATE_PATH, "w") as fh:
                json.dump({"area_threshold": area_threshold}, fh, indent=2)
        except Exception as exc:
            print(f"[motion] could not save threshold ({exc})")
        return self.get_motion_settings()

    def set_roi(self, x: float, y: float, w: float, h: float):
        """
        Replace the ROI (normalised 0-1) and persist it to disk.

        Values are clamped so the rectangle always stays inside the frame.
        """
        x = min(max(x, 0.0), 1.0)
        y = min(max(y, 0.0), 1.0)
        w = min(max(w, 0.01), 1.0 - x)
        h = min(max(h, 0.01), 1.0 - y)
        roi = {"x": x, "y": y, "w": w, "h": h}
        with self._lock:
            self._roi = roi
            # Reset the reference frame: the diff against an old frame would be
            # meaningless now that we are watching a different area.
            self._prev_gray = None
        try:
            config.ROI_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(config.ROI_STATE_PATH, "w") as fh:
                json.dump(roi, fh, indent=2)
        except Exception as exc:
            print(f"[motion] could not save ROI ({exc})")

    @staticmethod
    def roi_to_pixels(roi: dict, width: int, height: int) -> tuple[int, int, int, int]:
        """Convert a normalised ROI to integer pixel coords for a given size."""
        x = int(roi["x"] * width)
        y = int(roi["y"] * height)
        w = int(roi["w"] * width)
        h = int(roi["h"] * height)
        # Guarantee at least a 1px box and stay inside the frame.
        w = max(1, min(w, width - x))
        h = max(1, min(h, height - y))
        return x, y, w, h

    # -- the actual detection ----------------------------------------------

    def update(self, gray: np.ndarray) -> MotionResult:
        """
        Feed in the latest greyscale frame and get back whether motion occurred.

        `gray` is the small lores image from the camera (uint8, HxW).
        """
        height, width = gray.shape[:2]
        with self._lock:
            roi = dict(self._roi)
            prev = self._prev_gray
            area_threshold = self._area_threshold

        x, y, w, h = self.roi_to_pixels(roi, width, height)
        # Crop both the current and previous frames to the ROI.
        current_roi = gray[y:y + h, x:x + w].astype(np.int16)

        # First frame after start / ROI change: nothing to compare against yet.
        if prev is None or prev.shape != gray.shape:
            with self._lock:
                self._prev_gray = gray.copy()
            return MotionResult(False, 0.0, (x, y, w, h))

        prev_roi = prev[y:y + h, x:x + w].astype(np.int16)

        # Absolute brightness difference per pixel, then threshold to a boolean
        # "changed / not changed" mask, then measure how much of the ROI moved.
        diff = np.abs(current_roi - prev_roi)
        changed = diff > config.MOTION_PIXEL_THRESHOLD
        score = float(changed.mean()) if changed.size else 0.0
        detected = score >= area_threshold

        # When motion fired, record WHERE it happened as a normalised bbox of the
        # changed pixels, so the (later) OCR pass can reject reads away from it.
        # 2nd/98th percentiles trim isolated noise so the box hugs the real
        # moving blob (the runner) rather than the odd flickering pixel.
        motion_bbox = None
        if detected:
            ys_c, xs_c = np.nonzero(changed)
            if xs_c.size:
                x_lo, x_hi = np.percentile(xs_c, [2, 98])
                y_lo, y_hi = np.percentile(ys_c, [2, 98])
                motion_bbox = (
                    float((x + x_lo) / width),
                    float((y + y_lo) / height),
                    float(max(x_hi - x_lo, 1.0) / width),
                    float(max(y_hi - y_lo, 1.0) / height),
                )

        # Store this frame as the reference for next time, and remember the
        # score so the web UI can show a live motion readout.
        with self._lock:
            self._prev_gray = gray.copy()
            self._last_score = score

        return MotionResult(detected, score, (x, y, w, h), motion_bbox)
