#!/usr/bin/env python3
"""
reocr.py
========

Re-run bib recognition over captures that are already on disk, using the
current OCR pipeline in app/bib_recognizer.py.  Useful after the recognizer
has been improved: photos that got no (or a wrong) bib read the first time can
be given another pass without touching the camera or the running service.

Usage:
    python3 reocr.py                          # current event, unmatched only
    python3 reocr.py --event SWXCR5           # by event name (or numeric id)
    python3 reocr.py --event 6 --all          # every capture, even matched ones
    python3 reocr.py --limit 20               # first N (for a quick trial)

"Unmatched" means the capture has no detections row with a bib number - i.e.
the first pass found nothing.  Each re-processed capture has its old detection
rows REPLACED by the new result (matched captures are only touched with --all).

Safe to run while the finish-line service is running: the service only
processes captures with no detections rows at all, and this tool replaces rows
in a single step per capture, so the two never fight over the same photo.
"""

from __future__ import annotations

import argparse
import sys
import time

import numpy as np
from PIL import Image

from app import config, database
from app.bib_recognizer import create_recognizer


def _resolve_event(spec: str | None) -> tuple[int, str]:
    events = database.list_events()
    if spec is None:
        current = database.get_current_event_id()
        for e in events:
            if e["id"] == current:
                return e["id"], e["name"]
        sys.exit("no current event set - pass --event")
    for e in events:
        if spec == str(e["id"]) or spec.lower() == e["name"].lower():
            return e["id"], e["name"]
    sys.exit(f"no event matching {spec!r}; have: "
             + ", ".join(f"{e['id']}={e['name']}" for e in events))


def _target_capture_ids(event_id: int, include_matched: bool) -> list[int]:
    import sqlite3
    conn = sqlite3.connect(config.DATABASE_PATH)
    try:
        if include_matched:
            sql = "SELECT id FROM captures WHERE event_id = ? ORDER BY epoch ASC"
        else:
            # Unmatched = was OCR-processed but no bib found.  Captures with NO
            # detections rows at all are deliberately excluded: those belong to
            # the running service's own backlog (it re-queues them at startup),
            # and doing them here too would produce duplicate rows.
            sql = """
                SELECT c.id FROM captures c
                WHERE c.event_id = ?
                  AND EXISTS (SELECT 1 FROM detections d2
                              WHERE d2.capture_id = c.id)
                  AND NOT EXISTS (SELECT 1 FROM detections d
                                  WHERE d.capture_id = c.id
                                    AND d.bib_number IS NOT NULL)
                ORDER BY c.epoch ASC
            """
        return [r[0] for r in conn.execute(sql, (event_id,))]
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    ap.add_argument("--event", help="event name or id (default: current event)")
    ap.add_argument("--all", action="store_true",
                    help="re-run every capture, not just unmatched ones")
    ap.add_argument("--limit", type=int, help="stop after N captures")
    args = ap.parse_args()

    database.init_db()
    event_id, event_name = _resolve_event(args.event)
    ids = _target_capture_ids(event_id, args.all)
    if args.limit:
        ids = ids[: args.limit]
    if not ids:
        print(f"nothing to do for event {event_name!r}")
        return

    print(f"event {event_name!r}: re-processing {len(ids)} captures")
    recognizer = create_recognizer()

    n_matched = 0
    t_start = time.time()
    try:
        for i, capture_id in enumerate(ids, 1):
            row = database.fetch_capture(capture_id)
            if row is None:
                continue
            path = config.CAPTURE_DIR / row["filename"]
            try:
                with Image.open(path) as img:
                    image_rgb = np.asarray(img.convert("RGB"))
            except Exception as exc:
                print(f"  [{i}/{len(ids)}] {row['filename']}: unreadable ({exc})")
                continue

            t0 = time.time()
            try:
                detections = recognizer.recognize(image_rgb)
            except Exception as exc:
                print(f"  [{i}/{len(ids)}] {row['filename']}: OCR failed ({exc})")
                continue

            # Replace this capture's rows in one go.
            database.delete_detections_for_capture(capture_id)
            if detections:
                for det in detections:
                    database.insert_detection(capture_id, det.bib_number,
                                              det.confidence, det.bbox)
                n_matched += 1
                found = ", ".join(
                    f"{d.bib_number}@{d.confidence:.2f}" for d in detections)
            else:
                database.insert_detection(capture_id, None, None, None)
                found = "-"

            done = time.time() - t_start
            eta_min = (done / i) * (len(ids) - i) / 60
            print(f"  [{i}/{len(ids)}] {row['filename']}: {found} "
                  f"({time.time() - t0:.0f}s, eta {eta_min:.0f}m)")
    except KeyboardInterrupt:
        print("\ninterrupted - progress so far is saved")

    print(f"done: {n_matched} of the processed captures now have a bib read")


if __name__ == "__main__":
    main()
