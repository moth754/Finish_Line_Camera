"""
config.py
=========

Central configuration for the finish-line recorder.

Everything tunable lives here so that the rest of the code never contains
"magic numbers".  Static defaults only: anything the user can change from the
Settings tab is persisted by app/settings.py (data/settings.json) and merely
*seeded* from the DEFAULT_SETTINGS dict below.

The values are grouped by concern:
    * Paths / storage
    * Camera + recording (mobile-CCTV style continuous H.264 recording)
    * Motion detection
    * Bib-number recognition (OCR)
    * Web server
    * GPIO (future expansion)

Nothing in this file *does* anything - it only declares constants.
"""

from pathlib import Path

# ---------------------------------------------------------------------------
# Paths / storage
# ---------------------------------------------------------------------------

# Root of the project (the folder that contains this "app" package).
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Where JPEG stills live: snapshots and frames extracted from recordings for
# OCR.  One file per capture (these are what the OCR worker reads).
CAPTURE_DIR = PROJECT_ROOT / "captures"

# Where the continuous H.264 recordings go: one sub-folder per recording,
# holding 60-second (configurable) MP4 segments plus a small JSON "sidecar"
# per segment with the exact per-frame timestamps and motion scores.
RECORDINGS_DIR = PROJECT_ROOT / "recordings"

# Small amount of persistent state that is not media (SQLite DB + JSON files).
DATA_DIR = PROJECT_ROOT / "data"

# SQLite database: events, recordings, segments, captures, detections, jobs.
DATABASE_PATH = DATA_DIR / "race.db"

# User-changeable settings (Settings tab) are persisted here.
SETTINGS_PATH = DATA_DIR / "settings.json"

# The motion region-of-interest (ROI) is stored on disk so that it survives a
# restart.  It is written here as JSON whenever the user redraws it in the UI.
ROI_STATE_PATH = DATA_DIR / "roi.json"

# The focus setting chosen in the web UI is persisted here so it survives a
# restart (it overrides the CAMERA_AF_MODE / CAMERA_LENS_POSITION defaults).
FOCUS_STATE_PATH = DATA_DIR / "focus.json"

# The motion-sensitivity threshold chosen in the web UI is persisted here so it
# survives a restart (it overrides the MOTION_AREA_FRACTION default).
MOTION_STATE_PATH = DATA_DIR / "motion.json"

# Runtime-settable OCR options (panel proposals, detect width), persisted so a
# UI change survives a restart.  Seeds from the OCR_* constants below.
OCR_STATE_PATH = DATA_DIR / "ocr.json"


# ---------------------------------------------------------------------------
# Camera + recording
# ---------------------------------------------------------------------------

# USE_MOCK_CAMERA is the legacy master switch, kept so existing installs keep
# behaving as they did:
#   "auto"  -> the "camera_id" setting decides (this is what you want)
#   True    -> always use the synthetic mock camera (handy for development)
#   False   -> never use the mock; always pick a real camera
USE_MOCK_CAMERA = "auto"

# Which camera to record from is a user setting, not a constant - see
# DEFAULT_SETTINGS["camera_id"] below and app/cameras.py.  Three kinds exist:
#   "csi:0"             a Raspberry Pi camera on the ribbon cable (Picamera2)
#   "usb:/dev/video2"   any V4L2 camera: a USB webcam, a capture card
#   "mock"              the built-in synthetic scene
#   "auto"              Pi camera if present, else USB, else mock

# Image orientation for USB cameras.  Separate from the CAMERA_HFLIP/VFLIP pair
# below, which exist because this particular Camera Module 3 is mounted upside
# down; a webcam sitting on a tripod normally needs no flip at all.
USB_HFLIP = False
USB_VFLIP = False

# Recording modes.  The Raspberry Pi 5 has NO hardware video encoder, so H.264
# is encoded in software (libx264 "ultrafast", all four cores).  Measured on
# this Pi 5 with real finish-line content: ~78 fps at 2304x1296, ~19 fps at
# 4608x2592, ~160 fps at 1536x864.  The Camera Module 3 sensor itself caps
# 4608x2592 at 14 fps, 2304x1296 at 56 fps and 1536x864 at 120 fps.  The
# presets below stay inside both limits with headroom for the preview, motion
# detection and the web server.  Pick one in the Settings tab.
RECORD_MODES = {
    "2k50":   {"label": "2304×1296 @ 50 fps",
               "size": (2304, 1296), "fps": 50,
               "note": "Recommended: full field of view, 2×2 binned, ~3 cores"},
    "2k30":   {"label": "2304×1296 @ 30 fps",
               "size": (2304, 1296), "fps": 30,
               "note": "Same picture, lighter CPU / smaller files"},
    "full14": {"label": "4608×2592 @ 14 fps (full sensor)",
               "size": (4608, 2592), "fps": 14,
               "note": "Maximum detail for small/distant bibs; sensor limit is "
                       "14 fps; heaviest CPU; browser playback needs a capable laptop"},
    "hd60":   {"label": "1536×864 @ 60 fps",
               "size": (1536, 864), "fps": 60,
               "note": "Smoothest motion; reduced field of view (sensor crop)"},
    "hd100":  {"label": "1536×864 @ 100 fps",
               "size": (1536, 864), "fps": 100,
               "note": "Slow-motion timing; reduced field of view; large files"},
}

# Defaults for everything the Settings tab can change.  These are only used
# the first time (or for keys missing from data/settings.json).
DEFAULT_SETTINGS = {
    # Camera source
    "camera_id": "auto",            # "auto" | "csi:N" | "usb:/dev/videoN" | "mock"
    # Mode for a USB camera, as "FOURCC:WIDTHxHEIGHT@FPS".  Unlike the Pi
    # presets below, the real list is enumerated from the device itself
    # (app/v4l2.py), because a webcam can only offer what its firmware
    # supports.  This default is the one combination virtually every USB
    # camera can manage.
    "usb_mode": "MJPG:1280x720@30",
    # Recording
    "record_mode": "2k50",          # key into RECORD_MODES (Raspberry Pi camera)
    "record_crf": 20,               # libx264 constant quality: lower = better/bigger (14-30)
    "segment_seconds": 60,          # MP4 segment length; also the storage-cap granularity
    "storage_cap_gb": 100.0,        # ring buffer: oldest segments deleted beyond this
    "min_free_gb": 5.0,             # never let free disk space drop below this
    # Live preview (MJPEG to the browser)
    "preview_width": 1280,          # 640 / 960 / 1280 / 1920 (16:9)
    "preview_fps": 12,
    "preview_quality": 80,
    # OCR
    "ocr_while_recording": False,   # run the OCR worker even while recording (steals CPU)
    # Sending video to OCR (frame extraction defaults; per-job override in UI)
    "extract_motion_only": True,    # only frames with motion in the ROI
    "extract_max_fps": 2.0,         # keep at most this many frames per second
    "extract_dedup": 0.02,          # skip a frame if < this fraction of ROI changed (0 = off)
}

# Some Picamera2 / libcamera builds return the "main" stream with the red and
# blue colour channels swapped.  Only relevant to RGB captures (the mock camera
# and the snapshot path); recording uses YUV420 straight from the ISP.
CAMERA_RGB_SWAP = True

# Image orientation.  This particular Camera Module 3 reports a 180-degree
# mounting (Rotation: 180), so we flip both axes by default to produce an
# upright picture.  If your live view comes out upside-down, set BOTH of these
# to False; if it's mirrored, toggle just one.
CAMERA_HFLIP = True
CAMERA_VFLIP = True

# --- Focus (Camera Module 3 has motorised autofocus) ---
#   "continuous" - the camera continuously autofocuses.
#   "manual"     - lock focus at CAMERA_FOCUS_DISTANCE_M.  BEST for a finish
#                  line: set once, can never hunt or drift at the critical moment.
CAMERA_AF_MODE = "manual"

# Default focus distance, in METRES, used when CAMERA_AF_MODE == "manual".
# (0 = infinity.)  Changeable live from the UI; saved to data/focus.json.
CAMERA_FOCUS_DISTANCE_M = 6.0

# JPEG quality (0-100) for saved stills (snapshots + extracted frames).
JPEG_QUALITY = 90

# Where ffmpeg lives (used for H.264 encoding + segmenting).
FFMPEG_BINARY = "ffmpeg"

# libx264 threads.  4 = all cores; the capture loop, preview and web server
# share them but are light.  (Frame-threading, no zerolatency: ~5% faster.)
X264_THREADS = 4

# Keyframe interval in seconds.  Short = quicker seeking in the browser and
# cheaper single-frame extraction, slightly bigger files.
KEYFRAME_SECONDS = 2


# ---------------------------------------------------------------------------
# Motion detection
# ---------------------------------------------------------------------------

# Motion is analysed on a small greyscale copy of every frame (the preview
# stream's luminance plane, subsampled to at most this width).  Cheap enough
# to run at 50-100 fps alongside the encoder.
MOTION_ANALYSIS_MAX_WIDTH = 640

# The ROI is the rectangle within the frame that we watch for motion - e.g. the
# strip of track directly over the finish line.  Normalised coordinates
# (0.0-1.0), so it is independent of the camera resolution.
DEFAULT_ROI = {
    "x": 0.30,
    "y": 0.35,
    "w": 0.40,
    "h": 0.30,
}

# A pixel is considered "changed" between two frames if its greyscale value
# moves by more than this much (0-255).  Higher = less sensitive to noise.
MOTION_PIXEL_THRESHOLD = 25

# Fraction (0.0-1.0) of pixels inside the ROI that must change for the frame to
# count as "motion".  Higher = a bigger object is required.  Live-settable.
MOTION_AREA_FRACTION = 0.02


# ---------------------------------------------------------------------------
# Bib-number recognition (OCR)
# ---------------------------------------------------------------------------

# Which OCR backend to use:
#   "auto"      -> use Tesseract if available, else EasyOCR, else disabled
#   "tesseract" -> force Tesseract (needs the `tesseract-ocr` apt package)
#   "easyocr"   -> force EasyOCR (heavier; pulls in PyTorch)
#   "none"      -> disable OCR entirely (photos are still captured & stored)
# EasyOCR is used here: on real race bibs (number + event text + sponsor logos)
# Tesseract misreads badly, whereas EasyOCR's text-detection stage isolates the
# bib number reliably.  It is slower (see OCR_DETECT_WIDTH) but runs in a
# background backlog so it never holds up the actual photo capture.
OCR_BACKEND = "easyocr"

# --- Two-stage EasyOCR pipeline (see EasyOCRBackend) -----------------------
# Stage 1 finds WHERE text might be; stage 2 re-reads each region from the
# full-resolution photo, so small/low-contrast numbers still get a good read.

# Width the photo is downscaled to for the (slow) text-DETECTION stage.  Bigger
# finds smaller/further-away numbers but costs quadratically more time:
# ~9 s at 960, ~15 s at 1280, ~34 s at 1920 on the Pi 5.  Recognition happens
# in a background backlog that is allowed to run behind the captures, so a slow
# but thorough setting is fine ("instant" was explicitly not required).
OCR_DETECT_WIDTH = 1920

# CRAFT detector thresholds for stage 1.  Lower than EasyOCR's defaults so weak
# candidates (small, blurry, low-contrast numbers) still produce a region -
# stage 2's recognition pass is what decides whether a region really is a bib.
OCR_DETECT_LOW_TEXT = 0.3
OCR_DETECT_TEXT_THRESHOLD = 0.5

# Also propose bright rectangular panels (number boards / paper bibs) found by
# a cheap colour heuristic, in case the text detector misses them entirely.
# Wrong proposals only cost a fraction of a second each - the recognition pass
# simply reads nothing from them.
OCR_PANEL_PROPOSALS = True
OCR_PANEL_MAX_REGIONS = 10

# A detected text token is only accepted as a bib number if it is made of
# digits and its length is within this inclusive range.
# BIB_MIN_DIGITS = 2 filters out the most common OCR junk: a helmet vent, a
# logo letter or a pole occasionally reads as a convincing single digit, while
# genuine multi-digit numbers are almost never hallucinated.  Set it to 1 only
# for an event that really uses single-digit bib numbers.
BIB_MIN_DIGITS = 2
BIB_MAX_DIGITS = 5

# Detections below this confidence (0.0-1.0) are discarded.  Set to 0 to keep
# everything (useful while tuning).
# Raised from 0.40 -> 0.50 to trim low-confidence false reads (e.g. number
# plates / signage misread as a bib).  Kept conservative on purpose: this is a
# results system, so missing a genuine finisher is worse than an occasional
# junk number.  NOTE a threshold does NOT remove a *high*-confidence false read
# from a fixed object in frame (OCR scans the whole photo, not just the motion
# ROI) - for that, keep such objects out of shot or constrain OCR to the ROI.
OCR_MIN_CONFIDENCE = 0.50

# Restrict OCR to the motion ROI: crop each photo to the ROI (plus a small
# margin) before recognition instead of scanning the whole 4608x2592 frame.
# Benefits: OCR runs much faster (a far smaller image), reads better (the crop
# keeps full sensor resolution instead of being downscaled to OCR_DETECT_WIDTH),
# and fixed objects OUTSIDE the ROI - car number plates, car-park signage - can
# no longer be misread as bibs.  Detection bboxes are mapped back to full-frame
# coordinates so the stored x/y/w/h and the web overlay stay correct.
# NOTE this does NOT exclude a fixed object that sits INSIDE the ROI (e.g. a
# finish-line banner) - move the ROI off it for that.
OCR_USE_ROI = True
# Grow the ROI by this fraction of its width/height on each side before cropping,
# so a bib just outside the motion box is not clipped.  0.0 = exactly the ROI.
OCR_ROI_PADDING = 0.10

# Motion-gated detection: only accept a bib read if it sits near where motion
# actually happened in that frame.  A runner's bib is inside the region that
# moved; a FIXED object (a finish-line banner, a parked-car plate, signage) is
# not - so this rejects fixed-object false reads even when they fall inside the
# motion ROI.  The motion region is recorded per-capture (captures.motion_x/y/w/h,
# normalised) at capture time and re-used when the backlog OCR runs later.
# Captures with no recorded motion region (a forced /api/test_capture, a GPIO
# trigger, or pre-migration rows) are NOT gated - every read is kept.
OCR_MOTION_GATE = True
# Expand the motion region by this fraction of its OWN width/height on each side
# before testing whether a detection falls inside it.  Gives tolerance for the
# bib sitting at the edge of the moved area (torso vs swinging limbs) while still
# excluding a fixed object well outside it.
OCR_MOTION_GATE_MARGIN = 0.35

# OCR runs from a backlog of capture ids, newest first (so the most recent
# finisher shows up in the table quickly) and never drops work: anything OCR
# couldn't get to during a busy spell is simply processed later, including
# after a restart (unprocessed captures are re-queued at startup).

# Pre-process each photo before handing it to the OCR engine.  Bib numbers on a
# real outdoor course suffer from uneven lighting, motion blur and small text;
# greyscale + contrast normalisation + upscaling noticeably improves the read
# rate.  Uses OpenCV when available, otherwise a lighter NumPy/Pillow fallback.
# Turn off (False) if you want to feed the raw photo straight to OCR.
OCR_PREPROCESS = True

# Upscale factor applied during preprocessing.  Tesseract reads best when digits
# are reasonably tall; 2x is a good default for 1080p finish-line shots.  Larger
# = slower.  Ignored if OCR_PREPROCESS is False.
OCR_UPSCALE = 2.0

# Tesseract "page segmentation modes" to run and merge (Tesseract backend only).
# A finish-line photo has several bib numbers scattered around the frame, and no
# single PSM reliably finds them all:
#   11 = "sparse text"      - finds text anywhere, good for scattered bibs
#   12 = "sparse text +OSD" - similar, often catches ones PSM 11 misses
# We run both and merge, deduplicating by bib number (keeping the most confident
# read), which catches noticeably more bibs than any single mode.
# NOTE: we deliberately do NOT use tessedit_char_whitelist - it suppresses
# results with the LSTM engine in Tesseract 4/5; we filter to digits ourselves.
OCR_PSM_MODES = (11, 12)


# ---------------------------------------------------------------------------
# Web server
# ---------------------------------------------------------------------------

# Bind to 0.0.0.0 so the dashboard is reachable from other devices on the LAN.
WEB_HOST = "0.0.0.0"
WEB_PORT = 8000

# How many of the most recent detection rows the table endpoint returns.
TABLE_PAGE_SIZE = 200


# ---------------------------------------------------------------------------
# Shutdown (power the Pi off from the dashboard)
# ---------------------------------------------------------------------------

# Master switch for the dashboard shutdown button.  Set False to remove it.
SHUTDOWN_ENABLED = True

# Command used to power off.  Run with `sudo -n` (non-interactive): this needs a
# one-time sudoers rule allowing the service user to run it without a password -
# see finish-line-shutdown.sudoers / the README.  Change "-h" target to reboot
# if you ever want a reboot button instead.
SHUTDOWN_COMMAND = ["sudo", "-n", "/usr/sbin/shutdown", "-h", "now"]


# ---------------------------------------------------------------------------
# GPIO (future expansion - see gpio_controller.py)
# ---------------------------------------------------------------------------

# Master switch for GPIO.  Left False for now; flip to True once you wire up
# hardware and fill in the pin numbers below.
GPIO_ENABLED = False

# Example pin assignments for future use (BCM numbering).  These are referenced
# by gpio_controller.py and are safe to leave as-is until GPIO_ENABLED is True.
GPIO_CAPTURE_LED_PIN = 17      # lights briefly each time a photo is taken
GPIO_MOTION_LED_PIN = 27       # indicates live motion in the ROI
GPIO_EXTERNAL_TRIGGER_PIN = 22  # input: e.g. a beam-break sensor at the line
