"""The waiting room between a crop and the public website.

The figure cropper used to write ``PublicationImage`` directly, and a
``PublicationImage`` **is** publication: ``pipeline/public.py`` derives the
public site from "a named gene with at least one antibody carrying a published
figure", so the press labelled *Commit to live site* put a figure in front of
the world with no review meeting in between and no way to take a considered
second look. This module is the other half of that press. The cropper stages
into ``PendingPublicationImage``; **release** is a separate, later, deliberate
act that writes the public row.

Four rules it holds, each of which is a thing that would otherwise be wrong.

**Staging changes nothing a reader can see.** No public surface reads this
table, and the recommendation the human set in the cropper is held *on the
pending row* rather than on the antibody — ``core/recommendations.py::
curated_gene_ids`` asks whether any antibody on a gene carries a recommendation
at all, and that is what separates *tested and not recommended* from *never
assessed* on the public gene page. Writing the flag while withholding the
figure would have moved that verdict for every other antibody on the gene.

**Release is idempotent and re-runnable.** It reads the staged bytes, frees the
canonical object key (settings has ``FILE_OVERWRITE=False``, so a re-release
would otherwise suffix the filename), and ``update_or_create``s — the rule
``PublicationImage``'s unique constraint has always demanded.

**The number on the button is what was consented to.** ``manifest()`` is what
the panel prints, and ``release()`` takes the count that was shown and refuses a
set that has grown since. Re-asking "may you?" at the commit answers yes while
the manifest goes from three figures to thirty — the rule
``services/deletion.py`` learned first.

**A released row is kept.** It is the record of what went public, when, and by
whom; the gene page's history and the portal's released list are drawn from it.
Re-cropping a released figure puts the row back to ``pending`` with the new
bytes and leaves the public figure standing until somebody releases the
revision — which is the behaviour you want the day a crop turns out to be wrong.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from pipeline.models import (PendingPublicationImage, PublicationImage,
                             Target)

DB = "pipeline_db"

PENDING = PendingPublicationImage.Status.PENDING
RELEASED = PendingPublicationImage.Status.RELEASED

#: image application code → the ``Antibody`` recommendation boolean it sets on
#: release. Five, not six: this is OGA's own verdict about what OGA tested
#: (IHC since 26 Sep 2026, on HAP1 cell pellets), and is a different question
#: from ``commit._SUP_APP_FIELD``'s six, which is what the *supplier* claims.
#: The two are drawn in adjacent columns and must not share a list.
RECOMMENDATION_FIELD = {"WB": "wb_recommended", "IP": "ip_recommended",
                        "ICC-IF": "if_recommended", "FC": "fc_recommended",
                        "IHC": "ihc_recommended"}

#: The applications a figure can be filed under, in the order every OGA surface
#: prints them.
APPLICATIONS = ["WB", "IP", "ICC-IF", "FC", "IHC"]


class Refused(Exception):
    """A release or a discard the caller may not have, with the reason."""


# ── Reading ──────────────────────────────────────────────────────────────────

def _base():
    return (PendingPublicationImage.objects.using(DB)
            .select_related("antibody", "antibody__target", "antibody__company",
                            "antibody__site"))


def pending_qs():
    """Everything staged and not yet released."""
    return _base().filter(status=PENDING)


def released_qs():
    """Everything this route has put on the public site."""
    return _base().filter(status=RELEASED)


def for_target(target_id, *, status=PENDING):
    return _base().filter(antibody__target_id=target_id, status=status)


def counts_for(target_ids) -> dict:
    """``{target_id: n}`` — how many figures each gene has waiting, crops and
    whole IHC figures together.

    Its own query rather than a ``Count`` annotation on a queryset that already
    counts something else: joining a second multi-valued relation multiplies
    both, which is how a session with three results and two files came to read
    as six of each on the gene page.
    """
    from django.db.models import Count

    from pipeline.models import PendingIhcFigure
    ids = list(target_ids)
    out = {}
    rows = (PendingPublicationImage.objects.using(DB)
            .filter(status=PENDING, antibody__target_id__in=ids)
            .values("antibody__target_id")
            .annotate(n=Count("pk")))
    for r in rows:
        out[r["antibody__target_id"]] = r["n"]
    for r in (PendingIhcFigure.objects.using(DB)
              .filter(status=PENDING, target_id__in=ids)
              .values("target_id").annotate(n=Count("pk"))):
        out[r["target_id"]] = out.get(r["target_id"], 0) + r["n"]
    return out


def genes_waiting() -> list[dict]:
    """One row per gene with figures waiting, newest staging first.

    The review queue's index. Counts and the most recent stager come from one
    grouped query per table, so the page does not grow a query per gene. **Both
    tables**: a gene whose only waiting item is a whole IHC figure would
    otherwise never appear here, and its figure could never be reached.
    """
    from django.db.models import Count, Max

    from pipeline.models import PendingIhcFigure
    genes = {}
    rows = (PendingPublicationImage.objects.using(DB)
            .filter(status=PENDING)
            .values("antibody__target_id", "antibody__target__gene_name")
            .annotate(n=Count("pk"), last=Max("updated_at")))
    for r in rows:
        genes[r["antibody__target_id"]] = {
            "target_id": r["antibody__target_id"],
            "gene": r["antibody__target__gene_name"] or "",
            "count": r["n"], "crops": r["n"], "whole_figures": 0,
            "staged_at": r["last"]}
    for r in (PendingIhcFigure.objects.using(DB).filter(status=PENDING)
              .values("target_id", "target__gene_name")
              .annotate(n=Count("pk"), last=Max("updated_at"))):
        g = genes.setdefault(r["target_id"], {
            "target_id": r["target_id"], "gene": r["target__gene_name"] or "",
            "count": 0, "crops": 0, "whole_figures": 0, "staged_at": r["last"]})
        g["count"] += r["n"]
        g["whole_figures"] = r["n"]
        if r["last"] and (g["staged_at"] is None or r["last"] > g["staged_at"]):
            g["staged_at"] = r["last"]
    return sorted(genes.values(),
                  key=lambda g: g["staged_at"] or timezone.now(), reverse=True)


def published_pairs(rows) -> set:
    """``{(antibody_id, application)}`` for the rows that already have a live
    public figure — i.e. releasing these *replaces* something the world can see.

    One query for the whole set. A replacement is not a refusal; it is the
    sentence the manifest has to say out loud.
    """
    wanted = {(r.antibody_id, r.application_type) for r in rows}
    if not wanted:
        return set()
    live = (PublicationImage.objects.using(DB)
            .filter(antibody_id__in={a for a, _ in wanted})
            .values_list("antibody_id", "application_type"))
    return {pair for pair in live if pair in wanted}


def genes_becoming_public(rows) -> list[str]:
    """Gene symbols that have **no public page today** and will get one.

    The consequential half of a release and the half a count cannot show: three
    figures on a gene already published is a revision, three figures on a gene
    nobody outside has heard of is a new page on the website.
    """
    from pipeline.public import public_targets
    target_ids = {r.antibody.target_id for r in rows if r.antibody.target_id}
    if not target_ids:
        return []
    already = set(public_targets().using(DB)
                  .filter(pk__in=target_ids).values_list("pk", flat=True))
    return sorted({(r.antibody.target.gene_name or "")
                   for r in rows
                   if r.antibody.target_id and r.antibody.target_id not in already
                   and r.antibody.target and r.antibody.target.gene_name})


# ── The row a screen draws ───────────────────────────────────────────────────

def rows_for(items, *, url_of=None) -> list[dict]:
    """Serialise a set of staged figures, with **one** query for the question
    each row cannot answer about itself.

    ``replaces_published`` — does this figure already have a public counterpart
    — is per row and is asked of the whole set at once, because asking it row by
    row is how a page's cost comes to grow with the number of rows on it.
    """
    items = list(items)
    replaces = published_pairs(items)
    from pipeline.services import methods_text
    methods = methods_text.for_staged(items)
    return [{**row(i,
                   image_url=(url_of(i) if url_of else ""),
                   replaces_published=(i.antibody_id, i.application_type) in replaces),
             # The paragraph the Copy methods button will copy once released
             # — read here with the blot, so a wrong value is caught before it
             # reaches a page. "" where no methods are entered yet.
             "methods": methods.get(i.pk, "")}
            for i in items]


def row(item, *, image_url="", replaces_published=False) -> dict:
    """One pending figure as JSON. Every value a string, number or bool — a
    model instance here 500s the whole response and the page blames the filters
    (``tests_board_fieldtest.py::BoardRowsAreJsonTests``, one board over).
    """
    ab = item.antibody
    target = ab.target if ab.target_id else None
    return {
        "id": item.pk,
        "catalogue": ab.catalogue_number or "",
        "gene": (target.gene_name if target else "") or "",
        "target_id": ab.target_id,
        "supplier": _supplier_of(ab),
        "application": item.application_type,
        "recommended": bool(item.recommended),
        "status": item.status,
        "staged_by": item.staged_by,
        "staged_at": item.updated_at.isoformat() if item.updated_at else "",
        "released_by": item.released_by,
        "released_at": item.released_at.isoformat() if item.released_at else "",
        "notes": item.notes or "",
        "site": ab.site.name if ab.site_id else "",
        "image_url": image_url,
        # Releasing this replaces something the public can see today. Not a
        # refusal — a sentence the manifest has to say out loud.
        "replaces_published": bool(replaces_published),
    }


def _supplier_of(ab) -> str:
    """The name the record is stored under, not the public spelling.

    ``cropper/db.py::company_label``'s rule — ``display_name`` is what the
    public website prints and ``.name`` is what every screen showing this
    record shows, and announcing one while the record says the other is the
    substitution the paste previews had to be corrected away from.
    """
    if not ab.company_id:
        return ""
    from pipeline.services.cropper.db import company_label
    return company_label(ab.company)


def manifest(rows, figures=()) -> dict:
    """What a release would do, in the words the confirm panel prints.

    Counted rather than described, because the count is what comes back with
    the press and is checked against what was shown. ``figures`` are whole IHC
    figures (``services/ihc_figures.py``) released by the same press; ``count``
    is the **sum**, because one press is one consent.
    """
    from pipeline.services import ihc_figures
    rows = list(rows)
    figures = list(figures or ())
    replaces = published_pairs(rows)
    return {
        "count": len(rows) + len(figures),
        "crops": len(rows),
        "genes": sorted({(r.antibody.target.gene_name or "")
                         for r in rows if r.antibody.target_id and r.antibody.target}
                        | {(f.target.gene_name or "") for f in figures}),
        "antibodies": len({r.antibody_id for r in rows}),
        "applications": sorted({r.application_type for r in rows},
                               key=lambda a: APPLICATIONS.index(a)
                               if a in APPLICATIONS else 9),
        "replaces_published": len(replaces),
        "recommended": sum(1 for r in rows if r.recommended),
        "new_public_genes": genes_becoming_public(rows),
        "whole_figures": len(figures),
        "whole_figures_replacing": len(ihc_figures.live_slugs(figures)),
        # Whole figures on a gene that will have no public page after this
        # press — refused by `release_all`, named here so the panel says so
        # before anybody presses.
        "ihc_page_waits_for_gene": whole_figure_genes_not_public(rows, figures),
        # Whole figures already released on a gene that has no public page now
        # and gets one with this press — left behind when the gene went off the
        # site some other way than `withdraw` (its antibodies deleted, or moved
        # by a merge). They reappear on its IHC page with this press, so the
        # panel says so; nothing about them is written.
        "whole_figures_returning": whole_figures_returning(rows, figures),
    }


def whole_figures_returning(rows, figures=()) -> list[dict]:
    """``[{"gene", "count"}]`` — released whole figures that come back into
    view because these crops give their gene its first public page now.

    ``review.withdraw`` takes a gene's whole figures with it, but a gene can
    leave the site without it — a superuser deletes the antibodies carrying its
    crops, or ``merge_targets`` moves them — and its ``IhcFigure`` rows stay,
    hidden only because ``ihc_figures.page_figures`` asks whether the gene is
    public. A figure released by this same press is counted there, not here.
    """
    from pipeline.models import IhcFigure
    from pipeline.public import public_targets
    targets = {r.antibody.target_id: r.antibody.target for r in rows
               if r.antibody.target_id and r.antibody.target
               and r.antibody.target.gene_name}
    if not targets:
        return []
    already = set(public_targets().using(DB).filter(pk__in=targets)
                  .values_list("pk", flat=True))
    new = set(targets) - already
    if not new:
        return []
    this_press = {(f.target_id, f.slug) for f in (figures or ())}
    counts = {}
    for target_id, slug in (IhcFigure.objects.using(DB).filter(target_id__in=new)
                            .values_list("target_id", "slug")):
        if (target_id, slug) not in this_press:
            counts[target_id] = counts.get(target_id, 0) + 1
    return sorted(({"gene": targets[t].gene_name, "count": n}
                   for t, n in counts.items()), key=lambda d: d["gene"])


def _public_after(rows) -> set:
    """Target ids that will have a public gene page once these crops are
    released: public now, or given a figure by this press."""
    from pipeline.public import public_targets
    now = set(public_targets().using(DB).values_list("pk", flat=True))
    return now | {r.antibody.target_id for r in rows
                  if r.antibody.target_id and r.antibody.target
                  and r.antibody.target.gene_name}


def whole_figure_genes_not_public(rows, figures) -> list[str]:
    """Genes whose whole figures would go out with no gene page to hang from.

    The IHC page exists only while the gene page does
    (``core/views.py::antibody_ihc``), so a whole figure released on a gene
    that is not public would sit unseen and then appear, unreviewed by that
    later press, the day a crop made the gene public. Refused instead.
    """
    figures = list(figures or ())
    if not figures:
        return []
    public = _public_after(rows)
    return sorted({f.target.gene_name or f"target {f.target_id}"
                   for f in figures if f.target_id not in public})


def judgements(rows) -> dict:
    """``{pending pk: judgement}`` — the whole call for each waiting figure.

    The queue could record one bit (recommended or not) while the public site
    prints four rungs built from that bit *and* the outcome axes — detects and
    selective for a western blot, the band for IF, enrichment for IP. So the
    meeting could set the recommendation here and then had to open Judge
    outcomes for the other half, which refused the figure anyway because it was
    not published yet. Both halves are on the card now, and this says what the
    public page will print once the figure is released.

    The wording comes from ``core/recommendations.py::describe`` — the reader
    every public surface asks — handed the antibody **as release will leave
    it**: this figure's staged recommendation in place of the stored flag, and
    the gene counted as curated if anything on it will be recommended once
    everything waiting on it is released. That second half is the gene gate:
    on a gene where nothing is recommended every negative reads *Not tested*,
    and a card promising *Not supportive* there would be a preview that is not
    compared against the write.

    Batched: two queries per application for the axes, one for the gene gate.
    """
    import copy

    from core import recommendations as recs
    from pipeline.models import Antibody
    from pipeline.services import outcomes as outcome_svc

    rows = list(rows)
    if not rows:
        return {}

    by_app = {}
    for r in rows:
        by_app.setdefault(r.application_type, set()).add(r.antibody_id)
    axes_of = {}
    for app, ids in by_app.items():
        if outcome_svc.axes_for(app):
            for ab_id, axes in outcome_svc.for_antibodies(ids, app).items():
                axes_of[(ab_id, app)] = axes

    # The gene gate, as it will stand once every waiting figure on these genes
    # is released: the stored flags, overlaid with what the queue will write.
    target_ids = {r.antibody.target_id for r in rows if r.antibody.target_id}
    flags = {
        a["pk"]: a for a in Antibody.objects.using(DB)
        .filter(target_id__in=target_ids)
        .values("pk", "target_id", *recs.RECOMMENDATION_FIELDS)
    }
    for q in for_target_ids(target_ids):
        field_name = RECOMMENDATION_FIELD.get(q.application_type)
        if field_name and q.antibody_id in flags:
            flags[q.antibody_id][field_name] = bool(q.recommended)
    # Every flag, from the one reader of what "curated" asks of a gene.
    curated = {a["target_id"] for a in flags.values()
               if any(a[f] for f in recs.RECOMMENDATION_FIELDS)}

    out = {}
    for r in rows:
        app = r.application_type
        axes = axes_of.get((r.antibody_id, app))
        as_released = copy.copy(r.antibody)
        field_name = RECOMMENDATION_FIELD.get(app)
        if field_name:
            setattr(as_released, field_name, bool(r.recommended))
        public = recs.describe(as_released, app, {app},
                               r.antibody.target_id in curated, axes)
        out[r.pk] = {
            "application": app,
            "axes": axes or {},
            # What the figure shows, in the outcome page's own words.
            "outcome": outcome_svc.label(axes, app) if axes else None,
            "is_gap": outcome_svc.is_gap(axes, app) if axes else False,
            "conflict_direction": (outcome_svc.conflict_direction(
                axes, bool(r.recommended), app) if axes else None),
            "public_support": public["support"],
            "public_sentence": public["sentence"],
            "public_tone": public["tone"],
            "gene_curated": r.antibody.target_id in curated,
        }
    return out


def for_target_ids(target_ids):
    """Everything waiting on a set of genes — ``for_target`` for several."""
    return _base().filter(antibody__target_id__in=list(target_ids),
                          status=PENDING)


# ── Writing ──────────────────────────────────────────────────────────────────

def _still_referenced(name: str, *, pending_pk=None, public_pk=None,
                      pending_ihc_pk=None, public_ihc_pk=None) -> bool:
    """Does any row other than the one named still point at this object?

    Staged and published figures share one key now, so "delete my file" and
    "delete somebody else's figure" became the same call. Every delete in this
    module asks this first. The excluded pk is the row doing the deleting: it is
    about to stop pointing at the object, so it does not count as a reference.

    **The one reader across all four figure tables** — the two crop tables and
    the two whole-IHC-figure tables (``services/ihc_figures.py``). Nothing is
    expected to share a key across the pairs (they have separate prefixes), but
    "delete my file" is exactly where a second function would drift.
    """
    from pipeline.models import IhcFigure, PendingIhcFigure
    if not name:
        return False
    for model, excluded in ((PendingPublicationImage, pending_pk),
                            (PublicationImage, public_pk),
                            (PendingIhcFigure, pending_ihc_pk),
                            (IhcFigure, public_ihc_pk)):
        qs = model.objects.using(DB).filter(image=name)
        if excluded is not None:
            qs = qs.exclude(pk=excluded)
        if qs.exists():
            return True
    return False


def _free(storage, name: str) -> None:
    """Delete an object, never letting a storage error break the transaction."""
    if not name:
        return
    try:
        storage.delete(name)
    except Exception:
        pass


def stage(*, antibody, application_type, content: bytes | None, filename: str,
          recommended: bool | None = None, staged_by: str = "", session=None,
          notes: str = "", control_genotype: str = "") -> PendingPublicationImage:
    """Put one crop in the waiting room, replacing whatever was staged before.

    Upserts on ``(antibody, application)`` — the same key the public table
    uses — so re-cropping a figure revises the pending row rather than queueing
    a second copy of it, and re-cropping something already released puts the row
    back to ``pending`` with the public figure left standing.

    ``recommended=None`` — what the cropper passes, since it makes no judgement
    (owner, 25 Sep 2026) — **keeps** the verdict a row waiting in the queue
    already carries, and otherwise starts from the antibody's own flag. Never
    ``False`` by default: ``release`` writes the row's value onto the antibody
    in both directions, so re-cropping a recommended antibody would otherwise
    un-recommend it at release with nobody having decided that.
    """
    item = (PendingPublicationImage.objects.using(DB)
            .filter(antibody=antibody, application_type=application_type).first())
    if item is None:
        item = PendingPublicationImage(antibody=antibody,
                                       application_type=application_type)
    if content is not None:
        # `file_overwrite=False` (settings: AWS_S3_FILE_OVERWRITE) suffixes a
        # name that is already taken, which would make the "final public URL"
        # not final — a partner handed one URL would keep reading the old crop.
        # So free the key we are about to write. That *is* the replacement: a
        # published row pointing at it is a re-crop of a released figure, and
        # overwriting is what the owner asked for. What must not be freed is the
        # row's previous object at some *other* key that a live page still
        # serves.
        intended = item.image.field.generate_filename(item, filename)
        previous = item.image.name if item.image else ""
        _free(item.image.storage, intended)
        if previous and previous != intended and not _still_referenced(
                previous, pending_pk=item.pk):
            _free(item.image.storage, previous)

    if recommended is None:
        if item.pk is None or item.status == RELEASED:
            field_name = RECOMMENDATION_FIELD.get(application_type)
            recommended = bool(field_name and getattr(antibody, field_name, False))
        else:
            recommended = item.recommended
    item.recommended = bool(recommended)
    # Which kind of control the legend names — `KO` or `KD` from the cropper
    # session, or the public row's own value when `withdraw` re-stages one.
    # Blank leaves whatever the row had: a caller that does not know must not
    # erase a value one that did know wrote.
    if control_genotype:
        item.control_genotype = control_genotype.strip().upper()[:4]
    item.status = PENDING
    item.staged_by = (staged_by or "")[:150]
    item.released_by = ""
    item.released_at = None
    if notes:
        item.notes = notes
    if session is not None:
        item.source_session = session
    if content is None:
        # Adopt an object that is already at the right key — `withdraw` putting a
        # released figure back in the queue. Reading and rewriting identical
        # bytes would delete and recreate the file the gene page is serving and
        # leave a window where it 404s, which is a worse withdrawal than none.
        item.image.name = filename
    else:
        item.image.save(filename, ContentFile(content), save=False)
    item.save(using=DB)
    return item


@dataclass
class ReleaseResult:
    released: list = field(default_factory=list)   # row dicts, for the receipt
    genes: list = field(default_factory=list)
    replaced: int = 0
    recommended: int = 0
    new_public_genes: list = field(default_factory=list)


def release(rows, *, actor: str, consented_count: int | None = None) -> ReleaseResult:
    """Publish these staged figures. One transaction, and it is the only way a
    figure reaches the public site.

    ``consented_count`` is the number the panel printed. A set that has grown
    since is refused rather than published — the manifest is the consent, and a
    manifest that describes three figures does not authorise thirty.
    """
    rows = list(rows)
    if not rows:
        raise Refused("Nothing selected — there is nothing to release.")
    if consented_count is not None and consented_count != len(rows):
        raise Refused(
            f"This would release {len(rows)} figure(s); the list you were shown "
            f"had {consented_count}. Somebody has staged or released something "
            f"since. Open the list again and check it before releasing.")

    manifest_before = manifest(rows)
    now = timezone.now()
    result = ReleaseResult(genes=manifest_before["genes"],
                           replaced=manifest_before["replaces_published"],
                           recommended=manifest_before["recommended"],
                           new_public_genes=manifest_before["new_public_genes"])

    with transaction.atomic(using=DB):
        touched = {}
        for item in rows:
            if not item.image:
                raise Refused(
                    f"{item.antibody.catalogue_number} {item.application_type} has "
                    f"no staged image — crop it again before releasing.")
            # No bytes move. The crop was written at its final public key when
            # it was staged, so releasing is a row pointing at an object that is
            # already there — which is also why this can no longer half-succeed
            # with a file written and a row missing.
            live = (PublicationImage.objects.using(DB)
                    .filter(antibody_id=item.antibody_id,
                            application_type=item.application_type).first())
            superseded = ""
            if live and live.image and live.image.name != item.image.name:
                # An older figure at a different key: nothing will point at it
                # after this, so free it once the row has moved.
                superseded = live.image.name
            # The control kind rides across only when the pending row knows
            # it: a row staged before the column existed must not blank a
            # value `backfill_control_genotype` has since put on the live row.
            defaults = {"image": item.image.name}
            if item.control_genotype:
                defaults["control_genotype"] = item.control_genotype
            PublicationImage.objects.using(DB).update_or_create(
                antibody_id=item.antibody_id,
                application_type=item.application_type,
                defaults=defaults)
            if superseded and not _still_referenced(superseded):
                _free(item.image.storage, superseded)

            # The recommendation, held on the row since the cropper, applied to
            # the antibody now that the figure behind it is public.
            ab = touched.get(item.antibody_id) or item.antibody
            rec_field = RECOMMENDATION_FIELD.get(item.application_type)
            if rec_field:
                setattr(ab, rec_field, bool(item.recommended))
            touched[item.antibody_id] = ab

            item.status = RELEASED
            item.released_by = (actor or "")[:150]
            item.released_at = now
            item.save(using=DB, update_fields=["status", "released_by",
                                               "released_at", "updated_at"])
            result.released.append(row(item))

        for ab in touched.values():
            ab.save(using=DB, update_fields=list(RECOMMENDATION_FIELD.values()))

    return result


@dataclass
class ReleaseAllResult:
    crops: ReleaseResult = None
    whole_figures: list = field(default_factory=list)   # {"gene", "label"}

    @property
    def genes(self) -> list:
        return sorted(set(self.crops.genes if self.crops else [])
                      | {f["gene"] for f in self.whole_figures if f["gene"]})


def release_all(rows, figures=(), *, actor: str,
                consented_count: int | None = None) -> ReleaseAllResult:
    """The one Release press: crops and whole IHC figures, one consent.

    ``consented_count`` is the **sum** the panel printed, checked here against
    the sum of what is being released — one press, one number, never two
    counts each checked against half of it. Whole figures on a gene that will
    have no public page after this press are refused by name
    (``whole_figure_genes_not_public``), checked again here rather than trusted
    from the panel, since a preview is not a permission slip.

    One transaction. The whole figures' bytes are copied inside it (they are
    private until now) and, if the transaction fails, the copies it wrote are
    freed — storage does not roll back with the database.
    """
    from pipeline.services import ihc_figures
    rows, figures = list(rows), list(figures or ())
    total = len(rows) + len(figures)
    if not total:
        raise Refused("Nothing selected — there is nothing to release.")
    if consented_count is not None and consented_count != total:
        raise Refused(
            f"This would release {total} figure(s); the list you were shown "
            f"had {consented_count}. Somebody has staged or released something "
            f"since. Open the list again and check it before releasing.")
    waits = whole_figure_genes_not_public(rows, figures)
    if waits:
        raise Refused(
            f"The whole IHC figures for {', '.join(waits)} cannot be released on "
            f"their own: {', '.join(waits)} has no public gene page, and the IHC "
            f"page exists only beside it. Release a crop for "
            f"{', '.join(waits)} with them, or leave them waiting.")

    result = ReleaseAllResult()
    written = []
    try:
        with transaction.atomic(using=DB):
            if rows:
                result.crops = release(rows, actor=actor)
            if figures:
                result.whole_figures = ihc_figures.release(
                    figures, actor=actor, written=written)
    except Exception:
        storage = ihc_figures.IhcFigure._meta.get_field("image").storage
        for name in written:
            _free(storage, name)
        raise
    return result


def discard(rows, *, actor: str = "") -> int:
    """Take staged figures out of the queue without publishing them.

    The staged object goes with the row — nothing else points at it, and a file
    the app has forgotten is worse than no file. Nothing public is touched: a
    figure already released stays on the site, which is why discarding a
    released row is refused rather than quietly doing half of what it sounds
    like.
    """
    rows = list(rows)
    for item in rows:
        if item.status == RELEASED:
            raise Refused(
                f"{item.antibody.catalogue_number} {item.application_type} is "
                f"already published, so discarding it here would change nothing "
                f"on the website. Withdrawing a published figure is "
                f"`manage.py withdraw_figures`, which puts it back in this "
                f"queue and takes it off the site.")
    n = 0
    for item in rows:
        # A pending row can now share its object with a published one — that is
        # what re-cropping a released figure produces. Discarding the queue entry
        # must not blank the gene page still serving those bytes.
        if item.image and not _still_referenced(item.image.name,
                                                pending_pk=item.pk):
            _free(item.image.storage, item.image.name)
        item.delete(using=DB)
        n += 1
    return n


@dataclass
class WithdrawResult:
    withdrawn: list = field(default_factory=list)   # (catalogue, application)
    genes_leaving_public: list = field(default_factory=list)
    restaged: int = 0
    already_queued: int = 0
    recommendations_cleared: int = 0
    # (gene, label) of the whole IHC figures that went with the gene.
    whole_figures_withdrawn: list = field(default_factory=list)


def _genes_left_with_nothing(images, target_ids) -> set:
    """Of these genes, the ones every published crop of which is in
    ``images`` — the genes this withdrawal takes off the public site. Asked
    before anything moves, so the consent can count what goes with them."""
    chosen = {img.pk for img in images}
    live = {}
    for pk, tid in (PublicationImage.objects.using(DB)
                    .filter(antibody__target_id__in=target_ids)
                    .values_list("pk", "antibody__target_id")):
        live.setdefault(tid, set()).add(pk)
    return {tid for tid in target_ids if live.get(tid, set()) <= chosen}


def withdraw(images, *, actor: str = "", consented_count: int | None = None) -> WithdrawResult:
    """Take published figures off the public site and back into this queue.

    The mirror of :func:`release`, and the half that did not exist. ``release``
    could put a figure in front of the world and nothing could take it back:
    ``discard`` refuses a released row precisely *because* the public figure
    would stay up, and its refusal pointed at a control on the antibodies board
    that has never existed. The only thing that removed a ``PublicationImage``
    was deleting the whole antibody, which takes every reading with it.

    So a withdrawal is not a delete — it is a release run backwards, and the
    figure lands where it would have been before somebody released it:

    **The bytes are re-staged before the public row goes.** These figures are
    the only copy. The cropper wrote ``PublicationImage`` directly until the
    review queue existed, so the oldest ones have no pending row behind them at
    all, and deleting one would destroy the crop. ``stage`` is the one writer
    for the waiting room, so this calls it rather than building the row here.

    **Re-staging no longer moves the bytes, and withdrawal is weaker for it.**
    Staged and published crops share one object at one public key (owner's
    decision, 23 Aug 2026), so this hands the pending row the object that is
    already there rather than copying it. The honest consequence: a withdrawn
    figure leaves the gene page and **stays reachable at its URL**. This used to
    be the difference between *unpublished* and merely *unlinked*, and that
    difference is now gone by choice — say so to anyone who asks for a figure to
    be taken down, because "withdrawn" no longer means "unreachable".

    **The verdict rides back with it.** ``release`` applies the pending row's
    ``recommended`` to the antibody; this carries the antibody's flag back onto
    the pending row and clears it. Leaving the flag set would leave the antibody
    reading *recommended* with no figure behind it, and
    ``core/recommendations.py`` reads the flag first — so the public API would
    have gone on recommending a product whose evidence had just been withdrawn.
    Clearing it is also what makes the gene read *never assessed* rather than
    *tested and not recommended*, since ``curated_gene_ids`` asks whether any
    antibody on the gene carries a flag.

    **A newer crop is never clobbered.** If somebody has already re-cropped this
    figure, a ``pending`` row is sitting there with bytes the public row does not
    have. That row is the newer record and is left exactly as it is; the public
    row still goes.

    Returns what happened, including which genes leave the public site
    altogether — a gene is public only while something on it carries a figure
    (``pipeline/public.py::public_targets``), so withdrawing the last one takes
    the page down, and that is the part a count does not show.
    """
    from pipeline.models import IhcFigure
    from pipeline.services import ihc_figures
    images = list(images)
    if not images:
        raise Refused("Nothing selected — there is nothing to withdraw.")
    target_ids = {img.antibody.target_id for img in images
                  if img.antibody.target_id is not None}
    # A gene leaving the public site takes its whole IHC figures with it, in
    # this transaction and under this consent — or a later crop release would
    # bring the old whole figures back with nobody having looked at them.
    leaving = _genes_left_with_nothing(images, target_ids)
    whole = list(IhcFigure.objects.using(DB).filter(target_id__in=leaving)
                 .select_related("target")
                 .prefetch_related("antibody_links__antibody"))
    total = len(images) + len(whole)
    if consented_count is not None and consented_count != total:
        raise Refused(
            f"This would withdraw {total} figure(s); the list you were "
            f"shown had {consented_count}. Somebody has released something "
            f"since. Open the gene again and check it before withdrawing.")

    result = WithdrawResult()

    with transaction.atomic(using=DB):
        touched = {}
        for img in images:
            antibody = img.antibody
            rec_field = RECOMMENDATION_FIELD.get(img.application_type)
            was_recommended = bool(getattr(antibody, rec_field, False)) if rec_field else False

            queued = (PendingPublicationImage.objects.using(DB)
                      .filter(antibody_id=img.antibody_id,
                              application_type=img.application_type,
                              status=PENDING).first())
            if queued is not None:
                # Somebody has re-cropped this since it was released. Their crop
                # is newer than the public one; leave it alone.
                result.already_queued += 1
            elif img.image:
                stage(antibody=antibody,
                      application_type=img.application_type,
                      content=None,          # already at the public key
                      filename=img.image.name,
                      recommended=was_recommended,
                      staged_by=actor,
                      notes="Withdrawn from the public site.",
                      control_genotype=img.control_genotype or "")
                result.restaged += 1
            else:
                # A row with no file cannot be re-staged and has nothing to lose.
                result.restaged += 0

            if rec_field and was_recommended:
                ab = touched.get(img.antibody_id) or antibody
                setattr(ab, rec_field, False)
                touched[img.antibody_id] = ab
                result.recommendations_cleared += 1

            public_name = img.image.name if img.image else ""
            storage = img.image.storage if img.image else None
            result.withdrawn.append(
                (antibody.catalogue_number, img.application_type))
            img.delete(using=DB)
            if (public_name and storage is not None
                    and not _still_referenced(public_name)):
                # Only if nothing points at it any more. A withdrawal that
                # re-queued this figure left a pending row on the *same* object,
                # and freeing it there would destroy the only copy — the thing
                # this whole function exists to avoid.
                _free(storage, public_name)

        for ab in touched.values():
            ab.save(using=DB, update_fields=list(RECOMMENDATION_FIELD.values()))

        if whole:
            done = ihc_figures.withdraw(whole, actor=actor)
            result.whole_figures_withdrawn = done.withdrawn

        still_public = set(
            PublicationImage.objects.using(DB)
            .filter(antibody__target_id__in=target_ids)
            .values_list("antibody__target_id", flat=True))
        gone = target_ids - still_public
        if gone:
            result.genes_leaving_public = sorted(
                Target.objects.using(DB).filter(pk__in=gone)
                .exclude(gene_name__isnull=True).exclude(gene_name="")
                .values_list("gene_name", flat=True))

    return result


def withdraw_manifest(target_id) -> dict:
    """What withdrawing this gene would do, before anybody presses it.

    ``deletion.py``'s rule, applied to the other destructive press in this app:
    **a manifest, not a wall.** The counts are what the button prints, and
    ``withdraw`` takes the figure count back as ``consented_count`` — re-asking
    "may you?" at the press answers yes while the set grows underneath it.

    Two things a count on its own does not say, so both are named:
    ``leaves_public_site`` (a gene is public only while something on it carries
    a figure, so withdrawing the last one takes the page down), and
    ``already_queued`` (figures somebody has re-cropped since, whose queued crop
    is left exactly as it is).
    """
    images = list(PublicationImage.objects.using(DB)
                  .filter(antibody__target_id=target_id)
                  .select_related("antibody"))
    queued = set(
        PendingPublicationImage.objects.using(DB)
        .filter(antibody__target_id=target_id, status=PENDING)
        .values_list("antibody_id", "application_type"))

    recommendations = 0
    for img in images:
        field_name = RECOMMENDATION_FIELD.get(img.application_type)
        if field_name and getattr(img.antibody, field_name, False):
            recommendations += 1

    from pipeline.models import IhcFigure
    whole = IhcFigure.objects.using(DB).filter(target_id=target_id).count() \
        if images else 0
    return {
        "figures": len(images),
        # The gene's whole IHC figures leave with it (`withdraw`), so they are
        # counted, and the number handed back is the sum.
        "whole_figures": whole,
        "consent_count": len(images) + whole,
        "antibodies": len({img.antibody_id for img in images}),
        "applications": sorted({img.application_type for img in images}),
        "recommendations": recommendations,
        "already_queued": sum(
            1 for img in images
            if (img.antibody_id, img.application_type) in queued),
        # Every figure on the gene is in `images`, so withdrawing them all is
        # always the last one. Stated rather than assumed: a partial withdrawal
        # would make this false, and this page does a whole gene at a time.
        "leaves_public_site": bool(images),
    }


def withdraw_whole_figures(figs, *, actor: str = "",
                           consented_count: int | None = None):
    """Take chosen whole IHC figures off a gene's IHC page. The gene page is
    untouched. Superusers only — ``withdraw_refusal`` is asked by the caller."""
    from pipeline.services import ihc_figures
    figs = list(figs)
    if not figs:
        raise Refused("Nothing selected — there is nothing to withdraw.")
    if consented_count is not None and consented_count != len(figs):
        raise Refused(
            f"This would withdraw {len(figs)} figure(s); the list you were "
            f"shown had {consented_count}. Open the gene again and check it "
            f"before withdrawing.")
    with transaction.atomic(using=DB):
        return ihc_figures.withdraw(figs, actor=actor)


def withdraw_refusal(member, is_superuser) -> str:
    """Why this person may not withdraw a gene's figures, or ``""``.

    **Superusers only**, the same gate as ``release_refusal`` and for the same
    reason: this changes what the public website says about commercial
    products. Taking a gene down is no less consequential than putting it up —
    a reader who cited the page finds it gone — so it is not a bench decision
    either, and a rule that let each site withdraw only its own would split one
    gene's page across five people's permissions.

    One reader for the endpoint and the greyed button, so what the page says and
    what the save refuses cannot drift.
    """
    if is_superuser:
        return ""
    return ("Withdrawing takes a gene off the public website, and that is the "
            "review meeting's decision — it needs an administrator account. "
            "You can still set recommendations here; ask an administrator to "
            "withdraw the gene.")


def set_recommended(rows, value: bool, *, actor: str = "") -> int:
    """Set or clear OGA's recommendation on staged figures. Returns how many moved.

    **This is the review meeting's own decision, and until now it could only be
    made in the cropper** — by whoever cut the figure, alone at their laptop,
    before anybody had looked at it. The queue drew the verdict as a read-only
    pill, so the one screen where a group looks at a blot together and decides
    whether the field should buy that antibody was the one screen that could not
    record the answer. Changing your mind meant re-cropping the figure.

    It writes the **pending row only**, never the antibody — the invariant this
    whole table exists for. ``core/recommendations.py::curated_gene_ids`` asks
    whether any antibody on a gene carries a recommendation at all, which is
    what separates *tested and not recommended* from *never assessed* on the
    public gene page, so touching the antibody here would move the public
    verdict on every other antibody on the gene while this figure is still
    withheld. ``release`` applies it, and applies it in **both** directions —
    it sets the field to ``bool(item.recommended)``, so clearing the flag here
    and releasing genuinely un-recommends an antibody that was recommended
    before.
    """
    rows = [r for r in rows if r.status != RELEASED]
    if not rows:
        return 0
    value = bool(value)
    changed = [r for r in rows if bool(r.recommended) != value]
    from pipeline.services import judgement_log
    for item in changed:
        item.recommended = value
        item.save(using=DB, update_fields=["recommended", "updated_at"])
        judgement_log.log(item.antibody_id, item.application_type,
                          "recommended", judgement_log.yes_no(not value),
                          judgement_log.yes_no(value), queued=True, actor=actor)
    return len(changed)


def recommend_refusal(item, member, is_superuser) -> str:
    """Why this person may not change this figure's recommendation, or ``""``.

    **Any pipeline member may, and that is deliberate.** The cropper has always
    let any member set this flag on any gene, so a queue that was stricter would
    make re-cropping the figure the only way to correct a verdict — a rule
    enforced at one door and not the other, which is the shape of most of what
    this repo has had to fix. Nothing here reaches the public site: the flag
    rides on the staged row, and **release** is the gate, superusers only
    (``release_refusal``).

    The one refusal is a row that has already gone out. Its verdict is on the
    antibody now and this row is only the record of what was published, so
    changing it here would silently do nothing — the shape ``discard`` refuses
    for the same reason.
    """
    if item.status == RELEASED:
        return (f"{item.antibody.catalogue_number} {item.application_type} is "
                f"already published, so its recommendation is on the antibody "
                f"now and this row is only the record of what went out. Change "
                f"it on Set recommendations.")
    return ""


def release_refusal(member, is_superuser) -> str:
    """Why this person may not release, or ``""``.

    **Superusers only** (owner's decision, 13 Aug 2026). Releasing is the
    review meeting's act, not a bench decision, and it is the only press in this
    application that puts something in front of the world — so it is narrowed to
    the accounts that already carry that kind of authority, the way
    ``is_staff``/``is_superuser`` is handed out in Django admin rather than on
    the people board.

    Deliberately **not** scoped by site, which is what the ordinary ownership
    rule would have done: a McGill figure is released by whoever is running the
    meeting, and a rule that let each bench publish only its own would put the
    press back on five desks instead of one.

    One reader for both halves — the endpoint and the greyed button — so what
    the page says and what the save refuses cannot drift.
    """
    if is_superuser:
        return ""
    return ("Releasing publishes to the public website, and that is the review "
            "meeting's decision — it needs an administrator account. You can "
            "see everything waiting here and crop more figures; ask an "
            "administrator to release them.")


def discard_refusal(item, member, is_superuser) -> str:
    """Why this person may not discard this staged figure, or ``""``.

    Discarding destroys somebody's work, so it follows the ordinary ownership
    rule — your own bench's records, or you are a superuser — through
    ``services/deletion.py::site_refusal``, which is the one wording for that
    sentence and must not be written a second time.
    """
    from pipeline.services import deletion
    return deletion.site_refusal("antibody", item.antibody, member, is_superuser)
