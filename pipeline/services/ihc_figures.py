"""Whole IHC figures for a gene's public IHC page — the one writer.

A crop is one antibody in one application, cut out of a figure and put on the
gene page. The **whole** figure is something else: the report's own figure as
published — HAP1 pellets, other cell lines, tissue, H&E and secondary-only
panels, several suppliers' antibodies in one image — with the legend quoted as
published, shown at ``/antibodies/<GENE>/ihc/``. It lives in its own two tables
(``PendingIhcFigure`` → ``IhcFigure``) and never touches ``PublicationImage``,
so nothing that reads the published crops — the headline counts, the API, the
manifest, the archive, the extension, the MCP, the supplier feed — can see it.

**Private until release** (owner, 26 Sep 2026), which is the opposite of what
crops do since 23 Aug. A whole figure shows several manufacturers' unreleased
results at once, and pre-release data goes to the supplier whose reagent it is
and nobody else — so a queued figure is on private storage
(``pipeline/storages.py::attachment_storage``), served to members through a
gated view, and **release copies** the bytes to the public key. Three things
follow and each is held here:

* **withdrawing unpublishes.** The public object is deleted and the private
  copy stays on the re-staged pending row, so withdrawn means unreachable for
  a whole figure (it no longer does for a crop);
* **keys carry a checksum** (``<GENE>_IHC_<slug>_<sha>``), so a revised figure
  is written beside the old object and the old one is freed only after the
  database has committed — never "delete, then write", which leaves the live
  page's image 404ing if the write fails, and never an overwrite, which
  ``AWS_S3_FILE_OVERWRITE=False`` would silently suffix;
* **an unchanged figure is never rewritten**: re-saving a cropper session to
  fix a legend typo copies no bytes.

Every free asks ``review._still_referenced``, the one reader of "does anything
else point at this object" across all four figure tables.

Release is not a door of its own: ``review.release_all`` is the one press that
releases crops and whole figures together, under one summed count.
"""
from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass, field

from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from pipeline.models import (Antibody, IhcFigure, IhcFigureAntibody,
                             PendingIhcFigure, PendingIhcFigureAntibody, Target)

DB = "pipeline_db"

PENDING = PendingIhcFigure.Status.PENDING
RELEASED = PendingIhcFigure.Status.RELEASED


class Refused(Exception):
    """A write this module will not make, with the reason for the reader."""


# ── What a figure declares ────────────────────────────────────────────────────
#
# Typed in the cropper's "Whole figure on the gene's IHC page" fieldset and
# stored on the row. Every sentence the public page draws about a figure is
# derived from these and nothing else — no chromogen, fixation or replicate
# claim, because none of those is typed anywhere.

#: The controls a figure can carry, as stored → as printed.
CONTROLS = {
    "he": "H&E",
    "rabbit_secondary": "rabbit secondary only",
    "mouse_secondary": "mouse secondary only",
}

#: The tissue species offered in the cropper. "other" takes the typed name.
TISSUE_SPECIES = ("mouse", "human", "other")

#: Formats a browser draws. A byte-for-byte copy of anything else (TIFF, HEIC)
#: would publish a figure the page cannot show.
FORMATS = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}


def slug_of(label: str) -> str:
    """The key a label is stored and published under.

    Uniqueness is on this, not on the label: ``Figure 4`` and ``figure 4``, or
    ``Figure 3, page 2 of 5`` and ``Figure 3 page 2 of 5``, are one figure and
    one object — two rows with one key would have one delete the other's file.
    """
    return (slugify(label or "") or "figure")[:120]


def normalise(raw) -> dict:
    """``grid.ihcPage`` as the cropper saved it, in the shape the writer uses.

    Tolerant of a missing or partial dict — an older session has none — and of
    anything typed with stray whitespace. ``crop`` defaults to true: a figure
    is cropped for the gene page unless somebody said otherwise.
    """
    raw = raw if isinstance(raw, dict) else {}
    samples = raw.get("samples") if isinstance(raw.get("samples"), dict) else {}
    species = str(samples.get("tissue_species") or "").strip().lower()
    return {
        "on": bool(raw.get("on")),
        "crop": raw.get("crop") is not False,
        "label": str(raw.get("label") or "").strip()[:120],
        "legend": str(raw.get("legend") or "").strip(),
        "samples": {
            "hap1_pellets": bool(samples.get("hap1_pellets")),
            "other_lines": str(samples.get("other_lines") or "").strip()[:200],
            "tissue": bool(samples.get("tissue")),
            "tissue_species": species if species in TISSUE_SPECIES else "",
            "tissue_species_other": str(samples.get("tissue_species_other") or "").strip()[:60],
            "tissue_organs": str(samples.get("tissue_organs") or "").strip()[:200],
        },
        "controls": [c for c in CONTROLS if c in (raw.get("controls") or [])],
        "supplier_key": bool(raw.get("supplier_key")),
        "antibodies": [str(c).strip() for c in (raw.get("antibodies") or [])
                       if str(c or "").strip()],
        "source": str(raw.get("source") or "").strip()[:300],
    }


def tissue_species(samples) -> str:
    """``mouse`` / ``human`` / the typed name for "other", or ``""``."""
    s = samples or {}
    if s.get("tissue_species") == "other":
        return (s.get("tissue_species_other") or "").strip()
    return s.get("tissue_species") or ""


def describe_samples(samples) -> list[str]:
    """What the figure shows, as printed — one writer for the queue and the page."""
    s = samples or {}
    out = []
    if s.get("hap1_pellets"):
        out.append("HAP1 cell pellets")
    if s.get("other_lines"):
        out.append(f"other cell lines: {s['other_lines']}")
    if s.get("tissue"):
        what = " ".join(x for x in (tissue_species(s), "tissue") if x)
        out.append(what + (f": {s['tissue_organs']}" if s.get("tissue_organs") else ""))
    return out


def describe_controls(controls) -> list[str]:
    return [CONTROLS[c] for c in CONTROLS if c in (controls or [])]


def sniff(content: bytes) -> str:
    """The image format of these bytes (``JPEG``/``PNG``/``WEBP``/…), or ``""``.

    Read from the bytes, not the filename: the cropper accepts ``image/*``, and
    a TIFF renamed ``.jpg`` is still a TIFF to the browser that cannot draw it.
    Header only — ``Image.open`` does not decode the pixels.
    """
    from PIL import Image
    try:
        with Image.open(io.BytesIO(content)) as im:
            return (im.format or "").upper()
    except Exception:
        return ""


def type_refusal(name: str, fmt: str) -> str:
    """Why this file cannot go on the public IHC page, or ``""``."""
    if fmt in FORMATS:
        return ""
    what = f"a {fmt} file" if fmt else "not a file this can read as an image"
    return (f"“{name}” is {what}. The IHC page shows JPEG, PNG or WebP only — "
            f"a browser cannot draw anything else. Export the figure as one of "
            f"those, add it to the cropper again, and save.")


def _name(gene: str, slug: str, checksum: str, ext: str) -> str:
    return f"{gene}/{gene}_IHC_{slug}_{checksum[:10]}.{ext}"


def pending_name(gene, slug, checksum, ext) -> str:
    field_ = PendingIhcFigure._meta.get_field("image")
    return field_.generate_filename(None, _name(gene, slug, checksum, ext))


def public_name(gene, slug, checksum, ext) -> str:
    field_ = IhcFigure._meta.get_field("image")
    return field_.generate_filename(None, _name(gene, slug, checksum, ext))


def _ext_of(name: str) -> str:
    return (name.rsplit(".", 1)[-1] if "." in (name or "") else "jpg").lower()


def _still_referenced(name, **exclude) -> bool:
    from pipeline.services import review
    return review._still_referenced(name, **exclude)


def _free_after_commit(storage, name: str, **exclude) -> None:
    """Free an object once the database has committed, and only if nothing
    else points at it then. A free inside a transaction that rolls back is a
    live row pointing at nothing."""
    if not name:
        return

    def _go():
        if not _still_referenced(name, **exclude):
            try:
                storage.delete(name)
            except Exception:
                pass
    transaction.on_commit(_go, using=DB)


def _write(storage, name: str, content: bytes) -> str:
    """Put bytes at ``name`` unless the same object is already there. The name
    carries the checksum, so an object already at it *is* these bytes."""
    if storage.exists(name):
        return name
    return storage.save(name, ContentFile(content))


def _set_antibodies(link_model, figure, antibodies) -> None:
    link_model.objects.using(DB).filter(figure=figure).delete()
    seen = set()
    rows = []
    for i, ab in enumerate(antibodies):
        if ab is None or ab.pk in seen:
            continue
        seen.add(ab.pk)
        rows.append(link_model(figure=figure, antibody=ab, position=i))
    link_model.objects.using(DB).bulk_create(rows)


def ordered_antibodies(figure) -> list:
    """The antibodies a figure shows, in the order it shows them."""
    return [link.antibody for link in figure.antibody_links.all()]


# ── Staging ───────────────────────────────────────────────────────────────────

def stage(*, target, spec: dict, content: bytes, width: int, height: int,
          antibodies, staged_by: str = "", session=None,
          control_genotype: str = "", filename: str = "") -> tuple:
    """Put one whole figure in the waiting room. Returns ``(row, bytes_written)``.

    Upserts on ``(target, slug)``. Re-staging a released figure sets the queued
    row back to ``pending`` and leaves the public figure — and its public copy
    of the bytes — exactly as they are until somebody releases the revision.

    ``content`` is copied byte for byte: no re-encode and no resize, which is
    the "never modify original pixels" rule, and it holds because nothing here
    decodes the image beyond reading its header.
    """
    fmt = sniff(content)
    why = type_refusal(filename or spec.get("label") or "figure", fmt)
    if why:
        raise Refused(why)
    if not spec.get("label") or not spec.get("legend"):
        raise Refused("A whole figure needs its label and its legend as published.")

    slug = slug_of(spec["label"])
    checksum = hashlib.sha256(content).hexdigest()
    item = (PendingIhcFigure.objects.using(DB)
            .filter(target=target, slug=slug).first())
    if item is None:
        item = PendingIhcFigure(target=target, slug=slug)

    storage = PendingIhcFigure._meta.get_field("image").storage
    intended = pending_name(target.gene_name, slug, checksum, FORMATS[fmt])
    previous = item.image.name if item.image else ""
    written = False
    if previous == intended and storage.exists(intended):
        pass                                    # unchanged — copy nothing
    else:
        item.image.name = _write(storage, intended, content)
        written = True

    item.label = spec["label"]
    item.legend = spec["legend"]
    item.width, item.height = int(width or 0), int(height or 0)
    item.checksum = checksum
    item.samples = spec.get("samples") or {}
    item.controls = spec.get("controls") or []
    item.supplier_key = bool(spec.get("supplier_key"))
    item.scale = (spec.get("scale") or "")[:60]
    item.source = spec.get("source") or ""
    if control_genotype:
        item.control_genotype = control_genotype.strip().upper()[:4]
    item.status = PENDING
    item.staged_by = (staged_by or "")[:150]
    item.released_by = ""
    item.released_at = None
    if session is not None:
        item.source_session = session
    item.save(using=DB)
    _set_antibodies(PendingIhcFigureAntibody, item, antibodies)

    if previous and previous != item.image.name:
        _free_after_commit(storage, previous, pending_ihc_pk=item.pk)
    return item, written


# ── Reading ───────────────────────────────────────────────────────────────────

def pending_qs():
    return (PendingIhcFigure.objects.using(DB).filter(status=PENDING)
            .select_related("target")
            .prefetch_related("antibody_links__antibody__company",
                              "antibody_links__antibody__site"))


def for_target(target_id, *, status=PENDING):
    return (PendingIhcFigure.objects.using(DB)
            .filter(target_id=target_id, status=status)
            .select_related("target")
            .prefetch_related("antibody_links__antibody__company",
                              "antibody_links__antibody__site"))


def public_for_target(target_id):
    return (IhcFigure.objects.using(DB).filter(target_id=target_id)
            .select_related("target")
            .prefetch_related("antibody_links__antibody__company"))


def natural_key(label: str):
    """``Figure 2`` before ``Figure 10`` — the order the report numbers them."""
    return [int(t) if t.isdigit() else t.lower()
            for t in re.split(r"(\d+)", label or "")]


def live_slugs(figs) -> set:
    """``{(target_id, slug)}`` of these queued figures that replace a live one."""
    wanted = {(f.target_id, f.slug) for f in figs}
    if not wanted:
        return set()
    live = (IhcFigure.objects.using(DB)
            .filter(target_id__in={t for t, _ in wanted})
            .values_list("target_id", "slug"))
    return {pair for pair in live if pair in wanted}


def row(fig, *, image_url="", replaces_live=False) -> dict:
    """One whole figure as JSON — every value a string, number or bool."""
    from pipeline.services.review import _supplier_of
    abs_ = ordered_antibodies(fig)
    return {
        "id": fig.pk,
        "kind": "whole_figure",
        "gene": fig.target.gene_name or "",
        "target_id": fig.target_id,
        "label": fig.label,
        "legend": fig.legend,
        "antibodies": [{"catalogue": ab.catalogue_number or "",
                        "supplier": _supplier_of(ab)} for ab in abs_],
        "samples": describe_samples(fig.samples),
        "controls": describe_controls(fig.controls),
        "supplier_key": bool(fig.supplier_key),
        "scale": fig.scale or "",
        "source": fig.source or "",
        "width": fig.width, "height": fig.height,
        "status": getattr(fig, "status", "released"),
        "staged_by": getattr(fig, "staged_by", ""),
        "released_by": fig.released_by or "",
        "released_at": fig.released_at.isoformat() if fig.released_at else "",
        "image_url": image_url,
        "replaces_live": bool(replaces_live),
    }


def rows_for(figs, *, url_of=None) -> list[dict]:
    figs = list(figs)
    live = live_slugs(figs)
    figs.sort(key=lambda f: natural_key(f.label))
    return [row(f, image_url=(url_of(f) if url_of else ""),
                replaces_live=(f.target_id, f.slug) in live) for f in figs]


# ── Release (only through `review.release_all`) ───────────────────────────────

def release(figs, *, actor: str, written: list) -> list[dict]:
    """Copy each queued figure to its public key and write the public row.

    Called by ``review.release_all`` inside its transaction and nowhere else;
    ``written`` collects the public objects this call put in storage, so the
    caller can free them if the transaction then fails — storage does not roll
    back with the database.
    """
    public_storage = IhcFigure._meta.get_field("image").storage
    now = timezone.now()
    out = []
    for fig in figs:
        if not fig.image:
            raise Refused(f"{fig.target.gene_name} “{fig.label}” has no stored "
                          f"image — save it from the cropper again.")
        live = (IhcFigure.objects.using(DB)
                .filter(target_id=fig.target_id, slug=fig.slug).first())
        ext = _ext_of(fig.image.name)
        intended = public_name(fig.target.gene_name, fig.slug,
                               fig.checksum or "0" * 10, ext)
        superseded = ""
        if live is not None and live.image and live.image.name == intended \
                and public_storage.exists(intended):
            name = intended                     # same bytes already public
        else:
            if not public_storage.exists(intended):
                with fig.image.open("rb") as fh:
                    content = fh.read()
                name = public_storage.save(intended, ContentFile(content))
                written.append(name)
            else:
                name = intended
            if live is not None and live.image and live.image.name != name:
                superseded = live.image.name
        if live is None:
            live = IhcFigure(target_id=fig.target_id, slug=fig.slug)
        for f in ("label", "legend", "width", "height", "checksum", "samples",
                  "controls", "supplier_key", "scale", "source",
                  "control_genotype"):
            setattr(live, f, getattr(fig, f))
        live.image.name = name
        live.released_by = (actor or "")[:150]
        live.released_at = now
        live.save(using=DB)
        _set_antibodies(IhcFigureAntibody, live, ordered_antibodies(fig))
        if superseded:
            _free_after_commit(public_storage, superseded, public_ihc_pk=live.pk)

        fig.status = RELEASED
        fig.released_by = (actor or "")[:150]
        fig.released_at = now
        fig.save(using=DB, update_fields=["status", "released_by",
                                          "released_at", "updated_at"])
        out.append({"gene": fig.target.gene_name or "", "label": fig.label})
    return out


# ── Discard ───────────────────────────────────────────────────────────────────

def discard(figs) -> int:
    """Take queued whole figures out of the queue. Frees their private copy."""
    figs = list(figs)
    for fig in figs:
        if fig.status == RELEASED:
            raise Refused(
                f"{fig.target.gene_name} “{fig.label}” is on the public IHC page, "
                f"so discarding it here would change nothing there. Withdraw it "
                f"from the released list on the review queue instead.")
    storage = PendingIhcFigure._meta.get_field("image").storage
    n = 0
    for fig in figs:
        name = fig.image.name if fig.image else ""
        pk = fig.pk
        fig.delete(using=DB)
        _free_after_commit(storage, name, pending_ihc_pk=pk)
        n += 1
    return n


def discard_refusal(fig, member, is_superuser, username: str = "") -> str:
    """Why this person may not discard this queued whole figure, or ``""``.

    Three ways to be allowed, because the ordinary rule — your own bench's
    records — has no single answer for a figure: a target's site lives on its
    nominations and the founding targets have none, so keying on the gene
    would refuse the person who staged it their own mistake. So: the person
    who staged it, a member of a site whose antibody the figure shows, or a
    superuser.
    """
    if is_superuser:
        return ""
    if username and fig.staged_by == username:
        return ""
    mine = getattr(member, "site_id", None)
    sites = {link.antibody.site_id for link in fig.antibody_links.all()
             if link.antibody.site_id}
    if mine and mine in sites:
        return ""
    return (f"“{fig.label}” was saved by {fig.staged_by or 'somebody else'} and "
            f"shows no antibody of your site's, so it is not yours to discard. "
            f"Whoever saved it, or an administrator, can.")


# ── Withdraw ──────────────────────────────────────────────────────────────────

@dataclass
class WithdrawResult:
    withdrawn: list = field(default_factory=list)      # (gene, label)
    restaged: int = 0
    already_queued: int = 0


def withdraw(figs, *, actor: str = "") -> WithdrawResult:
    """Take whole figures off the public IHC page and back into the queue.

    **This one unpublishes.** The public object is deleted (after the commit),
    so a withdrawn whole figure is unreachable, and the private copy stays on
    the re-staged pending row — releasing it again is one press. A figure
    whose pending row already holds a newer edit keeps that edit; the public
    row still goes.

    Callers: ``review.withdraw`` (a gene leaving the public site takes its
    whole figures with it) and ``review.withdraw_whole_figures`` (one figure).
    Both inside a transaction; consent is theirs to check.
    """
    public_storage = IhcFigure._meta.get_field("image").storage
    private_storage = PendingIhcFigure._meta.get_field("image").storage
    result = WithdrawResult()
    for fig in figs:
        pending = (PendingIhcFigure.objects.using(DB)
                   .filter(target_id=fig.target_id, slug=fig.slug).first())
        if pending is not None and pending.status == PENDING:
            result.already_queued += 1
        elif pending is not None and pending.image and private_storage.exists(
                pending.image.name):
            pending.status = PENDING
            pending.released_by = ""
            pending.released_at = None
            pending.staged_by = (actor or "")[:150]
            pending.notes = "Withdrawn from the public IHC page."
            pending.save(using=DB)
            result.restaged += 1
        else:
            # No private copy to fall back on (the row was lost, or its object
            # was) — the public bytes are the only copy, so they are copied
            # back before the public object goes.
            content = b""
            if fig.image:
                with fig.image.open("rb") as fh:
                    content = fh.read()
            if pending is None:
                pending = PendingIhcFigure(target_id=fig.target_id, slug=fig.slug)
            for f in ("label", "legend", "width", "height", "checksum", "samples",
                      "controls", "supplier_key", "scale", "source",
                      "control_genotype"):
                setattr(pending, f, getattr(fig, f))
            if content:
                pending.image.name = _write(
                    private_storage,
                    pending_name(fig.target.gene_name, fig.slug,
                                 fig.checksum or "0" * 10, _ext_of(fig.image.name)),
                    content)
            pending.status = PENDING
            pending.released_by = ""
            pending.released_at = None
            pending.staged_by = (actor or "")[:150]
            pending.notes = "Withdrawn from the public IHC page."
            pending.save(using=DB)
            _set_antibodies(PendingIhcFigureAntibody, pending, ordered_antibodies(fig))
            result.restaged += 1

        name = fig.image.name if fig.image else ""
        pk = fig.pk
        result.withdrawn.append((fig.target.gene_name or "", fig.label))
        fig.delete(using=DB)
        _free_after_commit(public_storage, name, public_ihc_pk=pk)
    return result


# ── What the public page says, derived from what was typed ────────────────────

def reading_notes(figures) -> list[str]:
    """The "How to read these figures" sentences — each drawn only when some
    figure on the page declares the thing it explains.

    Nothing here is a method fact nobody typed: no chromogen, no fixation, no
    replicates, no reason a tissue was chosen. Those would read as fluent and
    specific and be invented, which is the defect a draft report was fixed for.
    """
    from pipeline.public import control_word
    figures = list(figures)
    samples = [f.samples or {} for f in figures]
    controls = set()
    for f in figures:
        controls |= set(f.controls or [])
    notes = []
    if any(s.get("hap1_pellets") for s in samples):
        words = sorted({control_word(f.control_genotype) or "knockout"
                        for f in figures if (f.samples or {}).get("hap1_pellets")})
        notes.append(
            "The HAP1 panels are cell pellets — wild-type and "
            + " or ".join(words) + " HAP1 cells, a cell line, not tissue. "
            "OGA's result in each table is judged from these pellets.")
    if "he" in controls:
        notes.append(
            "H&E (haematoxylin and eosin) shows the morphology of the section — "
            "that it holds intact cells or tissue. It is not antibody staining "
            "and not a specificity control.")
    hosts = [w for c, w in (("rabbit_secondary", "rabbit"),
                            ("mouse_secondary", "mouse")) if c in controls]
    if hosts:
        notes.append(
            "Secondary only (" + " and ".join(hosts) + ") is the background "
            "control: the section is stained with the secondary antibody for "
            "that host species and no primary antibody, so signal there is not "
            "the primary antibody's.")
    if any(f.supplier_key for f in figures):
        notes.append(
            "Where the antibody names in a figure are coloured, the colours are "
            "the supplier's own recommendation as the figure's key gives it — "
            "a claim that is the supplier's, not OGA's result.")
    if any(s.get("tissue") for s in samples):
        notes.append(
            "Tissue panels carry no OGA rating: OGA's result is for the cell "
            "pellets only. An antibody missing from a tissue figure is absent "
            "from that figure, not a failed result.")
    notes.append(
        "Each legend is quoted as published. OGA characterises antibodies; it "
        "does not validate them — whether an antibody suits your experiment is "
        "yours to establish in your own samples.")
    return notes


def meta_description(gene: str, figures, antibody_count: int) -> str:
    """The page's meta description, from the same figures the page draws."""
    from pipeline.public import control_phrase
    figures = list(figures)
    samples = [f.samples or {} for f in figures]
    parts = []
    if any(s.get("hap1_pellets") for s in samples):
        kinds = {(f.control_genotype or "KO").upper() for f in figures
                 if (f.samples or {}).get("hap1_pellets")}
        parts.append(f"{control_phrase(kinds, capital=False)} HAP1 cell pellets")
    if any(s.get("other_lines") for s in samples):
        parts.append("other cell lines")
    species = sorted({tissue_species(s) for s in samples if s.get("tissue")} - {""})
    if any(s.get("tissue") for s in samples):
        parts.append(" and ".join(species) + " tissue" if species else "tissue")
    controls = set()
    for f in figures:
        controls |= set(f.controls or [])
    ctrl = []
    if "he" in controls:
        ctrl.append("H&E")
    if controls & {"rabbit_secondary", "mouse_secondary"}:
        ctrl.append("secondary-only")
    from pipeline.public import application_list
    text = (f"Immunohistochemistry figures for {antibody_count} {gene} "
            f"antibod{'y' if antibody_count == 1 else 'ies'}")
    if parts:
        text += " on " + application_list(parts)
    if ctrl:
        text += f", with {' and '.join(ctrl)} controls"
    return text + " — characterisation data from YCharOS."


def merge_links(survivor, losers) -> int:
    """Before a merge of duplicate antibodies re-points the losers' links onto
    the survivor, drop each loser link to a figure the survivor is already
    in — one row per figure per antibody is the constraint, and the figure
    still lists the antibody through the survivor's own link. Returns how
    many were dropped. Everything else moves with the ordinary FK update
    (`services/duplicates.py::CHILD_RELATIONS`)."""
    dropped = 0
    loser_ids = [l.pk for l in losers]
    for link_model in (IhcFigureAntibody, PendingIhcFigureAntibody):
        seen = set(link_model.objects.using(DB).filter(antibody=survivor)
                   .values_list("figure_id", flat=True))
        doomed = []
        # Two losers in one figure are one antibody there too.
        for pk, fig_id in (link_model.objects.using(DB)
                           .filter(antibody_id__in=loser_ids)
                           .order_by("position", "pk")
                           .values_list("pk", "figure_id")):
            if fig_id in seen:
                doomed.append(pk)
            seen.add(fig_id)
        if doomed:
            dropped += link_model.objects.using(DB).filter(pk__in=doomed).delete()[0]
    return dropped


# ── The gene's IHC page (`/antibodies/<GENE>/ihc/`) — the one reader ─────────
#
# Here rather than in `pipeline/public.py` on purpose: that module is on the
# MCP connector's import path, whose database role may read only the tables
# `mcp_servers/common/grants.py` allows, and a whole figure must never reach
# the MCP. The page exists only while the gene page does, so every reader
# starts from `public_targets()`.

def page_figures(target) -> list:
    """The released whole figures of one public gene, in the order the report
    numbers them — for the page, its meta description and its tables. Empty
    for a gene that is not public, whatever `IhcFigure` holds."""
    from pipeline.public import public_targets
    if target is None or not public_targets().filter(pk=target.pk).exists():
        return []
    figs = list(IhcFigure.objects.filter(target=target)
                .prefetch_related("antibody_links__antibody__company",
                                  "antibody_links__antibody__publication_images"))
    return sorted(figs, key=lambda f: natural_key(f.label))


def tissue_anchors(target) -> dict:
    """``{antibody_id: slug}`` — for each antibody shown in a released
    **tissue** figure on the gene's IHC page, the first such figure (report
    order). The gene page links "See tissue staining" from that antibody's
    HAP1 result to ``/antibodies/<GENE>/ihc/#<slug>`` (owner, 26 Sep 2026: the
    IHC page is for tissue, and the pellets sit beside the antibody). A figure
    not ticked as showing tissue is never linked to, so the link cannot land
    on a pellet figure."""
    out = {}
    for fig in page_figures(target):
        if not (fig.samples or {}).get("tissue"):
            continue
        for link in fig.antibody_links.all():
            out.setdefault(link.antibody_id, fig.slug)
    return out


def page_targets():
    """Public genes that have an IHC page — for the sitemap."""
    from django.db.models import Exists, OuterRef

    from pipeline.public import public_targets
    return public_targets().filter(
        Exists(IhcFigure.objects.filter(target_id=OuterRef("pk"))))


def has_page(target) -> bool:
    """Whether `/antibodies/<GENE>/ihc/` answers 200 — asked before any
    surface draws a link there, so none links to a 404."""
    if target is None:
        return False
    return page_targets().filter(pk=target.pk).exists()
