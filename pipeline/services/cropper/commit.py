"""
Transactional commit for the figure cropper (spec §11).

Reads a saved CropperSession (its staged images + grids + mappings), regenerates
each assigned crop SERVER-SIDE with the canonical engine, and upserts the
gene's Antibody rows on pipeline_db (never a Target) — idempotently, in one
transaction, aligned to the write rules in CLAUDE.md.

**The crops land in the review queue, not on the website.** They used to be
written straight to ``PublicationImage``, which *is* publication — the public
site is derived from that relation — so one press by one person put figures in
front of the world with no review meeting in between. They go to
``PendingPublicationImage`` now (``services/review.py``), and releasing them is
a separate later act. The two things this changes here are worth stating
because both were silent leaks waiting to happen:

  * the **recommendation** is not set here at all (owner, 25 Sep 2026): the
    judgement is made on the review queue or Judge outcomes. The pending row
    keeps whatever verdict it already carries, or starts from the antibody's
    own flag, and nothing touches the antibody until release, because
    ``core/recommendations.py::curated_gene_ids`` reads any recommendation on a
    gene to decide whether the gene has been assessed at all; and
  * the **overwrite gate** is about the public file as well as the page. A
    crop is written at its final public key (`review.stage`, owner 23 Aug
    2026), so a re-crop of a figure published at that key replaces the file a
    live gene page is showing **as this press lands**, not at release — review
    gates which figures the page lists, not the bytes behind them. The summary
    counts both (`images_overwrite`, and `images_overwrite_live` for the ones
    that change a live page now) and the acknowledgement is asked for here,
    where the person who made the crop is standing.

**It never creates a gene.** A target is added on the targets doors and
nowhere else (CLAUDE.md, "Adding a gene"); a session whose gene is not on file
is refused by name with the board to add it on (`gene_refusal`).

`build_plan(session)` is read-only (drives the dry-run summary + overwrite gate).
`apply(session)` performs the writes.
"""
from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from io import BytesIO

from django.db import transaction
from PIL import Image

from pipeline.models import (Antibody, IhcFigure, PendingIhcFigure,
                             PendingPublicationImage, PublicationImage, Target)
from pipeline.services import concentration as concentration_svc
from pipeline.services import received as received_svc
from pipeline.services import review as review_svc
from pipeline.services import targets as target_svc
from pipeline.services.cropper import db as cdb
from pipeline.services.cropper import engine
from pipeline.services.cropper.engine import Cell, render_cell, filename_for
from pipeline import rrid_utils

DB = "pipeline_db"
# filename tag → DB application_type (IF crops store as ICC-IF)
_APP_DB = {"WB": "WB", "IP": "IP", "ICC-IF": "ICC-IF", "FC": "FC", "IF": "ICC-IF",
           "IHC": "IHC"}
# application → the Antibody "recommended" boolean field (spec §10). One list,
# in `services/review.py`, because release is what applies it and a second copy
# here is how the cropper and the release would come to disagree about which
# column an ICC-IF verdict lives in.
_REC_FIELD = review_svc.RECOMMENDATION_FIELD
# parsed supplier-recommended application code → Antibody supplier-validated field
#
# Six, not four. This is what the *supplier* claims, and a datasheet routinely
# claims more than the applications OGA characterises — the model has
# carried `supplier_validated_ihc` and `_elisa` since the Access import, and
# leaving them out of this map is what made `WB, IHC` store WB and lose IHC in
# silence. The OGA recommendation (`_REC_FIELD` above) stays five, because that
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


def _antibody_list(session):
    return [ln.strip() for ln in (session.antibody_list or "").splitlines() if ln.strip()]


def ihc_layout(im) -> str:
    """How one IHC figure is drawn: ``"row"`` — one antibody per row, its
    panels side by side, stacked by the crop (the TP53 figure) — or ``"cell"``
    — one antibody per grid cell, already stacked and labelled in the figure
    itself (PPP2R5D's Figure 4, a column of HAP1 WT / HAP1 KO / HAP1 cores
    under each antibody's name)."""
    return "cell" if (im.grid or {}).get("ihcLayout") == "cell" else "row"


def whole_spec(im):
    """The figure's "Whole figure on the gene's IHC page" settings, normalised,
    or ``None`` when it is not going on the IHC page. IHC figures only — the
    fieldset is offered for IHC alone, and a figure whose application was
    changed away from IHC afterwards is not a whole IHC figure any more."""
    from pipeline.services import ihc_figures
    if _APP_DB.get(im.application_type, im.application_type) != "IHC":
        return None
    spec = ihc_figures.normalise((im.grid or {}).get("ihcPage"))
    return spec if spec["on"] else None


def is_cropped(im) -> bool:
    """Whether this figure is cut into crops for the gene page. False only for
    an IHC figure ticked for the IHC page with "Also crop this figure"
    unticked — a tissue-only figure has no HAP1 pellet to crop. **One reader
    for the preview and the write**: `_planned` and `ihc_refusal` both ask it,
    so a figure the save will not crop is neither counted nor refused as a
    crop by the check before it."""
    spec = whole_spec(im)
    return spec is None or spec["crop"]


def ihc_labels(im, session, gene: str = "") -> list:
    """The gutter labels for one IHC figure, one per stacked panel.

    ``null`` in the saved grid means the box was never touched, and gives what
    it starts with (WT / knockout / mosaic under the gene the crops are **filed**
    under — the same default the page's box shows); an **empty** list means the
    person cleared it, because the figure's panels label themselves, and draws
    none. A figure laid out one antibody per cell never takes gutter labels —
    there is one piece, and its labels are already in it. One reader for the
    crop and the refusal."""
    if ihc_layout(im) == "cell":
        return []
    raw = (im.grid or {}).get("ihcLabels")
    if raw is None:
        return engine.ihc_default_labels(gene or session.gene, session.genotype)
    return [x for x in (str(v).strip() for v in raw) if x]


def _ihc_groups(im):
    """``[(where, [(key, rect, state), ...]), ...]`` — the assigned cells of one
    IHC figure, one group per antibody-shaped unit: a whole row, left to right,
    or a single cell, in reading order. ``where`` names it for a refusal."""
    cells = []
    for key, l, t, r, bot in grid_cells(im.grid or {}):
        st = (im.mapping or {}).get(key)
        if st and st.get("assigned"):
            band, col = (int(x) for x in key.split("_"))
            cells.append((band, col, key, (int(l), int(t), int(r), int(bot)), st))
    if ihc_layout(im) == "cell":
        return [(f"Cell {i + 1} of “{im.name}”", [(k, r, st)])
                for i, (_b, _c, k, r, st) in enumerate(cells)]
    rows = {}
    for band, col, key, rect, st in cells:
        rows.setdefault(band, []).append((col, key, rect, st))
    return [(f"Row {b + 1} of “{im.name}”",
             [(k, r, st) for _c, k, r, st in sorted(rows[b])])
            for b in sorted(rows)]


def _ihc_problem(where, cells, labels, abl) -> str:
    abs_ = {st.get("ab") for _k, _r, st in cells} - {None}
    if len(abs_) > 1:
        names = ", ".join(abl[a] if a < len(abl) else f"#{a + 1}" for a in sorted(abs_))
        return (f"{where} has its panels set to different antibodies ({names}). "
                f"An IHC row is one antibody — set them all to the same one.")
    if abs_ and labels and len(cells) != len(labels):
        return (f"{where} has {len(cells)} panel{'' if len(cells) == 1 else 's'} "
                f"assigned, and the figure's labels name {len(labels)} "
                f"({', '.join(labels)}). Assign one panel per label, change the "
                f"labels to match the columns, or clear them if the panels "
                f"carry their own.")
    return ""


def ihc_refusal(session) -> str:
    """Why an IHC figure's antibodies cannot be cropped as they stand, or ``""``.

    **One row of an IHC figure is one antibody**, so the check is per row and
    names it: a row with the wrong number of panels would stack two cell lines
    under three labels, and the labels are burned into the pixels — a figure
    whose knockout panel says "Mosaic" is not evidence of anything. Rows with
    nothing mapped are left out here, the same as an unmapped cell on any other
    figure (the preview counts those)."""
    abl, lines = _antibody_list(session), []
    for im in session.images.using(DB).all():
        if _APP_DB.get(im.application_type, im.application_type) != "IHC":
            continue
        if not is_cropped(im):
            continue
        labels = ihc_labels(im, session)
        for where, cells in _ihc_groups(im):
            why = _ihc_problem(where, cells, labels, abl)
            if why:
                lines.append(why)
    return "\n".join(lines)


def _planned(session):
    """Yield (catalogue, app_db, rect, image_row) for every assigned+mapped cell.

    For an IHC figure, one per **antibody** instead — a whole row, or a single
    cell, by the figure's layout — and `rect` is the list of its panels. A row `ihc_refusal` objects to is not yielded
    — `apply` refuses before it writes anything, so this only keeps the
    preview's counts from describing crops that would not be cut."""
    abl = _antibody_list(session)
    for im in session.images.using(DB).all():
        app = _APP_DB.get(im.application_type, im.application_type)
        if not is_cropped(im):
            continue
        if app == "IHC":
            labels = ihc_labels(im, session)
            for where, cells in _ihc_groups(im):
                abs_ = {st.get("ab") for _k, _r, st in cells} - {None}
                if len(abs_) != 1 or _ihc_problem(where, cells, labels, abl):
                    continue
                ab = abs_.pop()
                if ab >= len(abl):
                    continue
                yield abl[ab], app, [r for _k, r, _s in cells], im
            continue
        for key, l, t, r, bot in grid_cells(im.grid or {}):
            st = (im.mapping or {}).get(key)
            if not st or not st.get("assigned"):
                continue
            ab = st.get("ab")
            if ab is None or ab >= len(abl):
                continue
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

    **Which record a crop is filed on is asked of the file, not of the first
    row** — see ``_figure_record``.
    """
    match = target_svc.match_gene(session.gene)
    target = match.target
    ab_cache, items = {}, []
    for cat, app, rect, im in _planned(session):
        if cat not in ab_cache:
            ab_cache[cat] = cdb.find_antibody(target, "", cat) if target else None
        key = (_public_key(target.gene_name or session.gene, cat, app)
               if target else "")
        existing_ab, note, clash = (_figure_record(target, cat, app, key,
                                                   ab_cache[cat])
                                    if target else (None, "", ""))
        published = (PublicationImage.objects.using(DB)
                     .filter(antibody=existing_ab, application_type=app).first()
                     if existing_ab else None)
        staged_image = bool(existing_ab and PendingPublicationImage.objects.using(DB)
                            .filter(antibody=existing_ab, application_type=app).exists())
        items.append({"catalogue": cat, "app": app, "rect": rect, "image_row": im,
                      "existing_ab": existing_ab, "existing_image": published is not None,
                      "replaces_live_now": bool(
                          published is not None and published.image
                          and published.image.name == key),
                      "staged_image": staged_image,
                      "record_note": note, "key_clash": clash})
    return target, items, match


def whole_figure_plan(session, target) -> list[dict]:
    """Read-only. One entry per figure ticked for the gene's IHC page: what it
    will be stored as, which antibodies on file it names, and what it revises.

    Each antibody is resolved by ``_whole_figure_record`` — the rule the crop
    of that antibody is filed by — so a figure's table names the record that
    carries the IHC crop and its verdict, not another site's vial of the same
    product. (The IHC page reads the linked row's own figures and verdict, so
    the wrong vial would print "Not tested" beside a product the gene page
    shows as tested.)
    """
    from pipeline.services import ihc_figures
    out = []
    for im in session.images.using(DB).all():
        spec = whole_spec(im)
        if spec is None:
            continue
        spec = dict(spec, scale=str((im.grid or {}).get("ihcScale") or "").strip())
        found, missing = [], []
        for cat in spec["antibodies"]:
            ab = _whole_figure_record(target, cat) if target else None
            (found if ab is not None else missing).append((cat, ab))
        slug = ihc_figures.slug_of(spec["label"]) if spec["label"] else ""
        queued = live = False
        if target is not None and slug:
            queued = PendingIhcFigure.objects.using(DB).filter(
                target=target, slug=slug, status=PendingIhcFigure.Status.PENDING).exists()
            live = IhcFigure.objects.using(DB).filter(target=target, slug=slug).exists()
        out.append({"image_row": im, "spec": spec, "slug": slug,
                     "antibodies": [ab for _c, ab in found],
                     "missing": [c for c, _ab in missing],
                     "format": _figure_format(im),
                     "replaces_queued": queued, "replaces_live": live})
    return out


def _whole_figure_record(target, cat):
    """The antibody row a whole figure lists for this catalogue: the one its
    IHC crop is — or would be — filed on (``_figure_record`` with the IHC key),
    falling back to the product's first vial where that rule has no answer.

    A key clash is the crop door's refusal to make, not this one's; here it
    only means the file cannot settle the record, so the first vial stands."""
    picked = cdb.find_antibody(target, "", cat)
    key = _public_key(target.gene_name or "", cat, "IHC")
    owner, _note, clash = _figure_record(target, cat, "IHC", key, picked)
    return picked if clash else owner


def stale_whole_figures(session, target, whole) -> tuple[list, list]:
    """``(queued, left_live)`` — this session's whole figures that no figure in
    it declares any more: relabelled (``Fig 4`` → ``Figure 4`` is a new slug),
    unticked in box 6, set to another application, or removed.

    Upserting on ``(target, slug)`` leaves the old label's row where it was, so
    without this a relabel queued a second copy of one image, and the next
    Release put both on the IHC page. ``queued`` are still waiting and the save
    takes them out of the queue (``apply``, under the same stamp the check
    showed); ``left_live`` are on the public IHC page, which a cropper save
    never changes — the check names them and says where they are withdrawn.
    """
    if session is None or session.pk is None:
        return [], []
    declared = {(target.pk if target is not None else None, p["slug"])
                for p in whole if p["slug"]}
    queued, left_live = [], []
    for fig in (PendingIhcFigure.objects.using(DB).filter(source_session=session)
                .select_related("target").order_by("pk")):
        if (fig.target_id, fig.slug) in declared:
            continue
        if fig.status == PendingIhcFigure.Status.PENDING:
            queued.append(fig)
        if IhcFigure.objects.using(DB).filter(target_id=fig.target_id,
                                              slug=fig.slug).exists():
            left_live.append(fig)
    return queued, left_live


def _stale_name(fig, target) -> str:
    """“Fig 4”, or “Fig 4” (GENE) when it is filed under another gene."""
    name = f"“{fig.label}”"
    if target is None or fig.target_id != target.pk:
        name += f" ({fig.target.gene_name or 'another gene'})"
    return name


def _figure_format(im) -> str:
    """The image format of a staged figure, read from its bytes."""
    from pipeline.services import ihc_figures
    if not im.image:
        return ""
    try:
        with im.image.open("rb") as fh:
            head = fh.read(64 * 1024)
            fmt = ihc_figures.sniff(head)
            if not fmt:
                # A long metadata block (an ICC profile) can push the part
                # PIL needs past the first read; the whole file settles it.
                fmt = ihc_figures.sniff(head + fh.read())
            return fmt
    except Exception:
        return ""


def whole_figure_refusal(plans, target) -> str:
    """Why these whole figures cannot be saved, or ``""`` — one line per
    problem, each naming the figure. The same function feeds the check and
    the save, so the two cannot disagree."""
    from pipeline.services import ihc_figures
    lines, seen = [], {}
    gene = target.gene_name if target is not None else "this gene"
    for p in plans:
        im, spec = p["image_row"], p["spec"]
        where = f"“{im.name}” (whole figure for the IHC page)"
        if not spec["label"]:
            lines.append(f"{where} has no figure label. Type it as the report "
                         f"numbers it, e.g. Figure 4.")
        if not spec["legend"]:
            lines.append(f"{where} has no legend. Paste the legend as published "
                         f"— it is printed word for word under the figure.")
        if not spec["antibodies"]:
            lines.append(f"{where} names no antibody. Tick the antibodies the "
                         f"figure shows.")
        if not spec["samples"]["tissue"]:
            # Owner, 26 Sep 2026: the IHC page is for tissue — the most
            # selective antibodies in wild-type and knockout tissue. The HAP1
            # pellet figure is already in the report behind its DOI, and its
            # pellets are cropped for the gene page.
            lines.append(f"{where} is not ticked as showing tissue. The IHC page "
                         f"is for tissue results — the most selective antibodies "
                         f"in wild-type and knockout tissue. A HAP1 pellet figure "
                         f"is already in the report: untick “Show this whole "
                         f"figure” in box 6 and crop its pellet panels for the "
                         f"gene page. If the figure does show tissue, tick Tissue.")
        if p["missing"]:
            lines.append(f"{where} names {', '.join(p['missing'])}, which "
                         f"{'is' if len(p['missing']) == 1 else 'are'} not on file "
                         f"for {gene}. Log {'it' if len(p['missing']) == 1 else 'them'} "
                         f"on the antibodies board first, or untick "
                         f"{'it' if len(p['missing']) == 1 else 'them'}.")
        why = ihc_figures.type_refusal(im.name, p["format"])
        if why:
            lines.append(why)
        if p["slug"]:
            if p["slug"] in seen:
                lines.append(f"{where} and “{seen[p['slug']]}” are both labelled "
                             f"“{spec['label']}”. Each figure on the IHC page needs "
                             f"its own label.")
            seen.setdefault(p["slug"], im.name)
    return "\n".join(lines)


def _record_label(ab) -> str:
    site = ab.site.name if ab.site_id else "no site"
    number = f"A-{ab.ab_number}" if ab.ab_number else f"record #{ab.pk}"
    return f"{ab.catalogue_number} at {site} ({number})"


def _figure_record(target, cat, app, key, picked):
    """``(antibody, note, refusal)`` — the record this crop is filed on.

    **One product has one public figure per application, at one key**
    (``{GENE}_{catalogue}_{TYPE}.png``), while it can have a row per vial —
    23 catalogue numbers are stocked at two sites. ``find_antibody`` with no
    site picks the lowest id, and ``review.stage`` frees the key whoever owns
    it, so a re-crop filed on Leicester's row overwrote the file McGill's
    released row was serving, with no tick asked for and the panel saying
    nothing was replaced. So the file decides:

    * a row already holding the key (published or queued) is the record, when
      it is a vial of this product — the crop revises *its* figure, and the
      note names it because it is not the row the catalogue alone would pick;
    * failing that, a vial of this product that already has a published figure
      in this application (under an older key) — or release would put a second
      figure for one product on the gene page;
    * a key held by anything else — another product whose catalogue files
      under the same name, or two rows at once — is refused by name, since two
      records cannot share one public object.
    """
    holders = set(PublicationImage.objects.using(DB).filter(image=key)
                  .values_list("antibody_id", flat=True))
    holders |= set(PendingPublicationImage.objects.using(DB).filter(image=key)
                   .values_list("antibody_id", flat=True))
    product = cdb._product_qs(target, "", cat, DB)
    if not holders:
        if picked is None or PublicationImage.objects.using(DB).filter(
                antibody=picked, application_type=app).exists():
            return picked, "", ""
        live = (PublicationImage.objects.using(DB)
                .filter(antibody__in=product, application_type=app)
                .select_related("antibody", "antibody__site")
                .order_by("antibody_id").first())
        owner = live.antibody if live else None
    elif picked is not None and holders == {picked.pk}:
        return picked, "", ""
    else:
        rows = list(Antibody.objects.using(DB).filter(pk__in=holders)
                    .select_related("site").order_by("pk"))
        owner = rows[0] if (len(rows) == 1 and product.filter(
            pk=rows[0].pk).exists()) else None
        if owner is None:
            return picked, "", (
                f"{cat} {app}: the file this crop would be written to "
                f"({key.rsplit('/', 1)[-1]}) already belongs to "
                f"{', '.join(_record_label(r) for r in rows)}, not to this "
                f"antibody on {target.gene_name}. Two records cannot share one "
                f"public figure, so nothing was saved — sort the records out on "
                f"the antibodies board (filter it to {target.gene_name}) first.")
    if owner is None or (picked is not None and owner.pk == picked.pk):
        return picked, "", ""
    return owner, (
        f"{cat} {app} is filed on {_record_label(owner)}, which holds this "
        f"product's published figure — one product has one figure per "
        f"application"
        + (f", so not on {_record_label(picked)}" if picked is not None else "")
        + "."), ""


def _public_key(gene: str, catalogue: str, app: str) -> str:
    """The object key this crop will be written at — the same call
    `review.stage` makes, so "this replaces the file the live page shows" is
    asked of the key the write will actually use. A figure published under an
    older key (a previous year's folder) is replaced only at release."""
    field = PendingPublicationImage._meta.get_field("image")
    return field.generate_filename(None, filename_for(gene, catalogue, app))


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
    if match is not None and match.target is None and (match.typed or "").strip():
        # Never minted here: a gene is added on the targets doors, which check
        # the symbol against UniProt. This used to `Target.objects.create`
        # whatever was typed.
        return cdb.not_on_file_sentence(match.typed.strip())
    return ""


_LEGEND_NAMES_CELL_LINE = ("ICC-IF", "FC")


def cell_line_refusal(session, items) -> str:
    """Why these crops cannot be cut with the cell line as it stands, or "".

    An ICC-IF or FC crop burns "<cell line> wild-type cell line" into its
    legend (`engine.py`), so a blank box printed " wild-type cell line" on a
    figure that goes to a public page, where it cannot be edited. The IHC note
    leaves the clause out instead, so it is not refused here.
    """
    if (session.cell_line or "").strip():
        return ""
    n = sum(1 for i in items if i["app"] in _LEGEND_NAMES_CELL_LINE)
    if not n:
        return ""
    apps = sorted({i["app"] for i in items if i["app"] in _LEGEND_NAMES_CELL_LINE})
    return (f"The Cell line box is blank, and {n} {' and '.join(apps)} "
            f"crop{'' if n == 1 else 's'} print it in the legend (“HAP1 wild-type "
            f"cell line”). Type the cell line the figure shows, e.g. HAP1, then "
            f"save again.")


def summarize(session, target, items, match=None, whole=None, stale=None) -> dict:
    """What a save would do, for the check and the save alike. ``whole`` is
    ``whole_figure_plan``'s answer and ``stale`` ``stale_whole_figures``';
    ``None`` asks each here."""
    if whole is None:
        whole = whole_figure_plan(session, target)
    if stale is None:
        stale = stale_whole_figures(session, target, whole)
    dropped, left_live = stale
    cats = [i["catalogue"] for i in items]
    existing_cats = {i["catalogue"] for i in items if i["existing_ab"] is not None}
    new_cats = sorted({c for c in cats} - existing_cats)
    overwrite = sum(1 for i in items if i["existing_image"])
    overwrite_live = sum(1 for i in items if i.get("replaces_live_now"))
    restage = sum(1 for i in items if i["staged_image"])
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
            x for x in (gene_refusal(match), key_refusal(items),
                        figure_refusal(items), ihc_refusal(session),
                        cell_line_refusal(session, items),
                        whole_figure_refusal(whole, target))
            if x),
        # The targets board, when the gene is not on file — the way out of
        # that refusal, drawn as a link beside it.
        "add_target_url": (cdb.add_target_url(session.gene.strip())
                           if target is None and match is not None
                           and not match.ambiguous and session.gene.strip() else ""),
        # "update" or "not on file" — the cropper never creates a target.
        "target": "update" if target else "not on file",
        "crops": len(items),
        "antibodies_total": len(set(cats)),
        "antibodies_create": len(new_cats),
        "antibodies_update": len(existing_cats),
        # `images_create` / `images_overwrite` keep their names and their
        # meaning — how many of these crops are new to the *public site* and how
        # many replace a figure on it — because that is the question the
        # overwrite acknowledgement is about. `images_overwrite_live` is the
        # part of the second that lands *now*: the crop is written at the
        # public key, so where the published figure sits at that key the live
        # page changes with this press (see `_public_key`).
        "images_create": len(items) - overwrite,
        "images_overwrite": overwrite,
        "images_overwrite_live": overwrite_live,
        # New, and the one that describes *this* press: how many of these
        # revise something already in the review queue.
        "images_restage": restage,
        "new_antibodies": new_cats,
        # Said plainly, because the button used to read "Commit to live site"
        # and a person who has done this before will expect it still to.
        "destination": "review queue",
        "warnings": ([f"{d} assigned in >1 image — last crop wins" for d in dup]
                     + [i["record_note"] for i in items if i.get("record_note")]),
        # Whole figures for the gene's IHC page — private until released, so
        # saving one changes nothing anybody outside the pipeline can see.
        "whole_figures": len(whole),
        "whole_figure_labels": [p["spec"]["label"] for p in whole],
        "whole_figures_restage": sum(1 for p in whole if p["replaces_queued"]),
        "whole_figures_replacing_live": sum(1 for p in whole if p["replaces_live"]),
        # This session's whole figures no figure in it declares any more
        # (`stale_whole_figures`): the queued ones this save takes out of the
        # review queue, and the ones on the public IHC page, which it leaves.
        "whole_figures_dropped": [_stale_name(f, target) for f in dropped],
        "whole_figures_left_live": [_stale_name(f, target) for f in left_live],
        # What the consent is to, not only how many: the write compares it, so
        # a figure released (or a crop queued from another tab) between the
        # check and the press refuses rather than overwriting something the
        # panel described differently. The same shape as `bulk_targets` and
        # `renumber` stamping their plans.
        "stamp": plan_stamp(items, whole, dropped),
    }


def key_refusal(items) -> str:
    """One line per crop whose public file another record holds — see
    ``_figure_record``."""
    return "\n".join(dict.fromkeys(i["key_clash"] for i in items
                                    if i.get("key_clash")))


def plan_stamp(items, whole=(), dropped=()) -> str:
    """A short fingerprint of what each crop would do — which record, and what
    it replaces — of each whole figure's label and what it revises, and of the
    queued whole figures the save takes out of the queue. Not the pixels: a
    moved grid line is the page's own guard."""
    rows = sorted(
        (i["catalogue"], i["app"],
         i["existing_ab"].pk if i["existing_ab"] is not None else 0,
         bool(i["existing_image"]), bool(i.get("replaces_live_now")),
         bool(i["staged_image"]))
        for i in items)
    figs = sorted((p["slug"], bool(p["replaces_queued"]), bool(p["replaces_live"]))
                  for p in (whole or ()))
    gone = sorted(f.pk for f in (dropped or ()))
    if not figs and not gone:
        # Unchanged for a session with no whole figure, so a panel drawn
        # before this existed still matches.
        return hashlib.sha256(json.dumps(rows).encode()).hexdigest()[:16]
    if not gone:
        return hashlib.sha256(json.dumps([rows, figs]).encode()).hexdigest()[:16]
    return hashlib.sha256(json.dumps([rows, figs, gone]).encode()).hexdigest()[:16]


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
    # In kind or purchased. Fill-only-unknown by default, the same bargain
    # clonality strikes: a sparse paste must not reset what the Access import
    # recorded, and `""` from the parser means the sheet did not say.
    acq = (m.get("acquisition") or "").strip()
    if acq and (overwrite or not ab.acquisition_method
                or ab.acquisition_method == "unknown"):
        ab.acquisition_method = acq

    for src, field in (("clone_id", "clone_id"), ("host", "host_species"),
                       ("supplier_url", "supplier_url"), ("lot", "lot_number"),
                       # Free text with a strong convention (`IgG1`, `H, M, R`)
                       # rather than an enum, so nothing validates them here —
                       # the board offers what the column already holds and a
                       # new spelling is legitimate. See services/vocabulary.py.
                       ("isotype", "isotype"),
                       ("species_reactivity", "species_reactivity"),
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

    # When it arrived, and how much of that anybody knows. One reader for the
    # pair (`services/received.py`), so `Aug 2026` is stored as August and
    # printed as August rather than being promoted to the 1st — the day would
    # be a value nobody wrote down, on a column that ends up in a methods
    # section. Fill-only-blank like the rest; a cell this cannot read is left
    # alone here and named by the preview (`bulk_antibodies._with_received_note`),
    # which is the same arrangement concentration has above.
    when_raw = (m.get("received") or "").strip()
    if when_raw and (overwrite or ab.received_date is None):
        when, precision, _err = received_svc.parse(when_raw)
        if when is not None:
            ab.received_date = when
            ab.received_precision = precision

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
    whole = whole_figure_plan(session, target)
    stale = stale_whole_figures(session, target, whole)
    summary = summarize(session, target, items, match, whole, stale)
    refusal = summary["refusal"]
    if refusal:
        raise Refused(refusal)

    if target is None:
        # `gene_refusal` has already said this in words; a guard that lives in
        # the summary alone is one a caller that skips it does not have.
        raise Refused(cdb.not_on_file_sentence(session.gene))

    with transaction.atomic(using=DB):
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
        ab_cache, committed = {}, {}
        # One figure decoded at a time — see `_by_figure`. Holding them all is
        # what took the live container down mid-commit.
        for im_row, planned in _by_figure(items):
            with _open_figure(im_row) as src:
                for it in planned:
                    cat, app = it["catalogue"], it["app"]
                    # Keyed on the record, not the catalogue: two applications
                    # of one product can be filed on two vials (`_figure_record`).
                    rec = (it["existing_ab"].pk if it["existing_ab"] is not None
                           else ("new", cat))
                    ab = ab_cache.get(rec)
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
                        ab_cache[rec] = ab
                        committed.setdefault(ab.pk, cat)

                    rect = it["rect"]
                    panels = rect if app == "IHC" else []
                    if panels:
                        l = min(p[0] for p in panels); t = min(p[1] for p in panels)
                        r = max(p[2] for p in panels); bot = max(p[3] for p in panels)
                    else:
                        l, t, r, bot = rect
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
                                fc_secondary_only=session.fc_secondary,
                                panels=panels,
                                panel_labels=(ihc_labels(im_row, session,
                                                         filed_as)
                                              if panels else []),
                                note=(engine.ihc_note(
                                    session.cell_line,
                                    (im_row.grid or {}).get("ihcScale", ""))
                                      if panels else ""))
                    buf = BytesIO(); render_cell(src, cell).save(buf, "PNG"); buf.seek(0)
                    fn = filename_for(filed_as, cat, app)

                    # Into the review queue, never straight onto the website.
                    # The control genotype is the same value `render_cell` just burned into the legend
                    # as "knockout" or "knockdown", so the row and the pixels
                    # cannot disagree about what the figure shows.
                    # No judgement rides with it: the cropper makes none (owner,
                    # 25 Sep 2026). `recommended=None` keeps whatever the queue
                    # row already holds, or starts from the antibody's own flag
                    # — never a silent "not recommended" (`review.stage`).
                    review_svc.stage(
                        antibody=ab, application_type=app, content=buf.read(),
                        filename=fn, recommended=None,
                        staged_by=session.owner_username, session=session,
                        control_genotype=(session.genotype or "KO"))

        # persist antibody metadata set above
        for ab in ab_cache.values():
            ab.save(using=DB)

        # Whole figures for the gene's IHC page: the uploaded file itself,
        # byte for byte, onto private storage. Never pointed at the cropper's
        # own copy — that is scratch and deleted with the session. Width and
        # height are the ones recorded at upload (no decode here: see
        # `_by_figure` for why this commit does not hold figures it need not).
        from pipeline.services import ihc_figures
        saved = []
        for p in whole:
            im = p["image_row"]
            with im.image.open("rb") as fh:
                content = fh.read()
            try:
                fig, _written = ihc_figures.stage(
                    target=target, spec=p["spec"], content=content,
                    width=im.nat_w, height=im.nat_h, antibodies=p["antibodies"],
                    staged_by=session.owner_username, session=session,
                    control_genotype=(session.genotype or "KO"),
                    filename=im.name)
            except ihc_figures.Refused as exc:
                raise Refused(str(exc))
            saved.append(fig.label)
        summary["whole_figures_saved"] = saved
        # The old label's queued row, named in the check as going
        # (`stale_whole_figures`). Its private copy is freed after the commit,
        # and only if nothing else points at it.
        dropped = [f for f in stale[0]
                   if f.status == PendingIhcFigure.Status.PENDING]
        ihc_figures.discard(dropped)
        summary["whole_figures_discarded"] = [_stale_name(f, target)
                                              for f in dropped]

    # per-antibody handles so the UI can link straight to each record (e.g. to
    # complete the internal fields the cropper doesn't fill).
    summary["committed"] = [{"catalogue": cat, "id": pk}
                            for pk, cat in committed.items()]
    return target, summary
