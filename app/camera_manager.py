"""
camera_manager.py
=================

Owns the camera and hands the rest of the program a steady supply of frames.

Design
------
A single background thread continuously pulls frames from the camera.  Every
frame yields:

    * lores  - a small YUV420 image (the preview size, e.g. 1280x720) used for
               the live MJPEG view and, subsampled, for motion detection.
    * main   - the full recording-size YUV420 image.  It is only copied out of
               the camera buffer when someone needs it: while recording it is
               handed to the Recorder's frame *sink* (which pipes it into
               ffmpeg/libx264), and a one-shot request can grab it for a
               snapshot.  Nothing else touches the big buffer, which keeps the
               per-frame cost low enough for 50 fps at 2304x1296.

Motion detection runs *inside* the capture loop on every frame so that the
per-frame motion log written alongside a recording lines up exactly with the
frames that were encoded.

Every frame carries the sensor's exposure timestamp converted to wall-clock
epoch seconds: that is the time the finish-line photo was actually taken, and
it is what gets written to the recording sidecars.

Two interchangeable backends:
    * Picamera2Backend  - the real Raspberry Pi camera (Camera Module 3).
    * MockCameraBackend - a synthetic moving scene at the same sizes/format,
                          so the whole application can be developed, tested and
                          demoed with no camera attached (or while the real one
                          is in use by another process).

`create_camera()` at the bottom picks the right one based on config.
"""

from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass, field

import numpy as np
import simplejpeg  # fast JPEG encoder, already a dependency of picamera2

from . import config
from .motion_detector import MotionDetector, MotionResult
from .settings import Settings


# ---------------------------------------------------------------------------
# YUV420 helpers (shared by the camera, the recorder and the extractor)
# ---------------------------------------------------------------------------
#
# A "tight" YUV420 image is a (h*3/2, w) uint8 array: the Y plane in the first
# h rows, then the U plane (h/2 x w/2) packed into the next h/4 rows, then V.
# That is exactly the byte layout ffmpeg's rawvideo yuv420p expects.

def tight_yuv420(arr: np.ndarray, w: int, h: int) -> np.ndarray:
    """Return a tight (h*3/2, w) YUV420 array from a possibly padded one.

    Picamera2 returns YUV420 as (h*3/2, stride); when stride == w (true for
    all our sizes on the Pi 5) it is already tight and returned as-is.
    """
    stride = arr.shape[1]
    if stride == w and arr.shape[0] == h * 3 // 2:
        return arr
    y = arr[:h, :w]
    reshaped = arr.reshape((arr.shape[0] * 2, stride // 2))
    u = reshaped[2 * h: 2 * h + h // 2, :w // 2]
    v = reshaped[2 * h + h // 2: 2 * h + h, :w // 2]
    out = np.empty((h * 3 // 2, w), dtype=np.uint8)
    out[:h] = y
    out[h:h + h // 4] = np.ascontiguousarray(u).reshape(h // 4, w)
    out[h + h // 4:] = np.ascontiguousarray(v).reshape(h // 4, w)
    return out


def yuv420_planes(tight: np.ndarray, w: int, h: int):
    """Split a tight YUV420 array into (Y, U, V) plane views."""
    y = tight[:h, :w]
    u = tight[h:h + h // 4].reshape(h // 2, w // 2)
    v = tight[h + h // 4:h + h // 2].reshape(h // 2, w // 2)
    return y, u, v


def encode_yuv420_jpeg(tight: np.ndarray, w: int, h: int,
                       quality: int | None = None) -> bytes:
    """JPEG-encode a tight YUV420 image straight from its planes (no RGB step)."""
    if quality is None:
        quality = config.JPEG_QUALITY
    y, u, v = yuv420_planes(tight, w, h)
    return simplejpeg.encode_jpeg_yuv_planes(
        np.ascontiguousarray(y), np.ascontiguousarray(u),
        np.ascontiguousarray(v), int(quality))


def encode_jpeg(rgb: np.ndarray, quality: int | None = None) -> bytes:
    """Encode an RGB NumPy image to JPEG bytes."""
    if quality is None:
        quality = config.JPEG_QUALITY
    return simplejpeg.encode_jpeg(
        np.ascontiguousarray(rgb), quality=quality, colorspace="RGB")


# ---------------------------------------------------------------------------
# A small container for "the most recent frame", shared between threads.
# ---------------------------------------------------------------------------

@dataclass
class Frame:
    """One moment in time, as seen by the preview / motion side of things."""
    lores: np.ndarray            # tight YUV420 at preview size
    lores_size: tuple[int, int]  # (w, h) of the preview image
    gray: np.ndarray             # small greyscale used for motion analysis
    epoch: float                 # wall-clock time the frame was exposed
    seq: int                     # running frame counter
    motion: MotionResult | None = field(default=None)


# ---------------------------------------------------------------------------
# Focus settings (shared by all backends, persisted to disk like the ROI)
# ---------------------------------------------------------------------------
#
# Focus is expressed to the user in metres; the Camera Module 3 lens wants
# "lens position" in dioptres = 1 / distance_in_metres, with 0 = infinity.

def _distance_to_lens(distance_m: float) -> float:
    return 0.0 if distance_m <= 0 else 1.0 / distance_m


def _default_focus_state() -> dict:
    return {"mode": config.CAMERA_AF_MODE,
            "distance_m": float(config.CAMERA_FOCUS_DISTANCE_M)}


def _load_focus_state() -> dict:
    try:
        if config.FOCUS_STATE_PATH.exists():
            with open(config.FOCUS_STATE_PATH, "r") as fh:
                data = json.load(fh)
            mode = data.get("mode", "manual")
            if mode in ("manual", "continuous"):
                return {"mode": mode, "distance_m": float(data.get("distance_m", 0.0))}
    except Exception as exc:
        print(f"[camera] could not load focus state ({exc}); using default")
    return _default_focus_state()


def _save_focus_state(state: dict):
    try:
        config.FOCUS_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(config.FOCUS_STATE_PATH, "w") as fh:
            json.dump(state, fh, indent=2)
    except Exception as exc:
        print(f"[camera] could not save focus state ({exc})")


# ---------------------------------------------------------------------------
# Base camera: threading, frame cache, sink, focus
# ---------------------------------------------------------------------------

class _BaseCamera:
    """
    Common machinery shared by the real and mock cameras.

    Subclasses implement `_open()`, `_grab(want_main)` and `_close()`; the
    start/stop threading, latest-frame cache, motion detection, the recorder
    sink and the one-shot main capture all live here.
    """

    def __init__(self, settings: Settings, motion: MotionDetector):
        self._settings = settings
        self._motion = motion
        self._apply_mode_from_settings()

        self._latest: Frame | None = None
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._running = False
        self._frame_event = threading.Event()
        self._seq = 0

        # Frame sink: while recording, the Recorder registers a callable that
        # receives (main_yuv, epoch, seq, frame) for EVERY frame.  Called from
        # the capture thread; it may block (that is how back-pressure from a
        # slow encoder reaches the camera, which then drops frames).
        self._sink = None
        self._sink_lock = threading.Lock()

        # One-shot full-size grab (snapshot button).
        self._main_once_event = threading.Event()
        self._main_once_result: tuple[np.ndarray, float] | None = None
        self._main_once_wanted = False

        # Achieved loop rate (frames in the last ~2 s).
        self._tick_times: list[float] = []

        self._focus = _load_focus_state()
        self._focus_lock = threading.Lock()
        self._last_error: str | None = None

    # -- configuration ------------------------------------------------------

    def _apply_mode_from_settings(self):
        mode = self._settings.record_mode()
        self.mode_key = mode["key"]
        self.record_size = (int(mode["size"][0]), int(mode["size"][1]))
        self.fps = int(mode["fps"])
        pw, ph = self._settings.preview_size()
        # The preview stream can't be bigger than the recording stream.
        if pw > self.record_size[0] or ph > self.record_size[1]:
            pw, ph = self.record_size
        self.preview_size = (pw, ph)
        # Motion analysis works on the preview luminance subsampled to <=640 px.
        self._gray_step = max(1, math.ceil(pw / config.MOTION_ANALYSIS_MAX_WIDTH))

    def describe(self) -> dict:
        return {"mode": self.mode_key, "record_size": list(self.record_size),
                "fps": self.fps, "preview_size": list(self.preview_size),
                "camera_type": type(self).__name__,
                "loop_fps": self.loop_fps(), "error": self._last_error}

    # -- focus (settable at runtime from the web UI) ------------------------

    def get_focus(self) -> dict:
        with self._focus_lock:
            state = dict(self._focus)
        state["lens_position"] = round(_distance_to_lens(state["distance_m"]), 4)
        return state

    def set_focus(self, mode: str, distance_m: float = 0.0):
        if mode not in ("manual", "continuous"):
            raise ValueError("mode must be 'manual' or 'continuous'")
        if distance_m and distance_m > 0:
            distance_m = min(max(distance_m, 0.01), 100.0)
        state = {"mode": mode, "distance_m": float(distance_m)}
        with self._focus_lock:
            self._focus = state
        _save_focus_state(state)
        self._apply_focus()
        return self.get_focus()

    def _apply_focus(self):
        pass

    # -- lifecycle ----------------------------------------------------------

    def start(self):
        self._open()
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="camera-loop",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        self._close()

    def reconfigure(self):
        """Re-open the camera with the current Settings (mode / preview size).

        Refused while a sink (i.e. a recording) is attached.
        """
        with self._sink_lock:
            if self._sink is not None:
                raise RuntimeError("cannot change camera mode while recording")
        print("[camera] reconfiguring ...")
        self.stop()
        self._apply_mode_from_settings()
        self._motion.reset()
        self.start()
        print(f"[camera] now {self.record_size[0]}x{self.record_size[1]} @ {self.fps} fps, "
              f"preview {self.preview_size[0]}x{self.preview_size[1]}")

    # -- sink / one-shot main capture --------------------------------------

    def set_sink(self, sink):
        with self._sink_lock:
            self._sink = sink

    def has_sink(self) -> bool:
        with self._sink_lock:
            return self._sink is not None

    def capture_main(self, timeout: float = 3.0) -> tuple[np.ndarray, float] | None:
        """Grab one full-size tight YUV420 frame (plus its epoch) - for snapshots."""
        self._main_once_event.clear()
        self._main_once_result = None
        self._main_once_wanted = True
        if not self._main_once_event.wait(timeout):
            self._main_once_wanted = False
            return None
        return self._main_once_result

    # -- the capture loop ---------------------------------------------------

    def _loop(self):
        while self._running:
            with self._sink_lock:
                sink = self._sink
            want_main = sink is not None or self._main_once_wanted
            try:
                lores, main, epoch = self._grab(want_main)
            except Exception as exc:  # never let the loop die silently
                self._last_error = str(exc)
                print(f"[camera] frame grab failed: {exc}")
                time.sleep(0.2)
                continue
            self._last_error = None

            self._seq += 1
            pw, ph = self.preview_size
            gray = lores[:ph:self._gray_step, :pw:self._gray_step]
            motion = self._motion.update(np.ascontiguousarray(gray))
            frame = Frame(lores=lores, lores_size=(pw, ph), gray=gray,
                          epoch=epoch, seq=self._seq, motion=motion)

            with self._lock:
                self._latest = frame
            self._frame_event.set()
            self._frame_event.clear()

            now = time.monotonic()
            self._tick_times.append(now)
            if len(self._tick_times) > 400:
                del self._tick_times[:200]

            if main is not None:
                if self._main_once_wanted:
                    self._main_once_wanted = False
                    self._main_once_result = (main, epoch)
                    self._main_once_event.set()
                if sink is not None:
                    try:
                        sink(main, epoch, self._seq, frame)
                    except Exception as exc:
                        print(f"[camera] frame sink error: {exc}")

    def loop_fps(self) -> float:
        now = time.monotonic()
        recent = [t for t in self._tick_times[-400:] if t > now - 2.0]
        return round(len(recent) / 2.0, 1)

    # -- accessors used by the rest of the app ------------------------------

    def get_latest(self) -> Frame | None:
        with self._lock:
            return self._latest

    def wait_for_frame(self, timeout: float = 1.0) -> Frame | None:
        self._frame_event.wait(timeout)
        return self.get_latest()

    # -- hooks subclasses must implement ------------------------------------

    def _open(self):
        raise NotImplementedError

    def _grab(self, want_main: bool) -> tuple[np.ndarray, np.ndarray | None, float]:
        """Return (lores_tight_yuv420, main_tight_yuv420_or_None, epoch)."""
        raise NotImplementedError

    def _close(self):
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Real Raspberry Pi camera (Picamera2 / libcamera)
# ---------------------------------------------------------------------------

class Picamera2Backend(_BaseCamera):
    """Drives a real Camera Module 3 through the Picamera2 library."""

    def __init__(self, settings: Settings, motion: MotionDetector):
        super().__init__(settings, motion)
        self._picam2 = None

    def _open(self):
        from picamera2 import Picamera2
        from libcamera import Transform

        self._picam2 = Picamera2()
        transform = Transform(hflip=int(config.CAMERA_HFLIP),
                              vflip=int(config.CAMERA_VFLIP))

        # Pin the sensor to exactly the recording frame rate: both streams are
        # produced from the same sensor frame, and the H.264 file is written at
        # this constant rate.
        frame_us = int(round(1_000_000 / self.fps))
        cfg = self._picam2.create_video_configuration(
            main={"size": self.record_size, "format": "YUV420"},
            lores={"size": self.preview_size, "format": "YUV420"},
            transform=transform,
            controls={"FrameDurationLimits": (frame_us, frame_us)},
            buffer_count=8,
            display=None,
        )
        self._picam2.configure(cfg)
        # Record what libcamera actually gave us (stride etc.).
        self._main_cfg = self._picam2.camera_configuration()["main"]
        self._lores_cfg = self._picam2.camera_configuration()["lores"]
        print(f"[camera] main {self._main_cfg['size']} stride {self._main_cfg['stride']}, "
              f"lores {self._lores_cfg['size']} stride {self._lores_cfg['stride']}, "
              f"{self.fps} fps")
        self._picam2.start()
        self._apply_focus()
        time.sleep(0.5)

    def _apply_focus(self):
        if self._picam2 is None:
            return
        try:
            from libcamera import controls
        except Exception as exc:
            print(f"[camera] focus controls unavailable ({exc})")
            return
        focus = self.get_focus()
        if focus["mode"] == "manual":
            self._picam2.set_controls({
                "AfMode": controls.AfModeEnum.Manual,
                "LensPosition": focus["lens_position"],
            })
            dist = "infinity" if focus["distance_m"] <= 0 else f"{focus['distance_m']:.2f} m"
            print(f"[camera] manual focus @ {dist} (lens {focus['lens_position']} dioptres)")
        else:
            self._picam2.set_controls({"AfMode": controls.AfModeEnum.Continuous})
            print("[camera] continuous autofocus enabled")

    def _grab(self, want_main: bool):
        request = self._picam2.capture_request()
        try:
            metadata = request.get_metadata()
            lores = request.make_array("lores")
            main = request.make_array("main") if want_main else None
        finally:
            request.release()

        # SensorTimestamp is CLOCK_BOOTTIME nanoseconds at start of exposure;
        # convert to wall-clock epoch seconds.
        sensor_ns = metadata.get("SensorTimestamp")
        if sensor_ns:
            offset = time.time() - time.clock_gettime(time.CLOCK_BOOTTIME)
            epoch = sensor_ns / 1e9 + offset
        else:
            epoch = time.time()

        pw, ph = self.preview_size
        lores = tight_yuv420(lores, pw, ph)
        if main is not None:
            mw, mh = self.record_size
            main = tight_yuv420(main, mw, mh)
        return lores, main, epoch

    def _close(self):
        if self._picam2 is not None:
            try:
                self._picam2.stop()
                self._picam2.close()
            except Exception:
                pass
            self._picam2 = None


# ---------------------------------------------------------------------------
# Mock camera (no hardware required)
# ---------------------------------------------------------------------------

class MockCameraBackend(_BaseCamera):
    """
    Synthetic scene at the configured sizes: a textured background with a
    bright "runner" bar (carrying a fake bib number) that sweeps across the
    frame for ~3 s, then rests for ~3 s.  Produces real motion for the
    detector and real content for the encoder, so recording -> segments ->
    timeline -> playback -> extraction -> OCR can all be exercised.
    """

    def __init__(self, settings: Settings, motion: MotionDetector):
        super().__init__(settings, motion)
        self._t0 = time.time()
        self._next_frame_at = 0.0

    def _open(self):
        w, h = self.record_size
        rng = np.random.default_rng(1)
        # Soft blotchy background + horizontal "track" band.
        base = rng.integers(40, 90, size=(h // 32 + 1, w // 32 + 1), dtype=np.uint8)
        base = np.kron(base, np.ones((32, 32), dtype=np.uint8))[:h, :w]
        base[int(h * 0.55):int(h * 0.75), :] += 60
        self._bg_y = base
        self._t0 = time.time()
        self._next_frame_at = time.monotonic()

    def _grab(self, want_main: bool):
        # Pace to the configured frame rate.
        period = 1.0 / self.fps
        now = time.monotonic()
        if now < self._next_frame_at:
            time.sleep(self._next_frame_at - now)
        self._next_frame_at = max(self._next_frame_at + period, time.monotonic() - period)

        w, h = self.record_size
        y = self._bg_y.copy()

        # A bar sweeps left->right during the first 3 s of every 6 s cycle.
        t = time.time() - self._t0
        phase = t % 6.0
        if phase < 3.0:
            bar_x = int((phase / 3.0) * w)
            bar_w = max(40, w // 14)
            x0 = max(0, bar_x - bar_w // 2)
            x1 = min(w, bar_x + bar_w // 2)
            y[int(h * 0.3):int(h * 0.8), x0:x1] = 235
            self._draw_number(y, x0 + 8, int(h * 0.42), "42", scale=max(6, h // 60))

        main = None
        tight = np.empty((h * 3 // 2, w), dtype=np.uint8)
        tight[:h] = y
        tight[h:] = 128          # neutral chroma
        pw, ph = self.preview_size
        sx, sy = w // pw, h // ph
        lores = np.empty((ph * 3 // 2, pw), dtype=np.uint8)
        lores[:ph] = y[::sy, ::sx][:ph, :pw]
        lores[ph:] = 128
        if want_main:
            main = tight
        return lores, main, time.time()

    @staticmethod
    def _draw_number(img: np.ndarray, x_left: int, y_top: int, text: str, scale: int):
        glyphs = {
            "0": ["111", "101", "101", "101", "111"], "1": ["010", "110", "010", "010", "111"],
            "2": ["111", "001", "111", "100", "111"], "3": ["111", "001", "111", "001", "111"],
            "4": ["101", "101", "111", "001", "001"], "5": ["111", "100", "111", "001", "111"],
            "6": ["111", "100", "111", "101", "111"], "7": ["111", "001", "010", "010", "010"],
            "8": ["111", "101", "111", "101", "111"], "9": ["111", "101", "111", "001", "111"],
        }
        cx = x_left
        for ch in text:
            grid = glyphs.get(ch)
            if grid is None:
                continue
            for r, row in enumerate(grid):
                for c, bit in enumerate(row):
                    if bit == "1":
                        ry, rx = y_top + r * scale, cx + c * scale
                        img[ry:ry + scale, rx:rx + scale] = 10
            cx += 4 * scale

    def _close(self):
        pass


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_camera(settings: Settings, motion: MotionDetector) -> _BaseCamera:
    """
    Build the camera backend dictated by config.USE_MOCK_CAMERA.

    "auto" tries the real camera and quietly falls back to the mock if Picamera2
    or the hardware is unavailable, so the program always starts.
    """
    setting = config.USE_MOCK_CAMERA

    if setting is True:
        print("[camera] using MOCK camera (forced by config)")
        return MockCameraBackend(settings, motion)

    if setting is False:
        print("[camera] using REAL Picamera2 camera (forced by config)")
        return Picamera2Backend(settings, motion)

    try:
        from picamera2 import Picamera2
        if Picamera2.global_camera_info():
            print("[camera] real camera detected - using Picamera2")
            return Picamera2Backend(settings, motion)
        print("[camera] no camera detected - falling back to MOCK camera")
    except Exception as exc:
        print(f"[camera] Picamera2 unavailable ({exc}) - using MOCK camera")
    return MockCameraBackend(settings, motion)
