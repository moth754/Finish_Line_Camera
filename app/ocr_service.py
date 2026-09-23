"""
ocr_service.py
==============

Bib recognition, run in a SEPARATE PROCESS, plus the "save a still for OCR"
helper.

Why a process and not a thread: EasyOCR/PyTorch work holds Python's GIL for
long stretches, and in the same process that starves the camera loop - it was
measured to drop ~6% of frames at 50 fps whenever an image was being read.
In its own process (at a lower CPU priority) recognition cannot touch the
capture loop at all.

Flow
----
Stills arrive from the Extractor (frames cut out of a recording) and from the
Snapshot buttons.  `save_capture_yuv()` writes the JPEG + `captures` row and
pushes the id onto a never-dropping LIFO backlog held HERE, in the parent.
A feeder thread hands ONE id at a time to the worker process (so newest-first
order and pause take effect between images), a drain thread collects results.
The worker loads the JPEG, runs the two-stage EasyOCR recognizer cropped to
the motion zone, motion-gates the reads and writes the `detections` rows
itself (SQLite copes with two writer processes in WAL mode).

The feeder PAUSES while a recording is in progress (unless the
"ocr_while_recording" setting is on) and when the user pauses it from the
Results tab.  If the worker dies it is restarted and the image re-queued.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import queue
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import numpy as np

from . import config, database
from .bib_recognizer import BibDetection, _load_ocr_state, _save_ocr_state
from .camera_manager import encode_yuv420_jpeg
from .motion_detector import MotionDetector
from .settings import Settings

# Config paths the worker process must inherit (run.py --root overrides them
# in the parent only; "spawn" re-imports config fresh in the child).
_PATH_KEYS = ("DATA_DIR", "CAPTURE_DIR", "DATABASE_PATH", "OCR_STATE_PATH",
              "ROI_STATE_PATH", "MOTION_STATE_PATH")


# ---------------------------------------------------------------------------
# Recognition helpers (used by the worker process; importable for reocr.py)
# ---------------------------------------------------------------------------

def load_capture_image(row) -> np.ndarray | None:
    from PIL import Image
    path = config.CAPTURE_DIR / os.path.basename(row["filename"])
    try:
        with Image.open(path) as img:
            return np.asarray(img.convert("RGB"))
    except Exception as exc:
        print(f"[ocr] could not load {path.name}: {exc}", flush=True)
        return None


def recognize_in_roi(recognizer, motion: MotionDetector,
                     image_rgb: np.ndarray) -> list[BibDetection]:
    """Run bib recognition cropped to the motion ROI (+padding); bboxes are
    mapped back to full-frame coordinates."""
    if not config.OCR_USE_ROI:
        return recognizer.recognize(image_rgb)
    h, w = image_rgb.shape[:2]
    roi = motion.get_roi()
    rx, ry, rw, rh = motion.roi_to_pixels(roi, w, h)
    pad_x = int(rw * config.OCR_ROI_PADDING)
    pad_y = int(rh * config.OCR_ROI_PADDING)
    x0, y0 = max(0, rx - pad_x), max(0, ry - pad_y)
    x1, y1 = min(w, rx + rw + pad_x), min(h, ry + rh + pad_y)
    crop = np.ascontiguousarray(image_rgb[y0:y1, x0:x1])
    if crop.size == 0:
        return recognizer.recognize(image_rgb)
    detections = recognizer.recognize(crop)
    for det in detections:
        bx, by, bw, bh = det.bbox
        det.bbox = (bx + x0, by + y0, bw, bh)
    return detections


def gate_by_motion(detections: list[BibDetection], row) -> list[BibDetection]:
    """Keep only reads whose centre lies within the frame's recorded motion
    region (expanded by a margin).  No-op if gating is off / no region."""
    if not config.OCR_MOTION_GATE:
        return detections
    mx, my, mw, mh = (row["motion_x"], row["motion_y"], row["motion_w"], row["motion_h"])
    if None in (mx, my, mw, mh):
        return detections
    W, H = row["width"], row["height"]
    ex = mw * W * config.OCR_MOTION_GATE_MARGIN
    ey = mh * H * config.OCR_MOTION_GATE_MARGIN
    gx0, gy0 = mx * W - ex, my * H - ey
    gx1, gy1 = (mx + mw) * W + ex, (my + mh) * H + ey
    kept = []
    for det in detections:
        bx, by, bw, bh = det.bbox
        cx, cy = bx + bw / 2, by + bh / 2
        if gx0 <= cx <= gx1 and gy0 <= cy <= gy1:
            kept.append(det)
        else:
            print(f"[ocr] gated out bib {det.bib_number} @ ({int(cx)},{int(cy)}) "
                  f"- outside motion region", flush=True)
    return kept


def process_capture(recognizer, motion: MotionDetector, capture_id: int) -> list[str]:
    """Read one still and store its detections.  Returns the bibs found."""
    row = database.fetch_capture(capture_id)
    if row is None:
        return []                               # deleted while queued
    image_rgb = load_capture_image(row)
    detections = []
    if image_rgb is not None:
        try:
            detections = recognize_in_roi(recognizer, motion, image_rgb)
        except Exception as exc:
            print(f"[ocr] recognition failed: {exc}", flush=True)
    if detections:
        detections = gate_by_motion(detections, row)
    try:
        if detections:
            for det in detections:
                database.insert_detection(capture_id=capture_id,
                                          bib_number=det.bib_number,
                                          confidence=det.confidence, bbox=det.bbox)
            return [d.bib_number for d in detections]
        database.insert_detection(capture_id=capture_id, bib_number=None,
                                  confidence=None, bbox=None)
    except Exception as exc:
        print(f"[ocr] could not store result for capture {capture_id}: {exc}", flush=True)
    return []


# ---------------------------------------------------------------------------
# The worker process
# ---------------------------------------------------------------------------

def _worker_main(req_q, res_q, path_overrides: dict):
    """Entry point of the OCR worker process."""
    for k, v in path_overrides.items():
        setattr(config, k, Path(v))
    try:
        os.nice(10)                     # camera + encoder come first
    except Exception:
        pass
    try:
        import prctl, signal
        prctl.set_pdeathsig(signal.SIGTERM)   # die with the parent
    except Exception:
        pass
    parent = os.getppid()

    from .bib_recognizer import create_recognizer
    try:
        recognizer = create_recognizer()
    except Exception as exc:
        res_q.put(("fatal", f"could not create recognizer: {exc}"))
        return
    motion = MotionDetector()
    res_q.put(("ready", type(recognizer).__name__))

    while True:
        try:
            cid = req_q.get(timeout=2.0)
        except queue.Empty:
            if os.getppid() != parent:
                break
            continue
        except (EOFError, OSError):
            break
        if cid is None:
            break
        t0 = time.time()
        try:
            reload = getattr(recognizer, "reload_state", None)
            if reload:
                reload()
            motion.reload_roi()
            bibs = process_capture(recognizer, motion, int(cid))
        except Exception as exc:
            print(f"[ocr] worker error on capture {cid}: {exc}", flush=True)
            bibs = []
        res_q.put(("done", int(cid), bibs, time.time() - t0))


# ---------------------------------------------------------------------------
# Parent-side service
# ---------------------------------------------------------------------------

class OcrService:
    def __init__(self, motion: MotionDetector, settings: Settings,
                 recording_active=lambda: False, enabled: bool = True):
        self._motion = motion
        self._settings = settings
        self._recording_active = recording_active
        self._enabled = enabled

        self._backlog: deque[int] = deque()
        self._cond = threading.Condition()
        self._running = False
        self._user_paused = False

        self._ctx = mp.get_context("spawn")
        self._req_q = self._ctx.Queue()
        self._res_q = self._ctx.Queue()
        self._proc = None
        self._backend_name = "starting" if enabled else "NullBackend"
        self._ready = not enabled
        self._busy = False
        self._current: int | None = None
        self._started_at = 0.0
        self._restarts = 0
        self._fatal: str | None = None

        self._process_times: deque[float] = deque()
        self._rate_lock = threading.Lock()
        self._last_duration = 0.0
        self._threads: list[threading.Thread] = []

    # -- lifecycle ----------------------------------------------------------

    def start(self):
        config.CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
        self._running = True
        pending = database.fetch_unprocessed_capture_ids()
        if pending:
            print(f"[ocr] re-queueing {len(pending)} unprocessed captures")
            with self._cond:
                self._backlog.extend(pending)
                self._cond.notify()
        if self._enabled:
            self._spawn()
            t = threading.Thread(target=self._drain_loop, name="ocr-drain", daemon=True)
            t.start()
            self._threads.append(t)
        t = threading.Thread(target=self._feed_loop, name="ocr-feed", daemon=True)
        t.start()
        self._threads.append(t)

    def _spawn(self):
        overrides = {k: str(getattr(config, k)) for k in _PATH_KEYS}
        self._proc = self._ctx.Process(target=_worker_main,
                                       args=(self._req_q, self._res_q, overrides),
                                       name="ocr-worker", daemon=True)
        self._proc.start()
        self._ready = False
        print(f"[ocr] worker process started (pid {self._proc.pid})")

    def stop(self):
        self._running = False
        with self._cond:
            self._cond.notify_all()
        if self._proc is not None and self._proc.is_alive():
            try:
                self._req_q.put(None)
                self._proc.join(timeout=3.0)
            except Exception:
                pass
            if self._proc.is_alive():
                self._proc.terminate()
        for t in self._threads:
            t.join(timeout=2.0)

    # -- state for the UI ---------------------------------------------------

    def backlog_size(self) -> int:
        with self._cond:
            return len(self._backlog) + (1 if self._busy else 0)

    def is_paused(self) -> bool:
        return self._user_paused

    def set_paused(self, paused: bool) -> bool:
        self._user_paused = bool(paused)
        with self._cond:
            self._cond.notify_all()
        print(f"[ocr] worker {'PAUSED' if self._user_paused else 'RUNNING'} (by user)")
        return self._user_paused

    def _effectively_paused(self) -> bool:
        if self._user_paused:
            return True
        return bool(self._recording_active()
                    and not self._settings.get("ocr_while_recording"))

    def status(self) -> dict:
        blocked = self._effectively_paused()
        reason = None
        if self._fatal:
            reason = self._fatal
        elif self._user_paused:
            reason = "paused by user"
        elif blocked:
            reason = "waiting: recording in progress"
        elif self._enabled and not self._ready:
            reason = "OCR engine loading"
        return {"backend": self._backend_name,
                "backlog": self.backlog_size(),
                "paused": self._user_paused,
                "blocked": blocked or (self._enabled and not self._ready),
                "blocked_reason": reason,
                "processing": self._current,
                "processing_for": round(time.time() - self._started_at, 1) if self._busy else 0.0,
                "process_per_min": self._rate_per_min(),
                "last_duration_s": round(self._last_duration, 1)}

    def _rate_per_min(self, window: float = 120.0) -> float:
        cutoff = time.time() - window
        with self._rate_lock:
            while self._process_times and self._process_times[0] < cutoff:
                self._process_times.popleft()
            n = len(self._process_times)
        return round(n * 60.0 / window, 1)

    # -- OCR options (settable from the web UI; the worker re-reads the file) --

    def get_panel_proposals(self) -> bool:
        return bool(_load_ocr_state()["panel_proposals"])

    def set_panel_proposals(self, on: bool) -> bool:
        state = _load_ocr_state()
        state["panel_proposals"] = bool(on)
        _save_ocr_state(state)
        print(f"[ocr] panel proposals {'ENABLED' if on else 'DISABLED'}")
        return bool(on)

    def get_detect_width(self) -> int:
        return int(_load_ocr_state()["detect_width"])

    def set_detect_width(self, width: int) -> int:
        state = _load_ocr_state()
        state["detect_width"] = int(min(max(int(width), 320), 4608))
        _save_ocr_state(state)
        print(f"[ocr] detect width set to {state['detect_width']}px")
        return state["detect_width"]

    def get_ocr_options(self) -> dict:
        return {"panel_proposals": self.get_panel_proposals(),
                "detect_width": self.get_detect_width()}

    # -- saving stills ------------------------------------------------------

    def enqueue(self, capture_id: int):
        with self._cond:
            self._backlog.append(capture_id)
            self._cond.notify()

    def save_capture_yuv(self, tight_yuv: np.ndarray, width: int, height: int,
                         epoch: float, motion_score: float = 0.0,
                         motion_bbox=None, recording_id: int | None = None,
                         source: str = "snapshot",
                         event_id: int | None = None) -> int | None:
        """Write a YUV420 frame as a JPEG still, record it and queue it for OCR."""
        try:
            jpeg = encode_yuv420_jpeg(tight_yuv, width, height, config.JPEG_QUALITY)
        except Exception as exc:
            print(f"[ocr] could not encode still: {exc}")
            return None
        return self._store_jpeg(jpeg, width, height, epoch, motion_score,
                                motion_bbox, recording_id, source, event_id)

    def _store_jpeg(self, jpeg: bytes, width, height, epoch, motion_score,
                    motion_bbox, recording_id, source, event_id) -> int | None:
        dt = datetime.fromtimestamp(epoch)
        timestamp_iso = dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d}"
        stem = dt.strftime("cap_%Y%m%d_%H%M%S_") + f"{dt.microsecond // 1000:03d}"
        filename = stem + ".jpg"
        path = config.CAPTURE_DIR / filename
        n = 1
        while path.exists():                    # two frames in the same ms
            n += 1
            filename = f"{stem}_{n}.jpg"
            path = config.CAPTURE_DIR / filename
        try:
            with open(path, "wb") as fh:
                fh.write(jpeg)
        except Exception as exc:
            print(f"[ocr] failed to save still: {exc}")
            return None
        capture_id = database.insert_capture(
            filename=filename, timestamp_iso=timestamp_iso, epoch=epoch,
            width=width, height=height, motion_score=motion_score or 0.0,
            event_id=event_id, motion_bbox=tuple(motion_bbox) if motion_bbox else None,
            recording_id=recording_id, source=source)
        self.enqueue(capture_id)
        return capture_id

    # -- feeder: hands one id at a time to the worker ----------------------------

    def _feed_loop(self):
        while self._running:
            with self._cond:
                if (not self._backlog or self._busy or not self._ready
                        or self._effectively_paused() or self._fatal):
                    self._cond.wait(timeout=0.5)
                    continue
                cid = self._backlog.pop()          # newest first
                remaining = len(self._backlog)
                self._busy = True
                self._current = cid
                self._started_at = time.time()
            if not self._enabled:
                # No engine: file an empty row so the still shows up and is
                # never re-queued.
                try:
                    if database.fetch_capture(cid) is not None:
                        database.insert_detection(capture_id=cid, bib_number=None,
                                                  confidence=None, bbox=None)
                except Exception as exc:
                    print(f"[ocr] could not store empty result: {exc}")
                self._finish(cid, [], 0.0, remaining)
                continue
            try:
                self._req_q.put(cid)
            except Exception as exc:
                print(f"[ocr] could not hand capture {cid} to worker: {exc}")
                with self._cond:
                    self._backlog.append(cid)
                    self._busy = False
                    self._current = None
                time.sleep(1.0)

    def _finish(self, cid: int, bibs: list, duration: float, remaining: int):
        with self._cond:
            self._busy = False
            self._current = None
            self._cond.notify()
        self._last_duration = duration
        with self._rate_lock:
            self._process_times.append(time.time())
        if bibs:
            print(f"[ocr] capture {cid}: bibs {bibs} (backlog {remaining})")
        else:
            print(f"[ocr] capture {cid}: no bib detected (backlog {remaining})")

    # -- drain: results + worker supervision -------------------------------------

    def _drain_loop(self):
        while self._running:
            try:
                msg = self._res_q.get(timeout=0.5)
            except queue.Empty:
                self._check_worker()
                continue
            except (EOFError, OSError):
                self._check_worker()
                time.sleep(0.5)
                continue
            kind = msg[0]
            if kind == "ready":
                self._backend_name = msg[1]
                self._ready = True
                print(f"[ocr] worker ready ({self._backend_name})")
                with self._cond:
                    self._cond.notify()
            elif kind == "done":
                _, cid, bibs, dur = msg
                with self._cond:
                    remaining = len(self._backlog)
                self._finish(cid, bibs, dur, remaining)
            elif kind == "fatal":
                self._fatal = msg[1]
                print(f"[ocr] worker fatal: {msg[1]}")

    def _check_worker(self):
        if self._proc is None or self._proc.is_alive() or not self._running or self._fatal:
            return
        code = self._proc.exitcode
        print(f"[ocr] worker process died (exit {code})")
        with self._cond:
            if self._busy and self._current is not None:
                self._backlog.append(self._current)    # retry that image
            self._busy = False
            self._current = None
        self._restarts += 1
        if self._restarts > 5:
            self._fatal = "OCR worker keeps crashing - see the log"
            return
        time.sleep(2.0)
        self._req_q = self._ctx.Queue()
        self._res_q = self._ctx.Queue()
        self._spawn()
