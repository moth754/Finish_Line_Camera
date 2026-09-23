"""
extractor.py
============

Turns recorded video back into stills for OCR.

Two entry points:

  * Jobs ("send this time range to OCR") - the Extractor's worker thread runs
    `ocr_jobs` rows one at a time.  For each segment that overlaps the range it
    reads the sidecar (exact per-frame epochs + motion scores recorded live),
    picks candidate frames (inside the range; with motion in the ROI if asked;
    at most `max_fps` per second), decodes just those stretches of the MP4
    with PyAV (seeking to the keyframe before each wanted frame), drops
    near-duplicates, and hands every kept frame to OcrService.save_capture_yuv
    which files it under the event and queues it for recognition.

  * `decode_frame_at(recording, epoch)` - one exact frame for the playback
    "frame at time t" view and the "OCR this frame" button.

Decode cost on the Pi 5 is ~250 fps at 2304x1296, so an hour of continuous
motion is ~10 minutes of decoding; quiet stretches are skipped entirely.
"""

from __future__ import annotations

import bisect
import json
import os
import threading
import time
from fractions import Fraction
from functools import lru_cache
from pathlib import Path

import numpy as np

from . import config, database
from .motion_detector import MotionDetector
from .ocr_service import OcrService
from .recorder import load_sidecar, segment_sidecar_path
from .settings import Settings


# ---------------------------------------------------------------------------
# Sidecar / decoding helpers
# ---------------------------------------------------------------------------

@lru_cache(maxsize=64)
def _cached_sidecar(path: str, mtime: float) -> dict | None:
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return None


def sidecar_for(segment: dict) -> dict | None:
    path = segment_sidecar_path(config.RECORDINGS_DIR / os.path.basename(segment["dirname"])
                                / os.path.basename(segment["filename"]))
    try:
        return _cached_sidecar(str(path), path.stat().st_mtime)
    except FileNotFoundError:
        return None


def segment_path(segment: dict) -> Path:
    return (config.RECORDINGS_DIR / os.path.basename(segment["dirname"])
            / os.path.basename(segment["filename"]))


def frame_epochs(sidecar: dict) -> list[float]:
    start = sidecar["start_epoch"]
    return [start + ms / 1000.0 for ms in sidecar["frame_ms"]]


def _open_video(path: Path):
    import av
    container = av.open(str(path))
    stream = container.streams.video[0]
    stream.thread_type = "AUTO"
    return container, stream


def _frame_index(frame, stream, fps: int) -> int:
    """Frame number from a decoded frame's pts (segments start at pts 0)."""
    if frame.pts is None:
        return -1
    return int(round(float(frame.pts * stream.time_base) * fps))


def _seek_to_frame(container, stream, idx: int, fps: int):
    ts = int(Fraction(idx, fps) / stream.time_base)
    container.seek(ts, stream=stream, backward=True, any_frame=False)


def decode_frames(segment: dict, wanted: list[int], fps: int, on_frame,
                  should_stop=lambda: False):
    """Decode the `wanted` (sorted) frame indices of a segment.

    Calls on_frame(idx, av_frame) for each.  Seeks over gaps longer than a
    couple of GOPs instead of decoding through them.
    """
    if not wanted:
        return
    container, stream = _open_video(segment_path(segment))
    try:
        wanted_set = set(wanted)
        pos = 0
        gop = fps * config.KEYFRAME_SECONDS
        _seek_to_frame(container, stream, wanted[0], fps)
        for frame in container.decode(stream):
            if should_stop():
                return
            idx = _frame_index(frame, stream, fps)
            # Skip forward past wanted frames we have already passed.
            while pos < len(wanted) and wanted[pos] < idx:
                pos += 1
            if pos >= len(wanted):
                return
            if idx in wanted_set:
                on_frame(idx, frame)
                pos += 1
                if pos >= len(wanted):
                    return
            # Big gap ahead: jump to the keyframe before the next wanted frame.
            if wanted[pos] - idx > 2 * gop:
                _seek_to_frame(container, stream, wanted[pos], fps)
    finally:
        container.close()


def decode_frame_at(recording: dict, epoch: float):
    """Decode the frame closest to `epoch`.

    Returns (tight_yuv420, width, height, frame_epoch, motion_score,
    motion_bbox, segment) or None if the time isn't covered by video.
    """
    segs = database.segments_in_range(recording["id"], epoch - 0.5, epoch + 0.5)
    best = None
    for seg in segs:
        seg["dirname"] = recording["dirname"]
        sc = sidecar_for(seg)
        if not sc:
            continue
        epochs = frame_epochs(sc)
        i = bisect.bisect_left(epochs, epoch)
        cands = [j for j in (i - 1, i) if 0 <= j < len(epochs)]
        for j in cands:
            d = abs(epochs[j] - epoch)
            if best is None or d < best[0]:
                best = (d, seg, sc, j, epochs[j])
    if best is None:
        return None
    _, seg, sc, idx, fepoch = best
    result = {}

    def grab(i, frame):
        result["yuv"] = frame.to_ndarray(format="yuv420p")
        result["w"], result["h"] = frame.width, frame.height

    decode_frames(seg, [idx], int(sc["fps"]), grab)
    if "yuv" not in result:
        return None
    return (result["yuv"], result["w"], result["h"], fepoch,
            sc["motion"][idx] if idx < len(sc["motion"]) else 0.0,
            sc["bbox"][idx] if idx < len(sc["bbox"]) else None, seg)


# ---------------------------------------------------------------------------
# The job worker
# ---------------------------------------------------------------------------

class Extractor:
    def __init__(self, ocr: OcrService, motion: MotionDetector, settings: Settings):
        self._ocr = ocr
        self._motion = motion
        self._settings = settings
        self._running = False
        self._thread: threading.Thread | None = None
        self._wake = threading.Event()
        self._cancel_ids: set[int] = set()
        self._current_job: int | None = None

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="extractor", daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    # -- job API --------------------------------------------------------------

    def submit(self, recording_id: int, t_start: float, t_end: float,
               options: dict | None = None) -> int:
        rec = database.get_recording(recording_id)
        if rec is None:
            raise ValueError("no such recording")
        if t_end <= t_start:
            raise ValueError("end must be after start")
        opts = {
            "motion_only": bool(self._settings.get("extract_motion_only")),
            "max_fps": float(self._settings.get("extract_max_fps")),
            "dedup": float(self._settings.get("extract_dedup")),
            "motion_threshold": float(self._motion.get_motion_settings()["area_threshold"]),
        }
        for k, v in (options or {}).items():
            if k in opts and v is not None:
                opts[k] = type(opts[k])(v)
        opts["max_fps"] = min(max(opts["max_fps"], 0.05), 200.0)
        opts["dedup"] = min(max(opts["dedup"], 0.0), 0.5)
        job_id = database.create_ocr_job(recording_id, rec.get("event_id"),
                                         t_start, t_end, opts)
        self._wake.set()
        print(f"[extract] job {job_id} queued: recording {recording_id}, "
              f"{t_end - t_start:.1f}s, {opts}")
        return job_id

    def cancel(self, job_id: int):
        job = database.get_ocr_job(job_id)
        if job is None:
            return
        if job["status"] == "pending":
            database.update_ocr_job(job_id, status="cancelled",
                                    finished_epoch=time.time())
        elif job["status"] == "running":
            self._cancel_ids.add(job_id)

    def status(self) -> dict:
        return {"current_job": self._current_job}

    # -- worker ---------------------------------------------------------------

    def _loop(self):
        while self._running:
            job = database.next_pending_ocr_job()
            if job is None:
                self._wake.wait(timeout=1.0)
                self._wake.clear()
                continue
            self._current_job = job["id"]
            try:
                self._run(job)
            except Exception as exc:
                print(f"[extract] job {job['id']} failed: {exc}")
                database.update_ocr_job(job["id"], status="failed", message=str(exc),
                                        finished_epoch=time.time())
            finally:
                self._current_job = None
                self._cancel_ids.discard(job["id"])

    def _run(self, job: dict):
        job_id = job["id"]
        opts = json.loads(job["options"])
        rec = database.get_recording(job["recording_id"])
        if rec is None:
            raise RuntimeError("recording no longer exists")
        t0 = float(job["t_start"])
        t1 = float(job["t_end"])
        if job.get("progress_epoch"):
            t0 = max(t0, float(job["progress_epoch"]))       # resume
        database.update_ocr_job(job_id, status="running", started_epoch=time.time(),
                                message=None)

        segs = database.segments_in_range(rec["id"], t0, t1)
        if not segs:
            database.update_ocr_job(job_id, status="failed", finished_epoch=time.time(),
                                    message="no video covers that time range (deleted?)")
            return

        motion_only = bool(opts.get("motion_only", True))
        thr = float(opts.get("motion_threshold", 0.02))
        min_gap = 1.0 / float(opts.get("max_fps", 2.0))
        dedup = float(opts.get("dedup", 0.0))

        # Pass 1: choose candidate frames from the sidecars (cheap, no decode).
        plan = []            # (segment, [indices])
        total = 0
        last_pick = -1e12
        for seg in segs:
            seg["dirname"] = rec["dirname"]
            sc = sidecar_for(seg)
            if not sc:
                continue
            epochs = frame_epochs(sc)
            motion = sc["motion"]
            picks = []
            for i, e in enumerate(epochs):
                if e < t0 or e > t1:
                    continue
                if motion_only and motion[i] < thr:
                    continue
                if e - last_pick < min_gap:
                    continue
                picks.append(i)
                last_pick = e
            if picks:
                plan.append((seg, sc, epochs, picks))
                total += len(picks)
        print(f"[extract] job {job_id}: {total} candidate frames in {len(plan)} segment(s)")
        if total == 0:
            database.update_ocr_job(job_id, status="done", progress=1.0,
                                    finished_epoch=time.time(),
                                    message="no frames matched (no motion in range?)")
            return

        # Pass 2: decode + save.
        state = {"scanned": 0, "kept": 0, "last_gray": None, "last_update": 0.0,
                 "cancelled": False}
        step = max(1, int(np.ceil(rec["width"] / config.MOTION_ANALYSIS_MAX_WIDTH)))

        def should_stop():
            if not self._running or job_id in self._cancel_ids:
                state["cancelled"] = True
                return True
            return False

        for seg, sc, epochs, picks in plan:
            fps = int(sc["fps"])

            def on_frame(idx, frame, seg=seg, sc=sc, epochs=epochs):
                state["scanned"] += 1
                yuv = frame.to_ndarray(format="yuv420p")
                w, h = frame.width, frame.height
                gray = yuv[:h:step, :w:step]
                if dedup > 0 and state["last_gray"] is not None \
                        and state["last_gray"].shape == gray.shape:
                    if self._roi_change(gray, state["last_gray"]) < dedup:
                        return
                state["last_gray"] = gray.copy()
                e = epochs[idx]
                cid = self._ocr.save_capture_yuv(
                    yuv, w, h, e, motion_score=sc["motion"][idx],
                    motion_bbox=sc["bbox"][idx], recording_id=rec["id"],
                    source="extract", event_id=job.get("event_id"))
                if cid is not None:
                    state["kept"] += 1
                now = time.time()
                if now - state["last_update"] > 1.0:
                    state["last_update"] = now
                    database.update_ocr_job(
                        job_id, progress=min(0.999, state["scanned"] / total),
                        frames_scanned=state["scanned"], frames_kept=state["kept"],
                        progress_epoch=e)

            decode_frames(seg, picks, fps, on_frame, should_stop)
            if state["cancelled"]:
                break

        if state["cancelled"]:
            database.update_ocr_job(job_id, status="cancelled", frames_scanned=state["scanned"],
                                    frames_kept=state["kept"], finished_epoch=time.time(),
                                    message=f"cancelled after {state['kept']} frames")
            print(f"[extract] job {job_id} cancelled")
            return
        database.update_ocr_job(job_id, status="done", progress=1.0,
                                frames_scanned=state["scanned"], frames_kept=state["kept"],
                                finished_epoch=time.time(),
                                message=f"{state['kept']} frames sent to OCR")
        print(f"[extract] job {job_id} done: {state['kept']} frames sent to OCR")

    def _roi_change(self, gray: np.ndarray, ref: np.ndarray) -> float:
        h, w = gray.shape[:2]
        x, y, rw, rh = MotionDetector.roi_to_pixels(self._motion.get_roi(), w, h)
        cur = gray[y:y + rh, x:x + rw].astype(np.int16)
        prev = ref[y:y + rh, x:x + rw].astype(np.int16)
        changed = np.abs(cur - prev) > config.MOTION_PIXEL_THRESHOLD
        return float(changed.mean()) if changed.size else 0.0
