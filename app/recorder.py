"""
recorder.py
===========

Continuous "mobile CCTV" recording of the camera's main stream.

How it works
------------
Press REC and the Recorder:

  1. creates a folder  recordings/rec_<timestamp>/  and a `recordings` row;
  2. launches ONE ffmpeg process that reads raw YUV420 frames on stdin,
     encodes them with libx264 (software - the Pi 5 has no hardware encoder)
     and writes fixed-length MP4 *segments* (seg_000000.mp4, seg_000001.mp4,
     ...) with its segment muxer.  Keyframes are forced on the segment
     boundaries, so every segment is independently playable, starts on a
     keyframe, and contains exactly segment_seconds * fps frames;
  3. registers itself as the camera's frame sink: every captured frame is
     written to ffmpeg's stdin (a memcpy into a pipe - cheap) and its exact
     sensor timestamp + motion score/bbox are appended to an in-memory log;
  4. a "finalizer" thread notices when ffmpeg closes a segment (the next one
     appears), counts its frames, slices the matching timestamps/motion out
     of the log into a JSON *sidecar* (seg_000000.json), records the segment
     in the DB (with a per-second motion summary for the timeline) and then
     enforces the storage cap by deleting the oldest segments on disk.

Why an external ffmpeg rather than an in-process encoder: if this Python
process dies (kill -9, crash, restart) ffmpeg sees EOF on its pipe, finishes
the current segment cleanly and exits - nothing already recorded is lost.
Only a power cut can lose the segment in progress (at most segment_seconds).

Timing: the MP4 is constant-frame-rate at the sensor's pinned rate.  If the
encoder ever can't keep up, the pipe fills, the camera thread blocks and
libcamera drops sensor frames - the video simply skips, and the sidecar's
per-frame epochs (which are what playback and OCR use for the finish time)
stay exact.  Dropped frames are counted and shown in the UI.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import numpy as np

from . import config, database
from .camera_manager import _BaseCamera
from .settings import Settings

SEGMENT_PATTERN = "seg_%06d.mp4"


def segment_sidecar_path(mp4_path: Path) -> Path:
    return mp4_path.with_suffix(".json")


def load_sidecar(rec_dirname: str, filename: str) -> dict | None:
    """Load a segment's sidecar {fps, start_epoch, frame_ms[], motion[], bbox[]}."""
    path = segment_sidecar_path(config.RECORDINGS_DIR / rec_dirname / filename)
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return None


def probe_frame_count(path: Path) -> int:
    """Number of video packets (= frames) in an MP4, read from its index."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-count_packets", "-select_streams", "v:0",
             "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30)
        return int(out.stdout.strip().split(",")[0])
    except Exception as exc:
        print(f"[recorder] ffprobe failed for {path.name}: {exc}")
        return 0


class Recorder:
    def __init__(self, camera: _BaseCamera, settings: Settings):
        self._camera = camera
        self._settings = settings
        self._lock = threading.RLock()

        self._active = False
        self._rec: dict | None = None          # current recording info
        self._proc: subprocess.Popen | None = None
        self._stdin = None
        self._error: str | None = None

        # Per-frame log for frames pushed to ffmpeg, indexed by (global frame
        # number - _log_base).  Trimmed as segments are finalised.
        self._log_epoch: list[float] = []
        self._log_motion: list[float] = []
        self._log_bbox: list = []
        self._log_base = 0

        self._frames = 0            # frames pushed to ffmpeg
        self._frames_closed = 0     # frames accounted for in closed segments
        self._dropped = 0           # sensor frames missed (from timestamp gaps)
        self._last_epoch: float | None = None
        self._push_times: deque[float] = deque()
        self._last_frame_epoch: float | None = None

        self._next_close_idx = 0
        self._close_retries = 0
        self._finalizer: threading.Thread | None = None
        self._bytes = 0
        self._segments = 0

        self._last_storage_check = 0.0

    # -- public state -------------------------------------------------------

    def is_recording(self) -> bool:
        return self._active

    def status(self) -> dict:
        with self._lock:
            rec = self._rec
            now = time.time()
            recent = [t for t in self._push_times if t > now - 2.0]
            return {
                "recording": self._active,
                "id": rec["id"] if rec else None,
                "name": rec["name"] if rec else None,
                "start_epoch": rec["start_epoch"] if rec else None,
                "elapsed": (now - rec["start_epoch"]) if (rec and self._active) else 0.0,
                "frames": self._frames,
                "dropped": self._dropped,
                "fps_target": rec["fps"] if rec else self._camera.fps,
                "fps_actual": round(len(recent) / 2.0, 1) if self._active else 0.0,
                "segments": self._segments,
                "bytes": self._bytes,
                "width": rec["width"] if rec else self._camera.record_size[0],
                "height": rec["height"] if rec else self._camera.record_size[1],
                "mode": rec["mode"] if rec else self._camera.mode_key,
                "error": self._error,
            }

    # -- start / stop -------------------------------------------------------

    def start(self, name: str | None = None) -> dict:
        with self._lock:
            if self._active:
                return self.status()
            self._error = None

            # Make room first (a ring buffer must never fail to start for lack
            # of space when there is old video that can go).
            self._enforce_storage(force=True)

            now = time.time()
            stamp = datetime.fromtimestamp(now)
            dirname = stamp.strftime("rec_%Y%m%d_%H%M%S")
            rec_dir = config.RECORDINGS_DIR / dirname
            rec_dir.mkdir(parents=True, exist_ok=True)

            width, height = self._camera.record_size
            fps = self._camera.fps
            crf = int(self._settings.get("record_crf"))
            seg_s = int(self._settings.get("segment_seconds"))
            name = (name or "").strip() or stamp.strftime("Recording %Y-%m-%d %H:%M:%S")
            event_id = database.get_current_event_id()

            cmd = [
                config.FFMPEG_BINARY, "-hide_banner", "-loglevel", "warning", "-y",
                "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{width}x{height}",
                "-r", str(fps), "-i", "pipe:0",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", str(crf),
                "-threads", str(config.X264_THREADS),
                "-g", str(fps * config.KEYFRAME_SECONDS), "-pix_fmt", "yuv420p",
                "-force_key_frames", f"expr:gte(t,n_forced*{seg_s})",
                "-fps_mode", "passthrough",
                "-f", "segment", "-segment_time", str(seg_s),
                "-segment_format", "mp4",
                "-segment_format_options", "movflags=+faststart",
                "-reset_timestamps", "1",
                str(rec_dir / SEGMENT_PATTERN),
            ]
            log_fh = open(rec_dir / "ffmpeg.log", "ab")
            try:
                self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                              stdout=log_fh, stderr=log_fh)
            except Exception as exc:
                log_fh.close()
                self._error = f"could not start ffmpeg: {exc}"
                print(f"[recorder] {self._error}")
                return self.status()
            log_fh.close()
            self._stdin = self._proc.stdin
            # A bigger pipe smooths brief encoder hiccups (Linux: F_SETPIPE_SZ).
            try:
                fcntl.fcntl(self._stdin.fileno(), 1031, 4 * 1024 * 1024)
            except Exception:
                pass

            rec_id = database.create_recording(
                event_id=event_id, name=name, dirname=dirname, start_epoch=now,
                width=width, height=height, fps=fps, crf=crf,
                mode=self._camera.mode_key)
            self._rec = {"id": rec_id, "name": name, "dirname": dirname,
                         "dir": rec_dir, "start_epoch": now, "width": width,
                         "height": height, "fps": fps, "seg_seconds": seg_s,
                         "event_id": event_id, "mode": self._camera.mode_key}

            self._log_epoch, self._log_motion, self._log_bbox = [], [], []
            self._log_base = 0
            self._frames = self._frames_closed = self._dropped = 0
            self._last_epoch = None
            self._push_times.clear()
            self._next_close_idx = 0
            self._bytes = 0
            self._segments = 0

            self._active = True
            self._camera.set_sink(self._on_frame)
            self._finalizer = threading.Thread(target=self._finalizer_loop,
                                               name="rec-finalizer", daemon=True)
            self._finalizer.start()
            print(f"[recorder] REC started: {dirname} {width}x{height}@{fps} "
                  f"crf {crf}, {seg_s}s segments")
            return self.status()

    def stop(self) -> dict:
        with self._lock:
            if not self._active and self._proc is None:
                return self.status()
            self._active = False
            self._camera.set_sink(None)
            proc, stdin = self._proc, self._stdin
        # Outside the lock: the finalizer needs it while we wait.
        if stdin is not None:
            try:
                stdin.close()
            except Exception:
                pass
        if proc is not None:
            try:
                proc.wait(timeout=90)
            except subprocess.TimeoutExpired:
                print("[recorder] ffmpeg did not exit; killing it")
                proc.kill()
        if self._finalizer is not None:
            self._finalizer.join(timeout=120)
        with self._lock:
            rec = self._rec
            if rec is not None:
                end = self._last_frame_epoch or time.time()
                database.finish_recording(rec["id"], end)
                print(f"[recorder] REC stopped: {rec['dirname']} - {self._segments} "
                      f"segments, {self._frames} frames, {self._dropped} dropped")
            self._proc = None
            self._stdin = None
            self._finalizer = None
            self._rec = None
            return self.status()

    def toggle(self) -> dict:
        return self.stop() if self._active else self.start()

    def _fail(self, message: str):
        """Called from the camera thread when ffmpeg goes away: stop cleanly."""
        print(f"[recorder] ERROR: {message}")
        self._error = message
        self._active = False
        self._camera.set_sink(None)
        threading.Thread(target=self.stop, name="rec-fail-stop", daemon=True).start()

    # -- frame sink (runs on the camera thread) ------------------------------

    def _on_frame(self, main_yuv: np.ndarray, epoch: float, seq: int, frame):
        if not self._active or self._stdin is None:
            return
        rec = self._rec
        # Dropped-frame accounting from sensor timestamp gaps.
        if self._last_epoch is not None and rec is not None:
            period = 1.0 / rec["fps"]
            gap = epoch - self._last_epoch
            if gap > 1.5 * period:
                self._dropped += max(0, int(round(gap / period)) - 1)
        self._last_epoch = epoch

        try:
            self._stdin.write(memoryview(main_yuv).cast("B"))
        except (BrokenPipeError, OSError, ValueError) as exc:
            if self._active:
                self._fail(f"ffmpeg pipe closed ({exc}) - see ffmpeg.log")
            return

        m = frame.motion
        self._log_epoch.append(epoch)
        self._log_motion.append(m.score if m else 0.0)
        self._log_bbox.append(list(m.motion_bbox) if (m and m.motion_bbox) else None)
        self._frames += 1
        self._last_frame_epoch = epoch
        now = time.time()
        self._push_times.append(now)
        while self._push_times and self._push_times[0] < now - 3.0:
            self._push_times.popleft()

    # -- segment finalisation (own thread) -----------------------------------

    def _finalizer_loop(self):
        while True:
            proc = self._proc
            running = self._active and proc is not None and proc.poll() is None
            try:
                self._scan_segments(final=not running)
            except Exception as exc:
                print(f"[recorder] finalizer error: {exc}")
            if not running:
                if proc is not None and proc.poll() is None:
                    # stop() is draining; wait for ffmpeg to finish, then sweep.
                    time.sleep(0.25)
                    continue
                if self._active and proc is not None and proc.poll() is not None:
                    self._fail(f"ffmpeg exited with code {proc.returncode} - see ffmpeg.log")
                break
            time.sleep(0.5)

    def _scan_segments(self, final: bool):
        rec = self._rec
        if rec is None:
            return
        files = sorted(rec["dir"].glob("seg_*.mp4"))
        if not files:
            return
        # Every segment except the newest is closed; when ffmpeg has exited,
        # the newest is closed too.
        closed = files if final else files[:-1]
        for path in closed:
            idx = int(path.stem.split("_")[1])
            if idx < self._next_close_idx:
                continue
            if self._close_segment(idx, path):
                self._next_close_idx = idx + 1
                self._close_retries = 0
            else:
                # ffprobe couldn't read it yet (still being finalised?) - retry
                # a few times before giving up on this one (file is kept).
                self._close_retries += 1
                if self._close_retries >= 5:
                    print(f"[recorder] giving up on {path.name}")
                    self._next_close_idx = idx + 1
                    self._close_retries = 0
                break

    def _close_segment(self, idx: int, path: Path) -> bool:
        rec = self._rec
        n = probe_frame_count(path)
        if n <= 0:
            print(f"[recorder] {path.name}: ffprobe found no frames yet "
                  f"({path.stat().st_size} bytes)")
            return False
        a = self._frames_closed - self._log_base
        b = a + n
        epochs = list(self._log_epoch[a:b])
        motion = list(self._log_motion[a:b])
        bboxes = list(self._log_bbox[a:b])
        if len(epochs) < n:
            # Shouldn't happen (frames are logged when written) - pad by fps.
            print(f"[recorder] {path.name}: log has {len(epochs)} of {n} frames; padding")
            period = 1.0 / rec["fps"]
            last = epochs[-1] if epochs else (self._last_frame_epoch or time.time())
            while len(epochs) < n:
                last += period
                epochs.append(last)
                motion.append(0.0)
                bboxes.append(None)

        start = epochs[0]
        end = epochs[-1] + 1.0 / rec["fps"]
        # Per-second motion summary (max score per second) for the timeline.
        n_bins = max(1, int(end - start) + 1)
        bins = [0.0] * n_bins
        for e, s in zip(epochs, motion):
            k = int(e - start)
            if 0 <= k < n_bins and s > bins[k]:
                bins[k] = s
        sidecar = {
            "recording_id": rec["id"], "idx": idx, "filename": path.name,
            "fps": rec["fps"], "n_frames": n, "start_epoch": start, "end_epoch": end,
            "frame_ms": [int(round((e - start) * 1000)) for e in epochs],
            "motion": [round(s, 4) for s in motion],
            "bbox": [[round(v, 4) for v in bb] if bb else None for bb in bboxes],
        }
        try:
            with open(segment_sidecar_path(path), "w") as fh:
                json.dump(sidecar, fh, separators=(",", ":"))
        except Exception as exc:
            print(f"[recorder] could not write sidecar for {path.name}: {exc}")

        nbytes = path.stat().st_size
        database.insert_segment(rec["id"], idx, path.name, start, end, n,
                                rec["fps"], nbytes, [round(v, 3) for v in bins])
        self._frames_closed += n
        self._segments += 1
        self._bytes += nbytes
        # Trim the in-memory log.
        del self._log_epoch[:b]
        del self._log_motion[:b]
        del self._log_bbox[:b]
        self._log_base += b
        print(f"[recorder] closed {path.name}: {n} frames, {nbytes // 1024} KB, "
              f"{end - start:.2f}s wall")
        self._enforce_storage()
        return True

    # -- storage ring buffer ---------------------------------------------------

    def _enforce_storage(self, force: bool = False):
        """Delete the oldest segments until under the cap / above min free."""
        now = time.time()
        if not force and now - self._last_storage_check < 5.0:
            return
        self._last_storage_check = now
        cap = float(self._settings.get("storage_cap_gb")) * 1e9
        min_free = float(self._settings.get("min_free_gb")) * 1e9
        config.RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        total = database.total_segment_bytes()
        free = shutil.disk_usage(config.RECORDINGS_DIR).free
        removed = 0
        while total > cap or free < min_free:
            olds = database.oldest_segments(limit=20)
            if not olds:
                break
            for seg in olds:
                if not (total > cap or free < min_free):
                    break
                self.delete_segment_files(seg["dirname"], seg["filename"])
                database.delete_segment(seg["id"])
                total -= seg["bytes"]
                free += seg["bytes"]
                removed += 1
        if removed:
            print(f"[recorder] storage cap: deleted {removed} oldest segment(s)")
            self._bytes = 0  # recount lazily via status of DB in the UI
        # Recordings that have lost every segment disappear too.
        for rec in database.recordings_without_segments():
            if self._rec is not None and rec["id"] == self._rec["id"]:
                continue
            self.delete_recording_files(rec["dirname"])
            database.delete_recording(rec["id"])

    @staticmethod
    def delete_segment_files(dirname: str, filename: str):
        base = config.RECORDINGS_DIR / os.path.basename(dirname)
        mp4 = base / os.path.basename(filename)
        for p in (mp4, segment_sidecar_path(mp4)):
            try:
                p.unlink(missing_ok=True)
            except Exception as exc:
                print(f"[recorder] could not delete {p}: {exc}")

    @staticmethod
    def delete_recording_files(dirname: str):
        d = config.RECORDINGS_DIR / os.path.basename(dirname)
        try:
            shutil.rmtree(d, ignore_errors=True)
        except Exception as exc:
            print(f"[recorder] could not delete {d}: {exc}")

    # -- disk summary for the UI ---------------------------------------------

    def storage_summary(self) -> dict:
        total, used, free = shutil.disk_usage(config.RECORDINGS_DIR
                                              if config.RECORDINGS_DIR.exists()
                                              else config.PROJECT_ROOT)
        return {"disk_total": total, "disk_used": used, "disk_free": free,
                "recordings_bytes": database.total_segment_bytes(),
                "cap_bytes": float(self._settings.get("storage_cap_gb")) * 1e9}
