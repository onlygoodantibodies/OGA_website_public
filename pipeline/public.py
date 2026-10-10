"""What counts as a **public** gene — the one definition, used everywhere.

A `Target` row means "we intend to work on this gene". That is not the same as
"the public site has something to show for it", and conflating the two is how
`/antibodies/GAPDH/` ended up serving a page headed *"characterisation data for 0
GAPDH antibodies"*: GAPDH exists as a target (it is the standard loading control)
but has never been characterised, so there was nothing to render.

The rule the homepage counter has always used: **a named gene with at least one
antibody carrying a published figure.** It was copied by hand into the stats, the
search, the MCP portal and the selector, and simply not applied in the two places
that turned out to matter most — the gene page itself and the browser
extension's gene list. Copies drift; this module exists so there is only one.

The distinction gets sharper as the pipeline fills up: importing a site's target
list adds hundreds of `Target` rows for work that has not started. None of those
are public, and none of them should appear on the site, in the extension, or in
the MCP dataset until there is a figure behind them. **One exception, names
only**: a funder page lists its genes under way by symbol
(``core/funders.py::in_progress``), with nothing about the work behind them.
"""
from __future__ import annotations

from django.db.models import Exists, OuterRef


def public_targets():
    """Targets the public site will show — named, with ≥1 published figure.

    Uses ``Exists`` rather than a join so the result needs no ``.distinct()``:
    the queryset yields one row per target, which makes it safe to hand to
    ``get_object_or_404`` and makes ``.count()`` correct on its own.
    """
    from pipeline.models import Antibody, Target

    published = Antibody.objects.filter(
        target_id=OuterRef("pk"), publication_images__isnull=False)
    return (Target.objects
            .filter(gene_name__isnull=False)
            .exclude(gene_name="")
            .filter(Exists(published)))


def public_gene_names():
    """Sorted gene symbols with a public page — what the extension may claim."""
    return sorted(public_targets().values_list("gene_name", flat=True))


def unpublished_targets():
    """The complement: targets with a name but nothing published behind them.

    Not a bug in itself — it is most of the pipeline, and the target board is
    built on it. It is only a bug when one of these leaks into a public surface,
    so this is the queryset to audit with
    (``manage.py audit_public_genes``).
    """
    from pipeline.models import Antibody, Target

    published = Antibody.objects.filter(
        target_id=OuterRef("pk"), publication_images__isnull=False)
    return (Target.objects
            .filter(gene_name__isnull=False)
            .exclude(gene_name="")
            .filter(~Exists(published)))


# ---------------------------------------------------------------------------
# The other two headline numbers, on the same footing as the gene count.
#
# "159 genes · 1,645 antibodies · 4,321 tests" is one sentence, and only the
# first third of it had one definition. The other two were written out by hand
# on every surface that shows them, in three different ways:
#
#   * the home page, About and the impact page counted **distinct catalogue
#     numbers**;
#   * the API's own status manifest, `/gene-detail/`, the MCP connector and the
#     August availability check counted **antibody records**;
#   * the home page's gene cards and the API's `/genes/` feed counted **every
#     antibody on the gene**, published or not — 1,920 against 1,645, which is
#     the one that was visibly wrong rather than merely differently right.
#
# The first two agree on today's data and disagree structurally: two suppliers
# may print the same catalogue string, and the same product tested at a second
# site is a second record. So the unit here is the **record**, for two reasons
# that outlast the current rows — it is already what most readers use, and it is
# the only one where the parts add up to the whole. A gene page listing 11 rows
# under a site-wide total that deduped two of them away is the same shape as
# every count-versus-list defect in CLAUDE.md.
#
# A figure is one antibody × one application, so it is the natural unit of
# "tests" and it sums per gene the same way.
#
# `core/extension_index.py` is a fifth number (1,611) and deliberately not one
# of these: it is keyed on RRID because a catalogue number alone is not unique
# across suppliers, so 34 published records cannot go in it. That is a matching
# limit, not a smaller dataset, and the index says so in
# `counts.antibodies_without_rrid` — the CHANGELOG and the roadmap both quote
# the 1,611 as though it were the size of the dataset.
# ---------------------------------------------------------------------------

def published_antibodies():
    """Antibody records carrying at least one published figure.

    The public boundary for a reagent, and the queryset every public surface
    should count, list and serialise from. ``.distinct()`` because the filter
    joins the figures: an antibody with three of them is still one antibody.
    """
    from pipeline.models import Antibody

    return Antibody.objects.filter(publication_images__isnull=False).distinct()


def published_figures():
    """Published figure rows — one antibody, one application, one image.

    What the home page calls a *test*. Every row has an antibody behind it, so
    this needs no join to be the published set.

    **Every application OGA gives a verdict in** — `core/recommendations.py::
    APPLICATIONS`, five since IHC gained its verdict on 26 Sep 2026 (API
    2.2.0). This set is also what `/status/` reports, what the manifest and the
    bulk archive carry and what the API lists, and a value put into the public
    API can never be taken out again — 269 of its requests are keyless and
    there is nobody to tell. So the filter stays: a future figure-only
    application stays out of these sets until it has a verdict of its own,
    rather than arriving as an `application` nobody's client has seen with a
    verdict it cannot have. (IHC was that, for one day.)
    """
    from core.recommendations import APPLICATIONS
    from pipeline.models import PublicationImage

    return PublicationImage.objects.filter(application_type__in=APPLICATIONS)


def is_verdict_application(application) -> bool:
    """Is this a figure application the verdict surfaces carry? True for all
    five today — see `published_figures`. For loops over a prefetched
    ``publication_images``, where the queryset filter cannot reach."""
    from core.recommendations import APPLICATIONS

    return application in APPLICATIONS


def gene_page_figures(target=None, *, antibody_ids=None) -> tuple:
    """The applications a gene's page draws a column for — those it (or the
    given antibodies) has a published figure in. A column nobody ran is a
    column of "no data" down the whole page (TP53 has no flow), so the page
    draws only these, and names only these in its intro and meta description.

    The same question as `published_applications` since IHC gained its verdict
    (26 Sep 2026), so it asks that; the name stays because the gene page's
    callers read as what they are for."""
    return published_applications(target, antibody_ids=antibody_ids)


def published_applications(target=None, *, antibody_ids=None) -> tuple:
    """The applications a gene (or the given antibodies) has published figures
    in, in the order every OGA surface prints them — a subset of
    `core/recommendations.py::APPLICATIONS`.

    The gene page's intro line, `<title>`-adjacent meta description and JSON-LD
    all said "across WB, IP, ICC-IF and FC" on every gene, whatever had been
    imaged (MAPT, reported 25 Sep 2026). A heading that names an
    application nothing on the page shows is a claim the table under it
    contradicts. Empty when nothing is published.
    """
    from core.recommendations import APPLICATIONS

    qs = published_figures()
    if target is not None:
        qs = qs.filter(antibody__target=target)
    if antibody_ids is not None:
        qs = qs.filter(antibody_id__in=list(antibody_ids))
    have = set(qs.values_list("application_type", flat=True).distinct())
    return tuple(app for app in APPLICATIONS if app in have)


def application_list(apps) -> str:
    """`WB`, `WB and IP`, `WB, IP, ICC-IF and FC` — the spelling the gene page
    has always used, over whichever applications there are."""
    apps = list(apps or ())
    if len(apps) <= 1:
        return "".join(apps)
    return ", ".join(apps[:-1]) + " and " + apps[-1]


def public_summary(target) -> dict:
    """What a gene's public page holds, in numbers — for the internal gene
    record, so a bench can see what the world sees without leaving the pipeline.

    Counts every published antibody, discontinued ones included (the public
    page hides those by default and says how many — so does this). Per
    application: how many antibodies have a figure, and how many of those read
    supportive / limited support / not supportive, from the same
    `core/recommendations.py` verdicts the public page draws, so the two cannot
    disagree. A figure on a gene nobody has judged yet is counted as
    `unjudged`, never as a negative.

    `None` when the gene has no public page.
    """
    from core import recommendations as R

    if target is None or not target.gene_name:
        return None
    antibodies = list(
        published_antibodies().filter(target=target)
        .prefetch_related("publication_images"))
    if not antibodies:
        return None
    curated = target.pk in R.curated_gene_ids([target.pk])
    axes = R.capability_axes([ab.pk for ab in antibodies])

    rows = []
    for app in R.APPLICATIONS:
        row = {"application": app, "figures": 0, R.SUPPORTIVE: 0,
               R.LIMITED_SUPPORT: 0, R.NOT_SUPPORTIVE: 0}
        for ab in antibodies:
            tested = {img.application_type for img in ab.publication_images.all()}
            if app not in tested:
                continue
            row["figures"] += 1
            value, _clause, tempers = R.verdict_with_qualifier(
                ab, app, tested, curated, axes.get((ab.pk, app)))
            rung = R.support(value, tempers)
            if rung in row:
                row[rung] += 1
        if row["figures"]:
            row["unjudged"] = row["figures"] - sum(
                row[k] for k in (R.SUPPORTIVE, R.LIMITED_SUPPORT,
                                 R.NOT_SUPPORTIVE))
            # Template-friendly names; the controlled values have no hyphen
            # problem but `limited_support` reads better spelled out.
            row["supportive"] = row[R.SUPPORTIVE]
            row["limited"] = row[R.LIMITED_SUPPORT]
            row["not_supportive"] = row[R.NOT_SUPPORTIVE]
            rows.append(row)

    return {
        "antibody_count": len(antibodies),
        "discontinued": sum(1 for ab in antibodies if ab.out_of_market),
        "figure_count": sum(r["figures"] for r in rows),
        "applications": rows,
        "application_list": application_list(r["application"] for r in rows),
        "control_phrase": control_phrase(control_kinds(target)),
    }


# ── Knockout or knockdown ───────────────────────────────────────────────────
#
# Every public surface said "knockout-controlled" as page copy, over figures
# whose own burned-in legend said knockdown — the cropper has offered KO/KD for
# the legend since it was written, and the word reached the pixels and nothing
# else. `PublicationImage.control_genotype` holds it now, and this is the one
# place the two words are chosen from it. A blank is a figure released before
# the column existed (18 Sep 2026): it reads as the site's historical claim,
# knockout, until `backfill_control_genotype` fills it from the Access export's
# knockdown marks (the figures predate the cropper, so the export is the only
# record). That is a statement about what the site *was* saying, not an
# inference about the figure, which is why the backfill exists.
KNOCKOUT, KNOCKDOWN = "KO", "KD"
_CONTROL_WORD = {KNOCKOUT: "knockout", KNOCKDOWN: "knockdown"}


def control_word(value) -> str:
    """`knockout` / `knockdown` for a stored `control_genotype`; `""` for blank."""
    return _CONTROL_WORD.get((value or "").strip().upper(), "")


def control_kinds(target=None, *, antibody_ids=None) -> set:
    """The distinct control kinds over the published figures of one gene (or of
    the given antibodies): a subset of `{"KO", "KD"}`. A blank column reads as
    `KO` — see the module note above. Empty when nothing is published."""
    qs = published_figures()
    if target is not None:
        qs = qs.filter(antibody__target=target)
    if antibody_ids is not None:
        qs = qs.filter(antibody_id__in=list(antibody_ids))
    return {(v or "").strip().upper() or KNOCKOUT
            for v in qs.values_list("control_genotype", flat=True).distinct()}


def control_phrase(kinds, *, capital: bool = True) -> str:
    """`Knockout-controlled`, `Knockdown-controlled`, or `Knockout- and
    knockdown-controlled` — the adjective the gene page, its `<title>`, its
    JSON-LD and the API's summary all open with. One writer, so a page cannot
    say knockout in the title and knockdown under the count."""
    kinds = {k for k in (kinds or ()) if k in _CONTROL_WORD}
    if kinds == {KNOCKDOWN}:
        text = "knockdown-controlled"
    elif KNOCKDOWN in kinds:
        text = "knockout- and knockdown-controlled"
    else:
        text = "knockout-controlled"
    return text[0].upper() + text[1:] if capital else text


def control_noun(kinds) -> str:
    """`knockout controls` / `knockdown controls` / `knockout and knockdown
    controls` — the noun phrase for a sentence such as "assessed with …"."""
    kinds = {k for k in (kinds or ()) if k in _CONTROL_WORD}
    if kinds == {KNOCKDOWN}:
        return "knockdown controls"
    if KNOCKDOWN in kinds:
        return "knockout and knockdown controls"
    return "knockout controls"


def headline_counts():
    """The three numbers the public site leads with, from one place.

    Keys are the names the templates already use, so a page that shows two of
    the three keeps showing two — the point is that it cannot show a *different*
    two.
    """
    return {
        "gene_count": public_targets().count(),
        "antibody_count": published_antibodies().count(),
        "experiment_count": published_figures().count(),
    }


def published_suppliers():
    """The suppliers whose antibodies have a published figure, one name each.

    Spelled as the gene page prints a supplier (``display_name``, falling back
    to ``name``), and counted once per ``Company.canonical_key`` of that
    spelling, so two rows that print the same supplier are one supplier. About
    said "antibodies from 14 manufacturers" in three places while 24 suppliers
    had published antibodies (7 Oct 2026). "Supplier", not "manufacturer":
    the set includes non-profit sources such as DSHB, the Institute for
    Protein Innovation and Addgene.
    """
    from pipeline.models import Company

    has_published = published_antibodies().filter(company_id=OuterRef("pk"))
    names = {}
    for display_name, name in (Company.objects.filter(Exists(has_published))
                               .values_list("display_name", "name")):
        shown = display_name or name
        names.setdefault(Company.canonical_key(shown), shown)
    return sorted(names.values(), key=str.lower)


def supplier_count():
    return len(published_suppliers())
