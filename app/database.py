"""
database.py
===========

All persistent storage of capture / detection records, backed by SQLite.

Schema
------
The model matches the requirement that *one row in the web table == one bib
number*, and is scoped by "event" (a race / day) so multiple events stay
separate:

    events          one row per event (race / day)
        id, name, created_iso, created_epoch

    captures        one row per photo taken
        id            primary key
        event_id      -> events.id  (which event this photo belongs to)
        filename      the JPEG file on disk (in captures/)
        timestamp_iso human-readable capture time
        epoch         capture time as a float (for sorting / maths)
        width, height pixel size of the saved image
        motion_score  how much motion triggered this capture (diagnostic)

    meta            key/value app state (e.g. the current event id)

    recordings      one row per continuous recording session (press REC)
        id, event_id, name, dirname, start_epoch, end_epoch, status,
        width, height, fps, crf, mode

    segments        one row per finished MP4 segment of a recording
        id, recording_id, idx, filename, start_epoch, end_epoch, n_frames,
        fps, bytes, motion_1s (JSON: max motion score per second)

    ocr_jobs        "send this time range of a recording to OCR" requests
        id, recording_id, event_id, t_start, t_end, options (JSON), status,
        progress (0-1), frames_scanned, frames_kept, message, timestamps

    detections      one row per bib number found in a photo
        id            primary key
        capture_id    -> captures.id
        bib_number    the digits, or NULL if none were found
        confidence    0.0-1.0 OCR confidence, or NULL
        x, y, w, h    bounding box of the bib within the image, or NULL

A photo with three bibs produces three `detections` rows that all point at the
same `captures` row.  A photo with no readable bib still gets a single
`detections` row with NULL values, so it still appears in the table.

Thread-safety
-------------
The capture thread, the OCR worker and Flask request handlers all touch the DB.
SQLite handles concurrent access fine when each operation uses its own short
lived connection in WAL (write-ahead-log) mode, which is what we do here.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager

from . import config

# A process-wide lock serialises writes.  SQLite can do this itself, but the
# explicit lock makes the (low) contention here trivial to reason about.
_write_lock = threading.Lock()


@contextmanager
def _connect():
    """Yield a short-lived SQLite connection with sensible pragmas set."""
    conn = sqlite3.connect(config.DATABASE_PATH, timeout=10.0)
    # Return rows as dict-like objects so callers can use column names.
    conn.row_factory = sqlite3.Row
    # WAL lets readers and a writer coexist without blocking each other.
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    """
    Create the tables if they do not already exist and run any small
    migrations.  Safe to call repeatedly.
    """
    config.DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _write_lock, _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                name          TEXT    NOT NULL,
                created_iso   TEXT    NOT NULL,
                created_epoch REAL    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS captures (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                filename      TEXT    NOT NULL,
                timestamp_iso TEXT    NOT NULL,
                epoch         REAL    NOT NULL,
                width         INTEGER NOT NULL,
                height        INTEGER NOT NULL,
                motion_score  REAL    NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS detections (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                capture_id  INTEGER NOT NULL REFERENCES captures(id) ON DELETE CASCADE,
                bib_number  TEXT,
                confidence  REAL,
                x INTEGER, y INTEGER, w INTEGER, h INTEGER
            );

            -- Simple key/value store for app state (e.g. the current event id).
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_detections_capture
                ON detections(capture_id);

            CREATE TABLE IF NOT EXISTS recordings (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id    INTEGER,
                name        TEXT    NOT NULL,
                dirname     TEXT    NOT NULL,
                start_epoch REAL    NOT NULL,
                end_epoch   REAL,
                status      TEXT    NOT NULL DEFAULT 'recording',
                width       INTEGER NOT NULL,
                height      INTEGER NOT NULL,
                fps         INTEGER NOT NULL,
                crf         INTEGER NOT NULL,
                mode        TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS segments (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                recording_id INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
                idx          INTEGER NOT NULL,
                filename     TEXT    NOT NULL,
                start_epoch  REAL    NOT NULL,
                end_epoch    REAL    NOT NULL,
                n_frames     INTEGER NOT NULL,
                fps          INTEGER NOT NULL,
                bytes        INTEGER NOT NULL DEFAULT 0,
                motion_1s    TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_segments_rec ON segments(recording_id, idx);
            CREATE INDEX IF NOT EXISTS idx_segments_start ON segments(start_epoch);

            CREATE TABLE IF NOT EXISTS ocr_jobs (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                recording_id   INTEGER NOT NULL,
                event_id       INTEGER,
                t_start        REAL    NOT NULL,
                t_end          REAL    NOT NULL,
                options        TEXT    NOT NULL,
                status         TEXT    NOT NULL DEFAULT 'pending',
                progress       REAL    NOT NULL DEFAULT 0,
                progress_epoch REAL,
                frames_scanned INTEGER NOT NULL DEFAULT 0,
                frames_kept    INTEGER NOT NULL DEFAULT 0,
                message        TEXT,
                created_epoch  REAL    NOT NULL,
                started_epoch  REAL,
                finished_epoch REAL
            );
            """
        )

        # --- migration: add captures.event_id if it isn't there yet ---------
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(captures)")]
        if "event_id" not in cols:
            conn.execute("ALTER TABLE captures ADD COLUMN event_id INTEGER")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_captures_event ON captures(event_id)")

        # --- migration: record where motion happened (normalised 0-1 bbox) ---
        # Used by motion-gated OCR to reject reads away from the moved region.
        # Nullable: NULL means "no motion region recorded" -> that capture is
        # not gated (forced captures, or rows predating this column).
        for mc in ("motion_x", "motion_y", "motion_w", "motion_h"):
            if mc not in cols:
                conn.execute(f"ALTER TABLE captures ADD COLUMN {mc} REAL")

        # --- migration: link a capture to the recording it was cut from -----
        # NULL for a live snapshot or a photo from the old motion-capture era.
        if "recording_id" not in cols:
            conn.execute("ALTER TABLE captures ADD COLUMN recording_id INTEGER")
        if "source" not in cols:
            conn.execute("ALTER TABLE captures ADD COLUMN source TEXT")

        # --- crash recovery -------------------------------------------------
        # A recording still marked 'recording' means the app died mid-session;
        # its finished segments are intact (ffmpeg finalises each one), so just
        # close the session record.  Jobs that were running restart from where
        # they got to (see progress_epoch) once the extractor comes up.
        conn.execute("UPDATE recordings SET status = 'stopped', "
                     "end_epoch = COALESCE(end_epoch, "
                     "  (SELECT MAX(end_epoch) FROM segments s WHERE s.recording_id = recordings.id), "
                     "  start_epoch) WHERE status = 'recording'")
        conn.execute("UPDATE ocr_jobs SET status = 'pending' WHERE status = 'running'")

        # --- ensure at least one event exists -------------------------------
        n_events = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
        if n_events == 0:
            import time as _t
            cur = conn.execute(
                "INSERT INTO events (name, created_iso, created_epoch) VALUES (?, ?, ?)",
                ("Event 1", _t.strftime("%Y-%m-%d %H:%M:%S"), _t.time()),
            )
            default_event_id = int(cur.lastrowid)
        else:
            default_event_id = conn.execute(
                "SELECT id FROM events ORDER BY id LIMIT 1").fetchone()["id"]

        # --- adopt any orphaned captures (from before events existed) -------
        conn.execute(
            "UPDATE captures SET event_id = ? WHERE event_id IS NULL",
            (default_event_id,),
        )

        # --- ensure a valid 'current event' is selected ---------------------
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'current_event_id'").fetchone()
        valid = False
        if row is not None:
            valid = conn.execute(
                "SELECT 1 FROM events WHERE id = ?", (int(row["value"]),)
            ).fetchone() is not None
        if not valid:
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('current_event_id', ?)",
                (str(default_event_id),),
            )

        conn.commit()


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

def get_current_event_id() -> int | None:
    """Return the id of the event new captures are being filed under."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'current_event_id'").fetchone()
        return int(row["value"]) if row else None


def set_current_event_id(event_id: int):
    """Make `event_id` the current event (where new captures go / table shows)."""
    with _write_lock, _connect() as conn:
        # Guard against pointing at an event that doesn't exist.
        if conn.execute("SELECT 1 FROM events WHERE id = ?", (event_id,)).fetchone() is None:
            raise ValueError("no such event")
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('current_event_id', ?)",
            (str(event_id),),
        )
        conn.commit()


def list_events() -> list[dict]:
    """Return all events, newest first, each with its photo count."""
    with _connect() as conn:
        cur = conn.execute(
            """
            SELECT e.id, e.name, e.created_iso,
                   COUNT(c.id) AS capture_count
            FROM events e
            LEFT JOIN captures c ON c.event_id = e.id
            GROUP BY e.id
            ORDER BY e.id DESC
            """
        )
        return [dict(r) for r in cur.fetchall()]


def create_event(name: str) -> int:
    """Create a new event and return its id."""
    import time as _t
    name = (name or "").strip() or "Untitled event"
    with _write_lock, _connect() as conn:
        cur = conn.execute(
            "INSERT INTO events (name, created_iso, created_epoch) VALUES (?, ?, ?)",
            (name, _t.strftime("%Y-%m-%d %H:%M:%S"), _t.time()),
        )
        conn.commit()
        return int(cur.lastrowid)


def rename_event(event_id: int, name: str):
    """Rename an event."""
    name = (name or "").strip()
    if not name:
        raise ValueError("name cannot be empty")
    with _write_lock, _connect() as conn:
        conn.execute("UPDATE events SET name = ? WHERE id = ?", (name, event_id))
        conn.commit()


def get_filenames_for_event(event_id: int) -> list[str]:
    """Return the image filenames belonging to an event (for deleting on disk)."""
    with _connect() as conn:
        cur = conn.execute(
            "SELECT filename FROM captures WHERE event_id = ?", (event_id,))
        return [r["filename"] for r in cur.fetchall()]


def purge_event(event_id: int) -> int:
    """
    Delete all captures + detections for an event (but keep the event itself).
    Returns the number of captures removed.  Image files are deleted by the
    caller, which knows the captures directory.
    """
    with _write_lock, _connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM captures WHERE event_id = ?",
            (event_id,)).fetchone()["n"]
        # detections cascade-delete via the capture foreign key.
        conn.execute(
            "DELETE FROM detections WHERE capture_id IN "
            "(SELECT id FROM captures WHERE event_id = ?)", (event_id,))
        conn.execute("DELETE FROM captures WHERE event_id = ?", (event_id,))
        conn.commit()
        return int(n)


def delete_event(event_id: int) -> int:
    """
    Remove an event entirely (its captures + detections too).  If it was the
    current event, the current pointer is moved to another event (creating a
    fresh default one if none remain).  Returns captures removed.
    """
    n = purge_event(event_id)
    with _write_lock, _connect() as conn:
        conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
        # Make sure an event still exists and is selected.
        remaining = conn.execute(
            "SELECT id FROM events ORDER BY id DESC LIMIT 1").fetchone()
        if remaining is None:
            import time as _t
            cur = conn.execute(
                "INSERT INTO events (name, created_iso, created_epoch) VALUES (?, ?, ?)",
                ("Event 1", _t.strftime("%Y-%m-%d %H:%M:%S"), _t.time()))
            new_id = int(cur.lastrowid)
        else:
            new_id = remaining["id"]
        # Repoint the current event if it was the one we deleted.
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'current_event_id'").fetchone()
        if row is None or int(row["value"]) == event_id:
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('current_event_id', ?)",
                (str(new_id),))
        conn.commit()
    return n


# ---------------------------------------------------------------------------
# Captures / detections
# ---------------------------------------------------------------------------

def insert_capture(filename: str, timestamp_iso: str, epoch: float,
                   width: int, height: int, motion_score: float,
                   event_id: int | None = None,
                   motion_bbox: tuple[float, float, float, float] | None = None,
                   recording_id: int | None = None,
                   source: str | None = None) -> int:
    """Insert a capture row (filed under the current event) and return its id.

    motion_bbox is the normalised (x, y, w, h) region that moved in this frame
    (None for a forced capture); stored for motion-gated OCR.  recording_id
    links a frame back to the video it was cut from ("extract"); a live
    snapshot has source "snapshot" and no recording.
    """
    if event_id is None:
        event_id = get_current_event_id()
    mx, my, mw, mh = motion_bbox if motion_bbox else (None, None, None, None)
    mx, my, mw, mh = (float(v) if v is not None else None for v in (mx, my, mw, mh))
    with _write_lock, _connect() as conn:
        cur = conn.execute(
            """INSERT INTO captures
               (filename, timestamp_iso, epoch, width, height, motion_score,
                event_id, motion_x, motion_y, motion_w, motion_h,
                recording_id, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (filename, timestamp_iso, epoch, width, height, float(motion_score),
             event_id, mx, my, mw, mh, recording_id, source),
        )
        conn.commit()
        return int(cur.lastrowid)


def insert_detection(capture_id: int, bib_number: str | None,
                     confidence: float | None,
                     bbox: tuple[int, int, int, int] | None):
    """Insert one detection row (bib_number/confidence/bbox may be None)."""
    x, y, w, h = bbox if bbox else (None, None, None, None)
    # Coerce to native Python types before storing.  The recognizer's panel
    # path yields numpy int32 bbox coords; sqlite3 can't adapt those and stores
    # their raw memory as a BLOB, which then breaks jsonify() of the results
    # table ("Object of type bytes is not JSON serializable").  int()/float()
    # here guarantees the columns are always INTEGER/REAL.
    x, y, w, h = (int(v) if v is not None else None for v in (x, y, w, h))
    confidence = float(confidence) if confidence is not None else None
    bib_number = str(bib_number) if bib_number is not None else None
    with _write_lock, _connect() as conn:
        conn.execute(
            """INSERT INTO detections
               (capture_id, bib_number, confidence, x, y, w, h)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (capture_id, bib_number, confidence, x, y, w, h),
        )
        conn.commit()


def fetch_rows(limit: int | None = None, search: str | None = None,
               bib_filter: str = "all", min_conf: float | None = None,
               event_id: int | None = None) -> list[dict]:
    """
    Return rows for the web table: one row per detection, newest first, each
    joined to its parent capture so the table has the filename + timestamp.

    Optional server-side search / filters (so they work over the WHOLE race, not
    just the page that happens to be loaded in the browser):
        event_id   - only this event's rows (None = every event)
        search     - substring match on the bib number (e.g. "55")
        bib_filter - "all" | "with_bib" | "without_bib"
        min_conf   - only detections with confidence >= this (0-1)
        limit      - max rows (None = no limit, used by CSV export)
    """
    where: list[str] = []
    params: list = []

    if event_id is not None:
        where.append("c.event_id = ?")
        params.append(event_id)

    if bib_filter == "with_bib":
        where.append("d.bib_number IS NOT NULL")
    elif bib_filter == "without_bib":
        where.append("d.bib_number IS NULL")

    if search:
        # Escape LIKE wildcards in the user's input, then substring-match.
        safe = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("d.bib_number LIKE ? ESCAPE '\\'")
        params.append(f"%{safe}%")

    if min_conf is not None and min_conf > 0:
        where.append("d.confidence >= ?")
        params.append(min_conf)

    clause = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
        SELECT d.id            AS detection_id,
               c.id            AS capture_id,
               c.filename      AS filename,
               c.timestamp_iso AS timestamp,
               c.epoch         AS epoch,
               d.bib_number    AS bib_number,
               d.confidence    AS confidence,
               c.recording_id  AS recording_id,
               c.source        AS source,
               d.x AS x, d.y AS y, d.w AS w, d.h AS h
        FROM detections d
        JOIN captures c ON c.id = d.capture_id
        {clause}
        ORDER BY c.epoch DESC, d.id DESC
    """
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)

    with _connect() as conn:
        cur = conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def fetch_unprocessed_capture_ids() -> list[int]:
    """
    Ids of captures that have no detections row at all - i.e. their OCR pass
    never ran (the app stopped mid-backlog).  Oldest first.
    """
    with _connect() as conn:
        cur = conn.execute(
            """
            SELECT c.id FROM captures c
            LEFT JOIN detections d ON d.capture_id = c.id
            WHERE d.id IS NULL
            ORDER BY c.epoch ASC
            """
        )
        return [r["id"] for r in cur.fetchall()]


def delete_detections_for_capture(capture_id: int) -> int:
    """Remove all detections rows for one capture (used when re-running OCR).
    Returns the number of rows removed."""
    with _write_lock, _connect() as conn:
        cur = conn.execute(
            "DELETE FROM detections WHERE capture_id = ?", (capture_id,))
        conn.commit()
        return cur.rowcount


def fetch_capture(capture_id: int) -> dict | None:
    """Return a single capture row (for serving its image), or None."""
    with _connect() as conn:
        cur = conn.execute(
            "SELECT * FROM captures WHERE id = ?", (capture_id,)
        )
        row = cur.fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# Recordings / segments (continuous video)
# ---------------------------------------------------------------------------

def create_recording(event_id: int | None, name: str, dirname: str,
                     start_epoch: float, width: int, height: int, fps: int,
                     crf: int, mode: str) -> int:
    with _write_lock, _connect() as conn:
        cur = conn.execute(
            """INSERT INTO recordings
               (event_id, name, dirname, start_epoch, status, width, height,
                fps, crf, mode)
               VALUES (?, ?, ?, ?, 'recording', ?, ?, ?, ?, ?)""",
            (event_id, name, dirname, float(start_epoch), int(width),
             int(height), int(fps), int(crf), mode))
        conn.commit()
        return int(cur.lastrowid)


def finish_recording(recording_id: int, end_epoch: float):
    with _write_lock, _connect() as conn:
        conn.execute("UPDATE recordings SET status = 'stopped', end_epoch = ? "
                     "WHERE id = ?", (float(end_epoch), recording_id))
        conn.commit()


def get_recording(recording_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM recordings WHERE id = ?",
                           (recording_id,)).fetchone()
        return dict(row) if row else None


def list_recordings(event_id: int | None = None) -> list[dict]:
    """Recordings newest first, each with segment count / bytes / span."""
    where, params = "", []
    if event_id is not None:
        where, params = "WHERE r.event_id = ?", [event_id]
    with _connect() as conn:
        cur = conn.execute(f"""
            SELECT r.*,
                   COUNT(s.id)            AS segment_count,
                   COALESCE(SUM(s.bytes), 0) AS bytes,
                   MIN(s.start_epoch)     AS first_segment_epoch,
                   MAX(s.end_epoch)       AS last_segment_epoch
            FROM recordings r
            LEFT JOIN segments s ON s.recording_id = r.id
            {where}
            GROUP BY r.id
            ORDER BY r.start_epoch DESC
        """, params)
        return [dict(r) for r in cur.fetchall()]


def delete_recording(recording_id: int):
    """Remove a recording and its segment rows (files are the caller's job)."""
    with _write_lock, _connect() as conn:
        conn.execute("DELETE FROM segments WHERE recording_id = ?", (recording_id,))
        conn.execute("DELETE FROM recordings WHERE id = ?", (recording_id,))
        conn.commit()


def insert_segment(recording_id: int, idx: int, filename: str,
                   start_epoch: float, end_epoch: float, n_frames: int,
                   fps: int, nbytes: int, motion_1s: list | None) -> int:
    import json as _json
    with _write_lock, _connect() as conn:
        cur = conn.execute(
            """INSERT INTO segments
               (recording_id, idx, filename, start_epoch, end_epoch, n_frames,
                fps, bytes, motion_1s)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (recording_id, int(idx), filename, float(start_epoch),
             float(end_epoch), int(n_frames), int(fps), int(nbytes),
             _json.dumps(motion_1s) if motion_1s is not None else None))
        conn.commit()
        return int(cur.lastrowid)


def list_segments(recording_id: int) -> list[dict]:
    with _connect() as conn:
        cur = conn.execute("SELECT * FROM segments WHERE recording_id = ? "
                           "ORDER BY idx", (recording_id,))
        return [dict(r) for r in cur.fetchall()]


def segments_in_range(recording_id: int, t0: float, t1: float) -> list[dict]:
    """Segments of a recording overlapping the epoch range [t0, t1]."""
    with _connect() as conn:
        cur = conn.execute(
            "SELECT * FROM segments WHERE recording_id = ? AND end_epoch >= ? "
            "AND start_epoch <= ? ORDER BY idx", (recording_id, t0, t1))
        return [dict(r) for r in cur.fetchall()]


def delete_segment(segment_id: int):
    with _write_lock, _connect() as conn:
        conn.execute("DELETE FROM segments WHERE id = ?", (segment_id,))
        conn.commit()


def oldest_segments(limit: int = 50) -> list[dict]:
    """Oldest segments across ALL recordings (for the storage ring buffer)."""
    with _connect() as conn:
        cur = conn.execute(
            """SELECT s.*, r.dirname FROM segments s
               JOIN recordings r ON r.id = s.recording_id
               ORDER BY s.start_epoch ASC LIMIT ?""", (limit,))
        return [dict(r) for r in cur.fetchall()]


def total_segment_bytes() -> int:
    with _connect() as conn:
        row = conn.execute("SELECT COALESCE(SUM(bytes), 0) AS b FROM segments").fetchone()
        return int(row["b"])


def recordings_without_segments() -> list[dict]:
    """Stopped recordings whose every segment has been deleted (ring buffer)."""
    with _connect() as conn:
        cur = conn.execute(
            """SELECT r.* FROM recordings r
               WHERE r.status != 'recording'
                 AND NOT EXISTS (SELECT 1 FROM segments s WHERE s.recording_id = r.id)""")
        return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# OCR jobs (send a time range of a recording to OCR)
# ---------------------------------------------------------------------------

def create_ocr_job(recording_id: int, event_id: int | None, t_start: float,
                   t_end: float, options: dict) -> int:
    import json as _json
    import time as _t
    with _write_lock, _connect() as conn:
        cur = conn.execute(
            """INSERT INTO ocr_jobs
               (recording_id, event_id, t_start, t_end, options, status,
                created_epoch)
               VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
            (recording_id, event_id, float(t_start), float(t_end),
             _json.dumps(options), _t.time()))
        conn.commit()
        return int(cur.lastrowid)


def update_ocr_job(job_id: int, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with _write_lock, _connect() as conn:
        conn.execute(f"UPDATE ocr_jobs SET {cols} WHERE id = ?",
                     (*fields.values(), job_id))
        conn.commit()


def get_ocr_job(job_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM ocr_jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None


def next_pending_ocr_job() -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM ocr_jobs WHERE status = 'pending' "
                           "ORDER BY id ASC LIMIT 1").fetchone()
        return dict(row) if row else None


def list_ocr_jobs(limit: int = 50) -> list[dict]:
    with _connect() as conn:
        cur = conn.execute(
            """SELECT j.*, r.name AS recording_name FROM ocr_jobs j
               LEFT JOIN recordings r ON r.id = j.recording_id
               ORDER BY j.id DESC LIMIT ?""", (limit,))
        return [dict(r) for r in cur.fetchall()]


def delete_ocr_job(job_id: int):
    with _write_lock, _connect() as conn:
        conn.execute("DELETE FROM ocr_jobs WHERE id = ?", (job_id,))
        conn.commit()
