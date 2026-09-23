"""
web_app.py
==========

The Flask web layer: a four-tab single-page app (Live / Playback / Results /
Settings) plus the JSON + stream endpoints it talks to.

Endpoints (all JSON unless noted)
---------------------------------
    GET  /                                the app (templates/index.html)
    GET  /video_feed                      live MJPEG preview
    GET  /api/status                      everything the header needs, 1 call
    GET/POST /api/record                  recording on/off  {"recording": bool}
    POST /api/snapshot                    save the live frame as a still -> OCR
    GET  /api/recordings                  list (current event, or ?all=1)
    GET  /api/recordings/<id>             details + segments (+ per-second motion)
    DELETE /api/recordings/<id>           delete video files + rows
    GET  /api/recordings/<id>/frame?t=    JPEG of the exact frame at epoch t
    POST /api/recordings/<id>/snapshot    {t}: that frame -> still -> OCR
    GET  /recordings/<dir>/<file>         MP4 segments (supports Range requests)
    GET/POST /api/ocr_jobs, DELETE /api/ocr_jobs/<id>    send-to-OCR jobs
    GET/POST /api/ocr_worker              pause/resume the OCR worker
    GET/POST /api/ocr                     recognizer options
    GET/POST /api/settings                persisted settings; POST /api/camera/apply
    GET  /api/detections, /api/export.csv results table (search/filter)
    events / roi / motion / focus / purge / diskusage / shutdown / restart
    GET  /captures/<file>                 stored stills

`create_app()` receives the already-running services; the web layer owns no
hardware and no threads of its own.
"""

from __future__ import annotations

import csv
import io
import os
import time

from flask import (Flask, Response, abort, jsonify, render_template, request,
                   send_from_directory)

from . import config, database
from .camera_manager import _BaseCamera, encode_yuv420_jpeg
from .extractor import Extractor, decode_frame_at
from .motion_detector import MotionDetector
from .ocr_service import OcrService
from .recorder import Recorder
from .settings import Settings


def create_app(camera: _BaseCamera, motion: MotionDetector, recorder: Recorder,
               ocr: OcrService, extractor: Extractor, settings: Settings) -> Flask:
    app = Flask(__name__)
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

    # -- page ---------------------------------------------------------------

    @app.route("/")
    def index():
        return render_template("index.html")

    # -- live MJPEG preview -------------------------------------------------

    def _mjpeg_generator():
        boundary = b"--frame"
        last = 0.0
        while True:
            frame = camera.wait_for_frame(timeout=1.0)
            if frame is None:
                time.sleep(0.05)
                continue
            period = 1.0 / max(1, int(settings.get("preview_fps")))
            now = time.monotonic()
            if now - last < period:
                continue
            last = now
            w, h = frame.lores_size
            try:
                jpeg = encode_yuv420_jpeg(frame.lores, w, h,
                                          int(settings.get("preview_quality")))
            except Exception as exc:
                print(f"[web] preview encode failed: {exc}")
                time.sleep(0.2)
                continue
            yield (boundary + b"\r\n"
                   b"Content-Type: image/jpeg\r\n"
                   b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n"
                   + jpeg + b"\r\n")

    @app.route("/video_feed")
    def video_feed():
        return Response(_mjpeg_generator(),
                        mimetype="multipart/x-mixed-replace; boundary=frame")

    # -- status (one call for the header + tabs) -----------------------------

    @app.route("/api/status")
    def api_status():
        frame = camera.get_latest()
        m = motion.get_motion_settings()
        return jsonify({
            "time": time.time(),
            "camera": camera.describe(),
            "have_frame": frame is not None,
            "motion": {"last_score": m["last_score"],
                       "area_threshold": m["area_threshold"],
                       "bbox": list(frame.motion.motion_bbox)
                       if (frame and frame.motion and frame.motion.motion_bbox) else None,
                       "detected": bool(frame.motion.detected) if (frame and frame.motion) else False},
            "roi": motion.get_roi(),
            "recorder": recorder.status(),
            "storage": recorder.storage_summary(),
            "ocr": ocr.status(),
            "extractor": extractor.status(),
            "event_id": database.get_current_event_id(),
        })

    # -- recording ------------------------------------------------------------

    @app.route("/api/record", methods=["GET"])
    def api_get_record():
        return jsonify(recorder.status())

    @app.route("/api/record", methods=["POST"])
    def api_set_record():
        data = request.get_json(silent=True) or {}
        if "recording" not in data:
            abort(400, "expected JSON with boolean 'recording'")
        if data["recording"]:
            st = recorder.start(name=data.get("name"))
            if not st["recording"]:
                return jsonify(st), 500
            return jsonify(st)
        return jsonify(recorder.stop())

    @app.route("/api/snapshot", methods=["POST"])
    def api_snapshot():
        """Save the live full-size frame as a still and queue it for OCR."""
        got = camera.capture_main(timeout=3.0)
        if got is None:
            abort(503, "no camera frame available")
        main, epoch = got
        w, h = camera.record_size
        frame = camera.get_latest()
        mres = frame.motion if frame else None
        cid = ocr.save_capture_yuv(
            main, w, h, epoch,
            motion_score=mres.score if mres else 0.0,
            motion_bbox=None,          # a forced snapshot is never motion-gated
            recording_id=recorder.status()["id"] if recorder.is_recording() else None,
            source="snapshot")
        if cid is None:
            abort(500, "could not save snapshot")
        return jsonify({"capture_id": cid, "epoch": epoch})

    # -- recordings / playback -----------------------------------------------

    def _recording_dict(r: dict) -> dict:
        d = dict(r)
        d["duration"] = ((d.get("end_epoch") or time.time()) - d["start_epoch"])
        return d

    @app.route("/api/recordings")
    def api_recordings():
        all_events = request.args.get("all") in ("1", "true")
        event_id = None if all_events else database.get_current_event_id()
        recs = [_recording_dict(r) for r in database.list_recordings(event_id)]
        return jsonify({"recordings": recs})

    @app.route("/api/recordings/<int:rec_id>")
    def api_recording(rec_id):
        rec = database.get_recording(rec_id)
        if rec is None:
            abort(404)
        segs = database.list_segments(rec_id)
        out = []
        import json as _json
        for s in segs:
            d = {k: s[k] for k in ("id", "idx", "filename", "start_epoch", "end_epoch",
                                   "n_frames", "fps", "bytes")}
            d["url"] = f"/recordings/{rec['dirname']}/{s['filename']}"
            try:
                d["motion_1s"] = _json.loads(s["motion_1s"]) if s["motion_1s"] else []
            except Exception:
                d["motion_1s"] = []
            out.append(d)
        r = _recording_dict(rec)
        r["live"] = recorder.is_recording() and recorder.status()["id"] == rec_id
        return jsonify({"recording": r, "segments": out,
                        "motion_threshold": motion.get_motion_settings()["area_threshold"]})

    @app.route("/api/recordings/<int:rec_id>", methods=["DELETE"])
    def api_delete_recording(rec_id):
        rec = database.get_recording(rec_id)
        if rec is None:
            abort(404)
        if recorder.is_recording() and recorder.status()["id"] == rec_id:
            abort(409, "stop recording first")
        Recorder.delete_recording_files(rec["dirname"])
        database.delete_recording(rec_id)
        return jsonify({"ok": True})

    @app.route("/api/recordings/<int:rec_id>/frame")
    def api_recording_frame(rec_id):
        """The exact frame nearest epoch t, as a JPEG (for zoomed inspection)."""
        rec = database.get_recording(rec_id)
        if rec is None:
            abort(404)
        try:
            t = float(request.args.get("t"))
        except (TypeError, ValueError):
            abort(400, "t (epoch seconds) required")
        got = decode_frame_at(rec, t)
        if got is None:
            abort(404, "no frame at that time")
        yuv, w, h, fepoch, _score, _bbox, _seg = got
        jpeg = encode_yuv420_jpeg(yuv, w, h, 88)
        resp = Response(jpeg, mimetype="image/jpeg")
        resp.headers["X-Frame-Epoch"] = f"{fepoch:.6f}"
        return resp

    @app.route("/api/recordings/<int:rec_id>/snapshot", methods=["POST"])
    def api_recording_snapshot(rec_id):
        """Cut the frame at epoch t out of the recording and send it to OCR."""
        rec = database.get_recording(rec_id)
        if rec is None:
            abort(404)
        data = request.get_json(silent=True) or {}
        try:
            t = float(data["t"])
        except (KeyError, TypeError, ValueError):
            abort(400, "t (epoch seconds) required")
        got = decode_frame_at(rec, t)
        if got is None:
            abort(404, "no frame at that time")
        yuv, w, h, fepoch, score, bbox, _seg = got
        cid = ocr.save_capture_yuv(yuv, w, h, fepoch, motion_score=score,
                                   motion_bbox=bbox, recording_id=rec_id,
                                   source="frame", event_id=rec.get("event_id"))
        if cid is None:
            abort(500, "could not save frame")
        return jsonify({"capture_id": cid, "epoch": fepoch})

    @app.route("/recordings/<path:dirname>/<path:filename>")
    def recordings_file(dirname, filename):
        # conditional=True makes Flask honour Range requests, which the
        # browser's <video> element needs for seeking.
        d = config.RECORDINGS_DIR / os.path.basename(dirname)
        return send_from_directory(d, os.path.basename(filename), conditional=True)

    # -- OCR jobs --------------------------------------------------------------

    @app.route("/api/ocr_jobs", methods=["GET"])
    def api_ocr_jobs():
        import json as _json
        jobs = database.list_ocr_jobs(limit=40)
        for j in jobs:
            try:
                j["options"] = _json.loads(j["options"])
            except Exception:
                pass
        return jsonify({"jobs": jobs})

    @app.route("/api/ocr_jobs", methods=["POST"])
    def api_create_ocr_job():
        data = request.get_json(silent=True) or {}
        try:
            rec_id = int(data["recording_id"])
            t0 = float(data["t_start"])
            t1 = float(data["t_end"])
        except (KeyError, TypeError, ValueError):
            abort(400, "recording_id, t_start, t_end required")
        try:
            job_id = extractor.submit(rec_id, t0, t1, data.get("options") or {})
        except ValueError as exc:
            abort(400, str(exc))
        return jsonify({"job_id": job_id, "job": database.get_ocr_job(job_id)})

    @app.route("/api/ocr_jobs/<int:job_id>", methods=["DELETE"])
    def api_cancel_ocr_job(job_id):
        job = database.get_ocr_job(job_id)
        if job is None:
            abort(404)
        if job["status"] in ("pending", "running"):
            extractor.cancel(job_id)
        else:
            database.delete_ocr_job(job_id)
        return jsonify({"ok": True})

    # -- OCR worker + options ----------------------------------------------------

    @app.route("/api/ocr_worker", methods=["GET"])
    def api_ocr_worker():
        return jsonify(ocr.status())

    @app.route("/api/ocr_worker", methods=["POST"])
    def api_set_ocr_worker():
        data = request.get_json(silent=True) or {}
        if "paused" not in data:
            abort(400, "expected {'paused': bool}")
        ocr.set_paused(bool(data["paused"]))
        return jsonify(ocr.status())

    @app.route("/api/ocr", methods=["GET"])
    def api_get_ocr():
        return jsonify(ocr.get_ocr_options())

    @app.route("/api/ocr", methods=["POST"])
    def api_set_ocr():
        data = request.get_json(silent=True) or {}
        if "panel_proposals" not in data and "detect_width" not in data:
            abort(400, "expected 'panel_proposals' and/or 'detect_width'")
        if "panel_proposals" in data:
            ocr.set_panel_proposals(bool(data["panel_proposals"]))
        if "detect_width" in data:
            try:
                ocr.set_detect_width(int(data["detect_width"]))
            except (TypeError, ValueError):
                abort(400, "detect_width must be an integer")
        return jsonify(ocr.get_ocr_options())

    # -- settings --------------------------------------------------------------

    def _settings_payload():
        modes = [{"key": k, **{kk: (list(vv) if isinstance(vv, tuple) else vv)
                              for kk, vv in v.items()}}
                 for k, v in config.RECORD_MODES.items()]
        cam = camera.describe()
        return {"settings": settings.all(), "modes": modes,
                "camera": cam,
                "camera_matches": (cam["mode"] == settings.get("record_mode")
                                   and cam["preview_size"][0] == settings.get("preview_width")
                                   or (cam["preview_size"][0] == cam["record_size"][0]
                                       and cam["mode"] == settings.get("record_mode")))}

    @app.route("/api/settings", methods=["GET"])
    def api_get_settings():
        return jsonify(_settings_payload())

    @app.route("/api/settings", methods=["POST"])
    def api_set_settings():
        data = request.get_json(silent=True) or {}
        try:
            settings.update(data)
        except ValueError as exc:
            abort(400, str(exc))
        return jsonify(_settings_payload())

    @app.route("/api/camera/apply", methods=["POST"])
    def api_camera_apply():
        """Re-open the camera with the saved mode / preview size."""
        if recorder.is_recording():
            abort(409, "stop recording before changing the camera mode")
        try:
            camera.reconfigure()
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc), **_settings_payload()}), 500
        return jsonify({"ok": True, **_settings_payload()})

    # -- results table -----------------------------------------------------------

    def _table_query_args():
        search = (request.args.get("search") or "").strip() or None
        bib_filter = request.args.get("filter", "all")
        if bib_filter not in ("all", "with_bib", "without_bib"):
            bib_filter = "all"
        try:
            min_conf = float(request.args.get("min_conf", 0)) / 100.0
        except (TypeError, ValueError):
            min_conf = 0.0
        return search, bib_filter, min_conf

    @app.route("/api/detections")
    def api_detections():
        search, bib_filter, min_conf = _table_query_args()
        rows = database.fetch_rows(limit=config.TABLE_PAGE_SIZE, search=search,
                                   bib_filter=bib_filter, min_conf=min_conf,
                                   event_id=database.get_current_event_id())
        return jsonify(rows)

    @app.route("/api/export.csv")
    def api_export_csv():
        search, bib_filter, min_conf = _table_query_args()
        rows = database.fetch_rows(limit=None, search=search, bib_filter=bib_filter,
                                   min_conf=min_conf,
                                   event_id=database.get_current_event_id())
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["bib_number", "confidence", "timestamp", "capture_id",
                         "filename", "recording_id", "source"])
        for r in rows:
            conf = "" if r["confidence"] is None else round(r["confidence"], 4)
            writer.writerow([r["bib_number"] or "", conf, r["timestamp"],
                             r["capture_id"], r["filename"], r["recording_id"] or "",
                             r["source"] or ""])
        filename = time.strftime("finish_line_results_%Y%m%d_%H%M%S.csv")
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename={filename}"})

    # -- motion / ROI / focus ------------------------------------------------------

    @app.route("/api/motion", methods=["GET"])
    def api_get_motion():
        """Threshold + live score, plus where motion is right now (for the
        live-view overlay)."""
        m = motion.get_motion_settings()
        frame = camera.get_latest()
        mr = frame.motion if frame else None
        m["detected"] = bool(mr.detected) if mr else False
        m["bbox"] = list(mr.motion_bbox) if (mr and mr.motion_bbox) else None
        return jsonify(m)

    @app.route("/api/motion", methods=["POST"])
    def api_set_motion():
        data = request.get_json(silent=True) or {}
        try:
            threshold = float(data["area_threshold"])
        except (KeyError, TypeError, ValueError):
            abort(400, "expected JSON with numeric area_threshold (0-1)")
        return jsonify(motion.set_threshold(threshold))

    @app.route("/api/roi", methods=["GET"])
    def api_get_roi():
        return jsonify(motion.get_roi())

    @app.route("/api/roi", methods=["POST"])
    def api_set_roi():
        data = request.get_json(silent=True) or {}
        try:
            x = float(data["x"]); y = float(data["y"])
            w = float(data["w"]); h = float(data["h"])
        except (KeyError, TypeError, ValueError):
            abort(400, "expected JSON with numeric x, y, w, h (0-1)")
        motion.set_roi(x, y, w, h)
        return jsonify(motion.get_roi())

    @app.route("/api/focus", methods=["GET"])
    def api_get_focus():
        return jsonify(camera.get_focus())

    @app.route("/api/focus", methods=["POST"])
    def api_set_focus():
        data = request.get_json(silent=True) or {}
        mode = data.get("mode", "manual")
        try:
            distance_m = float(data.get("distance_m", 0.0))
        except (TypeError, ValueError):
            abort(400, "distance_m must be a number (metres)")
        try:
            result = camera.set_focus(mode, distance_m)
        except ValueError as exc:
            abort(400, str(exc))
        return jsonify(result)

    # -- events ---------------------------------------------------------------------

    @app.route("/api/events", methods=["GET"])
    def api_list_events():
        return jsonify({"events": database.list_events(),
                        "current": database.get_current_event_id()})

    @app.route("/api/events", methods=["POST"])
    def api_create_event():
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        if not name:
            abort(400, "event name is required")
        new_id = database.create_event(name)
        database.set_current_event_id(new_id)
        return jsonify({"events": database.list_events(), "current": new_id})

    @app.route("/api/events/current", methods=["POST"])
    def api_set_current_event():
        data = request.get_json(silent=True) or {}
        try:
            event_id = int(data["event_id"])
        except (KeyError, TypeError, ValueError):
            abort(400, "event_id (integer) is required")
        try:
            database.set_current_event_id(event_id)
        except ValueError as exc:
            abort(404, str(exc))
        return jsonify({"events": database.list_events(), "current": event_id})

    @app.route("/api/events/<int:event_id>/rename", methods=["POST"])
    def api_rename_event(event_id):
        data = request.get_json(silent=True) or {}
        try:
            database.rename_event(event_id, data.get("name", ""))
        except ValueError as exc:
            abort(400, str(exc))
        return jsonify({"events": database.list_events(),
                        "current": database.get_current_event_id()})

    @app.route("/api/events/<int:event_id>", methods=["DELETE"])
    def api_delete_event(event_id):
        """Delete an event: its stills, records and its recordings (video)."""
        if recorder.is_recording():
            abort(409, "stop recording first")
        _delete_capture_files(database.get_filenames_for_event(event_id))
        for r in database.list_recordings(event_id):
            Recorder.delete_recording_files(r["dirname"])
            database.delete_recording(r["id"])
        database.delete_event(event_id)
        return jsonify({"events": database.list_events(),
                        "current": database.get_current_event_id()})

    @app.route("/api/purge", methods=["POST"])
    def api_purge():
        """Clear all stills + OCR records for an event (video is kept)."""
        data = request.get_json(silent=True) or {}
        event_id = data.get("event_id")
        if event_id is None:
            event_id = database.get_current_event_id()
        _delete_capture_files(database.get_filenames_for_event(int(event_id)))
        removed = database.purge_event(int(event_id))
        return jsonify({"purged_captures": removed, "event_id": event_id})

    def _delete_capture_files(filenames):
        for name in filenames:
            try:
                (config.CAPTURE_DIR / os.path.basename(name)).unlink(missing_ok=True)
            except Exception as exc:
                print(f"[purge] could not delete {name}: {exc}")

    # -- disk / system ----------------------------------------------------------------

    @app.route("/api/diskusage")
    def api_diskusage():
        cap_bytes, cap_count = 0, 0
        try:
            with os.scandir(config.CAPTURE_DIR) as it:
                for entry in it:
                    if entry.is_file():
                        cap_bytes += entry.stat().st_size
                        cap_count += 1
        except FileNotFoundError:
            pass
        st = recorder.storage_summary()
        return jsonify({"total": st["disk_total"], "used": st["disk_used"],
                        "free": st["disk_free"], "captures_bytes": cap_bytes,
                        "captures_count": cap_count,
                        "recordings_bytes": st["recordings_bytes"],
                        "cap_bytes": st["cap_bytes"]})

    @app.route("/api/shutdown", methods=["POST"])
    def api_shutdown():
        import subprocess
        if not config.SHUTDOWN_ENABLED:
            abort(403, "shutdown is disabled in config")
        if recorder.is_recording():
            recorder.stop()
        try:
            proc = subprocess.run(config.SHUTDOWN_COMMAND, capture_output=True,
                                  text=True, timeout=10)
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500
        if proc.returncode != 0:
            msg = (proc.stderr or proc.stdout or "").strip() or "shutdown failed"
            print(f"[shutdown] failed: {msg}")
            return jsonify({"ok": False, "error": msg}), 500
        print("[shutdown] powering off now")
        return jsonify({"ok": True})

    @app.route("/api/restart", methods=["POST"])
    def api_restart():
        """Restart the app: stop recording cleanly, then exit non-zero so the
        systemd unit (Restart=on-failure) brings it straight back."""
        import threading

        def _go():
            time.sleep(0.3)
            try:
                if recorder.is_recording():
                    recorder.stop()
            finally:
                print("[web] restart requested - exiting for systemd to relaunch")
                os._exit(3)
        threading.Thread(target=_go, daemon=True).start()
        return jsonify({"ok": True})

    # -- serve stored stills -------------------------------------------------------------

    @app.route("/captures/<path:filename>")
    def captures(filename):
        return send_from_directory(config.CAPTURE_DIR, filename)

    return app
