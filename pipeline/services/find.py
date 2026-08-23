"""One search box for the whole pipeline.

There was none. Every journey started with "which board do I want?" — a question
you can only answer if you already know how the app is built — and finding a gene
from cold cost four clicks plus knowing to go to the target board first. Worse,
the same gene had to be typed again on each board, because looking at STMN2's
antibodies and then STMN2's cell lines is two boards with two filter boxes.

What this does is deliberately small: it resolves one typed string to one place
to go, and when it cannot, it says where the string *does* appear rather than
guessing. Two rules:

  * **An exact gene wins.** People type gene names, and a gene has a page that
    gathers everything about it. `STMN2` goes straight there.
  * **Everything else is reported, not guessed.** A catalogue number that matches
    three antibodies should show three antibodies, not silently pick one.

No outbound calls, and every count is a single aggregate query — this runs on
every search, including the ones that turn out to be typos.
"""
from __future__ import annotations

from django.db.models import Q

from pipeline.models import Antibody, CellLine, ExperimentSession, Target
from pipeline.services import c_number as c_number_svc
from pipeline.services import lab_numbers

DB = "pipeline_db"

# How many examples to show per group. The link goes to the board for the rest;
# this is "did I mean one of these", not a second grid.
PREVIEW = 5


# **What each kind of record is searched on — one definition, used everywhere.**
#
# These are `Q` builders rather than querysets because each board's own Search
# box uses them too. Both halves of that have now cost a field test.
#
# First, four hand-written lists *inside this module* had drifted: a run tag
# found antibodies by clone id but not by lot, sessions by comments but
# antibodies not by comments, cell lines by name alone — so the catalogue number
# printed on a vial in your hand matched nothing, under a box whose placeholder
# says "catalogue number".
#
# Then the same drift one level down, between this module and the boards. Run 9
# typed `RUN9` into the cell-lines board's Search box and got nothing while
# `/pipeline/find/?q=RUN9` returned both rows: the board reached name, C-number,
# Cellosaurus and clone; this reached catalogue, lot, supplier and both notes
# fields. Two search boxes in one app disagreeing about what "search" means reads
# as one of them being broken, and there is no way to tell which from outside.
#
# So: widening a search means widening it everywhere at once, and widening `_why`
# in the same commit — a surprising hit needs something to explain itself with.
# Still exact-gene-first, still no outbound call, still one aggregate query per
# group.
def target_q(term):
    return (Q(gene_name__icontains=term)
            | Q(protein_name__icontains=term)
            | Q(alternative_name__icontains=term)
            | Q(aliases__icontains=term)
            | Q(uniprot_id__icontains=term))


def _number_q(term, kind, field):
    """``Q(field=n)`` when the term reads as a lab number, else an empty ``Q``.

    An exact match rather than a substring, because ``A-118`` is a whole label
    and not a fragment — and it is what somebody holding the box types. The bare
    ``118`` still reaches the same row through the `icontains` clause beside
    this, which is what already found C-numbers.
    """
    number, err = lab_numbers.parse(term, kind=kind)
    return Q() if (err or number is None) else Q(**{field: number})


def antibody_q(term):
    # The lab's own A-number, as written on the freezer box. It was in no search
    # box at all — the field was not drawn on any screen either, so a number
    # somebody read off a box was a string this app could neither show nor find.
    return (_number_q(term, lab_numbers.ANTIBODY, "ab_number")
            | Q(catalogue_number__icontains=term)
            | Q(rrid__icontains=term)
            | Q(clone_id__icontains=term)
            | Q(lot_number__icontains=term)
            | Q(comments__icontains=term)
            | Q(company__name__icontains=term)
            | Q(target__gene_name__icontains=term))


def cell_line_q(term):
    # `origin_comments` and `ko_validation_notes` are where a cell line's free
    # text lives — there is no `comments` column here, which is why two field
    # tests running tagged their rows and then could not find them.
    #
    # **A C-number lives on two tables and both are written on tubes.** The line
    # carries one and each freeze-down batch carries its own
    # (`CellLineVial.c_number`), and on live data 219 of the 746 vial numbers —
    # 29% — appear on no `CellLine` row at all. They run consecutively, so a
    # line is C-23 and its vial is C-24: searching one worked and the other
    # returned nothing, from a box in somebody's hand. `cell_lines.by_c_number`
    # had always read both tables; this read one, which is the same two-readers
    # split the module header is about.
    return (Q(name__icontains=term)
            | _number_q(term, lab_numbers.CELL_LINE, "c_number")
            | _number_q(term, lab_numbers.CELL_LINE, "vials__c_number")
            | Q(cellosaurus_id__icontains=term)
            | Q(clone__icontains=term)
            | Q(c_number__icontains=term)
            | Q(vials__c_number__icontains=term)
            | Q(catalogue_number__icontains=term)
            | Q(lot_number__icontains=term)
            | Q(origin_comments__icontains=term)
            | Q(ko_validation_notes__icontains=term)
            | Q(company__name__icontains=term)
            | Q(target__gene_name__icontains=term))


def session_number(term):
    """The session a term names by number, or ``None``.

    A session's number is the one identifier this app prints on paper. It is
    stamped on all four bench sheets (``planning.SESSION_STAMP``), on every row
    of the per-gene workbook (``session_template.session_ref`` writes
    ``WB · NR3C2 #504``), and it is what the refusal quotes when a sheet is
    uploaded into the wrong session — so ``#504`` is on the desk of anybody
    recording a result, and it was the one thing the search box could not find.
    Searching ``501`` returned two genes matched on a UniProt ID, thirty-nine
    antibodies matched on catalogue substrings, and no Sessions section at all.

    Exact, and on the pk, for the reason ``_number_q`` is exact: a number
    somebody read off a printed sheet is a whole reference and not a fragment.
    `#` is accepted because that is how every one of those sheets writes it.
    """
    digits = (term or "").strip().lstrip("#").strip()
    if not digits.isdigit():
        return None
    number = int(digits)
    # `pk` is a 32-bit integer column, and PostgreSQL raises on a value outside
    # it rather than simply matching nothing — so typing a long run of digits
    # into the search box in the chrome would 500 every page it sits on.
    return number if 0 < number <= 2147483647 else None


def session_q(term):
    # A reading's own comment is part of the session's record, and it was the
    # last thing a run tag could not reach: run 8 and run 9 both wrote the tag on
    # every result row and both cleanup lists had to say the search would not
    # find them. Reached through the session, because a result has no page of its
    # own — the sessions board's row is where you would go to read it.
    number = session_number(term)
    return ((Q(pk=number) if number is not None else Q())
            | Q(target__gene_name__icontains=term)
            | Q(target__protein_name__icontains=term)
            | Q(comments__icontains=term)
            | Q(wb_results__comments__icontains=term)
            | Q(ip_results__comments__icontains=term)
            | Q(if_results__comments__icontains=term)
            | Q(fc_results__comments__icontains=term))


def _targets(term):
    return (Target.objects.using(DB)
            .filter(target_q(term))
            .order_by("gene_name"))


def _antibodies(term):
    return (Antibody.objects.using(DB)
            .filter(antibody_q(term))
            .select_related("target", "company", "site")
            .order_by("catalogue_number"))


def _cell_lines(term):
    # `.distinct()` because the C-number clauses reach `vials`, and a line with
    # three freeze-down batches would otherwise be listed three times — the same
    # reason `_sessions` needs it.
    return (CellLine.objects.using(DB)
            .filter(cell_line_q(term))
            .select_related("target", "site", "company")
            .prefetch_related("vials")
            .distinct()
            .order_by("name"))


def _sessions(term):
    # `.distinct()` because the result-row clauses join four tables: a session
    # with three matching readings would otherwise be counted three times, and
    # the headline number is the first thing a reader checks against the list.
    # Prefetched so `_why` can name the reading that matched without a query per
    # session. Only the `PREVIEW` slice is ever iterated, so this is four extra
    # queries in total, not four per row.
    return (ExperimentSession.objects.using(DB)
            .filter(session_q(term))
            .select_related("target", "site")
            .prefetch_related("wb_results", "ip_results", "if_results", "fc_results")
            .distinct()
            .order_by("-date"))


def exact_gene(term: str):
    """The gene this string names, if it names one exactly.

    Deliberately exact: `SOD1` should go to SOD1's page, and should not go to
    SOD1's page because someone typed `SOD`.
    """
    term = (term or "").strip()
    if not term:
        return None
    return Target.objects.using(DB).filter(gene_name__iexact=term).first()


def find(term: str) -> dict:
    """Where a typed string appears, grouped, with a few examples of each."""
    term = (term or "").strip()
    out = {"term": term, "groups": [], "total": 0}
    if not term:
        return out

    for key, label, qs, board, param in (
        ("targets", "Genes", _targets(term), "pipeline:target_board", "q"),
        ("antibodies", "Antibodies", _antibodies(term),
         "pipeline:antibody_board", "q"),
        ("cell_lines", "Cell lines", _cell_lines(term),
         "pipeline:cell_line_board", "q"),
        ("sessions", "Sessions", _sessions(term), "pipeline:session_board", "q"),
    ):
        count = qs.count()
        if not count:
            continue
        out["total"] += count
        out["groups"].append({
            "key": key,
            "label": label,
            "count": count,
            "board": board,
            "param": param,
            "rows": [_row(key, obj, term) for obj in qs[:PREVIEW]],
        })
    return out


def _why(obj, term, fields) -> str:
    """Which field the search actually matched on, when it was not the obvious one.

    Searching `HAP1` returned UBQLN2 under "Genes" and said nothing about why:
    UBQLN2's alternative name is *Chap1*, and "HAP1" is a substring of it. The
    result was correct and unreadable — which is the same fault the paste preview
    had when it printed two rows identically.

    Named only when the match is not visible in the title, so the common case
    stays quiet.
    """
    needle = (term or "").strip().lower()
    if not needle:
        return ""
    for label, value in fields:
        if needle in (value or "").lower():
            return "" if label == "title" else f"matched on {label}: {value}"
    return ""


def _row(key, obj, term="") -> dict:
    """One example line: what it is, why it matched, and where clicking it goes."""
    if key == "targets":
        title = obj.gene_name or obj.protein_name or f"#{obj.pk}"
        return {"title": title,
                "detail": obj.protein_name or "",
                "why": _why(obj, term, [("title", title),
                                        ("protein name", obj.protein_name),
                                        ("another name for it", obj.alternative_name),
                                        ("another name for it", obj.aliases),
                                        ("UniProt ID", obj.uniprot_id)]),
                "url": f"/pipeline/target/{obj.pk}/"}
    if key == "antibodies":
        gene = obj.target.gene_name if obj.target_id else ""
        bits = [obj.company.name if obj.company_id else "", gene,
                f"lot {obj.lot_number}" if obj.lot_number else "",
                obj.site.name if obj.site_id else ""]
        return {"title": obj.catalogue_number or f"#{obj.pk}",
                "detail": " · ".join(b for b in bits if b),
                # Widened with the query, in the same commit — a row that
                # matched on a number nothing on the line shows would look like
                # a hit on nothing at all.
                "why": _why(obj, term, [("title", obj.catalogue_number),
                                        ("the lab's A-number",
                                         lab_numbers.label(
                                             obj.ab_number,
                                             kind=lab_numbers.ANTIBODY)),
                                        ("RRID", obj.rrid),
                                        ("clone ID", obj.clone_id),
                                        ("lot number", obj.lot_number),
                                        ("supplier",
                                         obj.company.name if obj.company_id else ""),
                                        ("the gene it is against", gene),
                                        ("the antibody's comments", obj.comments)]),
                # The board, filtered to this antibody — the page you edit it on.
                "url": f"/pipeline/antibodies/board/?q={obj.catalogue_number or ''}"}
    if key == "cell_lines":
        bits = [obj.genotype or "", obj.target.gene_name if obj.target_id else "",
                obj.site.name if obj.site_id else ""]
        return {"title": obj.name or f"#{obj.pk}",
                "detail": " · ".join(b for b in bits if b),
                "why": _why(obj, term, [("title", obj.name),
                                        ("the lab's C-number",
                                         c_number_svc.label(obj.c_number)),
                                        # A vial's own number, or the reason a
                                        # row surfaced is invisible: the line
                                        # says C-23 and you searched C-24.
                                        ("a freeze-down vial's C-number",
                                         " ".join(c_number_svc.label(v.c_number)
                                                  for v in obj.vials.all()
                                                  if v.c_number is not None)),
                                        ("Cellosaurus ID", obj.cellosaurus_id),
                                        ("clone", obj.clone),
                                        ("catalogue number", obj.catalogue_number),
                                        ("lot number", obj.lot_number),
                                        ("supplier",
                                         obj.company.name if obj.company_id else ""),
                                        ("the gene it is a knockout of",
                                         obj.target.gene_name if obj.target_id else ""),
                                        ("the line's origin notes",
                                         obj.origin_comments),
                                        ("the knockout validation notes",
                                         obj.ko_validation_notes)]),
                "url": f"/pipeline/cell-lines/board/?q={obj.name or ''}"}
    gene = obj.target.gene_name if obj.target_id else ""
    bits = [obj.procedure_type or "", str(obj.date or ""),
            obj.site.name if obj.site_id else ""]
    return {"title": f"{gene} {obj.procedure_type or ''}".strip() or f"#{obj.pk}",
            "detail": " · ".join(b for b in bits if b),
            # Widened with the query, in the same commit: a session matched on a
            # reading's comment and said nothing about why would look like a hit
            # on nothing at all.
            "why": _why(obj, term, [("title", gene),
                                    # The number is not in the title — that is
                                    # the gene and the procedure — so a search
                                    # for `504` would otherwise hit one session
                                    # and explain nothing about which.
                                    ("the session number", f"#{obj.pk}"
                                     if session_number(term) == obj.pk else ""),
                                    ("the protein name",
                                     obj.target.protein_name if obj.target_id else ""),
                                    ("the session's comments", obj.comments),
                                    ("a result row's comments",
                                     _result_comment_hit(obj, term))]),
            "url": f"/pipeline/sessions/board/?open={obj.pk}"}


def _result_comment_hit(session, term) -> str:
    """The first reading comment on this session containing `term`, or ``""``."""
    needle = (term or "").strip().lower()
    if not needle:
        return ""
    for accessor in ("wb_results", "ip_results", "if_results", "fc_results"):
        for row in getattr(session, accessor).all():
            if needle in (row.comments or "").lower():
                return row.comments
    return ""
