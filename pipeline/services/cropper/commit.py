"""
Transactional commit for the figure cropper (spec §11).

Reads a saved CropperSession (its staged images + grids + mappings), regenerates
each assigned crop SERVER-SIDE with the canonical engine, and upserts
Target / Antibody on pipeline_db — idempotently, in one transaction, aligned to
the write rules in CLAUDE.md.

**The crops land in the review queue, not on the website.** They used to be
written straight to ``PublicationImage``, which *is* publication — the public
site is derived from that relation — so one press by one person put figures in
front of the world with no review meeting in between. They go to
``PendingPublicationImage`` now (``services/review.py``), and releasing them is
a separate later act. The two things this changes here are worth stating
because both were silent leaks waiting to happen:

  * the **recommendation** the human set per antibody per application is stored
    on the pending row rather than on the antibody, because
    ``core/recommendations.py::curated_gene_ids`` reads any recommendation on a
    gene to decide whether the gene has been assessed at all — so writing it
    while withholding the figure would have changed the public verdict on every
    *other* antibody on that gene; and
  * the **overwrite gate** now describes what release will do rather than what
    this press does. This press cannot replace a published figure — it can only
    queue a replacement — so the summary counts both, and the acknowledgement
    is still asked for here, where the person who made the crop is standing.

`build_plan(session)` is read-only (drives the dry-run summary + overwrite gate).
`apply(session)` performs the writes.
"""
from __future__ import annotations

import re
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from io import BytesIO

from django.db import transaction
from PIL import Image

from pipeline.models import Target, Antibody, PendingPublicationImage, PublicationImage
from pipeline.services import concentration as concentration_svc
from pipeline.services import gene_symbol
from pipeline.services import review as review_svc
from pipeline.services import targets as target_svc
from pipeline.services.cropper import db as cdb
from pipeline.services.cropper import engine
from pipeline.services.cropper.engine import Cell, render_cell, filename_for
from pipeline import rrid_utils

DB = "pipeline_db"
# filename tag → DB application_type (IF crops store as ICC-IF)
_APP_DB = {"WB": "WB", "IP": "IP", "ICC-IF": "ICC-IF", "FC": "FC", "IF": "ICC-IF"}
# application → the Antibody "recommended" boolean field (spec §10). One list,
# in `services/review.py`, because release is what applies it and a second copy
# here is how the cropper and the release would come to disagree about which
# column an ICC-IF verdict lives in.
_REC_FIELD = review_svc.RECOMMENDATION_FIELD
# parsed supplier-recommended application code → Antibody supplier-validated field
#
# Six, not four. This is what the *supplier* claims, and a datasheet routinely
# claims more than the four applications OGA characterises — the model has
# carried `supplier_validated_ihc` and `_elisa` since the Access import, and
# leaving them out of this map is what made `WB, IHC` store WB and lose IHC in
# silence. The OGA recommendation (`_REC_FIELD` above) stays four, because that
# is a verdict about what OGA tested.
_SUP_APP_FIELD = {"WB": "supplier_validated_wb", "IP": "supplier_validated_ip",
                  "IF": "supplier_validated_if", "FC": "supplier_validated_fc",
                  "IHC": "supplier_validated_ihc",
                  "ELISA": "supplier_validated_elisa"}


def grid_cells(grid: dict):
    """Reproduce the front-end cell geometry (bandsYof/bandColsOf/cellsFor) in
    Python. vLines are fractions [0..1] of each band's width. Returns
    [(key, l, t, r, bot), ...] in reading order."""
    b = grid.get("bounds") or {}
    if "top" not in b or "bottom" not in b:
        return []
    ys = [b["top"]] + sorted(grid.get("hLines") or []) + [b["bottom"]]
    band_left = grid.get("bandLeft") or []
    band_right = grid.get("bandRight") or []
    vlines = grid.get("vLines") or []
    out = []
    for bi in range(len(ys) - 1):
        if bi >= len(band_left) or bi >= len(band_right):
            continue
        L, R = band_left[bi], band_right[bi]
        fr = sorted(vlines[bi]) if bi < len(vlines) else []
        xs = [L] + [L + f * (R - L) for f in fr] + [R]
        for c in range(len(xs) - 1):
            out.append((f"{bi}_{c}", xs[c], ys[bi], xs[c + 1], ys[bi + 1]))
    return out


def _planned(session):
    """Yield (catalogue, app_db, rect, image_row) for every assigned+mapped cell."""
    abl = [ln.strip() for ln in (session.antibody_list or "").splitlines() if ln.strip()]
    for im in session.images.using(DB).all():
        for key, l, t, r, bot in grid_cells(im.grid or {}):
            st = (im.mapping or {}).get(key)
            if not st or not st.get("assigned"):
                continue
            ab = st.get("ab")
            if ab is None or ab >= len(abl):
                continue
            app = _APP_DB.get(im.application_type, im.application_type)
            yield abl[ab], app, (int(l), int(t), int(r), int(bot)), im


def build_plan(session):
    """Read-only. Returns (target, items) where each item records what would
    happen: the crop, the antibody it maps to (existing or new), whether a
    **published** figure already exists for that antibody+application (so
    releasing this would replace what the public can see), and whether one is
    already **staged** (so this press revises a queued crop rather than adding
    to the queue).

    The gene is resolved through ``targets.match_gene``, the same reader the
    status banner uses, so a session typed under an older symbol commits to the
    gene already in the pipeline instead of creating a second row for it. Asking
    a different question here from the one the banner asked is how a preview
    comes to describe something other than what lands.
    """
    match = target_svc.match_gene(session.gene)
    target = match.target
    ab_cache, items = {}, []
    for cat, app, rect, im in _planned(session):
        if cat not in ab_cache:
            ab_cache[cat] = cdb.find_antibody(target, "", cat) if target else None
        existing_ab = ab_cache[cat]
        existing_image = bool(existing_ab and PublicationImage.objects.using(DB)
                              .filter(antibody=existing_ab, application_type=app).exists())
        staged_image = bool(existing_ab and PendingPublicationImage.objects.using(DB)
                            .filter(antibody=existing_ab, application_type=app).exists())
        items.append({"catalogue": cat, "app": app, "rect": rect, "image_row": im,
                      "existing_ab": existing_ab, "existing_image": existing_image,
                      "staged_image": staged_image})
    return target, items, match


def figure_refusal(items) -> str:
    """Why these crops cannot be cut, or ``""`` — one line per oversized figure.

    The second half of the size guard. ``views/cropper.py::cropper_stage_image``
    refuses an oversized figure at upload, which is where it should be caught;
    this is for the ones already staged, which that check cannot reach. Without
    it the ceiling would apply only to figures added after it shipped, and the
    session that took the container down would still do so.

    Read from ``nat_w``/``nat_h``, recorded at staging, so it costs no storage
    read — and it is exactly the same rule the upload applied, so the two doors
    cannot disagree about one figure.
    """
    seen, lines = set(), []
    for it in items:
        im = it["image_row"]
        if im.id in seen:
            continue
        seen.add(im.id)
        why = engine.size_refusal(im.nat_w, im.nat_h, name=im.name)
        if why:
            lines.append(why)
    return "\n\n".join(lines)


def gene_refusal(match) -> str:
    """Why these crops cannot be committed under this gene name, or "".

    One writer, read by the summary the panel prints and by the guard in
    ``apply``. An older symbol that names two genes on file has no answer — see
    ``targets.match_gene`` — and the tool must not pick one, because a figure
    filed under the wrong gene is published under a name nobody chose.
    """
    if match is not None and match.ambiguous:
        return (f"“{match.typed}” is recorded as another name for more than one "
                f"gene on file ({', '.join(match.ambiguous)}). Set the gene to the "
                f"symbol you mean before saving these crops.")
    return ""


def summarize(session, target, items, match=None) -> dict:
    cats = [i["catalogue"] for i in items]
    existing_cats = {i["catalogue"] for i in items if i["existing_ab"] is not None}
    new_cats = sorted({c for c in cats} - existing_cats)
    overwrite = sum(1 for i in items if i["existing_image"])
    restage = sum(1 for i in items if i["staged_image"])
    rec = session.recommended or {}
    recommended = sum(1 for i in items if rec.get(i["catalogue"], {}).get(i["app"]))
    # same antibody+app assigned in two images → last write wins; warn
    seen, dup = set(), []
    for i in items:
        k = (i["catalogue"], i["app"])
        if k in seen:
            dup.append(f"{i['catalogue']} {i['app']}")
        seen.add(k)
    # The gene these crops will actually be filed under, which is not always the
    # one that was typed: an older symbol resolves to the target already in the
    # pipeline. The panel names both, because a summary that silently swaps the
    # gene is the same failure as a supplier preview echoing what was typed.
    resolved = (target.gene_name if target is not None
                else (match.gene_name if match is not None else session.gene))
    return {
        "gene": resolved,
        "typed_gene": session.gene,
        "gene_matched_via": (match.matched_via if match is not None else ""),
        "gene_on_file_as": (match.on_file_as if match is not None else ""),
        # Both blocking reasons, not the first — they are independent problems
        # and a person told only about the gene would fix it, press again, and
        # meet the other one.
        "refusal": "\n\n".join(
            x for x in (gene_refusal(match), figure_refusal(items)) if x),
        "target": "update" if target else "create",
        "crops": len(items),
        "antibodies_total": len(set(cats)),
        "antibodies_create": len(new_cats),
        "antibodies_update": len(existing_cats),
        # `images_create` / `images_overwrite` keep their names and their
        # meaning — how many of these crops are new to the *public site* and how
        # many would replace a figure on it — because that is the question the
        # overwrite acknowledgement is about, and it is still the right question
        # even though the answer is now settled at release rather than here.
        "images_create": len(items) - overwrite,
        "images_overwrite": overwrite,
        # New, and the one that describes *this* press: how many of these
        # revise something already in the review queue.
        "images_restage": restage,
        "recommended": recommended,
        "new_antibodies": new_cats,
        # Said plainly, because the button used to read "Commit to live site"
        # and a person who has done this before will expect it still to.
        "destination": "review queue",
        "warnings": ([f"{d} assigned in >1 image — last crop wins" for d in dup]),
    }


def _by_figure(items):
    """`[(image_row, [item, ...]), ...]` — the planned crops grouped by the
    figure they are cut from, in the order they were planned.

    **A decoded figure is far bigger than the file it came from, and the commit
    used to hold every one of them at once.** `apply` cached each source image
    in a dict keyed on its id and never released one, so peak memory was the
    *sum* of the whole session's figures rather than the largest of them. On
    14 Aug 2026 that killed the live container mid-commit: session 8 carries six
    figures — two flow panels at 7003×4961 (99 MB each once decoded), a western
    blot and an IP at 3300×2550 (24 MB each) and two IF panels at 2702×2450
    (18 MB each) — which is **282 MB** on top of a 244 MB resting baseline,
    against the 512 MB the web service has. The process was killed, the whole
    instance restarted, and the browser got Render's HTML error page where it
    expected JSON.

    Grouping is what makes the peak the largest single figure (99 MB) instead of
    all six. `_planned` already yields in figure order, so this is not
    re-ordering anything — it is written as an explicit grouping so that a later
    change to that order cannot quietly put the sum back.

    The figures are *not* a cache to be kept across the loop: each is opened,
    cropped from, and closed before the next is touched. That is the whole fix,
    and it costs nothing — no figure is ever revisited, because every crop cut
    from it is in its own group.
    """
    order, groups = [], {}
    for it in items:
        key = it["image_row"].id
        if key not in groups:
            order.append(key)
            groups[key] = (it["image_row"], [])
        groups[key][1].append(it)
    return [groups[k] for k in order]


@contextmanager
def _open_figure(image_row):
    """The decoded source figure, released on the way out.

    Both handles are closed deliberately: `Image.close()` frees the decoded
    pixels, and the Django `File` underneath it holds the object-storage stream
    that fed them. Leaving either to the garbage collector is what turns a
    bounded peak back into a slow climb across a long session.
    """
    fh = image_row.image.open("rb")
    try:
        src = Image.open(fh)
        try:
            src.load()
            yield src
        finally:
            src.close()
    finally:
        try:
            fh.close()
        except Exception:
            pass


_CLONALITY = {"monoclonal", "polyclonal", "recombinant", "unknown"}


def _apply_metadata(ab, m: dict, overwrite: bool = False):
    """Set antibody fields from a parsed metadata row, following the handoff:
    company via resolve_company (Bio-Techne split by catalogue), RRID normalised
    to bare AB_<n> + registry link, clonality mapped to the enum.

    By default only fills blank/unknown fields so an existing row isn't clobbered
    by a sparse paste. With overwrite=True (the "update existing values" round-trip
    from an edited export) a NON-EMPTY incoming value replaces the existing one —
    a blank cell still never wipes data."""
    if not m:
        return
    cat = ab.catalogue_number
    company = cdb.resolve_company(m.get("company", ""), cat, create=True) if m.get("company") else None
    if company and (overwrite or not ab.company_id):
        ab.company = company

    rrid_raw = (m.get("rrid") or "").strip()
    if rrid_raw and (overwrite or not ab.rrid):
        bare = rrid_utils.normalize_rrid(rrid_raw)
        if bare:
            ab.rrid = bare
            ab.rrid_link = rrid_utils.registry_url(bare)

    clon = (m.get("clonality") or "").strip().lower()
    if clon in _CLONALITY and (overwrite or not ab.clonality or ab.clonality == "unknown"):
        ab.clonality = clon
    if m.get("is_recombinant"):
        ab.is_recombinant = True

    for src, field in (("clone_id", "clone_id"), ("host", "host_species"),
                       ("supplier_url", "supplier_url"), ("lot", "lot_number"),
                       # A note written as the rows are created, rather than
                       # twenty-four second passes cell by cell afterwards.
                       ("comments", "comments")):
        val = (m.get(src) or "").strip()
        if val and (overwrite or not getattr(ab, field)):
            setattr(ab, field, val)
    if m.get("discontinued"):
        ab.out_of_market = True

    # concentration — converted into the stored unit (µg/mL), never stripped of
    # its unit. Fill only if blank, unless overwriting. Taking the digits out of
    # "1.0 mg/mL" filed it as 1 µg/mL: a thousand-fold error with nothing on
    # screen to catch it by. A cell whose unit cannot be converted is left alone
    # here and named by the paste preview (`bulk_antibodies._with_concentration_
    # note`) and by the Data I/O diff, both of which read the same parser this
    # does. See services/concentration.py.
    conc = (m.get("concentration") or "").strip()
    if conc and (overwrite or ab.concentration is None):
        value, _err = concentration_svc.parse(conc)
        if value is not None:
            ab.concentration = value

    # supplier's own validated applications from the table (e.g. "Wb, IF").
    # Additive only — set the flags the table lists, never force others False so a
    # sparse paste can't clear an existing claim.
    for code in (m.get("supplier_apps") or []):
        field = _SUP_APP_FIELD.get(code)
        if field:
            setattr(ab, field, True)


class Refused(Exception):
    """This commit will not be attempted, and the reason is for the reader."""


def apply(session, actor_member=None):
    """Perform the writes in one transaction. Returns (target, summary).

    Raises ``Refused`` when the gene cannot be settled — see ``gene_refusal``.
    Checked here rather than only in the view because this is the function that
    writes, and a guard that lives only on the surface in front of it is a guard
    the next surface will not have.
    """
    target, items, match = build_plan(session)
    summary = summarize(session, target, items, match)
    refusal = summary["refusal"]
    if refusal:
        raise Refused(refusal)

    with transaction.atomic(using=DB):
        if target is None:
            # `gene_symbol.canonical`, not what was typed: uppercase is the rule
            # and `orf` is the exception, so a session typed as `c9orf72` would
            # otherwise create a target spelled differently from every other row
            # — the write-path half of the eight mis-cased symbols already on
            # file. Nothing else here changes letters, only case.
            target = Target.objects.using(DB).create(
                gene_name=gene_symbol.canonical(session.gene),
                protein_name=session.protein_name or "")
        # backfill target metadata without clobbering existing values. uniprot_id
        # is UNIQUE — only set it if it's free (else another target owns it; skip
        # and warn rather than crash the whole commit).
        changed = False
        if session.uniprot_id and not target.uniprot_id:
            taken = (Target.objects.using(DB)
                     .filter(uniprot_id=session.uniprot_id).exclude(pk=target.pk).exists())
            if taken:
                summary["warnings"].append(
                    f"UniProt {session.uniprot_id} already belongs to another target — left unset")
            else:
                target.uniprot_id = session.uniprot_id; changed = True
        if session.protein_name and not target.protein_name:
            target.protein_name = session.protein_name; changed = True
        if changed:
            target.save(using=DB)

        meta = session.metadata or {}
        rec = session.recommended or {}
        ab_cache = {}
        # One figure decoded at a time — see `_by_figure`. Holding them all is
        # what took the live container down mid-commit.
        for im_row, planned in _by_figure(items):
            with _open_figure(im_row) as src:
                for it in planned:
                    cat, app = it["catalogue"], it["app"]
                    ab = ab_cache.get(cat)
                    if ab is None:
                        # Whose vial. This path created antibodies with no site
                        # at all, which was already a gap — a row that names no
                        # bench cannot be told from another site's copy of the
                        # same product — and it is now also what decides whether
                        # the record gets the lab's next A-number, since the run
                        # of numbers belongs to the site
                        # (`services/lab_numbers.py`). The committer's own
                        # bench, the same fallback every other write path uses.
                        ab = it["existing_ab"] or Antibody.objects.using(DB).create(
                            target=target, catalogue_number=cat,
                            site_id=getattr(actor_member, "site_id", None))
                        _apply_metadata(ab, meta.get(cat) or {})
                        ab_cache[cat] = ab

                    l, t, r, bot = it["rect"]
                    # **The gene the record is filed under, not the one typed.**
                    # `render_cell` burns the legend into the pixels — an IF or
                    # FC crop reads "HAP1 <gene> knockout cell line" — so a
                    # session typed as PARK8 and filed under LRRK2 would publish
                    # a figure whose own caption disagrees with the page it is
                    # on, permanently and un-editably. Same for the filename.
                    filed_as = target.gene_name or session.gene
                    cell = Cell(left=l, top=t, right=r, bottom=bot, app_type=app,
                                catalogue=cat, gene=filed_as,
                                cell_line=session.cell_line, genotype=session.genotype,
                                fc_secondary_only=session.fc_secondary)
                    buf = BytesIO(); render_cell(src, cell).save(buf, "PNG"); buf.seek(0)
                    fn = filename_for(filed_as, cat, app)

                    # Into the review queue, never straight onto the website. The
                    # human's recommendation for this application rides on the row
                    # and is applied to the antibody when the figure is released —
                    # see the module docstring, and `services/review.py`.
                    review_svc.stage(
                        antibody=ab, application_type=app, content=buf.read(),
                        filename=fn, recommended=bool(rec.get(cat, {}).get(app)),
                        staged_by=session.owner_username, session=session)

        # persist antibody metadata set above
        for ab in ab_cache.values():
            ab.save(using=DB)

    # per-antibody handles so the UI can link straight to each record (e.g. to
    # complete the internal fields the cropper doesn't fill).
    summary["committed"] = [{"catalogue": cat, "id": ab.pk}
                            for cat, ab in ab_cache.items()]
    return target, summary
