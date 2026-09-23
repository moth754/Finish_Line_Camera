"""
bib_recognizer.py
=================

Reads bib numbers out of a captured photo.

A bib number is a number printed on a contrasting background (black on white,
etc.).  We run optical character recognition (OCR) on the photo, keep only the
tokens that look like bib numbers (the right number of digits, high enough
confidence) and return them together with where they were found and how
confident the engine was.

Pluggable backends
------------------
OCR on a Raspberry Pi is a trade-off, so the engine is swappable:

    * TesseractBackend - lightweight, fast-ish, install with:
          sudo apt install tesseract-ocr
          pip install pytesseract
      Good for clear, high-contrast numbers (exactly the bib case).

    * EasyOCRBackend - much heavier (pulls in PyTorch) but more robust on
          awkward angles / lighting.  Install with:  pip install easyocr

    * NullBackend - no OCR available.  Photos are still captured and stored,
          they simply have no bib number attached.  The app stays fully usable.

`create_recognizer()` picks one based on config.OCR_BACKEND and what is actually
importable, so the program never crashes just because OCR is missing.

Every backend returns a list of `BibDetection` objects, giving the rest of the
program one consistent shape to work with regardless of engine.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import numpy as np

from . import config


# ---------------------------------------------------------------------------
# Runtime OCR settings (settable from the web UI, persisted like the ROI/focus)
# ---------------------------------------------------------------------------

def _load_ocr_state() -> dict:
    """Load persisted OCR options, falling back to the config defaults."""
    state = {"panel_proposals": bool(config.OCR_PANEL_PROPOSALS),
             "detect_width": int(config.OCR_DETECT_WIDTH)}
    try:
        if config.OCR_STATE_PATH.exists():
            with open(config.OCR_STATE_PATH) as fh:
                data = json.load(fh)
            if "panel_proposals" in data:
                state["panel_proposals"] = bool(data["panel_proposals"])
            if "detect_width" in data:
                state["detect_width"] = int(data["detect_width"])
    except Exception as exc:
        print(f"[ocr] could not load OCR state ({exc}); using default")
    return state


def _save_ocr_state(state: dict):
    """Persist OCR options so a UI change survives a restart."""
    try:
        config.OCR_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(config.OCR_STATE_PATH, "w") as fh:
            json.dump(state, fh, indent=2)
    except Exception as exc:
        print(f"[ocr] could not save OCR state ({exc})")


@dataclass
class BibDetection:
    """A single recognised bib number within a photo."""
    bib_number: str          # the digits, e.g. "42"
    confidence: float        # 0.0-1.0
    bbox: tuple[int, int, int, int]  # (x, y, w, h) in full-image pixels


# A bib token is a run of digits whose length is within the configured range.
_BIB_RE = re.compile(r"\d+")


# ---------------------------------------------------------------------------
# Image preprocessing (helps OCR a lot on real outdoor photos)
# ---------------------------------------------------------------------------
#
# Tesseract reads cleanest from a high-contrast, reasonably large greyscale
# image.  We prefer OpenCV (CLAHE adaptive contrast + good interpolation) and
# fall back to a NumPy/Pillow path if cv2 is not installed, so the feature
# degrades gracefully rather than failing.

try:
    import cv2  # optional; only used to improve OCR preprocessing
    _HAVE_CV2 = True
except Exception:
    _HAVE_CV2 = False


def preprocess_for_ocr(image_rgb: np.ndarray) -> np.ndarray:
    """
    Return a greyscale, contrast-enhanced, upscaled copy of the photo for OCR.

    The bounding boxes the OCR engine reports are in this *preprocessed* image's
    coordinate space, so the caller scales them back to the original image size
    using `OCR_UPSCALE` (see TesseractBackend.recognize).
    """
    if not config.OCR_PREPROCESS:
        # Caller still wants greyscale (Tesseract is happier with it).
        return image_rgb.mean(axis=2).astype(np.uint8)

    scale = float(config.OCR_UPSCALE)

    if _HAVE_CV2:
        gray = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2GRAY)
        # CLAHE = Contrast Limited Adaptive Histogram Equalisation: evens out
        # patchy outdoor lighting far better than a single global stretch.
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        gray = clahe.apply(gray)
        if scale != 1.0:
            gray = cv2.resize(gray, None, fx=scale, fy=scale,
                              interpolation=cv2.INTER_CUBIC)
        return gray

    # ---- Fallback: NumPy + Pillow (no OpenCV) -----------------------------
    from PIL import Image
    gray = image_rgb.mean(axis=2).astype(np.uint8)
    # Simple contrast stretch to use the full 0-255 range.
    lo, hi = int(gray.min()), int(gray.max())
    if hi > lo:
        gray = ((gray.astype(np.float32) - lo) * (255.0 / (hi - lo))).astype(np.uint8)
    if scale != 1.0:
        img = Image.fromarray(gray)
        img = img.resize((int(img.width * scale), int(img.height * scale)),
                         Image.BICUBIC)
        gray = np.asarray(img)
    return gray


# Characters the recognition model commonly returns instead of a digit when
# reading real digits (and only those - a permissive map would turn ordinary
# words into fake bib numbers).
_DIGIT_CONFUSABLES = str.maketrans({
    "O": "0", "o": "0", "Q": "0", "D": "0",
    "I": "1", "l": "1", "i": "1", "|": "1",
    "Z": "2", "z": "2",
    "S": "5", "s": "5",
    "G": "6", "b": "6",
    "B": "8",
    "g": "9", "q": "9",
})


def _digitize(text: str) -> str | None:
    """
    Reduce a free-charset OCR read to a bib number, or None.

    The OCR pass deliberately runs WITHOUT a digit allowlist: with one, a van
    logo like "OCEAN" is force-read as digits at high confidence.  Instead we
    ask for an honest read and then require it to actually BE a number - at
    most one confusable letter is forgiven (a "5O" is bib 50; "OCEAN" is not).
    """
    t = (text or "").strip().replace(" ", "").rstrip(".,:;-")
    if not t:
        return None
    n_digits = sum(ch.isdigit() for ch in t)
    if len(t) - n_digits > 1:        # more than one non-digit: it's a word
        return None
    if n_digits == 0:                # a lone confusable ("l", "O") is not a bib
        return None
    mapped = t.translate(_DIGIT_CONFUSABLES)
    if not mapped.isdigit():
        return None
    if config.BIB_MIN_DIGITS <= len(mapped) <= config.BIB_MAX_DIGITS:
        return mapped
    return None


def _looks_like_bib(text: str) -> str | None:
    """
    Reduce a raw OCR token to a bib number, or return None if it isn't one.

    We strip everything that isn't a digit, then check the length is plausible.
    e.g. "No.123" -> "123";  "12:34" -> rejected (would be "1234", too long if
    BIB_MAX_DIGITS < 4, otherwise kept - tune via config).
    """
    digits = "".join(_BIB_RE.findall(text))
    if not digits:
        return None
    if config.BIB_MIN_DIGITS <= len(digits) <= config.BIB_MAX_DIGITS:
        return digits
    return None


# ---------------------------------------------------------------------------
# Backend implementations
# ---------------------------------------------------------------------------

class NullBackend:
    """Does nothing - used when no OCR engine is installed."""
    name = "none"

    def recognize(self, image_rgb: np.ndarray) -> list[BibDetection]:
        return []


class TesseractBackend:
    """OCR via the Tesseract engine (through the pytesseract wrapper)."""
    name = "tesseract"

    def __init__(self):
        import pytesseract  # raises ImportError if the wrapper is not installed
        self._pt = pytesseract
        # The pytesseract *wrapper* can import even when the tesseract *binary*
        # is missing.  Probe for the binary now so create_recognizer() can fall
        # back cleanly instead of failing on every photo at runtime.
        # (Raises TesseractNotFoundError if `tesseract` is not on PATH.)
        version = pytesseract.get_tesseract_version()
        print(f"[ocr] found tesseract binary v{version}")
        # The page-segmentation modes we run and merge (see config note).  We do
        # NOT pass a char whitelist - it suppresses LSTM results; instead we keep
        # only digit tokens ourselves in _looks_like_bib().
        self._psm_modes = config.OCR_PSM_MODES

    def recognize(self, image_rgb: np.ndarray) -> list[BibDetection]:
        from pytesseract import Output

        # Preprocess once (greyscale + contrast + upscale).  The boxes Tesseract
        # returns are in this upscaled image's coordinates, so we divide them
        # back down by the upscale factor to map onto the saved photo.
        gray = preprocess_for_ocr(image_rgb)
        scale = float(config.OCR_UPSCALE) if config.OCR_PREPROCESS else 1.0

        # Run each PSM mode and gather every digit token it found.
        candidates: list[BibDetection] = []
        for psm in self._psm_modes:
            data = self._pt.image_to_data(
                gray, config=f"--psm {psm}", output_type=Output.DICT
            )
            for i, raw in enumerate(data["text"]):
                text = (raw or "").strip()
                if not text:
                    continue
                bib = _looks_like_bib(text)
                if bib is None:
                    continue
                conf_raw = float(data["conf"][i])  # 0-100, or -1 for "no value"
                if conf_raw < 0:
                    continue
                confidence = conf_raw / 100.0
                if confidence < config.OCR_MIN_CONFIDENCE:
                    continue
                bbox = (
                    int(data["left"][i] / scale), int(data["top"][i] / scale),
                    int(data["width"][i] / scale), int(data["height"][i] / scale),
                )
                candidates.append(BibDetection(bib, confidence, bbox))

        # Merge: the PSM passes overlap, so the same physical bib is often found
        # twice.  Bib numbers are unique within a race, so we deduplicate by the
        # number string and keep the most confident read of each.
        best: dict[str, BibDetection] = {}
        for det in candidates:
            if det.bib_number not in best or det.confidence > best[det.bib_number].confidence:
                best[det.bib_number] = det
        return list(best.values())


class EasyOCRBackend:
    """Two-stage OCR via EasyOCR: find candidate regions cheaply, then read
    each region from the FULL-resolution photo.

    Why two stages?  EasyOCR's recognition network only ever sees a text line
    ~64 px tall, so what matters is handing it a good crop.  Running the whole
    photo through `readtext` downscaled (the old approach) destroyed exactly
    the pixels it needed: a number board 90 px wide at 1080p became ~45 px in
    the OCR input and was misread or missed.

    Stage 1 - region proposals:
      * EasyOCR's CRAFT text detector on a copy downscaled to
        config.OCR_DETECT_WIDTH, with permissive thresholds - it only has to
        FIND text, not read it.
      * Optionally (OCR_PANEL_PROPOSALS) bright, solid, roughly rectangular
        panels found by a cheap colour heuristic - catches number boards the
        text detector missed.  False proposals are harmless: recognition just
        reads nothing from them.

    Stage 2 - recognition, per region, cropped from the ORIGINAL image:
      * several contrast "views" of each crop are tried, because a number can
        be invisible in plain greyscale yet obvious in colour: yellow digits
        on a blue board have almost no luminance contrast (worse still through
        a NoIR camera), but stand out sharply in a B-(R+G)/2 projection, and a
        per-crop PCA projection adapts to any colour pairing.
      * `reader.recognize()` (no re-detection) keeps this stage fast: ~0.1 s
        per region vs ~1.5 s for a full `readtext` on the crop.
    """
    name = "easyocr"

    def __init__(self):
        import easyocr  # raises ImportError if not installed
        import torch

        # Make sure PyTorch uses all of the Pi's CPU cores for inference.
        try:
            import os
            torch.set_num_threads(os.cpu_count() or 4)
        except Exception:
            pass

        # gpu=False: the Pi has no supported GPU for EasyOCR.  Loading the model
        # is slow (~30 s) and downloads it on first ever run, so we do it once
        # here at startup rather than per image.
        print("[ocr] loading EasyOCR model (first run downloads it, ~30s)...")
        self._reader = easyocr.Reader(["en"], gpu=False, verbose=False)
        print("[ocr] EasyOCR model ready")

        # Panel proposals: settable live from the web UI (persisted).  On =
        # higher recall on low-contrast boards the text detector misses; off =
        # notably faster (skips a readtext pass over every bright blob).
        # NB: distinct name from the _panel_proposals() method below - a bool
        # attribute of the same name would shadow the method and break recognize.
        _state = _load_ocr_state()
        self._panels_on = _state["panel_proposals"]
        self._detect_width = _state["detect_width"]

    def _save_ocr(self):
        _save_ocr_state({"panel_proposals": self._panels_on,
                         "detect_width": self._detect_width})

    def reload_state(self):
        """Re-read the persisted options (the UI writes them from another
        process now that recognition runs in its own worker process)."""
        _state = _load_ocr_state()
        self._panels_on = _state["panel_proposals"]
        self._detect_width = _state["detect_width"]

    def get_panel_proposals(self) -> bool:
        return self._panels_on

    def set_panel_proposals(self, on: bool) -> bool:
        """Enable/disable the panel-proposal stage at runtime and persist it."""
        self._panels_on = bool(on)
        self._save_ocr()
        print(f"[ocr] panel proposals "
              f"{'ENABLED' if self._panels_on else 'DISABLED'}")
        return self._panels_on

    def get_detect_width(self) -> int:
        return self._detect_width

    def set_detect_width(self, width: int) -> int:
        """Set the CRAFT detection width (px) at runtime and persist it.

        Lower = faster detection but small/distant bibs may be missed.  Clamped
        to a sane range; values above the crop width simply mean 'no downscale'.
        """
        self._detect_width = int(min(max(int(width), 320), 4608))
        self._save_ocr()
        print(f"[ocr] detect width set to {self._detect_width}px")
        return self._detect_width

    # -- stage 1a: CRAFT text detection on a downscaled copy ----------------

    def _detect_text_boxes(self, image_rgb: np.ndarray) -> list[list[int]]:
        """Return candidate text regions as [x0, y0, x1, y1] in full-res px."""
        h, w = image_rgb.shape[:2]
        det_w = int(self._detect_width)
        scale = min(1.0, det_w / w)
        if scale < 1.0:
            size = (det_w, int(h * scale))
            if _HAVE_CV2:
                small = cv2.resize(image_rgb, size, interpolation=cv2.INTER_AREA)
            else:
                from PIL import Image
                small = np.asarray(Image.fromarray(image_rgb).resize(size))
        else:
            small = image_rgb

        h_list, f_list = self._reader.detect(
            small,
            low_text=config.OCR_DETECT_LOW_TEXT,
            text_threshold=config.OCR_DETECT_TEXT_THRESHOLD,
        )
        boxes: list[list[int]] = []
        for x0, x1, y0, y1 in h_list[0]:
            boxes.append([int(x0 / scale), int(y0 / scale),
                          int(x1 / scale), int(y1 / scale)])
        for quad in f_list[0]:  # rotated quads -> use their bounding rect
            xs = [p[0] / scale for p in quad]
            ys = [p[1] / scale for p in quad]
            boxes.append([int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))])
        return boxes

    # -- stage 1b: bright-panel colour heuristic ----------------------------

    @staticmethod
    def _panel_proposals(image_rgb: np.ndarray) -> list[list[int]]:
        """Find bright, solid, panel-shaped blobs (number boards, paper bibs)."""
        if not _HAVE_CV2:
            return []
        h, w = image_rgb.shape[:2]
        hsv = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2HSV)
        s, v = hsv[..., 1], hsv[..., 2]
        # "Bright and not too vividly coloured" covers white paper bibs and the
        # pale plastic number boards (which a NoIR camera washes out further).
        mask = ((v > 140) & (s < 120)).astype(np.uint8) * 255
        mask = cv2.morphologyEx(
            mask, cv2.MORPH_CLOSE,
            cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5)))

        n, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        cands = []
        for i in range(1, n):
            x, y, bw, bh, area = stats[i]
            if bw < 18 or bh < 18 or bw > w * 0.25 or bh > h * 0.25:
                continue
            aspect = bw / float(bh)
            if not (0.4 <= aspect <= 3.0):
                continue
            # Panels are solid: most of their bounding box is lit mask.
            if area / float(bw * bh) < 0.55:
                continue
            cands.append((area, [x, y, x + bw, y + bh]))
        cands.sort(reverse=True, key=lambda c: c[0])
        return [box for _, box in cands[:int(config.OCR_PANEL_MAX_REGIONS)]]

    # -- region tidy-up ------------------------------------------------------

    @staticmethod
    def _pad_and_merge(boxes: list[list[int]], w: int, h: int) -> list[list[int]]:
        """Pad each box a little, then merge boxes that look like fragments of
        the SAME line of text (CRAFT often splits a number into per-digit
        boxes).  Boxes on different lines are deliberately never merged: a
        runner's bib has event text above and a name below the number, and a
        multi-line crop confuses the single-line recognition model."""
        padded = []
        for x0, y0, x1, y1 in boxes:
            px = (x1 - x0) * 0.08 + 4
            py = (y1 - y0) * 0.15 + 4
            padded.append([max(0, int(x0 - px)), max(0, int(y0 - py)),
                           min(w, int(x1 + px)), min(h, int(y1 + py))])

        merged = True
        while merged:
            merged = False
            out: list[list[int]] = []
            for box in padded:
                for other in out:
                    bh_a = box[3] - box[1]
                    bh_b = other[3] - other[1]
                    if max(bh_a, bh_b) > 1.8 * min(bh_a, bh_b):
                        continue  # very different sizes: not the same number
                    # Same text line = vertical centres close together.
                    cy_a = (box[1] + box[3]) / 2
                    cy_b = (other[1] + other[3]) / 2
                    if abs(cy_a - cy_b) > 0.5 * min(bh_a, bh_b):
                        continue
                    gap = min(bh_a, bh_b) * 0.6
                    if box[0] < other[2] + gap and other[0] < box[2] + gap:
                        other[0] = min(other[0], box[0])
                        other[1] = min(other[1], box[1])
                        other[2] = max(other[2], box[2])
                        other[3] = max(other[3], box[3])
                        merged = True
                        break
                else:
                    out.append(list(box))
            padded = out
        return padded

    # -- stage 2: recognition on full-res crops ------------------------------

    @staticmethod
    def _crop_views(crop_rgb: np.ndarray) -> list[np.ndarray]:
        """Greyscale 'views' of a crop, ordered cheapest/most-usual first."""
        gray = (cv2.cvtColor(crop_rgb, cv2.COLOR_RGB2GRAY) if _HAVE_CV2
                else crop_rgb.mean(axis=2).astype(np.uint8))
        views = [gray]

        r = crop_rgb[..., 0].astype(np.int16)
        g = crop_rgb[..., 1].astype(np.int16)
        b = crop_rgb[..., 2].astype(np.int16)
        # Blue-vs-yellow axis: separates yellow digits from blue boards (and
        # vice versa) even when their brightness is identical.
        chroma = (b - (r + g) // 2).astype(np.float32)
        lo, hi = float(chroma.min()), float(chroma.max())
        if hi - lo > 8:  # only useful when there IS colour variation
            views.append(((chroma - lo) * (255.0 / (hi - lo))).astype(np.uint8))

        # Adaptive view: project pixels onto their principal colour axis, which
        # maximises whatever colour contrast this particular crop has.
        px = crop_rgb.reshape(-1, 3).astype(np.float32)
        mean = px.mean(axis=0)
        centered = px - mean
        cov = centered.T @ centered / max(1, len(px) - 1)
        evals, evecs = np.linalg.eigh(cov)
        proj = centered @ evecs[:, -1]
        lo, hi = float(proj.min()), float(proj.max())
        if hi - lo > 8:
            views.append(((proj - lo) * (255.0 / (hi - lo)))
                         .reshape(crop_rgb.shape[:2]).astype(np.uint8))
        return views

    def _read_region(self, image_rgb: np.ndarray,
                     box: list[int]) -> tuple[str, float] | None:
        """Best digit read for one text-line region, or None."""
        x0, y0, x1, y1 = box
        crop = image_rgb[y0:y1, x0:x1]
        ch, cw = crop.shape[:2]
        if ch < 10 or cw < 10:
            return None
        # Upscale small crops before recognition: EasyOCR resizes text lines to
        # 64 px tall internally, and a good interpolation here beats its own.
        if ch < 90 and _HAVE_CV2:
            f = min(5.0, 90.0 / ch)
            crop = cv2.resize(crop, None, fx=f, fy=f,
                              interpolation=cv2.INTER_CUBIC)
            ch, cw = crop.shape[:2]

        best: tuple[str, float] | None = None
        for view in self._crop_views(crop):
            res = self._reader.recognize(
                view, horizontal_list=[[0, cw, 0, ch]], free_list=[])
            for _, text, conf in res:
                bib = _digitize(text)
                if bib is None:
                    continue
                if best is None or conf > best[1]:
                    best = (bib, float(conf))
            if best is not None and best[1] >= 0.80:
                break  # confident enough; skip the remaining views
        return best

    def _read_panel(self, image_rgb: np.ndarray,
                    box: list[int]) -> tuple[str, float] | None:
        """Best digit read inside one panel proposal, or None.

        Panels are whole number boards / bibs, usually with several lines of
        text, so they go through `readtext` (detection INSIDE the crop, then
        recognition per found line).  This is slower than `recognize()` but
        immune to its habit of hallucinating a digit out of grass or a pole:
        if the crop holds no text, the detector simply finds nothing.
        """
        x0, y0, x1, y1 = box
        crop = image_rgb[y0:y1, x0:x1]
        ch, cw = crop.shape[:2]
        if ch < 18 or cw < 18 or not _HAVE_CV2:
            return None
        # Digits on a board are roughly half the panel height; get them to a
        # comfortable size for the detector without an enormous crop.
        f = max(1.0, min(4.0, 280.0 / ch))
        crop = cv2.resize(crop, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)

        best: tuple[str, float] | None = None
        for view in (crop, *self._crop_views(crop)[1:]):  # RGB, chroma, PCA
            for _, text, conf in self._reader.readtext(view):
                bib = _digitize(text)
                if bib is None:
                    continue
                if best is None or conf > best[1]:
                    best = (bib, float(conf))
            if best is not None and best[1] >= 0.80:
                break
        return best

    # -- public entry point --------------------------------------------------

    def recognize(self, image_rgb: np.ndarray) -> list[BibDetection]:
        h, w = image_rgb.shape[:2]

        text_boxes = self._pad_and_merge(self._detect_text_boxes(image_rgb), w, h)

        results: list[BibDetection] = []
        for box in text_boxes:
            read = self._read_region(image_rgb, box)
            if read is None:
                continue
            bib, conf = read
            if conf < config.OCR_MIN_CONFIDENCE:
                continue
            x0, y0, x1, y1 = box
            results.append(BibDetection(bib, conf, (x0, y0, x1 - x0, y1 - y0)))

        # Panels only add value where the text detector came up empty: skip any
        # panel that already contains a decent text-box read.
        if self._panels_on:
            covered = [d for d in results if d.confidence >= 0.5]
            for box in self._panel_proposals(image_rgb):
                x0, y0, x1, y1 = box
                if any(x0 <= d.bbox[0] + d.bbox[2] / 2 <= x1
                       and y0 <= d.bbox[1] + d.bbox[3] / 2 <= y1
                       for d in covered):
                    continue
                read = self._read_panel(image_rgb, box)
                if read is None or read[1] < config.OCR_MIN_CONFIDENCE:
                    continue
                results.append(BibDetection(
                    read[0], read[1], (x0, y0, x1 - x0, y1 - y0)))

        # The same physical number can be found via several proposals; keep the
        # most confident read of each distinct number.
        best: dict[str, BibDetection] = {}
        for det in results:
            if det.bib_number not in best or det.confidence > best[det.bib_number].confidence:
                best[det.bib_number] = det
        return list(best.values())


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_recognizer():
    """
    Build the OCR backend dictated by config.OCR_BACKEND, falling back to a
    backend that is actually importable, and finally to NullBackend so the app
    always starts.
    """
    backend = config.OCR_BACKEND

    def try_tesseract():
        try:
            be = TesseractBackend()
            print("[ocr] using Tesseract backend")
            return be
        except Exception as exc:
            print(f"[ocr] Tesseract unavailable: {exc}")
            return None

    def try_easyocr():
        try:
            be = EasyOCRBackend()
            print("[ocr] using EasyOCR backend")
            return be
        except Exception as exc:
            print(f"[ocr] EasyOCR unavailable: {exc}")
            return None

    if backend == "none":
        print("[ocr] OCR disabled by config")
        return NullBackend()

    if backend == "tesseract":
        return try_tesseract() or NullBackend()

    if backend == "easyocr":
        return try_easyocr() or NullBackend()

    # "auto": prefer the light engine, then the heavy one, then nothing.
    be = try_tesseract() or try_easyocr()
    if be is None:
        print("[ocr] no OCR backend available - photos will be stored without "
              "bib numbers. Install with: sudo apt install tesseract-ocr && "
              "pip install pytesseract")
        return NullBackend()
    return be
