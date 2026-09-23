/*
 * live.js
 * =======
 * Live tab: the MJPEG preview with a canvas overlay that shows the motion
 * zone (ROI) and where motion is happening right now, and lets the user drag
 * a new zone.  Also the "Snapshot -> OCR" button.
 */

"use strict";

(function () {
    const canvas = $("#roi-canvas");
    const ctx = canvas.getContext("2d");
    const liveImg = $("#live");
    const wrap = $("#live-wrap");
    let roi = null;             // current ROI (normalised)
    let motion = null;          // last /api/motion payload
    let pollTimer = null;
    let dragging = false, startX = 0, startY = 0, curX = 0, curY = 0;

    function syncCanvas() {
        const imgRect = liveImg.getBoundingClientRect();
        const wrapRect = wrap.getBoundingClientRect();
        canvas.width = Math.max(1, Math.round(imgRect.width));
        canvas.height = Math.max(1, Math.round(imgRect.height));
        canvas.style.left = (imgRect.left - wrapRect.left) + "px";
        canvas.style.top = (imgRect.top - wrapRect.top) + "px";
        draw();
    }
    window.addEventListener("resize", syncCanvas);
    liveImg.addEventListener("load", syncCanvas);
    new ResizeObserver(syncCanvas).observe(wrap);

    function draw() {
        const W = canvas.width, H = canvas.height;
        ctx.clearRect(0, 0, W, H);
        if (!$("#roi-show").checked) return;
        if (roi) {
            ctx.strokeStyle = "#36d07f";
            ctx.lineWidth = 2;
            ctx.setLineDash([]);
            ctx.strokeRect(roi.x * W, roi.y * H, roi.w * W, roi.h * H);
        }
        if (motion && motion.bbox && motion.detected) {
            const [x, y, w, h] = motion.bbox;
            ctx.strokeStyle = "#f0b542";
            ctx.lineWidth = 2;
            ctx.setLineDash([4, 3]);
            ctx.strokeRect(x * W, y * H, w * W, h * H);
            ctx.setLineDash([]);
        }
        if (dragging) {
            const x = Math.min(startX, curX), y = Math.min(startY, curY);
            const w = Math.abs(curX - startX), h = Math.abs(curY - startY);
            ctx.strokeStyle = "#36d07f";
            ctx.lineWidth = 2;
            ctx.setLineDash([6, 4]);
            ctx.strokeRect(x, y, w, h);
            ctx.fillStyle = "rgba(54, 208, 127, 0.15)";
            ctx.fillRect(x, y, w, h);
            ctx.setLineDash([]);
        }
    }

    function pos(evt) {
        const rect = canvas.getBoundingClientRect();
        return { x: evt.clientX - rect.left, y: evt.clientY - rect.top };
    }
    canvas.addEventListener("pointerdown", (evt) => {
        evt.preventDefault();
        canvas.setPointerCapture(evt.pointerId);
        syncCanvas();
        dragging = true;
        const p = pos(evt);
        startX = curX = p.x; startY = curY = p.y;
    });
    canvas.addEventListener("pointermove", (evt) => {
        if (!dragging) return;
        const p = pos(evt);
        curX = p.x; curY = p.y;
        draw();
    });
    canvas.addEventListener("pointerup", async () => {
        if (!dragging) return;
        dragging = false;
        const x = Math.min(startX, curX), y = Math.min(startY, curY);
        const w = Math.abs(curX - startX), h = Math.abs(curY - startY);
        draw();
        if (w < 8 || h < 8) return;
        await postRoi({ x: x / canvas.width, y: y / canvas.height,
                        w: w / canvas.width, h: h / canvas.height });
    });
    canvas.addEventListener("pointercancel", () => { dragging = false; draw(); });

    async function postRoi(r) {
        try {
            roi = await App.api("/api/roi", "POST", r);
            draw();
        } catch (e) { App.toast("Could not set zone: " + e.message, true); }
    }
    $("#roi-reset").addEventListener("click", () =>
        postRoi({ x: 0.30, y: 0.35, w: 0.40, h: 0.30 }));
    $("#roi-show").addEventListener("change", draw);

    async function pollMotion() {
        try {
            motion = await App.api("/api/motion");
            const pct = (motion.last_score * 100).toFixed(1);
            const thr = motion.area_threshold * 100;
            const el = $("#motion-live");
            el.textContent = `motion ${pct}% (trigger ${thr.toFixed(1)}%)`;
            el.classList.toggle("hot", motion.detected);
            draw();
        } catch (e) { /* ignore */ }
    }

    $("#snapshot-btn").addEventListener("click", async () => {
        const btn = $("#snapshot-btn");
        btn.disabled = true;
        try {
            const r = await App.api("/api/snapshot", "POST", {});
            App.toast(`Snapshot #${r.capture_id} saved and queued for OCR`);
        } catch (e) {
            App.toast("Snapshot failed: " + e.message, true);
        } finally { btn.disabled = false; }
    });

    App.statusListeners.push((s) => {
        roi = s.roi;
        const c = s.camera;
        $("#live-res").textContent =
            `recording ${c.record_size[0]}×${c.record_size[1]} @ ${c.fps} fps · `
            + `preview ${c.preview_size[0]}×${c.preview_size[1]}`;
        if (App.tab !== "live") draw();
    });

    App.tabShown.live = () => {
        // (Re)connect the stream when the tab is shown so a hidden tab doesn't
        // keep pulling MJPEG frames.
        if (!liveImg.getAttribute("src")) liveImg.src = "/video_feed?" + Date.now();
        syncCanvas();
        pollMotion();
        clearInterval(pollTimer);
        pollTimer = setInterval(pollMotion, 300);
    };
    App.tabHidden.live = () => {
        clearInterval(pollTimer);
        pollTimer = null;
        liveImg.removeAttribute("src");
    };
})();
