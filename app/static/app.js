/*
 * app.js
 * ======
 * Core of the single-page UI: tab switching, the /api/status poll that feeds
 * the header (REC button, storage, OCR backlog, connection), the event
 * selector, and small shared helpers used by the tab modules
 * (live.js / playback.js / results.js / settings.js).
 *
 * Dependency-free (plain DOM APIs) so there is nothing to build/install.
 */

"use strict";

const $ = (sel) => document.querySelector(sel);

const App = {
    tab: "live",
    status: null,            // last /api/status payload
    clockOffset: 0,          // server epoch - browser epoch (seconds)
    tabShown: {},            // name -> callback when the tab becomes visible
    tabHidden: {},           // name -> callback when it is hidden
    statusListeners: [],     // (status) => {}
    eventsListeners: [],     // (events, currentId) => {}
    events: [],
    currentEventId: null,

    // ---- fetch helper -----------------------------------------------------
    async api(path, method = "GET", body = undefined) {
        const opts = { method, headers: {} };
        if (body !== undefined) {
            opts.headers["Content-Type"] = "application/json";
            opts.body = JSON.stringify(body);
        }
        const resp = await fetch(path, opts);
        let data = null;
        const text = await resp.text();
        try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; }
        if (!resp.ok) {
            const msg = (data && (data.error || data.message))
                || (text && text.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim().slice(0, 200))
                || `${resp.status} ${resp.statusText}`;
            throw new Error(msg);
        }
        return data;
    },

    // ---- formatting -------------------------------------------------------
    fmtBytes(n) {
        if (n == null || isNaN(n)) return "–";
        const u = ["B", "KB", "MB", "GB", "TB"];
        let i = 0;
        while (n >= 1000 && i < u.length - 1) { n /= 1000; i++; }
        return n.toFixed(n < 10 && i > 0 ? 1 : 0) + " " + u[i];
    },
    pad(n, w = 2) { return String(n).padStart(w, "0"); },
    fmtClock(epoch, ms = false) {
        if (epoch == null || isNaN(epoch)) return ms ? "--:--:--.---" : "--:--:--";
        const d = new Date(epoch * 1000);
        let s = `${App.pad(d.getHours())}:${App.pad(d.getMinutes())}:${App.pad(d.getSeconds())}`;
        if (ms) s += "." + App.pad(d.getMilliseconds(), 3);
        return s;
    },
    fmtDate(epoch) {
        if (epoch == null) return "";
        const d = new Date(epoch * 1000);
        return `${d.getFullYear()}-${App.pad(d.getMonth() + 1)}-${App.pad(d.getDate())}`;
    },
    fmtDuration(sec) {
        if (sec == null || isNaN(sec)) return "–";
        sec = Math.max(0, Math.round(sec));
        const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
        return h ? `${h}:${App.pad(m)}:${App.pad(s)}` : `${m}:${App.pad(s)}`;
    },

    // ---- toast ------------------------------------------------------------
    toast(msg, isError = false) {
        let el = $("#toast");
        if (!el) {
            el = document.createElement("div");
            el.id = "toast";
            el.style.cssText = "position:fixed;left:50%;bottom:1.2rem;transform:translateX(-50%);"
                + "background:#232b38;color:#e6e9ef;border:1px solid #2e3744;border-radius:8px;"
                + "padding:0.5rem 0.9rem;font-size:0.9rem;z-index:99;box-shadow:0 4px 18px rgba(0,0,0,0.5);"
                + "max-width:80vw;";
            document.body.appendChild(el);
        }
        el.textContent = msg;
        el.style.borderColor = isError ? "#b23b3b" : "#1f8a52";
        el.style.display = "block";
        clearTimeout(App._toastTimer);
        App._toastTimer = setTimeout(() => { el.style.display = "none"; }, isError ? 6000 : 3000);
    },

    // ---- tabs -------------------------------------------------------------
    showTab(name) {
        if (App.tab === name) return;
        const prev = App.tab;
        App.tab = name;
        document.querySelectorAll(".tab-btn").forEach((b) =>
            b.classList.toggle("active", b.dataset.tab === name));
        document.querySelectorAll("main .tab").forEach((s) =>
            s.classList.toggle("active", s.id === "tab-" + name));
        if (App.tabHidden[prev]) App.tabHidden[prev]();
        if (App.tabShown[name]) App.tabShown[name]();
        try { localStorage.setItem("fl-tab", name); } catch (e) { /* ignore */ }
    },

    // ---- status poll ------------------------------------------------------
    async refreshStatus() {
        const conn = $("#chip-conn");
        try {
            const s = await App.api("/api/status");
            App.status = s;
            App.clockOffset = s.time - Date.now() / 1000;
            conn.textContent = s.camera.camera_type === "MockCameraBackend"
                ? "mock camera" : `camera ${s.camera.loop_fps} fps`;
            conn.className = "chip status ok";
            App.updateRec(s.recorder);
            App.updateStorage(s.storage);
            App.updateOcrChip(s.ocr);
            if (App.currentEventId !== null && s.event_id !== App.currentEventId) {
                App.loadEvents();          // changed elsewhere (another browser)
            }
            App.statusListeners.forEach((fn) => { try { fn(s); } catch (e) { console.error(e); } });
        } catch (e) {
            conn.textContent = "disconnected";
            conn.className = "chip status err";
        }
    },

    updateRec(r) {
        const btn = $("#rec-btn"), info = $("#rec-info");
        App.recording = !!r.recording;
        btn.classList.toggle("on", App.recording);
        btn.textContent = App.recording ? "■ STOP" : "● REC";
        if (r.error) {
            info.textContent = "⚠ " + r.error;
            info.className = "rec-info err";
            return;
        }
        if (App.recording) {
            const drop = r.dropped ? ` · ${r.dropped} dropped` : "";
            info.textContent = `${App.fmtDuration(r.elapsed)} · ${r.width}×${r.height} `
                + `${r.fps_actual}/${r.fps_target} fps${drop} · ${r.segments} seg`;
            const bad = r.fps_actual < r.fps_target * 0.9 && r.elapsed > 5;
            info.className = "rec-info" + (bad || r.dropped > 0 ? " warn" : "");
        } else {
            info.textContent = `${r.width}×${r.height} @ ${r.fps_target}`;
            info.className = "rec-info";
        }
    },

    updateStorage(st) {
        const el = $("#chip-storage");
        const pct = st.cap_bytes ? Math.min(100, st.recordings_bytes / st.cap_bytes * 100) : 0;
        el.textContent = `📼 ${App.fmtBytes(st.recordings_bytes)} / ${App.fmtBytes(st.cap_bytes)}`
            + ` · ${App.fmtBytes(st.disk_free)} free`;
        el.className = "chip" + (pct > 90 ? " warn" : "") + (st.disk_free < 3e9 ? " err" : "");
    },

    updateOcrChip(o) {
        const el = $("#chip-ocr");
        el.hidden = false;
        if (o.backlog > 0) {
            const why = o.blocked ? " (waiting)" : "";
            el.textContent = `⏳ OCR: ${o.backlog} to read${why}`;
            el.className = "chip busy";
        } else {
            el.textContent = "✓ OCR caught up";
            el.className = "chip ok";
        }
    },

    // ---- events -----------------------------------------------------------
    async loadEvents() {
        try {
            const d = await App.api("/api/events");
            App.events = d.events;
            App.currentEventId = d.current;
            for (const sel of document.querySelectorAll("#event-select, #s-event-select")) {
                sel.innerHTML = "";
                for (const e of d.events) {
                    const opt = document.createElement("option");
                    opt.value = e.id;
                    opt.textContent = `${e.name} (${e.capture_count})`;
                    if (e.id === d.current) opt.selected = true;
                    sel.appendChild(opt);
                }
            }
            App.eventsListeners.forEach((fn) => { try { fn(d.events, d.current); } catch (e) { console.error(e); } });
        } catch (e) { /* ignore */ }
    },
    currentEventName() {
        const e = App.events.find((x) => x.id === App.currentEventId);
        return e ? e.name : "";
    },
    async setCurrentEvent(id) {
        await App.api("/api/events/current", "POST", { event_id: parseInt(id, 10) });
        await App.loadEvents();
    },
};

// ---- wire up the header ---------------------------------------------------

document.querySelectorAll(".tab-btn").forEach((b) =>
    b.addEventListener("click", () => App.showTab(b.dataset.tab)));

$("#rec-btn").addEventListener("click", async () => {
    const btn = $("#rec-btn");
    btn.disabled = true;
    try {
        if (App.recording) {
            if (!confirm("Stop recording?")) return;
            const r = await App.api("/api/record", "POST", { recording: false });
            App.updateRec(r);
            App.toast("Recording stopped");
        } else {
            const r = await App.api("/api/record", "POST", { recording: true });
            App.updateRec(r);
            App.toast("Recording started");
        }
    } catch (e) {
        App.toast("Recording error: " + e.message, true);
    } finally {
        btn.disabled = false;
        App.refreshStatus();
    }
});

$("#event-select").addEventListener("change", async (ev) => {
    await App.setCurrentEvent(ev.target.value);
});
$("#event-new").addEventListener("click", async () => {
    const name = prompt("Name for the new event (race / day):", "");
    if (!name) return;
    await App.api("/api/events", "POST", { name });
    await App.loadEvents();
});

// Keyboard: 1-4 switch tabs when not typing in a field.
document.addEventListener("keydown", (ev) => {
    if (ev.target && (ev.target.tagName === "INPUT" || ev.target.tagName === "SELECT"
                      || ev.target.tagName === "TEXTAREA")) return;
    const map = { "1": "live", "2": "playback", "3": "results", "4": "settings" };
    if (map[ev.key]) App.showTab(map[ev.key]);
});

// ---- boot -----------------------------------------------------------------

window.addEventListener("load", () => {
    App.loadEvents();
    App.refreshStatus();
    setInterval(App.refreshStatus, 1000);
    let saved = null;
    try { saved = localStorage.getItem("fl-tab"); } catch (e) { /* ignore */ }
    if (saved && saved !== "live") App.showTab(saved);
    if (App.tabShown[App.tab]) App.tabShown[App.tab]();
});
