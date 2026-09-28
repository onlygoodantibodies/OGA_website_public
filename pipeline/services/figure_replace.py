"""Replacing one antibody's figure with a finished image, found by antibody.

The cropper is the way a composite becomes figures; this is the way *one*
figure gets swapped when somebody already has the right panel in hand — a
re-run blot, a better exposure, a figure somebody else assembled. It is a
second door into the same waiting room, not a second way to publish:
``stage`` is ``review.stage``, and the figure reaches the public gene page only
when somebody releases it on ``/pipeline/review/``.

Four things it holds.

**The verdict rides along unchanged.** ``release`` writes the pending row's
``recommended`` onto the antibody, so a replacement staged with the default
``False`` would quietly *un*-recommend an antibody whose verdict nobody meant
to touch. Swapping a picture is not a judgement; the row carries whatever is
queued for it already, else the antibody's current flag.

**The control kind rides along too** — the live figure's ``control_genotype``,
so a knockdown figure does not read as a knockout after its image is swapped.

**It lands on the cropper's canvas, and the pixels are not edited.** A panel
uploaded at its own size (a WB strip is tall and narrow) drew at a different
size from every cropped figure beside it on the gene page, so the upload goes
through ``engine.fit_to_canvas``: the cropper's 490x490 white square, with the
cropper's three operations only — trim the outer white margin, LANCZOS scale,
paste onto white. A transparent background is flattened onto white first.

**It says whether the live page changes now or at release.** A staged figure
lives at ``publication_images/<year>/<GENE>_<cat>_<TYPE>.png``. When the live
figure is already at exactly that key, staging *replaces the object the gene
page is serving* — the owner's 23 Aug 2026 decision (see ``review.py``) — and
the receipt must say so rather than promise a review that no longer gates it.
"""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

from PIL import Image, UnidentifiedImageError

from pipeline.models import Antibody, PendingPublicationImage, PublicationImage
from pipeline.services import find
from pipeline.services import review
from pipeline.services.cropper.engine import filename_for, fit_to_canvas

DB = "pipeline_db"

#: A figure panel is a few hundred kilobytes. Anything over this is a wrong
#: file (a whole gel scan, a TIFF stack), and PIL would decode all of it.
MAX_BYTES = 20 * 1024 * 1024

#: The most antibodies one search draws. A catalogue number names a handful.
MAX_RESULTS = 30

APP_LABELS = {"WB": "Western blot", "IP": "Immunoprecipitation",
              "ICC-IF": "Immunofluorescence", "FC": "Flow cytometry",
              # A finished stack of HAP1 pellet panels; the cropper's stacking
              # is not applied to an upload here.
              "IHC": "Immunohistochemistry"}


class Refused(Exception):
    """This file cannot be staged, and the reason is for the reader."""


# ── Reading ──────────────────────────────────────────────────────────────────

def search(term: str, gene: str = ""):
    """Antibodies matching ``term`` (the search box's own rules), optionally on
    one exact gene. An empty search with no gene is no search at all."""
    term = (term or "").strip()
    gene = (gene or "").strip()
    if not term and not gene:
        return []
    qs = (Antibody.objects.using(DB)
          .select_related("target", "company", "site"))
    if term:
        qs = qs.filter(find.antibody_q(term))
    if gene:
        qs = qs.filter(target__gene_name__iexact=gene)
    return list(qs.distinct().order_by("target__gene_name", "catalogue_number",
                                       "pk")[:MAX_RESULTS])


def figures_for(antibodies, *, public_url, pending_url) -> dict:
    """``{antibody_pk: {app: {"live": url|"", "pending": url|"", ...}}}``.

    Two queries for the whole list. ``public_url``/``pending_url`` turn a row
    into an address, so this module never decides how an image is served.
    """
    ids = [a.pk for a in antibodies]
    out = {pk: {app: {"live": "", "pending": "", "pending_status": ""}
                for app in review.APPLICATIONS} for pk in ids}
    for img in PublicationImage.objects.using(DB).filter(antibody_id__in=ids):
        slot = out[img.antibody_id].get(img.application_type)
        if slot is not None and img.image:
            slot["live"] = public_url(img)
    for item in (PendingPublicationImage.objects.using(DB)
                 .filter(antibody_id__in=ids, status=review.PENDING)):
        slot = out[item.antibody_id].get(item.application_type)
        if slot is not None and item.image:
            slot["pending"] = pending_url(item)
    return out


# ── Writing ──────────────────────────────────────────────────────────────────

def _as_png(content: bytes, app_type: str) -> bytes:
    """Decode, place on the cropper's canvas, and encode as PNG. Refuses
    anything that is not an image."""
    if not content:
        raise Refused("That file is empty. Choose the image file again.")
    if len(content) > MAX_BYTES:
        raise Refused(
            f"That file is {len(content) // (1024 * 1024)} MB; a figure panel is "
            f"well under {MAX_BYTES // (1024 * 1024)} MB. Export the panel on its "
            f"own as PNG or JPG — e.g. RAB6A_9625_WB.png.")
    try:
        img = Image.open(BytesIO(content))
        img.load()
    except (UnidentifiedImageError, OSError, ValueError):
        raise Refused(
            "That file could not be read as an image. Upload a PNG or JPG of "
            "the finished panel — e.g. RAB6A_9625_WB.png.")
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        img = img.convert("RGBA")
        canvas = Image.new("RGB", img.size, "white")
        canvas.paste(img, mask=img.split()[-1])
        img = canvas
    img = fit_to_canvas(img, app_type)
    buf = BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


@dataclass
class Staged:
    item: PendingPublicationImage
    live_changed_now: bool      # the gene page's object was overwritten
    had_live: bool              # a published figure exists for this pair
    recommended: bool           # the verdict carried onto the pending row


def stage(*, antibody, application_type: str, content: bytes,
          staged_by: str = "") -> Staged:
    """Put this image in the review queue as ``antibody``'s ``application_type``
    figure, carrying the verdict and control kind it already has."""
    if application_type not in review.APPLICATIONS:
        raise Refused(
            f"No application called {application_type!r}. Choose one of "
            f"{', '.join(review.APPLICATIONS)} — e.g. WB for a western blot.")
    gene = antibody.target.gene_name if antibody.target_id else ""
    if not gene:
        raise Refused(
            f"{antibody.catalogue_number or 'This antibody'} is filed under no "
            f"gene, and a figure's file name and its public page both come from "
            f"the gene. Set the gene in the antibody's identity dialog on the "
            f"antibodies board first.")
    if not (antibody.catalogue_number or "").strip():
        raise Refused("This antibody has no catalogue number, which the "
                      "figure's file name is built from. Add it on the "
                      "antibodies board first.")
    png = _as_png(content, application_type)

    live = (PublicationImage.objects.using(DB)
            .filter(antibody=antibody, application_type=application_type).first())
    queued = (PendingPublicationImage.objects.using(DB)
              .filter(antibody=antibody, application_type=application_type,
                      status=review.PENDING).first())
    if queued is not None:
        recommended = queued.recommended
    else:
        rec_field = review.RECOMMENDATION_FIELD[application_type]
        recommended = bool(getattr(antibody, rec_field, False))
    control = (live.control_genotype if live else "") or (
        queued.control_genotype if queued else "")

    filename = filename_for(gene, antibody.catalogue_number, application_type)
    probe = queued or PendingPublicationImage(antibody=antibody,
                                              application_type=application_type)
    intended = probe.image.field.generate_filename(probe, filename)
    live_changed_now = bool(live and live.image and live.image.name == intended)

    item = review.stage(antibody=antibody, application_type=application_type,
                        content=png, filename=filename, recommended=recommended,
                        staged_by=staged_by, control_genotype=control,
                        notes="Replaced by upload on Replace a figure.")
    return Staged(item=item, live_changed_now=live_changed_now,
                  had_live=bool(live), recommended=recommended)
