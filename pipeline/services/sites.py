"""One place that turns a typed site name into a `Site`.

There were five of these, and they disagreed. The target board refused an
unknown name and listed the sites on file; the antibodies and cell-lines boards
refused it and named none, so the only way to find out what to type was to guess;
`target_list_io._resolve_site` *created* the site, which is right when importing
Carl's workbook (the sheet is the source of truth for which sites exist) and
wrong everywhere a person types, because it turns "Leicster" into a real site
that then sits in the site filter of all four boards forever.

And the bulk importers had no site handling at all, which is the defect that
brought this module into being: a downloaded antibody sheet carried no `site`
column, so uploading McGill's 22 SOD1 vials back as a Leicester user previewed
all 22 as **new**. The round trip the boards advertise — "download these, edit
them in Excel, and put them back" — would have doubled another site's records.

So: `resolve` looks a site up and never creates one, `strict_id` refuses by
naming the sites that do exist, `chosen_id` is the same refusal for a value that
came from a dropdown rather than a cell, and `for_row` is what the importers
use — the sheet's own site when it has one, the member's when it hasn't.
"""
from __future__ import annotations

from pipeline.models import Site

DB = "pipeline_db"

# The column name the exports write and the importers read. One string, so the
# header alias tables and the spreadsheet writers cannot drift apart.
SITE_COLUMN = "site"

# `?site=none` — the rows with no site recorded at all.
#
# The Portfolio's "By site" table counts 374 of 584 targets, because 210 have no
# nomination naming a site and so have nowhere to be counted. It says so, and
# saying so was not enough: a number in a caveat with nothing to click is the
# same shape as a refusal that names no alternative. This is what the sentence
# links to. It is the sibling of `NA` in a gene box — the app's existing word
# for "there isn't one" — and a site is never called this, because `Site.name`
# is a real institution and `resolve` would have to match it first anyway.
NONE = "none"


class UnknownSite(ValueError):
    """A typed site name that is not on file. Carries the names that are."""


def known_names(db: str = DB) -> list:
    return list(Site.objects.using(db).filter(is_active=True)
                .order_by("name").values_list("name", flat=True))


def resolve(value, db: str = DB):
    """The Site called `value`, matched on name then short code. None if blank.

    Never creates. An identity fix, a pasted spreadsheet and a cell edit are all
    places where a typo must fail rather than mint a site.
    """
    name = (value or "").strip()
    if not name:
        return None
    return (Site.objects.using(db).filter(name__iexact=name).first()
            or Site.objects.using(db).filter(short_code__iexact=name).first())


def refusal(name: str, db: str = DB) -> str:
    """Why that name was refused, and what to type instead."""
    known = ", ".join(known_names(db))
    if known:
        return f"There is no site called '{name}'. The sites on file are: {known}."
    return f"There is no site called '{name}'."


def strict_id(value, db: str = DB):
    """The site's pk, or raise `UnknownSite`. Blank means "no site" (clears it)."""
    name = (value or "").strip()
    if not name:
        return None
    site = resolve(name, db=db)
    if site is None:
        raise UnknownSite(refusal(name, db=db))
    return site.pk


def chosen_id(value, member=None, db: str = DB):
    """The site a *control* chose — a pk, a name or a short code. Never creates.

    `strict_id` is what a spreadsheet cell and a grid edit use, and it takes a
    name only: a cell reading `12` is a typo, not site 12. A ``<select>`` submits
    the pk, so a second reader is needed rather than a widened one — and it takes
    a name too, because the same value arrives from a URL and from an API caller,
    exactly as ``filter_by`` already accepts all three.

    Blank falls back to the member's own site, which is what every add surface
    did unconditionally before any of them offered the choice. An id that names
    no site is **refused**, not quietly swapped for the member's: a stale page
    silently filing a batch of genes under the wrong institution is the failure
    the choice was added to prevent, and nothing has been written yet when this
    is asked, so refusing costs a press and never a half-done batch.
    """
    raw = str(value or "").strip()
    if not raw:
        return getattr(member, "site_id", None)
    if raw.isdigit():
        site = Site.objects.using(db).filter(pk=int(raw)).first()
    else:
        site = resolve(raw, db=db)
    if site is None:
        known = ", ".join(known_names(db))
        raise UnknownSite(
            f"That site is not on file{'' if raw.isdigit() else f' ({raw})'}. "
            + (f"The sites on file are: {known}." if known else
               "No sites are on file at all."))
    return site.pk


# What a `?site=` value turned out to mean. `filter_by` covers the ordinary
# one-column case; a caller whose "whose is this" lives in *two* columns asks for
# the verdict and builds its own `Q` — see `target_board._filter_site`, where a
# target's site is its nominations' or, failing those, the Access import's.
ALL = "all"          # no filter at all
NO_SITE = "no-site"  # `?site=none` — the rows with no site recorded anywhere
ONE = "one"          # a real site; the pk comes back with it
NOTHING = "nothing"  # a name that is not on file: match nothing, never everything


def filter_kind(value, db: str = DB):
    """`(kind, site_pk)` for a `?site=` value — a pk, a name, or a short code.

    One resolution path for every board, so a value that narrows one board cannot
    silently widen another. `NOTHING` rather than `ALL` for an unknown name is the
    whole point: a filter that quietly matches everything is worse than one that
    matches no rows, because the reader believes the list in front of them.
    """
    raw = str(value or "").strip()
    if not raw:
        return ALL, None
    if raw.lower() == NONE:
        return NO_SITE, None
    if raw.isdigit():
        return ONE, int(raw)
    site = resolve(raw, db=db)
    return (ONE, site.pk) if site else (NOTHING, None)


def filter_by(qs, field: str, value, db: str = DB):
    """Narrow `qs` on a `?site=` value — a pk, a name, or a short code.

    All four boards did `qs.filter(site_id=value)`, which is right for the id the
    filter form submits and a **500** for anything else. `?site=Leicester` is a
    natural thing to type or to copy out of a board URL, and it took down the
    rows endpoint and both exports. A name that resolves filters on it; a name
    that resolves to nothing matches nothing, which is the honest answer and is
    what the empty-result message already says.
    """
    kind, pk = filter_kind(value, db=db)
    if kind == ALL:
        return qs
    if kind == NOTHING:
        return qs.none()
    if kind == NO_SITE:
        # `exclude(…__isnull=False)`, not `filter(…__isnull=True)`. On a direct
        # column the two agree; across a relation they do not — a target with no
        # nominations at all has no joined row for `isnull=True` to match, and
        # those are most of the 210 this exists for.
        return qs.exclude(**{f"{field}__isnull": False})
    return qs.filter(**{field: pk})


def form_value(value, db: str = DB) -> str:
    """A ``?site=`` URL value as the board's filter form spells it — the pk.

    ``filter_by`` has taken a pk, a name or a short code since it was written, so
    ``?site=Leicester`` narrowed the queryset correctly — and the board still
    showed the whole consortium. ``board.js`` builds its query from the **form**,
    and the form's ``<select>`` carries pks, so a name matched no option, the box
    read *All sites*, and the very first rows fetch dropped the filter. The page
    looked filtered while the data was not: exactly the failure the ``?gene=``
    rule already names, one control along.

    An unresolvable name is returned unchanged rather than blanked, so the board
    can render it as a selected option and match nothing — which is what
    ``filter_by`` does with it — instead of quietly widening to everything.
    """
    raw = str(value or "").strip()
    if not raw or raw.isdigit() or raw.lower() == NONE:
        return raw
    site = resolve(raw, db=db)
    return str(site.pk) if site else raw


def for_row(value, member=None, db: str = DB):
    """`(site_id, error)` for one row of a pasted or uploaded sheet.

    A sheet that names a site is honoured, which is what makes the round trip
    safe: re-uploading McGill's row updates McGill's row instead of creating a
    Leicester copy of it. A sheet with no site column, or a blank cell, falls
    back to whoever is pasting — the behaviour before there was a column, and
    the right default for someone entering their own bench's reagents.

    Refuses rather than falling back on an unrecognised name: silently treating
    "McGil" as the uploader's own site is exactly the re-homing this exists to
    stop.
    """
    name = (value or "").strip()
    if not name:
        return getattr(member, "site_id", None), None
    site = resolve(name, db=db)
    if site is None:
        return None, refusal(name, db=db)
    return site.pk, None
