"""The one reader for what an antibody's clonality *is*, and how it prints.

**Two columns answer one question and neither answers it alone.** A vial's
`clonality` is an enum (`monoclonal`/`polyclonal`/`recombinant`/`unknown`) and
`is_recombinant` is a separate boolean, and the two sites filled them under
different conventions — because their source systems asked different questions:

* **Leicester's** sheet had a Clonality column *and* a Recombinant tick, so
  `import_leicester_data` stored `monoclonal` + `is_recombinant=True`. 178 of
  its 334 rows carry the recombinant fact in the boolean **only**.
* **McGill's** Access export had one column holding `recombinant mono` /
  `recombinant poly` / `recombinant super`, which `import_access_data` collapsed
  to the single enum value `recombinant` — losing mono-versus-poly, and leaving
  342 rows whose boolean is `False` while the enum says recombinant.

So neither column is the accurate one; the pair is. Reading the enum alone drew
identical vials of the same clone as `Monoclonal` at Leicester and `Recombinant`
at McGill on one screen (target 1180, 16 Aug 2026) — the enum was believed and
the boolean was drawn nowhere at all. Reading the boolean alone is wrong in the
other direction and would report McGill's 342 as not recombinant.

The data was checked in both directions before this module was written. Of the
23 catalogue numbers stocked at both sites, 22 agree once the pair is read
together; of the 25 clone IDs held at both, **all 25** agree. The two
conventions were describing the same reagents correctly all along.

Nothing here writes. The columns are left as the two labs recorded them —
Leicester's pair is *more* informative than McGill's collapsed enum, so
normalising one onto the other would destroy what it knows. The label is
derived at every read instead.
"""

from django.db.models import Q

RECOMBINANT = "recombinant"
MONOCLONAL = "monoclonal"
POLYCLONAL = "polyclonal"
UNKNOWN = "unknown"

# What a reader sees. `Recombinant monoclonal`/`Recombinant polyclonal` are what
# the suppliers themselves print, and are the whole reason the pair beats either
# column: a recombinant *is* a clonality and a production method at once.
# A bare `Recombinant` is not a shorter way of saying the same thing — it is the
# honest label for a row that does not record which, which is every McGill row.
LABELS = (
    "Recombinant monoclonal",
    "Recombinant polyclonal",
    "Recombinant",
    "Monoclonal",
    "Polyclonal",
    "Unknown",
)


def label_of(stored, recombinant_flag) -> str:
    """The label for a `(clonality, is_recombinant)` pair.

    The primitive, so a screen with the two values in hand and a screen with a
    whole antibody get the same answer from the same line of code.
    """
    stored = (stored or "").strip().lower()
    base = stored if stored in (MONOCLONAL, POLYCLONAL) else ""
    if stored == RECOMBINANT or bool(recombinant_flag):
        return f"Recombinant {base}" if base else "Recombinant"
    return base.capitalize() if base else "Unknown"


def is_recombinant(ab) -> bool:
    """Whether this vial is a recombinant, asking both columns."""
    return (ab.clonality or "").strip().lower() == RECOMBINANT or bool(ab.is_recombinant)


def label(ab) -> str:
    """The printed clonality — the only string a reader should be shown."""
    return label_of(ab.clonality, ab.is_recombinant)


def label_q(value: str) -> Q:
    """A filter for a label from :data:`LABELS`.

    A bare enum value is accepted too, so a `?clonality=monoclonal` URL copied
    out of the app before this module existed keeps meaning what it meant — the
    public gene page's filter is in people's bookmarks. An enum value asks the
    *stored column*, deliberately: that is the question the old link asked.
    """
    v = (value or "").strip()
    if v == "Recombinant monoclonal":
        return Q(clonality=MONOCLONAL, is_recombinant=True)
    if v == "Recombinant polyclonal":
        return Q(clonality=POLYCLONAL, is_recombinant=True)
    if v == "Recombinant":
        return Q(clonality=RECOMBINANT) | (
            Q(is_recombinant=True) & ~Q(clonality__in=(MONOCLONAL, POLYCLONAL)))
    if v == "Monoclonal":
        return Q(clonality=MONOCLONAL, is_recombinant=False)
    if v == "Polyclonal":
        return Q(clonality=POLYCLONAL, is_recombinant=False)
    if v == "Unknown":
        return ~Q(clonality__in=(MONOCLONAL, POLYCLONAL, RECOMBINANT)) & Q(
            is_recombinant=False)
    return Q(clonality=v.lower())


def recombinant_q(want: bool) -> Q:
    """Rows that are (or are not) recombinants, asking both columns."""
    q = Q(clonality=RECOMBINANT) | Q(is_recombinant=True)
    return q if want else ~q


def options_for(queryset) -> list:
    """The labels these rows actually hold, in :data:`LABELS` order.

    A filter offering an option that matches nothing is a question the page
    cannot answer, so the options are derived from the rows. It is one small
    `GROUP BY` over the two columns rather than a walk over the rows: there are
    at most eight distinct pairs however many antibodies the filter covers.
    """
    pairs = queryset.values_list("clonality", "is_recombinant").distinct()
    seen = {label_of(stored, flag) for stored, flag in pairs}
    return [text for text in LABELS if text in seen]
