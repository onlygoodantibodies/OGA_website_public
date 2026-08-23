"""
Test the OCR pre-fill module (spec §6). Needs Pillow + the tesseract binary
(pytesseract) + rapidfuzz. Skips cleanly if Tesseract is not installed, since
the tool itself degrades to reading-order fallback in that case.

Run:  python pipeline/services/cropper/tests/test_ocr.py
"""
import os
import sys

HERE = os.path.dirname(__file__)
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
sys.path.insert(0, ROOT)

from PIL import Image, ImageDraw, ImageFont  # noqa: E402
from pipeline.services.cropper import ocr  # noqa: E402


def _font(sz):
    try:
        return ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", sz)
    except OSError:
        return ImageFont.load_default()


def _titled_panel(catalogue, printed=None):
    """A small panel with its catalogue title at the top (as gridded under the
    'title inside the cell' rule)."""
    printed = printed or catalogue
    w, h = 240, 300
    im = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(im)
    d.text((w // 2, 8), printed, font=_font(22), fill="black", anchor="mt")
    d.rectangle([20, 60, w - 20, h - 20], outline=(0, 0, 0))
    d.line([(60, 150), (110, 150)], fill=(0, 0, 0), width=8)   # a "band"
    return im


def test_normalize():
    # OCR-confusion collapse is symmetric, asterisks/space stripped
    assert ocr.normalize("ab209814**") == ocr.normalize("ab209814")
    assert ocr.normalize("AB_2764072") == ocr.normalize("A8_2764072")  # B/8
    assert ocr.normalize(" 168 013 ") == "168013"


def test_match_and_ambiguity():
    cands = ["GTX637386", "GTX637387", "ab209814", "ARP49413_P050"]
    # partial print still resolves to the full catalogue
    assert ocr.match_catalogue("ARP49413", cands).match == "ARP49413_P050"
    # a clean read of one of two near-identical catalogues is flagged ambiguous
    amb = ocr.match_catalogue("GTX63738", cands)     # last digit lost
    assert amb.ambiguous, "near-identical catalogues must be flagged"
    # a clearly-distinct read is confident
    good = ocr.match_catalogue("ab209814", cands)
    assert good.match == "ab209814" and good.confident


def test_ocr_reads_titles():
    if not ocr.available():
        print("SKIP — tesseract not installed (tool falls back to reading order)")
        return
    cands = ["ab209814", "GTX637386", "MAB11618", "MA5-51494", "68723-1-Ig"]
    hits = 0
    for cat in cands:
        panel = _titled_panel(cat, printed=cat + "**")
        res = ocr.prefill(panel, [(0, 0, panel.width, panel.height)], cands)[0]
        if res.match == cat:
            hits += 1
    # allow one miss to keep the test robust to OCR variance on synthetic text
    assert hits >= len(cands) - 1, f"only {hits}/{len(cands)} titles matched"
    print(f"OCR read {hits}/{len(cands)} synthetic titles correctly.")


def main():
    test_normalize()
    test_match_and_ambiguity()
    test_ocr_reads_titles()
    print("OK — OCR normalise, fuzzy-match, ambiguity guard verified.")


if __name__ == "__main__":
    main()
