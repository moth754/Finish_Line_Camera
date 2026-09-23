/*
 * playback.js
 * ===========
 * Playback tab: recorded video review.
 *
 *   - recording selector (the current event's recordings, newest first;
 *     a recording in progress is playable up to its last closed segment)
 *   - a big <video> that plays the MP4 segments back to back, with zoom/pan
 *   - a timeline canvas: green where video exists (this recording's segments,
 *     plus the event's other recordings as bands you can click to open), red
 *     bars where motion was detected in the zone; click-to-seek, wheel / button
 *     zoom (2 s .. 12 h across the screen), pan, and draggable IN / OUT markers
 *   - transport: play/pause, frame step, +/-1 s, +/-10 s, speed
 *   - "Send snapshot to OCR" (exact frame -> still -> OCR) and "Send range to OCR"
 *     (motion-filtered frames of the IN..OUT range -> stills -> OCR), with a
 *     live job list
 *
 * Time model: every segment knows its wall-clock start/end (from the sensor
 * timestamps recorded live) and its video duration (frames / fps).  Video time
 * within a segment is mapped linearly onto that wall-clock span, so the clock
 * shown is the real time the frame was taken.
 */

"use strict";

const Playback = (function () {
    const video = $("#pb-video");
    const stage = $("#pb-stage");
    const zoomBox = $("#pb-zoom");
    const canvas = $("#pb-timeline");
    const ctx = canvas.getContext("2d");

    const TL_MIN_SPAN = 2;              // seconds across the timeline when fully zoomed in
    const TL_MAX_SPAN = 12 * 3600;      // ...and when fully zoomed out (or 1.2x the recording, if longer)

    const S = {
        recs: [], rec: null, segs: [], segIdx: -1,
        playing: false, speed: 1,
        view: null,                // {t0, t1} epochs shown on the timeline
        markIn: null, markOut: null,
        pendingSeek: null, pendingPlay: false,
        zoom: 1, tx: 0, ty: 0,
        raf: null, refreshTimer: null, jobsTimer: null,
        motionThr: 0.02,
        baseCache: null,           // offscreen canvas with the motion bars
        active: false,
        follow: true,
    };

    // ---- recordings list ----------------------------------------------------

    async function loadRecordings(selectId) {
        try {
            const d = await App.api("/api/recordings");
            S.recs = d.recordings;
        } catch (e) { return; }
        const sel = $("#pb-rec-select");
        const prev = selectId != null ? String(selectId) : sel.value;
        sel.innerHTML = "";
        for (const r of S.recs) {
            const opt = document.createElement("option");
            opt.value = r.id;
            const live = r.status === "recording" ? " ● LIVE" : "";
            opt.textContent = `${App.fmtDate(r.start_epoch)} ${App.fmtClock(r.start_epoch)} · `
                + `${App.fmtDuration(r.duration)} · ${App.fmtBytes(r.bytes)}${live}`;
            sel.appendChild(opt);
        }
        if (!S.recs.length) {
            S.rec = null; S.segs = []; S.segIdx = -1;
            video.removeAttribute("src");
            $("#pb-empty").hidden = false;
            $("#pb-rec-info").textContent = "";
            draw();
            return;
        }
        const want = S.recs.some((r) => String(r.id) === prev) ? prev : String(S.recs[0].id);
        sel.value = want;
        if (!S.rec || String(S.rec.id) !== want) await loadRecording(parseInt(want, 10));
    }

    async function loadRecording(id, keepView = false) {
        let d;
        try { d = await App.api(`/api/recordings/${id}`); }
        catch (e) { App.toast("Could not load recording: " + e.message, true); return; }
        const isNew = !S.rec || S.rec.id !== d.recording.id;
        S.rec = d.recording;
        S.motionThr = d.motion_threshold || 0.02;
        S.segs = d.segments.map((s) => ({
            ...s, vdur: s.n_frames / s.fps, wdur: Math.max(0.001, s.end_epoch - s.start_epoch),
        }));
        S.motionSecs = null;
        if (isNew) {
            S.segIdx = -1; S.markIn = S.markOut = null; S.zoom = 1;
            applyZoom();
            video.removeAttribute("src");
            S.playing = false;
            updatePlayBtn();
        }
        const r = S.rec;
        const end = r.end_epoch || (S.segs.length ? S.segs[S.segs.length - 1].end_epoch : r.start_epoch + 1);
        if (isNew || !keepView || !S.view) S.view = { t0: r.start_epoch, t1: Math.max(end, r.start_epoch + 10) };
        else if (r.live) S.view.t1 = Math.max(S.view.t1, end);
        $("#pb-rec-info").textContent =
            `${r.name} · ${r.width}×${r.height} @ ${r.fps} fps · ${S.segs.length} segment(s)`
            + (r.live ? " · recording now" : "");
        $("#pb-empty").hidden = S.segs.length > 0;
        if (!S.segs.length) {
            $("#pb-empty").textContent = r.live
                ? "Recording… the first segment becomes playable when it closes."
                : "This recording has no video (segments deleted by the storage cap).";
        }
        if (isNew && S.segs.length) seekEpoch(S.segs[0].start_epoch);
        S.baseCache = null;
        updateRangeText();
        draw();
        clearTimeout(S.refreshTimer);
        if (r.live && S.active) S.refreshTimer = setTimeout(() => loadRecording(r.id, true), 5000);
    }

    // ---- time mapping ----------------------------------------------------------

    function segForEpoch(e) {
        for (let i = 0; i < S.segs.length; i++) {
            const s = S.segs[i];
            if (e >= s.start_epoch && e < s.end_epoch) return i;
        }
        // Not covered: the next segment after e, else the last one.
        for (let i = 0; i < S.segs.length; i++) if (S.segs[i].start_epoch > e) return i;
        return S.segs.length - 1;
    }
    function segTime(s, e) {
        const f = Math.min(1, Math.max(0, (e - s.start_epoch) / s.wdur));
        return Math.min(Math.max(0, s.vdur - 0.001), f * s.vdur);
    }
    function segEpoch(s, t) {
        return s.start_epoch + (t / s.vdur) * s.wdur;
    }
    function currentEpoch() {
        if (S.segIdx < 0 || !S.segs[S.segIdx]) return S.view ? S.view.t0 : null;
        return segEpoch(S.segs[S.segIdx], video.currentTime || 0);
    }

    function seekEpoch(e, play = null) {
        if (!S.segs.length) return;
        const i = segForEpoch(e);
        const s = S.segs[i];
        const t = segTime(s, e);
        if (i !== S.segIdx || !video.getAttribute("src")) {
            S.segIdx = i;
            S.pendingSeek = t;
            S.pendingPlay = play === null ? S.playing : play;
            video.src = s.url;
            video.playbackRate = S.speed;
            video.load();
        } else {
            video.currentTime = t;
            if (play === true) doPlay();
            else if (play === false) doPause();
        }
        updateClock();
    }

    video.addEventListener("loadedmetadata", () => {
        if (S.pendingSeek != null) {
            video.currentTime = S.pendingSeek;
            S.pendingSeek = null;
        }
        video.playbackRate = S.speed;
        if (S.pendingPlay) doPlay();
    });
    video.addEventListener("ended", () => {
        const next = S.segIdx + 1;
        if (next < S.segs.length) seekEpoch(S.segs[next].start_epoch, true);
        else { S.playing = false; updatePlayBtn(); }
    });
    video.addEventListener("error", () => {
        if (video.getAttribute("src")) App.toast("Video playback error (segment unreadable?)", true);
    });

    // ---- transport ---------------------------------------------------------------

    function doPlay() {
        if (!S.segs.length) return;
        if (!video.getAttribute("src")) { seekEpoch(S.view.t0, true); return; }
        S.playing = true;
        video.playbackRate = S.speed;
        const p = video.play();
        if (p && p.catch) p.catch(() => {});
        updatePlayBtn();
    }
    function doPause() {
        S.playing = false;
        video.pause();
        updatePlayBtn();
    }
    function updatePlayBtn() { $("#pb-play").textContent = S.playing ? "❚❚" : "▶"; }

    function stepFrame(dir) {
        if (S.segIdx < 0) return;
        doPause();
        const s = S.segs[S.segIdx];
        const dt = 1 / s.fps;
        let t = video.currentTime + dir * dt;
        if (t < 0) {
            if (S.segIdx > 0) { const p = S.segs[S.segIdx - 1]; seekEpoch(p.end_epoch - dt * (p.wdur / p.vdur), false); }
            return;
        }
        if (t >= s.vdur - dt / 2) {
            if (S.segIdx + 1 < S.segs.length) seekEpoch(S.segs[S.segIdx + 1].start_epoch, false);
            return;
        }
        video.currentTime = t;
        updateClock();
    }
    function skip(sec) {
        const e = currentEpoch();
        if (e == null) return;
        seekEpoch(e + sec);
    }

    $("#pb-play").addEventListener("click", () => S.playing ? doPause() : doPlay());
    $("#pb-prev").addEventListener("click", () => stepFrame(-1));
    $("#pb-next").addEventListener("click", () => stepFrame(1));
    $("#pb-back1").addEventListener("click", () => skip(-1));
    $("#pb-fwd1").addEventListener("click", () => skip(1));
    $("#pb-back10").addEventListener("click", () => skip(-10));
    $("#pb-fwd10").addEventListener("click", () => skip(10));
    $("#pb-speed").addEventListener("change", () => {
        S.speed = parseFloat($("#pb-speed").value) || 1;
        video.playbackRate = S.speed;
    });
    $("#pb-live-btn").addEventListener("click", () => {
        if (!S.segs.length) return;
        const last = S.segs[S.segs.length - 1];
        seekEpoch(Math.max(last.start_epoch, last.end_epoch - 3), true);
    });
    $("#pb-refresh").addEventListener("click", () => loadRecordings());
    $("#pb-rec-select").addEventListener("change", (ev) => loadRecording(parseInt(ev.target.value, 10)));
    $("#pb-delete").addEventListener("click", async () => {
        if (!S.rec) return;
        if (!confirm(`Delete the video of "${S.rec.name}"?\n\nStills and OCR results already taken from it are kept. This cannot be undone.`)) return;
        try { await App.api(`/api/recordings/${S.rec.id}`, "DELETE"); }
        catch (e) { App.toast("Delete failed: " + e.message, true); return; }
        S.rec = null;
        await loadRecordings();
    });

    document.addEventListener("keydown", (ev) => {
        if (!S.active) return;
        if (ev.target && (ev.target.tagName === "INPUT" || ev.target.tagName === "SELECT")) return;
        switch (ev.key) {
            case " ": ev.preventDefault(); S.playing ? doPause() : doPlay(); break;
            case "ArrowLeft": ev.preventDefault(); ev.shiftKey ? skip(-1) : stepFrame(-1); break;
            case "ArrowRight": ev.preventDefault(); ev.shiftKey ? skip(1) : stepFrame(1); break;
            case "[": setMark("in"); break;
            case "]": setMark("out"); break;
            case "j": skip(-10); break;
            case "l": skip(10); break;
            default: break;
        }
    });

    // ---- clock + animation loop ------------------------------------------------------

    function updateClock() {
        const e = currentEpoch();
        $("#pb-clock").textContent = App.fmtClock(e, true);
        $("#pb-date").textContent = e ? App.fmtDate(e) : "";
    }
    function tick() {
        if (!S.active) { S.raf = null; return; }
        updateClock();
        if (S.playing && S.follow && S.view) {
            const e = currentEpoch();
            if (e != null && (e > S.view.t1 || e < S.view.t0)) {
                const w = S.view.t1 - S.view.t0;
                S.view = { t0: e - w * 0.1, t1: e + w * 0.9 };
                S.baseCache = null;
            }
        }
        drawOverlay();
        S.raf = requestAnimationFrame(tick);
    }

    // ---- timeline -------------------------------------------------------------------

    function canvasSize() {
        const dpr = window.devicePixelRatio || 1;
        const rect = canvas.getBoundingClientRect();
        const W = Math.max(50, Math.round(rect.width)), H = Math.max(30, Math.round(rect.height));
        if (canvas.width !== Math.round(W * dpr) || canvas.height !== Math.round(H * dpr)) {
            canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
            S.baseCache = null;
        }
        return { W, H, dpr };
    }
    function t2x(t, W) { return (t - S.view.t0) / (S.view.t1 - S.view.t0) * W; }
    function x2t(x, W) { return S.view.t0 + x / W * (S.view.t1 - S.view.t0); }

    function buildBase(W, H, dpr) {
        const off = document.createElement("canvas");
        off.width = Math.round(W * dpr); off.height = Math.round(H * dpr);
        const c = off.getContext("2d");
        c.scale(dpr, dpr);
        c.fillStyle = "#11151c";
        c.fillRect(0, 0, W, H);
        if (!S.view) return off;
        const top = 12, bottom = H - 3;
        // Video coverage, in green: the event's other recordings first (dim
        // bands - click one to open it), then this recording's exact segments.
        for (const r of S.recs) {
            if (S.rec && r.id === S.rec.id) continue;
            const b = recBand(r);
            if (!b) continue;
            const x0 = t2x(b.t0, W), x1 = t2x(b.t1, W);
            if (x1 < 0 || x0 > W) continue;
            c.fillStyle = "#1f7a49";
            c.fillRect(x0, 2, Math.max(1, x1 - x0), top - 4);
            c.fillStyle = "rgba(54,208,127,0.08)";
            c.fillRect(x0, top, Math.max(1, x1 - x0), bottom - top);
        }
        for (const s of S.segs) {
            const x0 = t2x(s.start_epoch, W), x1 = t2x(s.end_epoch, W);
            if (x1 < 0 || x0 > W) continue;
            c.fillStyle = "#36d07f";
            c.fillRect(x0, 2, Math.max(1, x1 - x0), top - 4);
            c.fillStyle = "rgba(54,208,127,0.16)";
            c.fillRect(x0, top, Math.max(1, x1 - x0), bottom - top);
        }
        // Motion: max score per pixel column, from the per-second summaries.
        const secs = S.motionSecs || (S.motionSecs = motionBySecond());
        const thr = S.motionThr || 0.02;
        const span = S.view.t1 - S.view.t0;
        const secPerPx = span / W;
        for (let x = 0; x < W; x++) {
            const a = Math.floor(x2t(x, W)), b = Math.floor(x2t(x + 1, W));
            let m = 0;
            if (secPerPx > 2000) {                       // absurd zoom-out guard
                for (const [k, v] of secs) if (k >= a && k <= b && v > m) m = v;
            } else {
                for (let k = a; k <= b; k++) { const v = secs.get(k); if (v && v > m) m = v; }
            }
            if (m <= 0) continue;
            const hgt = Math.max(2, Math.min(1, m / (thr * 4)) * (bottom - top));
            c.fillStyle = m >= thr ? "#ff5a4a" : "#7a3d38";
            c.fillRect(x, bottom - hgt, Math.max(1, Math.ceil(1)), hgt);
        }
        // Threshold line.
        const ty = bottom - Math.min(1, 0.25) * (bottom - top);
        c.strokeStyle = "rgba(139,151,168,0.35)";
        c.setLineDash([3, 3]);
        c.beginPath(); c.moveTo(0, ty); c.lineTo(W, ty); c.stroke();
        c.setLineDash([]);
        // Time ticks.
        const step = niceStep(span / Math.max(2, W / 90));
        c.fillStyle = "#8b97a8"; c.font = "10px system-ui, sans-serif"; c.textBaseline = "top";
        const first = Math.ceil(S.view.t0 / step) * step;
        for (let t = first; t <= S.view.t1; t += step) {
            const x = t2x(t, W);
            c.fillStyle = "rgba(139,151,168,0.25)";
            c.fillRect(Math.round(x), top, 1, bottom - top);
            c.fillStyle = "#8b97a8";
            const lbl = App.fmtClock(t);
            c.fillText(step >= 60 ? lbl.slice(0, 5) : lbl, Math.round(x) + 3, top + 1);
        }
        return off;
    }
    function niceStep(s) {
        const steps = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800,
                       3600, 7200, 10800, 14400, 21600, 43200];
        for (const st of steps) if (st >= s) return st;
        return 86400;
    }
    // Max motion score per wall-clock second across all segments.  Built once per
    // segment list (the timeline is redrawn on every pan / zoom frame, and a 12 h
    // view would otherwise rebuild a ~43k-entry map each frame).
    function motionBySecond() {
        const secs = new Map();
        for (const s of S.segs) {
            const bins = s.motion_1s || [];
            for (let k = 0; k < bins.length; k++) {
                const key = Math.floor(s.start_epoch) + k;
                const v = bins[k];
                if (v > (secs.get(key) || 0)) secs.set(key, v);
            }
        }
        return secs;
    }

    function draw() {
        if (!S.view) { const { W, H } = canvasSize(); ctx.clearRect(0, 0, canvas.width, canvas.height); return; }
        S.baseCache = null;
        drawOverlay();
    }
    function updateViewLabels() {
        const { t0, t1 } = S.view;
        const d0 = App.fmtDate(t0), d1 = App.fmtDate(t1);
        $("#tl-start").textContent = d0 + " " + App.fmtClock(t0);
        $("#tl-end").textContent = (d1 !== d0 ? d1 + " " : "") + App.fmtClock(t1);
        const span = t1 - t0;
        $("#tl-span").textContent = fmtSpan(span);
        $("#tl-zoom-in").disabled = span <= TL_MIN_SPAN + 1e-6;
        $("#tl-zoom-out").disabled = span >= maxSpan() - 1e-6;
    }
    function fmtSpan(sec) {
        if (sec < 10) return sec.toFixed(1) + "s";
        sec = Math.round(sec);
        if (sec < 60) return sec + "s";
        const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
        if (!h) return s ? `${m}m ${s}s` : `${m}m`;
        return m ? `${h}h ${m}m` : `${h}h`;
    }
    function drawOverlay() {
        const { W, H, dpr } = canvasSize();
        if (!S.view) return;
        if (!S.baseCache) {
            S.baseCache = buildBase(W, H, dpr);
            updateViewLabels();
        }
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.drawImage(S.baseCache, 0, 0);
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        // IN / OUT range.
        if (S.markIn != null || S.markOut != null) {
            const a = S.markIn != null ? t2x(S.markIn, W) : -1e9;
            const b = S.markOut != null ? t2x(S.markOut, W) : 1e9;
            ctx.fillStyle = "rgba(96,165,250,0.18)";
            ctx.fillRect(Math.max(0, a), 0, Math.min(W, b) - Math.max(0, a), H);
            ctx.fillStyle = "#60a5fa";
            if (S.markIn != null) { ctx.fillRect(a - 1, 0, 2, H); ctx.fillText("[", a + 2, 0); }
            if (S.markOut != null) { ctx.fillRect(b - 1, 0, 2, H); ctx.fillText("]", b - 8, 0); }
        }
        // Cursor.
        const e = currentEpoch();
        if (e != null) {
            const x = t2x(e, W);
            ctx.fillStyle = "#ffffff";
            ctx.fillRect(Math.round(x) - 1, 0, 2, H);
        }
    }

    // Timeline interaction: click = seek, drag = pan, wheel = zoom, drag markers.
    let tl = { down: false, x0: 0, t0: 0, moved: false, handle: null, id: null };
    function markerHit(x, W) {
        if (S.markIn != null && Math.abs(t2x(S.markIn, W) - x) <= 7) return "in";
        if (S.markOut != null && Math.abs(t2x(S.markOut, W) - x) <= 7) return "out";
        return null;
    }
    canvas.addEventListener("pointerdown", (ev) => {
        if (!S.view) return;
        canvas.setPointerCapture(ev.pointerId);
        const { W } = canvasSize();
        const x = ev.clientX - canvas.getBoundingClientRect().left;
        tl = { down: true, x0: x, t0: S.view.t0, t1: S.view.t1, moved: false,
               handle: markerHit(x, W), id: ev.pointerId };
    });
    canvas.addEventListener("pointermove", (ev) => {
        if (!tl.down || !S.view) return;
        const { W } = canvasSize();
        const x = ev.clientX - canvas.getBoundingClientRect().left;
        if (tl.handle) {
            const t = clampToRec(x2t(x, W));
            if (tl.handle === "in") S.markIn = S.markOut != null ? Math.min(t, S.markOut) : t;
            else S.markOut = S.markIn != null ? Math.max(t, S.markIn) : t;
            updateRangeText();
            return;
        }
        if (Math.abs(x - tl.x0) > 4) tl.moved = true;
        if (tl.moved) {
            const span = tl.t1 - tl.t0;
            const dt = (x - tl.x0) / W * span;
            S.view = { t0: tl.t0 - dt, t1: tl.t1 - dt };
            S.follow = false;
            S.baseCache = null;
        }
    });
    canvas.addEventListener("pointerup", (ev) => {
        if (!tl.down) return;
        tl.down = false;
        if (tl.handle || tl.moved) return;
        const { W } = canvasSize();
        const x = ev.clientX - canvas.getBoundingClientRect().left;
        const t = x2t(x, W);
        const other = recordingAt(t);
        if (other) { openRecordingAt(other.id, t); return; }
        seekEpoch(clampToRec(t));
    });
    canvas.addEventListener("pointercancel", () => { tl.down = false; });
    canvas.addEventListener("wheel", (ev) => {
        if (!S.view) return;
        ev.preventDefault();
        const { W } = canvasSize();
        const x = ev.clientX - canvas.getBoundingClientRect().left;
        zoomView(ev.deltaY > 0 ? 1.3 : 1 / 1.3, x / W);
    }, { passive: false });
    canvas.addEventListener("dblclick", fitView);

    // Zoom the visible span by `factor` (> 1 zooms out), keeping the time at
    // fraction `frac` (0..1) of the timeline width where it is on screen.
    function zoomView(factor, frac) {
        if (!S.view) return;
        const span0 = S.view.t1 - S.view.t0;
        const t = S.view.t0 + span0 * frac;
        const span = clampSpan(span0 * factor);
        S.view = { t0: t - span * frac, t1: t + span * (1 - frac) };
        S.follow = false;
        S.baseCache = null;
    }
    function maxSpan() {
        const full = recSpan();
        return Math.max(TL_MAX_SPAN, (full.t1 - full.t0) * 1.2);
    }
    function clampSpan(span) { return Math.min(Math.max(span, TL_MIN_SPAN), maxSpan()); }
    function fitView() { if (S.rec) { S.view = recSpan(); S.follow = true; S.baseCache = null; } }

    // Zoom buttons: zoom about the playhead while it is on screen, else about the middle.
    function zoomAnchor() {
        const e = currentEpoch();
        if (S.view && e != null && e >= S.view.t0 && e <= S.view.t1) return (e - S.view.t0) / (S.view.t1 - S.view.t0);
        return 0.5;
    }
    $("#tl-zoom-in").addEventListener("click", () => zoomView(0.5, zoomAnchor()));
    $("#tl-zoom-out").addEventListener("click", () => zoomView(2, zoomAnchor()));
    $("#tl-zoom-fit").addEventListener("click", fitView);
    new ResizeObserver(() => { S.baseCache = null; }).observe(canvas);

    function recSpan() {
        if (!S.rec) return { t0: 0, t1: 1 };
        const end = S.rec.end_epoch || (S.segs.length ? S.segs[S.segs.length - 1].end_epoch : S.rec.start_epoch + 10);
        return { t0: S.rec.start_epoch, t1: Math.max(end, S.rec.start_epoch + 10) };
    }
    function clampToRec(t) {
        const r = recSpan();
        return Math.min(Math.max(t, r.t0), r.t1);
    }
    // Wall-clock span of a recording in the list (its closed segments).
    function recBand(r) {
        const t0 = r.first_segment_epoch != null ? r.first_segment_epoch : r.start_epoch;
        const t1 = r.last_segment_epoch != null ? r.last_segment_epoch : r.end_epoch;
        if (t0 == null || t1 == null || t1 <= t0) return null;
        return { t0, t1 };
    }
    // Another recording of the event (not the open one) whose video covers epoch t.
    function recordingAt(t) {
        for (const r of S.recs) {
            if (S.rec && r.id === S.rec.id) continue;
            const b = recBand(r);
            if (b && t >= b.t0 && t <= b.t1) return r;
        }
        return null;
    }
    // Open another recording at epoch t, keeping the timeline view where it is.
    async function openRecordingAt(id, t) {
        const view = S.view;
        $("#pb-rec-select").value = String(id);
        await loadRecording(id);
        if (!S.rec || S.rec.id !== id) return;
        S.view = view; S.follow = false; S.baseCache = null;
        seekEpoch(t, false);
    }

    // ---- IN / OUT + jobs ------------------------------------------------------------

    function setMark(which) {
        const e = currentEpoch();
        if (e == null) return;
        if (which === "in") { S.markIn = e; if (S.markOut != null && S.markOut < e) S.markOut = null; }
        else { S.markOut = e; if (S.markIn != null && S.markIn > e) S.markIn = null; }
        updateRangeText();
    }
    function updateRangeText() {
        const a = S.markIn, b = S.markOut;
        $("#pb-mark-in").classList.toggle("marked", a != null);
        $("#pb-mark-out").classList.toggle("marked", b != null);
        if (a == null && b == null) { $("#pb-range-text").textContent = "no range set"; return; }
        const dur = (a != null && b != null) ? ` (${App.fmtDuration(b - a)})` : "";
        $("#pb-range-text").textContent =
            `${a != null ? App.fmtClock(a, true) : "start"} → ${b != null ? App.fmtClock(b, true) : "end"}${dur}`;
    }
    $("#pb-mark-in").addEventListener("click", () => setMark("in"));
    $("#pb-mark-out").addEventListener("click", () => setMark("out"));
    $("#pb-range-text").addEventListener("click", () => { S.markIn = S.markOut = null; updateRangeText(); });

    $("#pb-frame-ocr").addEventListener("click", async () => {
        const e = currentEpoch();
        if (!S.rec || e == null) return;
        const btn = $("#pb-frame-ocr"), status = $("#pb-frame-ocr-status");
        btn.disabled = true;
        status.textContent = "cutting frame…";
        try {
            const r = await App.api(`/api/recordings/${S.rec.id}/snapshot`, "POST", { t: e });
            const o = (App.status && App.status.ocr) || {};
            const when = o.blocked
                ? ` – OCR is paused (${o.blocked_reason || "paused"}), it will be read when OCR resumes`
                : "";
            status.textContent = `sent ${App.fmtClock(r.epoch, true)} → capture #${r.capture_id}${when}`;
            App.toast(`Frame at ${App.fmtClock(r.epoch, true)} sent to OCR (#${r.capture_id})`);
        } catch (err) {
            status.textContent = "";
            App.toast("Could not cut frame: " + err.message, true);
        } finally { btn.disabled = false; }
    });

    $("#job-submit").addEventListener("click", async () => {
        if (!S.rec) return;
        const span = recSpan();
        let t0 = S.markIn != null ? S.markIn : span.t0;
        let t1 = S.markOut != null ? S.markOut : span.t1;
        if (S.markIn == null && S.markOut == null
            && !confirm(`No IN/OUT range set - send the WHOLE recording (${App.fmtDuration(t1 - t0)}) to OCR?`)) return;
        const options = {
            motion_only: $("#job-motion").checked,
            max_fps: parseFloat($("#job-maxfps").value) || 2,
            dedup: (parseFloat($("#job-dedup").value) || 0) / 100,
        };
        try {
            const r = await App.api("/api/ocr_jobs", "POST",
                { recording_id: S.rec.id, t_start: t0, t_end: t1, options });
            App.toast(`OCR job #${r.job_id} queued`);
            refreshJobs();
        } catch (e) { App.toast("Could not queue job: " + e.message, true); }
    });

    async function refreshJobs() {
        let d;
        try { d = await App.api("/api/ocr_jobs"); } catch (e) { return; }
        const list = $("#job-list");
        list.innerHTML = "";
        for (const j of d.jobs.slice(0, 12)) {
            const div = document.createElement("div");
            div.className = "job " + j.status;
            const pct = Math.round((j.progress || 0) * 100);
            const range = `${App.fmtClock(j.t_start)}–${App.fmtClock(j.t_end)}`;
            div.innerHTML = `<span class="st">${j.status}</span>`
                + `<span>#${j.id} · ${j.recording_name || ("rec " + j.recording_id)} · ${range}</span>`
                + `<div class="bar"><div class="fill" style="width:${pct}%"></div></div>`
                + `<span>${j.frames_kept} kept / ${j.frames_scanned} scanned</span>`
                + `<span class="hint">${j.message || ""}</span>`;
            const b = document.createElement("button");
            b.type = "button";
            b.textContent = (j.status === "pending" || j.status === "running") ? "cancel" : "✕";
            b.addEventListener("click", async () => {
                try { await App.api(`/api/ocr_jobs/${j.id}`, "DELETE"); } catch (e) { /* ignore */ }
                refreshJobs();
            });
            div.appendChild(b);
            list.appendChild(div);
        }
    }

    // ---- video zoom / pan -----------------------------------------------------------

    function applyZoom() {
        zoomBox.style.transform = `translate(${S.tx}px, ${S.ty}px) scale(${S.zoom})`;
        $("#zoom-val").textContent = S.zoom <= 1.001 ? "fit" : S.zoom.toFixed(1) + "×";
        stage.classList.toggle("grab", S.zoom > 1);
    }
    function clampPan() {
        const W = stage.clientWidth, H = stage.clientHeight;
        S.tx = Math.min(0, Math.max(W - W * S.zoom, S.tx));
        S.ty = Math.min(0, Math.max(H - H * S.zoom, S.ty));
    }
    function zoomAt(factor, sx, sy) {
        const z = Math.min(12, Math.max(1, S.zoom * factor));
        // Keep the content point under (sx, sy) fixed.
        const cx = (sx - S.tx) / S.zoom, cy = (sy - S.ty) / S.zoom;
        S.zoom = z;
        S.tx = sx - cx * z; S.ty = sy - cy * z;
        if (z <= 1) { S.tx = 0; S.ty = 0; }
        clampPan();
        applyZoom();
    }
    stage.addEventListener("wheel", (ev) => {
        ev.preventDefault();
        const r = stage.getBoundingClientRect();
        zoomAt(ev.deltaY < 0 ? 1.25 : 1 / 1.25, ev.clientX - r.left, ev.clientY - r.top);
    }, { passive: false });
    $("#zoom-in").addEventListener("click", () => zoomAt(1.5, stage.clientWidth / 2, stage.clientHeight / 2));
    $("#zoom-out").addEventListener("click", () => zoomAt(1 / 1.5, stage.clientWidth / 2, stage.clientHeight / 2));
    $("#zoom-reset").addEventListener("click", () => { S.zoom = 1; S.tx = S.ty = 0; applyZoom(); });

    let pan = { down: false, x: 0, y: 0, moved: false };
    stage.addEventListener("pointerdown", (ev) => {
        if (ev.target.closest(".pb-zoom-ctl")) return;
        pan = { down: true, x: ev.clientX, y: ev.clientY, moved: false };
        stage.setPointerCapture(ev.pointerId);
        stage.classList.toggle("grabbing", S.zoom > 1);
    });
    stage.addEventListener("pointermove", (ev) => {
        if (!pan.down) return;
        const dx = ev.clientX - pan.x, dy = ev.clientY - pan.y;
        if (Math.abs(dx) > 3 || Math.abs(dy) > 3) pan.moved = true;
        if (S.zoom > 1 && pan.moved) {
            S.tx += dx; S.ty += dy;
            pan.x = ev.clientX; pan.y = ev.clientY;
            clampPan(); applyZoom();
        }
    });
    stage.addEventListener("pointerup", (ev) => {
        if (!pan.down) return;
        pan.down = false;
        stage.classList.remove("grabbing");
        if (!pan.moved && !ev.target.closest(".pb-zoom-ctl")) { S.playing ? doPause() : doPlay(); }
    });

    // ---- public + lifecycle -------------------------------------------------------------

    async function jumpTo(recId, epoch) {
        App.showTab("playback");
        if (!S.rec || S.rec.id !== recId) {
            await loadRecordings(recId);
            if (!S.rec || S.rec.id !== recId) await loadRecording(recId);
        }
        if (S.rec && S.rec.id === recId) {
            const w = S.view ? (S.view.t1 - S.view.t0) : 60;
            const span = Math.min(w, 120);
            S.view = { t0: epoch - span / 2, t1: epoch + span / 2 };
            S.baseCache = null;
            seekEpoch(epoch, false);
        }
    }

    App.tabShown.playback = () => {
        S.active = true;
        loadRecordings();
        refreshJobs();
        clearInterval(S.jobsTimer);
        S.jobsTimer = setInterval(refreshJobs, 3000);
        if (!S.raf) S.raf = requestAnimationFrame(tick);
    };
    App.tabHidden.playback = () => {
        S.active = false;
        clearInterval(S.jobsTimer); S.jobsTimer = null;
        clearTimeout(S.refreshTimer);
        doPause();
    };
    App.eventsListeners.push(() => { if (S.active) loadRecordings(); });

    return { jumpTo, state: S };
})();
window.Playback = Playback;
