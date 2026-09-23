#!/usr/bin/env python3
"""
run.py
======

Entry point for the finish-line recorder.  Wires the components together,
starts the background services and runs the Flask web server.

    python3 run.py                      # normal: real camera (mock if absent), :8000
    python3 run.py --mock --port 8001   # synthetic camera on another port
    python3 run.py --root /tmp/fl-test  # keep DB / captures / recordings elsewhere
    python3 run.py --no-ocr             # skip loading EasyOCR (fast start, tests)

Startup order matters and is commented inline.  Shutdown (Ctrl-C / SIGTERM)
stops any recording cleanly first (ffmpeg finalises the last segment).
"""

from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path


def _apply_root(root: str):
    """Point every data path under a different folder (for isolated testing)."""
    from app import config
    base = Path(root).expanduser().resolve()
    config.DATA_DIR = base / "data"
    config.CAPTURE_DIR = base / "captures"
    config.RECORDINGS_DIR = base / "recordings"
    config.DATABASE_PATH = config.DATA_DIR / "race.db"
    config.SETTINGS_PATH = config.DATA_DIR / "settings.json"
    config.ROI_STATE_PATH = config.DATA_DIR / "roi.json"
    config.FOCUS_STATE_PATH = config.DATA_DIR / "focus.json"
    config.MOTION_STATE_PATH = config.DATA_DIR / "motion.json"
    config.OCR_STATE_PATH = config.DATA_DIR / "ocr.json"
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)


def main():
    parser = argparse.ArgumentParser(description="Finish-line recorder")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--mock", action="store_true", help="use the synthetic camera")
    parser.add_argument("--no-ocr", action="store_true", help="don't load the OCR engine")
    parser.add_argument("--root", default=None, help="alternate data/captures/recordings root")
    args = parser.parse_args()

    if args.root:
        _apply_root(args.root)

    from app import config, database
    if args.mock:
        config.USE_MOCK_CAMERA = True

    from app.camera_manager import create_camera
    from app.extractor import Extractor
    from app.gpio_controller import GpioController
    from app.motion_detector import MotionDetector
    from app.ocr_service import OcrService
    from app.recorder import Recorder
    from app.settings import Settings
    from app.web_app import create_app

    print("=" * 60)
    print(" Finish-line recorder starting")
    print("=" * 60)

    # 1. Storage first - make sure the DB schema exists before anything writes.
    database.init_db()
    config.CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    config.RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)

    # 2. Settings (record mode, storage cap, ...) and the motion detector.
    settings = Settings()
    motion = MotionDetector()

    # 3. Camera - runs motion detection inside its capture loop.
    camera = create_camera(settings, motion)
    camera.start()

    # 4. Recorder (ffmpeg pipeline) - registers with the camera when REC is on.
    recorder = Recorder(camera, settings)

    # 5. OCR worker - pauses itself while recording (unless allowed).
    ocr = OcrService(motion, settings, recording_active=recorder.is_recording,
                     enabled=not args.no_ocr)
    ocr.start()

    # 6. Extractor - turns "send this range to OCR" jobs into stills.
    extractor = Extractor(ocr, motion, settings)
    extractor.start()

    # 7. GPIO - a physical button (if wired up) toggles recording.
    gpio = GpioController()
    gpio.setup(trigger_callback=lambda: recorder.toggle())

    # 8. Web app - reads from the shared objects above; owns no hardware.
    app = create_app(camera, motion, recorder, ocr, extractor, settings)

    def shutdown(signum, _frame):
        print("\n[main] shutting down...")
        try:
            recorder.stop()
        finally:
            extractor.stop()
            ocr.stop()
            camera.stop()
            gpio.cleanup()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    port = args.port or config.WEB_PORT
    print(f"[main] dashboard at http://{config.WEB_HOST}:{port}/")
    app.run(host=config.WEB_HOST, port=port, threaded=True, use_reloader=False,
            debug=False)


if __name__ == "__main__":
    main()
