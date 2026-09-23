/*
 * results.js
 * ==========
 * Results tab: the detections table (one row per bib read), the photo
 * viewer, search / filter / export / purge, and the OCR worker controls.
 */

"use strict";

(function () {
    let selectedDetectionId = null;
    let refreshTimer = null;
    let searchTimer = null;

    function tableQuery() {
        const params = new URLSearchParams();
        const s = $("#search").value.trim();
        if (s) params.set("search", s);
        const f = $("#filter-mode").value;
        if (f && f !== "all") params.set("filter", f);
        const mc = parseFloat($("#min-conf").value) || 0;
        if (mc > 0) params.set("min_conf", mc);
        return params.toString();
    }

    async function refreshTable() {
        let rows;
        try { rows = await App.api("/api/detections?" + tableQuery()); }
        catch (e) { return; }
        const body = $("#results-body");
        body.innerHTML = "";
        for (const row of rows) {
            const tr = document.createElement("tr");
            tr.dataset.detectionId = row.detection_id;
            tr.dataset.captureId = row.capture_id;
            tr.dataset.filename = row.filename;

            const bibTd = document.createElement("td");
            bibTd.innerHTML = row.bib_number
                ? `<span class="bib">${row.bib_number}</span>`
                : `<span class="bib none">no bib</span>`;

            const confTd = document.createElement("td");
            if (row.confidence != null) {
                const pct = Math.round(row.confidence * 100);
                confTd.innerHTML = `<div class="conf-wrap"><div class="conf-bar">`
                    + `<div class="conf-fill" style="width:${pct}%"></div></div>`
                    + `<span class="conf-pct">${pct}%</span></div>`;
            } else {
                confTd.innerHTML = `<span class="hint">—</span>`;
            }

            const timeTd = document.createElement("td");
            timeTd.textContent = row.timestamp || "";

            const srcTd = document.createElement("td");
            const src = row.source || "photo";
            srcTd.innerHTML = `<span class="src">${src}</span>`;
            if (row.recording_id) {
                const b = document.createElement("button");
                b.type = "button";
                b.className = "jump";
                b.title = "Show this moment in the video";
                b.textContent = "▶ video";
                b.addEventListener("click", (ev) => {
                    ev.stopPropagation();
                    if (window.Playback) window.Playback.jumpTo(row.recording_id, row.epoch);
                });
                srcTd.appendChild(b);
            }

            tr.append(bibTd, confTd, timeTd, srcTd);
            if (String(row.detection_id) === String(selectedDetectionId)) tr.classList.add("active");
            tr.addEventListener("click", () => selectRow(tr));
            body.appendChild(tr);
        }
        $("#row-count").textContent = `${rows.length} row${rows.length === 1 ? "" : "s"}`;
    }

    function selectRow(tr) {
        selectedDetectionId = tr.dataset.detectionId;
        document.querySelectorAll("#results-body tr.active").forEach((r) => r.classList.remove("active"));
        tr.classList.add("active");
        const img = $("#viewer");
        img.src = "/captures/" + tr.dataset.filename;
        img.style.display = "block";
        $("#viewer-empty").style.display = "none";
        const bib = tr.querySelector(".bib").textContent;
        $("#viewer-caption").textContent = `capture #${tr.dataset.captureId} · ${bib}`;
    }

    function clearViewer() {
        selectedDetectionId = null;
        const img = $("#viewer");
        img.style.display = "none";
        img.removeAttribute("src");
        $("#viewer-empty").style.display = "";
        $("#viewer-caption").textContent = "";
    }

    $("#search").addEventListener("input", () => {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(refreshTable, 250);
    });
    $("#filter-mode").addEventListener("change", refreshTable);
    $("#min-conf").addEventListener("change", refreshTable);
    $("#export-btn").addEventListener("click", () => {
        window.location = "/api/export.csv?" + tableQuery();
    });
    $("#purge-btn").addEventListener("click", async () => {
        if (!confirm(`Purge ALL stills and OCR records for "${App.currentEventName()}"?\n\n`
                     + `Recorded video is kept. This cannot be undone.`)) return;
        try {
            const r = await App.api("/api/purge", "POST", {});
            App.toast(`Purged ${r.purged_captures} still(s)`);
        } catch (e) { App.toast("Purge failed: " + e.message, true); }
        await App.loadEvents();
        clearViewer();
        refreshTable();
    });

    // OCR worker pause / resume + status line.
    $("#ocr-pause-btn").addEventListener("click", async () => {
        const o = App.status ? App.status.ocr : null;
        const paused = o ? o.paused : false;
        try { await App.api("/api/ocr_worker", "POST", { paused: !paused }); }
        catch (e) { App.toast(e.message, true); }
        App.refreshStatus();
    });

    App.statusListeners.push((s) => {
        const o = s.ocr;
        const st = $("#ocr-worker-state");
        const btn = $("#ocr-pause-btn");
        btn.textContent = o.paused ? "▶ Resume OCR" : "⏸ Pause OCR";
        if (o.paused) {
            st.textContent = `OCR paused · ${o.backlog} waiting`;
            st.className = "chip warn";
        } else if (o.blocked) {
            st.textContent = `OCR waiting (recording) · ${o.backlog} queued`;
            st.className = "chip warn";
        } else if (o.processing) {
            st.textContent = `OCR reading #${o.processing} · ${o.backlog} queued`;
            st.className = "chip busy";
        } else {
            st.textContent = o.backlog ? `OCR · ${o.backlog} queued` : "OCR idle · all read";
            st.className = "chip ok";
        }
        $("#ocr-rate").textContent = o.process_per_min
            ? `${o.process_per_min}/min · last ${o.last_duration_s}s · ${o.backend}`
            : `${o.backend}`;
    });

    App.eventsListeners.push(() => { clearViewer(); refreshTable(); });

    App.tabShown.results = () => {
        refreshTable();
        clearInterval(refreshTimer);
        refreshTimer = setInterval(refreshTable, 2000);
    };
    App.tabHidden.results = () => { clearInterval(refreshTimer); refreshTimer = null; };
})();
