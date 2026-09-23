# 🏁 Finish Line Recorder

A "mobile CCTV" finish-line camera for running / cycling races, built for a
**Raspberry Pi 5 + Camera Module 3** and equally happy on an ordinary Linux PC
with a **USB camera**.

Press **REC** and it records continuously (a loop with a storage cap), full
field of view at up to 50 fps.  Afterwards - or at any time during the race -
review the video on a big playback screen with a **motion timeline**, step
frame by frame, zoom in, and **send any time range to OCR** to read the **bib
numbers** in it.  Every bib read is stored with the exact **time the frame was
taken** and the OCR **confidence**, and can be exported as CSV.

Recording is deliberately decoupled from recognition: recording is cheap and
must never miss a finisher; OCR is slow and thorough and can run overnight in
a batch from the video.

---

## Features

- **Continuous recording** - one press starts a session; video is written as
  60-second MP4 segments (H.264) with an exact per-frame timestamp log, so a
  crash or restart never loses more than the segment in progress.
- **Storage cap (loop recording)** - set the maximum GB to use; the oldest
  segments are deleted automatically.  Stills and results are never deleted
  by the cap.
- **Resolution / frame-rate presets** - 2304×1296 @ 50 or 30 fps, full-sensor
  4608×2592 @ 14 fps, or 1536×864 @ 60 / 100 fps (see *Performance*).
- **Live tab** - big live preview; drag a box to set the **motion zone**;
  snapshot button (still → OCR).
- **Playback tab** - big video; timeline (**green** = video recorded, with the
  event's other recordings as clickable bands; **red** = motion in the zone); click to
  seek, wheel or − / + buttons to zoom the timeline (2 s to 12 h across the
  screen, *fit* = whole recording), drag to pan; play / pause, ±1 frame,
  ±1 s, ±10 s, 0.1×–8× speed; zoom & pan into the picture; the wall-clock time
  of the frame on screen; **IN / OUT** markers → *Send range to OCR* (motion-
  filtered, rate-limited, de-duplicated frames) with live job progress;
  **Send snapshot to OCR**.
- **Results tab** - one row per bib read, with confidence bar, time, source and
  a *▶ video* button that jumps to that moment; photo viewer; search, filter,
  CSV export, purge; OCR worker pause / resume and backlog.
- **Settings tab** - camera mode & quality, live preview size, focus, motion
  sensitivity, storage cap, OCR options, extraction defaults, events, restart
  and shutdown.
- **Two-stage EasyOCR bib reader** (unchanged from the photo version): text
  regions + bright number-board panels are located, then each is re-read from
  the full-resolution still through several colour-contrast views; reads are
  cropped to the motion zone and *motion-gated* (a fixed banner never moves).
- **OCR waits while recording** by default, so PyTorch never steals the CPU
  the software encoder needs.  A never-dropping backlog survives restarts.
- **Events** - recordings, stills and results are grouped per race / day.
- **Camera sources** - a Raspberry Pi camera on the ribbon cable, any USB /
  V4L2 camera (webcam, capture card), or the built-in mock. When more than one
  is connected, pick the one to use from a dropdown in Settings; the modes on
  offer are enumerated from the chosen device, so a webcam is only ever asked
  for resolutions and frame rates it can actually deliver.
- **Mock camera** - the whole app runs with no camera for development.

---

## How it fits together

```
 Camera Module 3 ──► picamera2 (YUV420: main 2304×1296 + preview 1280×720)
        │                        │
        │                        ├──► MJPEG preview ──► Live tab
        │                        └──► motion detector (zone) ─► per-frame motion log
        │
   REC on ──► main frames ──► ffmpeg / libx264 ──► recordings/rec_*/seg_000000.mp4
                                                      + seg_000000.json (frame times, motion)
                                                      + SQLite `segments` row (per-second motion)
 Playback tab ◄── segments + timeline ◄──────────────────┘
        │
        └─ "Send range to OCR" ──► extractor: pick frames (motion, max fps, dedup)
                                     ──► decode (PyAV) ──► captures/*.jpg + `captures` row
                                                              │
                                   OCR worker (EasyOCR) ◄─────┘  ──► `detections` rows ──► Results tab
```

---

## Project layout

```
finish_line_camera/
├── install.sh              # one-command install (Pi or any Linux PC)
├── run.py                  # entry point (--mock, --port, --root, --no-ocr)
├── requirements.txt
├── README.md
├── recordings/             # rec_<time>/seg_NNNNNN.mp4 + .json sidecars (runtime)
├── captures/               # JPEG stills for OCR: snapshots + extracted frames
├── data/                   # race.db, settings.json, roi/focus/motion/ocr state
└── app/
    ├── config.py           # static defaults (record modes, OCR tuning, paths)
    ├── settings.py         # persisted user settings (data/settings.json)
    ├── camera_manager.py   # Pi / USB / mock backends; YUV420 frames; motion in the loop
    ├── cameras.py          # which cameras exist, and which one to use
    ├── v4l2.py             # USB camera + mode enumeration (ioctl, no extra deps)
    ├── recorder.py         # ffmpeg segment pipeline, sidecars, storage cap
    ├── extractor.py        # video → stills jobs; exact-frame decode
    ├── ocr_service.py      # OCR backlog worker + still saving
    ├── bib_recognizer.py   # OCR backends (EasyOCR two-stage / Tesseract / none)
    ├── motion_detector.py  # NumPy frame differencing inside the zone
    ├── database.py         # SQLite: events, recordings, segments, captures,
    │                       #   detections, ocr_jobs
    ├── gpio_controller.py  # optional hardware button → REC toggle
    ├── web_app.py          # Flask routes + MJPEG stream
    ├── templates/index.html
    └── static/{style.css, app.js, live.js, playback.js, results.js, settings.js}
```

---

## Install

```bash
git clone https://github.com/<you>/finish_line_camera.git
cd finish_line_camera
./install.sh
```

That is the whole install on a Raspberry Pi *and* on an ordinary Linux PC.
The script works out which it is on and does the right thing: system packages,
a virtualenv, the Python dependencies, the runtime folders, a systemd unit that
starts on boot, and the one-line sudoers rule the dashboard's shutdown button
needs.  It is safe to re-run to upgrade an install, and it never touches
`recordings/`, `captures/` or `data/`.

When it finishes it prints the dashboard URL - open `http://<host-ip>:8000`.

### Options

| Flag | Effect |
|---|---|
| `--ocr tesseract` | use Tesseract instead of EasyOCR (no PyTorch, ~2 GB smaller, but noticeably worse on real bibs) |
| `--ocr none` | no OCR at all: recording, playback and the timeline only |
| `--port 8080` | serve the dashboard on another port |
| `--no-service` | don't install the systemd unit (run it by hand instead) |
| `--system-python` | install into the system Python rather than a virtualenv |
| `--skip-system-pkgs` | don't touch apt/dnf/pacman |
| `--user NAME` | run the service as a different user |
| `-y` | don't prompt |

`./install.sh --help` lists them all.

### What you get on each kind of machine

**Raspberry Pi 5 + Camera Module 3** - the full system: live preview, motion
zone, continuous recording, playback, OCR.  The camera stack (picamera2,
libcamera, PyAV, NumPy) is installed from apt on purpose, because picamera2 is
compiled against the system NumPy and a pip copy of it in the same environment
breaks the camera.  The virtualenv is created with `--system-site-packages` so
it can still see them.

**Any other Linux PC** (Debian, Ubuntu, Fedora, Arch, openSUSE) - the same
system, driven by a **USB camera**: any V4L2 device, so a webcam or an HDMI
capture card both work. Live capture, recording, the motion timeline, playback
and OCR all behave as they do on the Pi. There is no picamera2 backend off a
Pi, so the ribbon-cable camera is Pi-only.

With no camera attached the app falls back to the mock and says so in the
header, which still makes a laptop a useful second machine for a race:

- play back recordings copied over from the Pi, with the full motion timeline,
  frame stepping and zoom,
- *send range to OCR* and grind through a whole race far faster than the Pi can,
- the results table, photo viewer, search, filter and CSV export.

Copy a race across with:

```bash
rsync -a pi@<pi-address>:~/finish_line_camera/recordings/ ~/finish_line_camera/recordings/
```

### Manual install

If you would rather not run the script, `requirements.txt` documents the
package sets for both cases and `install.sh` is readable top to bottom.

### Running it by hand

```bash
.venv/bin/python run.py                                   # normal
.venv/bin/python run.py --mock --port 8001 --root /tmp/fl-test --no-ocr   # dev
```

---

## Choosing a camera

Settings -> **Camera** lists every source found: Pi cameras on the ribbon
cable, USB / V4L2 devices, and the mock. **Auto** (the default) picks a Pi
camera if one is attached, otherwise a USB camera, otherwise the mock, so an
existing install keeps behaving exactly as it did. **Rescan** re-checks the
devices without reloading the page.

Changing camera restarts the capture pipeline, so it is refused while
recording. Press **Apply camera settings** to make the change live.

The **Mode** dropdown follows the camera. A Pi camera offers the presets in
`config.RECORD_MODES`; a USB camera offers what the device itself reports
through V4L2, so you are never offered a resolution or frame rate it cannot
produce. Whatever the driver actually grants on open is what gets used and
recorded, even if it differs from what was asked for.

### One caveat that matters for timing

A Pi camera stamps each frame with the sensor's own exposure clock, so a
recorded finish time is the moment the shutter opened.

A USB camera gives no such clock, so a frame is stamped when it arrives at the
application, after USB transfer and MJPEG decode. Expect a consistent offset of
roughly one frame period. For placing finishers in order this makes no
difference; for absolute times against an external clock, it does.

USB cameras also have no controllable focus, so the Focus panel dims out when
one is selected.

---

## Race day

1. **Live tab**: aim the camera, set focus (Settings → Focus), drag the motion
   zone over the finish line.  The zone drives the timeline's motion bars and
   which frames get sent to OCR - keep it tight on where riders/runners cross.
2. Check the event in the header (or create a new one).
3. Press **● REC**.  The header shows elapsed time, actual / target fps, dropped
   frames and segment count.  Leave it running.
4. During the race: **Playback tab** → pick the live recording, click the
   timeline to review a finish, use **[ IN** / **OUT ]** and *Send to OCR* for
   a quick read, or **Send snapshot to OCR**.
5. After the race: press **■ STOP**.  The OCR worker (paused during recording)
   starts reading whatever has been queued.  Send the whole race - or just the
   finish windows - to OCR and leave it overnight.  Results tab → **Export CSV**.

Frame times shown in Playback and stored with results come from the camera's
sensor timestamp of each frame, not from the video's playback position, so
they stay exact even if frames were dropped.

---

## Performance (Raspberry Pi 5)

The Pi 5 has **no hardware video encoder**; H.264 is encoded by libx264 in
software on all four cores.  Measured with real finish-line footage:

| Preset | Sensor limit | Encoder | Verdict |
|---|---|---|---|
| 2304×1296 @ 50 fps | 56 fps | ~78 fps | **Recommended.** ~65% CPU, steady 50/50 |
| 2304×1296 @ 30 fps | 56 fps | ~78 fps | lighter, smaller files |
| 4608×2592 @ 14 fps | 14 fps | ~19 fps | max detail; ~75% CPU; browser playback of a 12 MP stream needs a capable laptop |
| 1536×864 @ 60 / 100 fps | 120 fps | ~160 fps | smoothest; reduced field of view |

File size at CRF 20 and 2304×1296@50: a static scene ~65 MB/min (≈4 GB/h),
a busy scene ~150 MB/min (≈9 GB/h).  Raise CRF to 23 for ~30 % smaller files.

**Keep "OCR while recording" off** (the default).  PyTorch would take the CPU
the encoder needs and frames would be dropped.  The OCR worker also runs at a
lower CPU priority than the camera thread.

---

## Configuration

Everything the Settings tab changes is saved in `data/settings.json`.  Static
tuning (OCR thresholds, detection sizes, record-mode presets, key-frame
interval, paths) lives in `app/config.py` with comments.

Useful `config.py` knobs:
- `RECORD_MODES` - the presets offered in Settings.
- `KEYFRAME_SECONDS` (2) - seek granularity in the browser / single-frame decode cost.
- `OCR_*` / `BIB_*` - recognition tuning (see comments; unchanged from the photo version).

---

## Re-running OCR on a past event

`reocr.py` re-runs the current recognizer over an event's stills that have no
bib match: `python3 reocr.py --event NAME` (see `--help`).  To re-read a time
range from the *video* instead, use Playback → *Send range to OCR* again;
each run adds new stills (purge the old ones first if you want a clean slate).

---

## Service (systemd)

`install.sh` generates `/etc/systemd/system/finish-line.service` for the user,
paths, Python and port it installed with, and enables it at boot with
`Restart=on-failure`.

```bash
journalctl -u finish-line.service -f      # follow the logs
sudo systemctl restart finish-line        # restart
sudo systemctl disable --now finish-line  # stop and remove from boot
```

The Settings tab's **Restart app** button exits with a non-zero code so systemd
relaunches it; a recording in progress is stopped cleanly first (ffmpeg always
finalises the current segment on exit), which is why the unit allows 30 seconds
to shut down.

The **Shut down** button needs a one-line sudoers rule; `install.sh` writes and
validates it.  `finish-line.service` and `finish-line-shutdown.sudoers` in the
repo are the hand-written originals from the first Pi, kept for reference.

---

## Camera not detected?

The app falls back to the mock camera and says so in the header ("mock
camera").  Settings -> **Camera** lists everything found; press **Rescan** if
the camera was plugged in after the page loaded.

For a **USB camera**, in order of likelihood:

1. The service user is not in the `video` group, so `/dev/video*` cannot be
   opened.  `install.sh` fixes this; after it runs the first time you need to
   log out and back in, or restart the service.
2. OpenCV is missing.  `pip install opencv-python-headless`, or
   `sudo apt install python3-opencv`.
3. Another program already has the camera open. Only one at a time.
4. The camera really does not offer a format we can decode.  `v4l2-ctl
   --list-formats-ext -d /dev/videoN` shows what it does offer; MJPG or YUYV
   is what you want to see.

For a **Pi camera**, check `rpicam-hello --list-cameras`, the ribbon cable, and
`journalctl -u finish-line.service`.

## GPIO (optional)

`gpio_controller.py` is a stub: with `GPIO_ENABLED = True` and a button on
`GPIO_EXTERNAL_TRIGGER_PIN`, a press toggles recording.
