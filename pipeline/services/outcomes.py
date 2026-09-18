"""What the evidence showed for one antibody in one application — the one reader.

A recommendation is one bit. The bench recorded two, and has done since the
Access days: **does it detect the target**, and **is it selective**. The pair is
what separates an antibody that gives a clean band at the right size from one
that gives that band *and* four others, and the boolean cannot hold the
difference — see ``models.AntibodyOutcome`` for the counts that make the case.

Two places hold an answer, and this module is the only thing that reconciles
them:

``WbResult.signal`` / ``WbResult.rating``
    The bench record, written at the session. ``signal`` is the Access
    ``SpecificSignal`` column (detects) and ``rating`` is ``SelectiveSignal``
    (selective) — ``rating`` is simply misnamed by the import, and renaming a
    live column is the one migration a rollback cannot undo, so the name stays
    and this module says what it means.

``AntibodyOutcome``
    The review judgement, made from the published figure on
    ``/pipeline/outcomes/``. It exists for the antibodies the session rows are
    silent about, which on live WB is 227 of 1,584 published figures.

**The session record wins wherever it says anything.** A judgement made months
later from a crop does not overrule what the person who ran the blot wrote down;
it fills the blanks. Where both speak and disagree, this module reports the
disagreement rather than picking a side — the same rule
``session_board._duplicates_shown`` follows one panel down, and for the same
reason: the row where two records really do differ is the one a person has to
see.

**A value that is neither YES nor NO is not a guess.** ``rating`` is free text
and nothing on any screen ever said what it wanted, so a cell may hold ``5``,
``pass`` or a sentence. Live WB holds only ``YES``/``NO``/blank today, but
finding a value in a string is not reading one: anything unrecognised comes back
as ``other`` carrying the raw text, and never as a verdict.
"""
from __future__ import annotations

from decimal import Decimal

from pipeline.models import (AntibodyOutcome, IfResult, IpResult,
                             PublicationImage, WbResult)

DB = "pipeline_db"

#: Every axis this module knows, in the order every surface prints them. The
#: model row carries a column per axis; an application uses the ones below.
AXES = ("detects", "selective", "enriches")

#: **Which questions each application actually answers**, checked against the
#: data rather than assumed. Forcing western blot's two axes onto the other
#: three would invent structure the bench never recorded:
#:
#: * **WB** genuinely has two — ``SpecificSignal`` and ``SelectiveSignal``.
#: * **ICC-IF** has one. The WT/KO intensity ratio *is* the selectivity
#:   measure (signal that survives the knockout is not the target's), and
#:   ``specific_signal`` is the lab's visual call of that same thing. Nothing
#:   in the IF schema records "is there any signal at all" separately.
#: * **IP** has one — ``enrichment``, did it pull the target down. The
#:   three-fraction columns (``sm_``/``ub_``/``ip_assessment``) exist on the
#:   model and are blank in all 1,754 live rows, so there is no second axis to
#:   read (owner, 28 Aug 2026).
#: * **FC** is absent: ``histogram_shift`` is blank on all 22 live rows, so a
#:   page offering it would be an empty box with no source behind it.
APPLICATION_AXES = {
    "WB": ("detects", "selective"),
    "ICC-IF": ("selective",),
    "IP": ("enriches",),
}

#: Human wording for each axis — one name per fact, so the page, the receipt and
#: any future public surface all call it the same thing.
AXIS_LABELS = {
    "detects": "Detects the target",
    "selective": "Selective for the target",
    "enriches": "Enriches the target",
}


def axes_for(application):
    """The axes this application answers. Empty for one that answers none."""
    return APPLICATION_AXES.get(application, ())

#: Which result column carries each axis, for the applications whose answer is a
#: plain YES/NO cell. ICC-IF is not here: its answer is a number, and
#: ``_if_selective`` derives it.
RESULT_FIELDS = {
    "WB": {"detects": "signal", "selective": "rating"},
    "IP": {"enriches": "enrichment"},
}

#: Which model holds an application's readings.
RESULT_MODELS = {"WB": WbResult, "ICC-IF": IfResult, "IP": IpResult}

# ── The ICC-IF cut-offs ─────────────────────────────────────────────────────
#
# **Both were measured against the lab's own visual calls, not chosen round.**
# The unit is the WT/KO intensity ratio **at the concentration the lab named
# best** (owner, 28 Aug 2026) — see ``best_ratio`` for why no other ratio is
# used, and ``unused_ratio`` for what happens to the one it sets aside.
#
# ``SELECTIVE_FLOOR`` is 1.5, which the lab has used historically and which the
# data independently picks out: across the 266 best-concentration ratios that
# also carry a visual call, agreement with that call peaks exactly there —
# 80.8% at 1.3, 86.8% at 1.4, **90.6% at 1.5**, 89.5% at 1.6, 82.3% at 2.0. It
# sits in the gap between the two distributions: the NO calls run to a 75th
# percentile of 1.30, the YES calls from a 25th of 1.70.
#
# ``STRONGLY_SELECTIVE`` is 2.6 — the **median of the ratios the lab called
# specific**, which is the "what does the visual assessment centre around"
# question asked of the data. Nothing arbitrary rides on it: **0 of 145** rows
# the lab called NOT specific reach it.
#
# Neither is a verdict on its own. A ratio decides the band; a visual call that
# disagrees with it is reported as a disagreement, never overwritten.
SELECTIVE_FLOOR = Decimal("1.5")
STRONGLY_SELECTIVE = Decimal("2.6")

#: **What each axis may be set to.** WB and IP answer yes/no; ICC-IF grades its
#: one axis on the three bands, because there the answer is a ratio and "yes"
#: would throw away how strongly. A page that offered a value not in here would
#: draw a control the writer refuses — so the writer reads this and the page is
#: handed it, rather than each keeping a list.
BANDS = ("no_selective_signal", "selective", "strongly_selective")
_YES_NO = ("yes", "no", "unclear")
AXIS_VALUES = {
    ("WB", "detects"): _YES_NO,
    ("WB", "selective"): _YES_NO,
    ("IP", "enriches"): _YES_NO,
    ("ICC-IF", "selective"): BANDS + ("unclear",),
}


def values_for(application, axis):
    """The values this axis accepts. Empty for an axis it does not answer."""
    return AXIS_VALUES.get((application, axis), ())


def binary_of(value):
    """A band or a verdict, reduced to yes/no — for comparing like with like.

    The measured band and the bench's visual YES/NO answer the same question at
    different resolutions, so a disagreement between them can only be judged
    after both are put on the same scale.
    """
    if value in BANDS:
        return NO if value == "no_selective_signal" else YES
    return value if value in (YES, NO) else None


#: The three bands, and the wording every surface prints for each.
BAND_LABELS = {
    "no_selective_signal": "No selective signal",
    "selective": "Selective",
    "strongly_selective": "Strongly selective",
}


def band_for(ratio):
    """Which band a WT/KO ratio falls in, or None if there is no ratio."""
    if ratio is None:
        return None
    ratio = Decimal(str(ratio))
    if ratio < SELECTIVE_FLOOR:
        return "no_selective_signal"
    if ratio < STRONGLY_SELECTIVE:
        return "selective"
    return "strongly_selective"


def _measured(ratio):
    """A stored ratio, or ``None`` where the column is not a measurement.

    **Zero means "not recorded", not "no signal at all".** 601 of the 1,817
    live IF rows store `0.0000`, and it is a sentinel rather than a
    measurement: 466 of them have no bench call either, and **38 sit on rows the
    bench called specific** — MA5-53442 stores 0.0000 while the ratios are
    printed on its own published figure as 7.7 and 9.1. The distribution
    settles it: there is **nothing at all between 0 and 0.5**, so this is a
    discrete placeholder and not the tail of anything.

    Read as a measurement it is worse than useless, because it is below every
    cut-off: it banded 238 published antibodies as *no selective signal* and
    accounted for **92 of the 101** ratio-driven conflicts on the review
    worklist — false alarms that would have sent somebody to re-judge figures
    that were never in doubt. Had the owner's "the ratio decides" rule shipped
    first, those 92 would have become public negatives about named commercial
    products on the strength of an empty column.

    Same rule as everywhere else in this repo: a blank means "not written
    down", and a zero in a column that holds a measured quantity is a blank
    wearing a number.
    """
    if ratio is None:
        return None
    return None if ratio == 0 else ratio


def best_ratio(row):
    """The WT/KO ratio at the concentration the lab named best, or None.

    Two concentrations are tested and two ratios recorded; which one *is* the
    antibody's is the one the lab marked ``best_concentration``, matched back to
    ``concentration_1``/``concentration_2``.

    **There is no fallback, and that is the finding.** ``best_concentration`` is
    blank on 1,225 of 1,817 live IF rows, and the obvious stand-in — take the
    higher of the two ratios — was checked against the 428 rows that name a best
    *and* carry both ratios: the named best is the higher ratio **58.2% of the
    time**. That is a coin toss, because the lab picks a working concentration
    on background, morphology and staining pattern, not on the ratio alone. So a
    row with no named best has **no** derived answer here; it leaves a named gap
    for a person, which is the whole of why this is a function and not a
    ``GREATEST()`` in a query.

    **What it must not do is make the measurement disappear** (owner, 12 Sep
    2026). 637 of the 1,817 rows hold exactly one measurement and name no
    usable best, and a number the bench recorded was being dropped with nothing
    on any screen saying so. ``unused_ratio`` is the one reader for those, and
    they are drawn on the card and gathered into a worklist — but they stay out
    of the derived band, because a recomputation that moved public verdicts is
    the thing this split exists to prevent.
    """
    best = (row.best_concentration or "").strip()
    if not best:
        return None
    if best == (row.concentration_1 or "").strip():
        return _measured(row.wt_ko_ratio_1)
    if best == (row.concentration_2 or "").strip():
        return _measured(row.wt_ko_ratio_2)
    # Named a concentration that is neither of the two tested (42 live rows).
    return None


def unused_ratio(row):
    """The measurement this row holds that ``best_ratio`` sets aside, or None.

    Only where the row holds **exactly one**, because that is the case with no
    choice in it: the other column is blank, or the ``0`` placeholder
    ``_measured`` exists to catch. 637 of 1,817 live rows are in it. Where two
    were measured and no best is named there is a genuine question about which
    is the antibody's — 2 live rows — and this stays quiet rather than putting a
    number on the screen that might be the wrong one.

    **It is evidence for a person, not an answer.** Taking it automatically was
    written and reverted the same afternoon: it would have moved 24 public cards
    to *some selective signal* and taken *strongly selective* off 5 more, with
    nobody deciding. What the number is worth is real — across the 632 of these
    rows that also carry a bench call, reading it against ``SELECTIVE_FLOOR``
    agrees with the lab's own eye **94.1%** of the time, against **90.6%** for
    the best-named population the floor was tuned on — but "usually right" is
    the argument for showing somebody, not for writing it down.
    """
    if best_ratio(row) is not None:
        return None
    measured = [r for r in (_measured(row.wt_ko_ratio_1),
                            _measured(row.wt_ko_ratio_2)) if r is not None]
    return measured[0] if len(measured) == 1 else None


YES, NO, UNCLEAR = "yes", "no", "unclear"

#: What a result cell may say and still be read as a verdict. Deliberately
#: short: this is a recognition list, not a parser.
_YES_WORDS = {"YES", "Y", "TRUE", "1"}
_NO_WORDS = {"NO", "N", "FALSE", "0"}


def read_cell(raw):
    """One result cell → ``(verdict, raw_text)``.

    ``verdict`` is ``'yes'``, ``'no'``, ``None`` for a blank cell, or
    ``'other'`` for text this module declines to interpret. ``raw_text`` is
    always what was stored, so a caller can print the thing it could not read
    instead of reporting it as absent.
    """
    text = (raw or "").strip()
    if not text:
        return None, ""
    upper = text.upper()
    if upper in _YES_WORDS:
        return YES, text
    if upper in _NO_WORDS:
        return NO, text
    return "other", text


def _blank_axis():
    return {"value": None, "source": None, "raw": "", "runs": 0,
            "conflict": False, "session_value": None, "review_value": None,
            # ICC-IF only: the number behind the answer, and which band it puts
            # the antibody in. A band with no ratio printed beside it is a
            # verdict nobody can check.
            "ratio": None, "band": None, "needs_grade": False,
            # Set when a person judged an axis whose runs disagree. The
            # disagreement is kept — it is a fact about the sessions — and this
            # says it no longer needs anybody.
            "settled": False,
            # Set when a person judged an axis the bench had already answered.
            # The bench value stays in `session_value` and on the card: an
            # override that hid what it overrode would be indistinguishable
            # from a reading nobody questioned.
            "overridden": False,
            # ICC-IF: ratios were recorded but none of them is the one at the
            # concentration the lab named best, so there is nothing to derive a
            # band from. Distinct from "nothing recorded", and the card has to
            # say which — `best_ratio` explains why no fallback is honest.
            "no_best_concentration": False,
            # ICC-IF: the measurement `best_ratio` set aside, where the row held
            # exactly one. Drawn on the card and gathered into a worklist, and
            # deliberately not folded into `value` or `band` — it is evidence
            # for a person, and a number that moved a public verdict on its own
            # would be the recomputation `unused_ratio` was split out to avoid.
            "unused_ratio": None, "unused_runs": 0,
            # Where that measurement sits on the published scale, and whether
            # the recorded answer contradicts it. The band is printed because a
            # row is *in* the worklist on the strength of it — a list whose
            # reason the card does not show is one nobody can check.
            "unused_band": None, "ratio_mismatch": False}


def _if_selective(rows):
    """ICC-IF's one axis: which of the three bands this antibody is in.

    Three routes to an answer, and they are not equally strong:

    * **A measured ratio decides**, at the concentration the lab named best.
      That is the opposite of the WB rule, deliberately and on the owner's
      instruction (28 Aug 2026): where western blot's answer *is* the person's
      YES/NO, IF's is a measurement, and ``specific_signal`` is that same
      person's eye on the same quantity. The two agree 90.6% of the time; the
      rest are exactly the rows worth looking at, so neither is dropped.
    * **A visual NO with no ratio is an answer** — below the floor there is
      nothing to grade, so ``no_selective_signal`` stands on its own.
    * **A visual YES with no ratio is half an answer.** It says the antibody is
      at least selective and nothing about how strongly, and on live data that
      is 210 of the 1,148 published figures. Reporting them as plain
      ``selective`` would have put a measured 1.5–2.6 band and an ungraded
      "the lab said yes" under one word, with nothing on any surface saying
      which — the one-fact-two-answers shape this whole module exists to undo.
      So the binary is fixed by the bench and the **grade is left to a person**,
      who picks between the two upper bands by eye (owner, 28 Aug 2026).
    """
    entry = _blank_axis()
    ratios = sorted(r for r in (best_ratio(row) for row in rows) if r is not None)
    # Asked with ``_measured``, not against the raw column: a row holding only
    # the ``0`` placeholder recorded nothing, and "set a best concentration" is
    # then a message naming the wrong control — there is no ratio to attribute.
    # A row that did measure one keeps this flag, because the fix really is to
    # name the best concentration; ``unused_ratio`` below is what the card
    # prints beside it in the meantime.
    entry["no_best_concentration"] = not ratios and any(
        _measured(row.wt_ko_ratio_1) is not None
        or _measured(row.wt_ko_ratio_2) is not None
        for row in rows)
    # Set before every return below, because a row can hold an unused
    # measurement whatever the axis goes on to answer — including where another
    # run did name a best, and including where a person has judged it. The card
    # prints it either way; nothing here reads it back.
    unused = sorted(r for r in (unused_ratio(row) for row in rows)
                    if r is not None)
    if unused:
        # The middle one across runs, for the same reason the band takes the
        # median and never the highest.
        entry["unused_ratio"] = unused[len(unused) // 2]
        entry["unused_runs"] = len(unused)
        entry["unused_band"] = band_for(entry["unused_ratio"])
    visual, raws = [], []
    for row in rows:
        verdict, text = read_cell(row.specific_signal)
        if verdict is None:
            continue
        raws.append(text)
        if verdict in (YES, NO) and verdict not in visual:
            visual.append(verdict)

    entry["raw"] = "; ".join(dict.fromkeys(raws))

    if ratios:
        entry["runs"] = len(ratios)
        bands = {band_for(r) for r in ratios}
        if len({binary_of(b) for b in bands}) > 1:
            # Two runs land on opposite sides of the floor. A median across them
            # would answer a question nobody asked.
            entry["conflict"] = True
            entry["source"] = "session"
            return entry
        # The middle ratio represents the antibody across its runs. Never the
        # highest — `best_ratio`'s docstring is about choosing between
        # concentrations, and the same caution applies to choosing between runs.
        entry["ratio"] = ratios[len(ratios) // 2]
        entry["band"] = band_for(entry["ratio"])
        entry["value"] = entry["band"]
        entry["source"] = "session"
        entry["session_value"] = entry["value"]
        if len(visual) == 1 and visual[0] != binary_of(entry["value"]):
            entry["conflict"] = True
        return entry

    if len(visual) > 1:
        entry["runs"] = len(raws)
        entry["conflict"] = True
        entry["source"] = "session"
        return entry

    if len(visual) == 1:
        entry["runs"] = len(raws)
        if visual[0] == NO:
            entry["value"] = "no_selective_signal"
            entry["band"] = entry["value"]
            entry["source"] = "session"
            entry["session_value"] = entry["value"]
        else:
            # At least selective, grade unknown. Left as a gap on purpose, with
            # what the bench said carried so the page can say why only the two
            # upper bands are offered.
            entry["needs_grade"] = True
            entry["source"] = "session"
    return entry


def session_axes(rows, application="WB"):
    """The session record's answer on each axis, across every run of one antibody.

    ``rows`` is that antibody's result rows for the application. Several runs may
    have recorded the axis — 213 published WB antibodies have two or more rows —
    so this aggregates: one distinct verdict is the answer, two is a
    ``conflict``, and the values seen are carried so a page can print them.
    """
    out = {axis: _blank_axis() for axis in axes_for(application)}
    if application == "ICC-IF":
        if "selective" in out:
            out["selective"] = _if_selective(rows)
        return out

    fields = RESULT_FIELDS.get(application) or {}
    for axis, field in fields.items():
        if axis not in out:
            continue
        seen, raws = [], []
        for row in rows:
            verdict, text = read_cell(getattr(row, field, ""))
            if verdict is None:
                continue
            raws.append(text)
            # Only a yes or a no is a verdict. `other` is carried in `raw` so
            # the page can print what it could not read, and never counted as
            # an answer — a `5` in `rating` is not agreement or disagreement.
            if verdict in (YES, NO) and verdict not in seen:
                seen.append(verdict)
        entry = out[axis]
        entry["runs"] = len(raws)
        entry["raw"] = "; ".join(dict.fromkeys(raws))
        if len(seen) == 1:
            entry["value"] = seen[0]
        elif len(seen) > 1:
            # Two runs said different things. Neither is "the" answer, and
            # choosing one silently is how a disagreement becomes a fact.
            entry["conflict"] = True
        if entry["value"] in (YES, NO) or entry["conflict"] or entry["raw"]:
            entry["source"] = "session"
            entry["session_value"] = entry["value"]
    return out


def merge(session, review, application="WB"):
    """Session answer + review row → the answer, with where it came from.

    **A judgement made here wins, and what it overrode is kept.** That inverts
    the rule this module shipped with — the bench record used to win and the
    page filled only its blanks — on the owner's instruction, 29 Aug 2026: every
    change is to be makeable from the screen that has the figure on it, rather
    than half of them here and half on the sessions board.

    What that buys is a single place to do the work. What it costs is that a
    call made from a crop can now sit on top of a reading typed at the bench, so
    the whole of the safety is that **nothing is hidden**: ``session_value``
    still holds what the session recorded, ``ratio`` still holds what was
    measured, and ``overridden`` says the two disagree because somebody decided
    they should. Every card prints all three. Nothing here writes to a session
    row — the runs are the record of what happened on a day, and they are left
    exactly as they are.

    Three shapes a stored judgement can take, and the card words each
    differently:

    * **A blank filled** — no session recorded the axis at all.
    * **A disagreement settled** — two runs said different things, so there was
      no single bench answer to begin with.
    * **A reading overridden** — the bench answered and a person disagreed.
    """
    merged = {}
    for axis in axes_for(application):
        entry = dict(session.get(axis) or _blank_axis())
        stored = (getattr(review, axis, "") or "") if review is not None else ""
        entry["review_value"] = stored or None
        if not stored:
            merged[axis] = entry
            continue

        bench = entry["value"]
        entry["value"] = stored
        entry["band"] = stored if stored in BANDS else None
        entry["source"] = "review"
        if entry["conflict"] and bench is None:
            # The runs disagreed, so there was nothing to override.
            entry["settled"] = True
        elif bench is not None and stored != bench:
            entry["overridden"] = True
        merged[axis] = entry
    # Asked here rather than in `_if_selective`, because a stored judgement
    # replaces the value it compares against — computed before the merge, the
    # flag would answer about the bench call on every row a person has judged,
    # which is the half of the worklist that matters most.
    if application == "ICC-IF" and "selective" in merged:
        merged["selective"]["ratio_mismatch"] = ratio_mismatch(merged)
    return merged


def for_gene(target_id, application="WB"):
    """Every antibody on a gene with a published figure for ``application``.

    One dict per antibody, keyed by antibody id, holding the merged answer on
    each axis that application answers. Batched: three queries whatever the
    gene's size, because the boards' rule that query count must not grow with
    row count applies to a page that draws 40 figures at once just as much.
    """
    ab_ids = list(
        PublicationImage.objects.using(DB)
        .filter(application_type=application, antibody__target_id=target_id)
        .values_list("antibody_id", flat=True)
    )
    if not ab_ids:
        return {}
    return _assemble(application, ab_ids)


def _assemble(application, ab_ids):
    """Shared tail of ``for_gene`` and ``by_gene`` — two queries, one merge.

    Kept in one place because the two callers differ only in how they group the
    answer, and a second copy is how the picker's counts and the grid's counts
    would come to disagree on the same screen.
    """
    rows_by_ab = {}
    model = RESULT_MODELS.get(application)
    if model is not None:
        for row in model.objects.using(DB).filter(antibody_id__in=ab_ids):
            rows_by_ab.setdefault(row.antibody_id, []).append(row)

    reviews = {
        r.antibody_id: r
        for r in AntibodyOutcome.objects.using(DB).filter(
            antibody_id__in=ab_ids, application_type=application)
    }
    return {
        ab_id: merge(session_axes(rows_by_ab.get(ab_id, []), application),
                     reviews.get(ab_id), application)
        for ab_id in ab_ids
    }


def by_gene(application="WB"):
    """Every published figure's merged answer, grouped by gene — in three queries.

    The picker needs a per-gene count of what is outstanding, and asking
    ``for_gene`` once per gene is 462 round trips for one dropdown. The boards'
    rule that query count must not grow with row count is about long tables; a
    picker over every public gene is one.

    Returns ``{gene_name: {antibody_id: axes}}``.
    """
    pairs = list(
        PublicationImage.objects.using(DB)
        .filter(application_type=application)
        .exclude(antibody__target__gene_name="")
        .filter(antibody__target__gene_name__isnull=False)
        .values_list("antibody_id", "antibody__target__gene_name")
    )
    if not pairs:
        return {}

    merged = _assemble(application, [ab_id for ab_id, _ in pairs])
    out = {}
    for ab_id, gene in pairs:
        out.setdefault(gene, {})[ab_id] = merged[ab_id]
    return out


#: Which axis carries "did it do the thing this application is for" — the half
#: of the outcome a recommendation is layered over. WB asks whether it detects,
#: IP whether it enriches, ICC-IF whether it is selective. FC is absent because
#: nothing records an outcome for it.
CAPABILITY_AXIS = {"WB": "detects", "IP": "enriches", "ICC-IF": "selective"}


def for_antibodies(antibody_ids, application="WB"):
    """Merged axes for a set of antibodies — the bulk form of ``for_gene``.

    Two queries whatever the size of the set, because the public surfaces that
    ask this resolve a whole page or a whole snapshot at once. ``for_gene``
    starts from a gene and this starts from the antibodies, which is what a
    caller holding an already-filtered list has.
    """
    ids = list(antibody_ids)
    return _assemble(application, ids) if ids else {}


def capability(axes, application="WB"):
    """Did it do the thing? ``'yes'``, ``'no'``, or ``None`` if unanswered.

    Reduced to the binary on purpose: ICC-IF grades three bands and a caller
    asking "is this antibody able to do the job at all" wants the same shape of
    answer for every application. ``label()`` is where the band survives.
    """
    axis = CAPABILITY_AXIS.get(application)
    if not axis or not axes:
        return None
    return binary_of((axes.get(axis) or {}).get("value"))


#: Which recommendation boolean carries each application. `core/recommendations.py`
#: owns what the flag *means* publicly; this is the column, so the conflict
#: query below can ask whether it agrees with what the bench recorded.
REC_FIELD = {"WB": "wb_recommended", "IP": "ip_recommended",
             "ICC-IF": "if_recommended", "FC": "fc_recommended"}


def strongest_evidence(axes, application="WB"):
    """Is the recorded evidence as strong as this application can record?

    Not the same question as "did it do the thing", which is what
    ``capability`` asks and what the ordinary amber rests on. This is the top of
    the scale, and a negative verdict sitting on top of it is worth a look:

    * **ICC-IF** — the ratio, or a judgement, puts it in the *strongly
      selective* band. 5 live antibodies are that and not recommended.
    * **WB** — it detects **and** is selective, which is everything the two
      axes record. 20 live antibodies are that and not recommended.
    * **IP** — nothing. Its flag is the finer judgement (*enriched
      significantly*) on its only axis, so "enriches but not flagged" is the
      ordinary amber and there is no stronger evidence to disagree with.
    """
    if application == "ICC-IF":
        return (axes.get("selective") or {}).get("value") == "strongly_selective"
    if application == "WB":
        return all(binary_of((axes.get(a) or {}).get("value")) == YES
                   for a in ("detects", "selective"))
    return False


#: The three ways a recommendation and a recorded outcome can disagree, worst
#: first — the order ``conflict_direction`` tests them in, so an antibody that
#: qualifies on two is reported under the stronger statement.
CONFLICT_DIRECTIONS = ("flagged_but_not_capable", "supportive_but_not_selective",
                       "strongest_but_not_flagged")


def conflict_direction(axes, flag, application="WB"):
    """How this antibody's recommendation and its recorded outcome disagree.

    One of ``CONFLICT_DIRECTIONS``, or ``None`` where they do not. The rule for
    one antibody, so the worklist and a single redrawn card cannot come to two
    answers about the same row — a card that stops showing its own note the
    moment somebody acts on it is worse than no note.
    """
    capable = capability(axes, application)

    # Flagged supportive, and the data says it did not do what the application
    # is for. The strongest form, so it is tested first.
    if capable is not None and flag and capable != YES:
        return "flagged_but_not_capable"

    # Flagged supportive on a western blot whose selectivity axis says no. Not
    # a contradiction in the way the first one is — `Supportive — but not
    # selective` is a real and publishable state, and 312 live rows are in it
    # — but it is the pair that most often turns out to be a stale rating, so
    # it is a standing worklist rather than a one-off pass (owner, 29 Aug
    # 2026). ab190355 on ATP2B1 was exactly this: two runs recorded NO, the
    # published blot shows one clean band, and the judgement was re-made from
    # the figure.
    #
    # WB only, and that is the data rather than a choice: ICC-IF's selectivity
    # axis *is* its capability axis, so the same shape is already the first
    # direction above, and IP records no selectivity at all.
    if (flag and application == "WB"
            and binary_of((axes.get("selective") or {}).get("value")) == NO):
        return "supportive_but_not_selective"

    # Everything the application records is positive and the verdict is still
    # negative.
    if capable is not None and not flag and strongest_evidence(axes, application):
        return "strongest_but_not_flagged"
    return None


def conflicts(application="WB"):
    """Antibodies whose recommendation and whose recorded outcome disagree.

    **One disagreement, and it is the same one in every application**: flagged
    supportive, and the data says the antibody did not do what the application
    is for. Somebody judged it worth using and the bench record says it did not
    detect, enrich, or clear the selectivity floor. On live data that is 25
    ICC-IF, 20 IP and a handful of WB.

    **Merely doing the thing is not a conflict**, and reading it as one was a
    misreading of the owner's rule (corrected 29 Aug 2026). Not flagged, and the
    data says the antibody did the thing, is the ordinary qualified negative —
    313 WB rows, 103 IP, and on ICC-IF the ones whose ratio clears 1.5.
    Clearing the floor does not make the data supportive; it means *some
    selective signal*, which is the amber qualifier on a negative and not a
    verdict waiting to be flipped. The ratio **vetoes** a supportive verdict
    when it falls below the floor; it never confers one. Listing 58 ordinary
    ambers as conflicts sent somebody to re-judge figures that were saying
    exactly what they should.

    **The evidence at its strongest is the second one**, added 29 Aug 2026 once
    the first list had been worked through. Strongly selective on ICC-IF, or
    detects *and* selective on WB, with no recommendation: everything the
    application records is positive and the verdict is still negative. That may
    well be deliberate — a judgement can rest on something no column holds —
    but it is a small enough set to confirm rather than assume. 5 on ICC-IF, 20
    on WB.
    """
    return _rows_where(
        application,
        lambda axes, flag: conflict_direction(axes, flag, application))


def unrecommended_but_capable(application="WB"):
    """Published figures that are **not** recommended and did the thing anyway.

    The ordinary amber — 313 WB rows, 103 IP, and on ICC-IF the ones whose
    ratio clears the floor. ``conflicts`` deliberately leaves them out, because
    listing them beside the real disagreements buries those; but they are the
    largest thing on the public site nobody has a screen for, and the owner
    found IP examples on a gene page that the conflicts list, correctly, had
    never shown (29 Aug 2026).

    **It is a review list, not a correction list.** Every row here is drawing
    the amber it should — *not supportive, and yet it did enrich* is a real and
    publishable answer. What it is for is confirming that the negative is still
    the one somebody would make in front of the figure.

    Same shape as ``conflicts`` so the two lists cannot drift into two card
    layouts: ``{antibody_id: {"axes", "flag", "gene", "direction"}}``.
    """
    return _rows_where(
        application,
        lambda axes, flag: (not flag
                            and capability(axes, application) == YES))


def ratio_mismatch(axes):
    """Does the recorded answer contradict the ratio the row measured?

    Only where there is an unused ratio to contradict, and only where the
    record actually answers — a blank cannot disagree with anything.

    **Two resolutions, because two different questions were asked.** A stored
    judgement was picked from the three bands, so it is compared band for band:
    *strongly selective* over a ratio of 2.1 is a real disagreement about how
    strongly, even though both are positive. A bench visual call answered
    yes-or-no and says nothing about strength, so a ``YES`` awaiting its grade
    is contradicted only by a ratio **below the floor** — treating it as a
    claim about the band would flag rows where the bench never made one.

    ``unclear`` disagrees with every band on purpose: somebody could not tell,
    and a number on file is exactly what might settle it.
    """
    entry = axes.get("selective") or {}
    band = entry.get("unused_band")
    if band is None:
        return False
    value = entry.get("value")
    if value is None:
        # The bench called it specific and left the grade open.
        return bool(entry.get("needs_grade")) and binary_of(band) == NO
    return value != band


def ratio_not_used(application="ICC-IF"):
    """Published figures where a ratio on file contradicts the recorded answer.

    The third worklist. A session measured a WT/KO ratio, the row names no best
    concentration so ``best_ratio`` sets it aside, **and** what the record says
    about the antibody is not what that number says. 90 live rows: 41 where the
    two fall on opposite sides of ``SELECTIVE_FLOOR``, and 49 where they agree
    the signal is selective and disagree about how strongly.

    **Narrowed to disagreements on the owner's instruction, 12 Sep 2026.** The
    first version listed every unused ratio — 269 rows — and most of them were
    a bench ``NO`` over a number that is also below the floor, which is two
    sources agreeing and nothing to look at. What is worth somebody's time is
    the pair that cannot both be right.

    **It exists because the alternative is a silent recomputation.** Reading
    those measurements into the band automatically was written and reverted the
    same day: it would have added *some selective signal* to 24 public cards
    and taken *strongly selective* off 5, none of it decided by a person. The
    same evidence routed through a worklist costs one click each and leaves the
    decision where it belongs — and the click is already built, because judging
    the band is what this page does.

    Rows a person has already judged are **in** it, which the first version had
    backwards: 234 live antibodies were graded by eye before the ratio was ever
    on the screen, and 64 of those grades disagree with it. Judging a row again
    still clears it, because the new answer is what gets compared.

    ICC-IF only, which is the data rather than a choice: it is the one
    application whose answer is a number, so it is the only one with a
    measurement to disagree with.

    Same shape as the other two so the three cannot drift into three card
    layouts: ``{antibody_id: {"axes", "flag", "gene", "direction"}}``.
    """
    if application != "ICC-IF":
        return {}
    return _rows_where(application, lambda axes, flag: ratio_mismatch(axes))


def _rows_where(application, keep):
    """Every published figure for ``application`` whose axes satisfy ``keep``.

    Shared by both worklists, so a row appears in each on the same evidence and
    a gene's counts add up the same way whichever list you came from.
    """
    field = REC_FIELD.get(application)
    if not field or not CAPABILITY_AXIS.get(application):
        return {}

    rows = list(
        PublicationImage.objects.using(DB)
        .filter(application_type=application)
        .exclude(antibody__target__gene_name="")
        .filter(antibody__target__gene_name__isnull=False)
        .values_list("antibody_id", "antibody__target__gene_name",
                     f"antibody__{field}")
    )
    if not rows:
        return {}

    merged = _assemble(application, [r[0] for r in rows])
    out = {}
    for ab_id, gene, flag in rows:
        axes = merged.get(ab_id) or {}
        verdict = keep(axes, flag)
        if not verdict:
            continue
        out[ab_id] = {"axes": axes, "flag": flag, "gene": gene,
                      "direction": verdict if isinstance(verdict, str) else None}
    return out


def is_gap(axes, application="WB"):
    """Is there still something to judge here?

    True when either axis has no answer at all. A conflict is *not* a gap — it
    is a thing to look at, counted separately, because filling it in is not what
    resolves it.
    """
    wanted = axes_for(application) or tuple(axes)
    return any(axes[axis]["value"] is None for axis in wanted)


def summarise(by_ab, application="WB"):
    """Counts for the header — judged, still to judge, and disagreements.

    A count with no list under it invents the noun, so the page draws the
    antibodies these count; this only says how many.
    """
    total = len(by_ab)
    gaps = sum(1 for axes in by_ab.values() if is_gap(axes, application))
    # Disagreements that still need somebody. One a person has settled, and
    # one the ICC-IF ratio decides, are both facts the card keeps printing —
    # they are not work, and counting them as work is what sent people at rows
    # the page then refused.
    conflicts = sum(1 for axes in by_ab.values()
                    if any(entry["conflict"] and entry["value"] is None
                           for entry in axes.values()))
    return {"total": total, "judged": total - gaps, "gaps": gaps,
            "conflicts": conflicts}


def label(axes, application="WB"):
    """The outcome the axes resolve to, or None while an answer is missing.

    This is the wording the public surfaces will eventually carry, held here so
    the extension, the MCP and the gene page cannot each invent their own — the
    mistake ``core/recommendations.py`` was written to undo. Nothing public
    reads it yet.
    """
    if application == "ICC-IF":
        # The value *is* the band — measured from a ratio, or graded by eye
        # where none was recorded. Both carry the same words on purpose; the
        # axis's `source` and `ratio` say which route produced it.
        value = (axes.get("selective") or {}).get("value")
        return value if value in BANDS else None

    if application == "IP":
        value = (axes.get("enriches") or {}).get("value")
        if value == YES:
            return "enriches"
        if value == NO:
            return "does_not_enrich"
        return None

    detects = axes["detects"]["value"]
    selective = axes["selective"]["value"]
    if detects == YES and selective == YES:
        return "specific_and_selective"
    if detects == YES and selective == NO:
        return "detects_not_selective"
    if detects == NO:
        return "no_specific_signal"
    return None


#: What each verdict says on a screen. One writer, so the card, the receipt and
#: any future public surface cannot drift into three wordings of one fact.
VERDICT_LABELS = {
    "specific_and_selective": "Detects the target, and nothing else",
    "detects_not_selective": "Detects the target, plus other bands",
    "no_specific_signal": "No signal for the target",
    "no_selective_signal": "No selective signal",
    "selective": "Selective",
    "strongly_selective": "Strongly selective",
    "enriches": "Enriches the target",
    "does_not_enrich": "Does not enrich the target",
    # The button wording for the plain yes/no axes, so one dict answers both
    # "what does this verdict say" and "what goes on this control".
    "yes": "Yes",
    "no": "No",
    "unclear": "Could not tell",
}
