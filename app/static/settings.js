/*
 * settings.js
 * ===========
 * Settings tab: camera mode / quality / preview, focus, motion sensitivity,
 * storage cap, OCR options, extraction defaults, events, system.
 * Most controls save immediately; the camera mode needs "Apply" (restarts
 * the camera, refused while recording).
 */

"use strict";

(function () {
    let modes = [];
    let settings = {};
    let diskTimer = null;
    // Which setting the Mode dropdown writes to: "record_mode" for the Pi
    // camera's presets, "usb_mode" for the modes a webcam reports itself.
    let modeSetting = "record_mode";
    let cameras = [];

    function modeByKey(k) { return modes.find((m) => m.key === k); }

    function fillCameras(list, selected) {
        cameras = list || [];
        const sel = $("#s-camera");
        sel.innerHTML = "";
        const auto = document.createElement("option");
        auto.value = "auto";
        auto.textContent = "Auto (use whatever is connected)";
        sel.appendChild(auto);
        for (const c of cameras) {
            const opt = document.createElement("option");
            opt.value = c.id;
            const kind = c.kind === "usb" ? "USB" : (c.kind === "csi" ? "Pi" : "");
            opt.textContent = kind ? `${kind}: ${c.name}` : c.name;
            sel.appendChild(opt);
        }
        // A camera saved while it was plugged in may no longer be listed;
        // keep showing it rather than silently snapping to something else.
        if (selected && selected !== "auto" && !cameras.some((c) => c.id === selected)) {
            const missing = document.createElement("option");
            missing.value = selected;
            missing.textContent = `${selected} (not connected)`;
            sel.appendChild(missing);
        }
        sel.value = selected || "auto";
    }

    function showCameraNote(p) {
        const chosen = $("#s-camera").value;
        const entry = cameras.find((c) => c.id === (chosen === "auto" ? p.resolved_camera : chosen));
        const bits = [];
        if (chosen === "auto" && p.resolved_camera) {
            const auto = cameras.find((c) => c.id === p.resolved_camera);
            bits.push(`currently resolves to ${auto ? auto.name : p.resolved_camera}`);
        }
        if (entry && entry.detail) bits.push(entry.detail);
        if (entry && entry.kind === "usb") {
            bits.push("frame times are arrival times, about one frame later than the sensor");
        }
        if (entry && entry.kind === "mock") {
            bits.push("synthetic picture - nothing is really being filmed");
        }
        $("#s-camera-note").textContent = bits.join(" · ");
    }

    function fillFromPayload(p) {
        settings = p.settings;
        modes = p.modes;
        modeSetting = p.mode_setting || "record_mode";
        fillCameras(p.cameras, settings.camera_id);
        showCameraNote(p);
        const sel = $("#s-record-mode");
        sel.innerHTML = "";
        for (const m of modes) {
            const opt = document.createElement("option");
            opt.value = m.key;
            opt.textContent = m.label;
            sel.appendChild(opt);
        }
        if (!modes.length) {
            const opt = document.createElement("option");
            opt.textContent = "no modes reported by this camera";
            sel.appendChild(opt);
        }
        sel.value = p.wanted_mode;
        showModeNote();
        $("#s-record-crf").value = settings.record_crf;
        $("#s-record-crf-val").textContent = settings.record_crf;
        $("#s-preview-width").value = String(settings.preview_width);
        $("#s-preview-fps").value = settings.preview_fps;
        $("#s-storage-cap").value = settings.storage_cap_gb;
        $("#s-min-free").value = settings.min_free_gb;
        $("#s-segment").value = settings.segment_seconds;
        $("#s-ocr-while-rec").checked = !!settings.ocr_while_recording;
        $("#s-extract-motion").checked = !!settings.extract_motion_only;
        $("#s-extract-maxfps").value = settings.extract_max_fps;
        $("#s-extract-dedup").value = (settings.extract_dedup * 100).toFixed(1).replace(/\.0$/, "");
        describeCamera(p.camera, p.camera_matches, p.wanted_mode);
    }

    function showModeNote() {
        const m = modeByKey($("#s-record-mode").value);
        $("#s-mode-note").textContent = (m && m.note) ? m.note : "";
    }

    function describeCamera(c, matches, wantedMode) {
        const wanted = modeByKey(wantedMode);
        $("#s-camera-state").textContent =
            `camera now: ${c.camera_name || c.camera_type} — `
            + `${c.record_size[0]}×${c.record_size[1]} @ ${c.fps} fps, `
            + `preview ${c.preview_size[0]}×${c.preview_size[1]}`
            + (matches ? "" : ` — press Apply to switch to ${wanted ? wanted.label : wantedMode}`);
        $("#s-camera-apply").classList.toggle("primary", !matches);
        // The focus panel only means anything on a camera with a movable lens.
        const focusCard = $("#focus-mode") && $("#focus-mode").closest(".card");
        if (focusCard) {
            focusCard.classList.toggle("disabled", !c.supports_focus);
            focusCard.title = c.supports_focus
                ? "" : "This camera has no controllable focus";
        }
    }

    async function save(changes) {
        try {
            const p = await App.api("/api/settings", "POST", changes);
            fillFromPayload(p);
        } catch (e) { App.toast("Could not save: " + e.message, true); }
    }

    async function load() {
        try { fillFromPayload(await App.api("/api/settings")); }
        catch (e) { /* ignore */ }
    }

    // --- camera & recording -------------------------------------------------
    $("#s-record-mode").addEventListener("change", () => {
        showModeNote();
        save({ [modeSetting]: $("#s-record-mode").value });
    });
    $("#s-camera").addEventListener("change", () => {
        // Switching camera changes which modes exist, so reload the whole
        // payload rather than patching the dropdown in place.
        save({ camera_id: $("#s-camera").value });
    });
    $("#s-camera-rescan").addEventListener("click", async () => {
        try {
            await App.api("/api/cameras");     // forces a fresh device scan
            await load();
            App.toast("Camera list refreshed");
        } catch (e) { App.toast("Rescan failed: " + e.message, true); }
    });
    $("#s-record-crf").addEventListener("input", () => {
        $("#s-record-crf-val").textContent = $("#s-record-crf").value;
    });
    $("#s-record-crf").addEventListener("change", () =>
        save({ record_crf: parseInt($("#s-record-crf").value, 10) }));
    $("#s-preview-width").addEventListener("change", () =>
        save({ preview_width: parseInt($("#s-preview-width").value, 10) }));
    $("#s-preview-fps").addEventListener("change", () =>
        save({ preview_fps: parseInt($("#s-preview-fps").value, 10) }));
    $("#s-camera-apply").addEventListener("click", async () => {
        if (App.recording) { App.toast("Stop recording before changing the camera", true); return; }
        const btn = $("#s-camera-apply");
        btn.disabled = true;
        btn.textContent = "Applying…";
        try {
            const p = await App.api("/api/camera/apply", "POST", {});
            fillFromPayload(p);
            App.toast("Camera reconfigured");
        } catch (e) { App.toast("Camera apply failed: " + e.message, true); }
        finally { btn.disabled = false; btn.textContent = "Apply camera settings"; }
    });

    // --- storage ------------------------------------------------------------
    $("#s-storage-cap").addEventListener("change", () =>
        save({ storage_cap_gb: parseFloat($("#s-storage-cap").value) }));
    $("#s-min-free").addEventListener("change", () =>
        save({ min_free_gb: parseFloat($("#s-min-free").value) }));
    $("#s-segment").addEventListener("change", () =>
        save({ segment_seconds: parseInt($("#s-segment").value, 10) }));

    // --- OCR ----------------------------------------------------------------
    $("#s-ocr-while-rec").addEventListener("change", () =>
        save({ ocr_while_recording: $("#s-ocr-while-rec").checked }));
    $("#s-extract-motion").addEventListener("change", () =>
        save({ extract_motion_only: $("#s-extract-motion").checked }));
    $("#s-extract-maxfps").addEventListener("change", () =>
        save({ extract_max_fps: parseFloat($("#s-extract-maxfps").value) }));
    $("#s-extract-dedup").addEventListener("change", () =>
        save({ extract_dedup: parseFloat($("#s-extract-dedup").value) / 100 }));

    const ocrPanels = $("#ocr-panels"), detectWidth = $("#detect-width");
    let detectTimer = null;
    async function loadOcr() {
        try {
            const o = await App.api("/api/ocr");
            ocrPanels.checked = !!o.panel_proposals;
            detectWidth.value = o.detect_width;
            $("#detect-width-val").textContent = o.detect_width;
        } catch (e) { /* ignore */ }
    }
    ocrPanels.addEventListener("change", () =>
        App.api("/api/ocr", "POST", { panel_proposals: ocrPanels.checked }).catch(() => {}));
    detectWidth.addEventListener("input", () => {
        $("#detect-width-val").textContent = detectWidth.value;
        clearTimeout(detectTimer);
        detectTimer = setTimeout(() =>
            App.api("/api/ocr", "POST", { detect_width: parseInt(detectWidth.value, 10) }).catch(() => {}), 250);
    });

    // --- motion sensitivity ---------------------------------------------------
    const motionSlider = $("#motion-threshold"), motionVal = $("#motion-threshold-val");
    let motionTimer = null;
    async function loadMotion() {
        try {
            const m = await App.api("/api/motion");
            const pct = (m.area_threshold * 100).toFixed(1);
            motionSlider.value = pct;
            motionVal.textContent = pct;
        } catch (e) { /* ignore */ }
    }
    motionSlider.addEventListener("input", () => {
        motionVal.textContent = parseFloat(motionSlider.value).toFixed(1);
        clearTimeout(motionTimer);
        motionTimer = setTimeout(() =>
            App.api("/api/motion", "POST", { area_threshold: parseFloat(motionSlider.value) / 100 }).catch(() => {}), 250);
    });

    // --- focus -----------------------------------------------------------------
    const focusMode = $("#focus-mode"), focusDistance = $("#focus-distance");
    function updateFocusVisibility() {
        $("#focus-distance-wrap").style.display = focusMode.value === "manual" ? "inline" : "none";
    }
    function describeFocus(f) {
        $("#focus-current").textContent = f.mode === "continuous" ? "active: autofocus"
            : (f.distance_m > 0 ? `active: fixed @ ${f.distance_m} m` : "active: fixed @ infinity");
    }
    async function loadFocus() {
        try {
            const f = await App.api("/api/focus");
            focusMode.value = f.mode;
            if (f.mode === "manual" && f.distance_m > 0) focusDistance.value = f.distance_m;
            updateFocusVisibility();
            describeFocus(f);
        } catch (e) { /* ignore */ }
    }
    focusMode.addEventListener("change", updateFocusVisibility);
    $("#focus-apply").addEventListener("click", async () => {
        const body = { mode: focusMode.value };
        if (focusMode.value === "manual") body.distance_m = parseFloat(focusDistance.value) || 0;
        try { describeFocus(await App.api("/api/focus", "POST", body)); }
        catch (e) { App.toast("Focus failed: " + e.message, true); }
    });

    // --- events ------------------------------------------------------------------
    $("#s-event-select").addEventListener("change", (ev) => App.setCurrentEvent(ev.target.value));
    $("#s-event-rename").addEventListener("click", async () => {
        const id = $("#s-event-select").value;
        const name = prompt("Rename event:", App.currentEventName());
        if (!name) return;
        await App.api(`/api/events/${id}/rename`, "POST", { name });
        await App.loadEvents();
    });
    $("#s-event-delete").addEventListener("click", async () => {
        const id = $("#s-event-select").value;
        if (!confirm(`Delete event "${App.currentEventName()}" with ALL its recordings, `
                     + `stills and results?\n\nThis cannot be undone.`)) return;
        try { await App.api(`/api/events/${id}`, "DELETE"); }
        catch (e) { App.toast("Delete failed: " + e.message, true); }
        await App.loadEvents();
    });

    // --- disk / system -----------------------------------------------------------
    async function refreshDisk() {
        try {
            const d = await App.api("/api/diskusage");
            const pct = (n) => d.total ? (n / d.total * 100).toFixed(2) + "%" : "0%";
            $("#disk-video").style.width = pct(d.recordings_bytes);
            $("#disk-stills").style.width = pct(d.captures_bytes);
            $("#disk-used").style.width = pct(Math.max(0, d.used - d.recordings_bytes - d.captures_bytes));
            $("#s-storage-info").textContent =
                `video ${App.fmtBytes(d.recordings_bytes)} of ${App.fmtBytes(d.cap_bytes)} cap · `
                + `stills ${App.fmtBytes(d.captures_bytes)} (${d.captures_count}) · `
                + `disk ${App.fmtBytes(d.used)} used / ${App.fmtBytes(d.total)} · ${App.fmtBytes(d.free)} free`;
            const s = App.status;
            if (s) {
                $("#s-system-info").textContent =
                    `${s.camera.camera_type} · OCR engine: ${s.ocr.backend} · server time ${App.fmtClock(s.time)}`;
            }
        } catch (e) { /* ignore */ }
    }

    $("#restart-btn").addEventListener("click", async () => {
        if (!confirm("Restart the recorder app? Any recording in progress is stopped cleanly first.")) return;
        try { await App.api("/api/restart", "POST", {}); App.toast("Restarting… back in ~10 s"); }
        catch (e) { App.toast(e.message, true); }
    });
    $("#shutdown-btn").addEventListener("click", async () => {
        if (!confirm("Shut down the Raspberry Pi?\n\nRecording stops and the dashboard goes offline.")) return;
        if (!confirm("Are you ABSOLUTELY sure? You'll need physical access to power it back on.")) return;
        try {
            const r = await App.api("/api/shutdown", "POST", {});
            if (r.ok) document.body.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;'
                + 'height:100vh;font-size:1.3rem;">🔌 The Raspberry Pi is shutting down.</div>';
            else alert("Shutdown failed: " + r.error);
        } catch (e) {
            document.body.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;'
                + 'height:100vh;font-size:1.3rem;">🔌 The Raspberry Pi is shutting down.</div>';
        }
    });

    App.tabShown.settings = () => {
        load(); loadOcr(); loadMotion(); loadFocus(); refreshDisk();
        clearInterval(diskTimer);
        diskTimer = setInterval(refreshDisk, 10000);
    };
    App.tabHidden.settings = () => { clearInterval(diskTimer); diskTimer = null; };
    App.statusListeners.push((s) => {
        $("#s-camera-apply").disabled = !!s.recorder.recording;
    });
})();
