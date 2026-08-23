"""
OCR pre-fill for the figure cropper (spec §6). Classical Tesseract only — NO
AI/LLM. Reads each panel's printed title and fuzzy-matches it to a catalogue
number so the cell→antibody mapping is pre-filled by what's actually printed
(not by reading order, which differs between applications for the same gene).

Degrades gracefully: if Tesseract is unavailable, `available()` is False and
callers fall back to reading-order pre-fill.

The OCR string only decides *which* candidate a cell points at; the authoritative
catalogue for naming/DB always comes from the confirmed list / DB row, never the
raw OCR text.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from PIL import Image

try:
    import pytesseract
    from pytesseract import TesseractNotFoundError
except Exception:                       # pragma: no cover
    pytesseract = None
    TesseractNotFoundError = Exception

try:
    from rapidfuzz import fuzz
except Exception:                       # pragma: no cover
    fuzz = None


# Common OCR confusions (spec §6.2). Applied to BOTH sides so they cancel.
_CONFUSE = {"O": "0", "I": "1", "L": "1", "S": "5", "B": "8"}


def available() -> bool:
    """True if Tesseract is usable right now."""
    if pytesseract is None:
        return False
    try:
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def normalize(s: str) -> str:
    """Uppercase, strip asterisks/whitespace, collapse common OCR confusions.
    Hyphens/underscores are kept (they carry meaning in catalogue numbers) but
    are compared consistently on both sides."""
    s = (s or "").upper()
    s = re.sub(r"[*\s]+", "", s)
    return "".join(_CONFUSE.get(ch, ch) for ch in s)


def _title_band(source: Image.Image, rect, band_frac=0.16, max_h_frac=0.4):
    """Crop the title strip at the TOP of the cell.

    The user grids each panel to include its printed catalogue title (owner
    decision 2026-07-14: the title stays in the crop), so the title is a known
    region — the top of the cell — not something to guess for above it. WB/IP/FC
    titles are a thin line at the top; ICC-IF titles sit in the tan bar at the
    top. Band height scales with cell width (title size tracks figure
    resolution) and is capped so it never swallows the whole panel."""
    l, t, r, b = rect
    w, h = r - l, b - t
    band_h = min(int(h * max_h_frac), max(24, int(w * band_frac)))
    top = max(0, t)
    bottom = min(source.height, t + band_h)
    left = max(0, l)
    right = min(source.width, r)
    if right <= left or bottom <= top:
        return None
    return source.crop((left, top, right, bottom))


def _prep(img: Image.Image) -> Image.Image:
    """Grayscale + upscale small printed text so Tesseract can read it.
    Upscaling to a comfortable cap-height is the single biggest accuracy win on
    figure titles; Tesseract handles binarisation internally."""
    g = img.convert("L")
    if g.height < 1:
        return g
    factor = max(1, min(6, round(90 / g.height)))
    if factor > 1:
        g = g.resize((g.width * factor, g.height * factor), Image.LANCZOS)
    return g


def ocr_text(img: Image.Image) -> str:
    """Raw Tesseract read of a title strip, tuned for a single short line."""
    if not available() or img is None:
        return ""
    try:
        # PSM 7 = single text line; whitelist the chars catalogue titles use.
        cfg = ("--psm 7 -c tessedit_char_whitelist="
               "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_ ")
        return pytesseract.image_to_string(_prep(img), config=cfg).strip()
    except TesseractNotFoundError:      # pragma: no cover
        return ""
    except Exception:
        return ""


@dataclass
class CellOCR:
    raw: str                  # raw OCR text
    match: Optional[str]      # best candidate catalogue (original form), or None
    score: float             # 0..100 similarity of the best match
    ambiguous: bool = False   # best & runner-up are near-tied → needs human check
    runner_up: Optional[str] = None

    @property
    def confident(self) -> bool:
        """A match the UI can pre-fill without a warning flag."""
        return self.match is not None and not self.ambiguous


def match_catalogue(raw: str, candidates, threshold: float = 72.0,
                    ambiguity_margin: float = 6.0) -> CellOCR:
    """Fuzzy-match an OCR string against candidate catalogue numbers.
    `candidates` is any iterable of original catalogue strings (typically the
    confirmed paste list UNION the gene's existing DB antibodies).

    If the top two candidates score within `ambiguity_margin`, the match is
    flagged `ambiguous` — OCR can't safely disambiguate near-identical
    catalogues (e.g. GTX637386 vs GTX637387), so the human must confirm."""
    raw = raw or ""
    if not raw or fuzz is None:
        return CellOCR(raw=raw, match=None, score=0.0)
    norm_raw = normalize(raw)
    scored = []
    for cat in candidates:
        nc = normalize(cat)
        if nc:
            scored.append((fuzz.WRatio(norm_raw, nc), cat))
    if not scored:
        return CellOCR(raw=raw, match=None, score=0.0)
    scored.sort(reverse=True)
    best_score, best = scored[0]
    second = scored[1] if len(scored) > 1 else None
    if best_score < threshold:
        return CellOCR(raw=raw, match=None, score=best_score)
    ambiguous = second is not None and (best_score - second[0]) < ambiguity_margin
    return CellOCR(raw=raw, match=best, score=best_score, ambiguous=ambiguous,
                   runner_up=second[1] if second else None)


def prefill(source: Image.Image, rects, candidates, threshold: float = 72.0):
    """Run OCR title-matching over every cell rect. Returns a list of CellOCR,
    one per rect, in the same order. Falls back to empty results (match=None)
    per cell where OCR is blank."""
    out = []
    for rect in rects:
        strip = _title_band(source, rect)
        raw = ocr_text(strip)
        out.append(match_catalogue(raw, candidates, threshold))
    return out
