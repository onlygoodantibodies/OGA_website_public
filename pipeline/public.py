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
the MCP dataset until there is a figure behind them.
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
    """
    from pipeline.models import PublicationImage

    return PublicationImage.objects.all()


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
