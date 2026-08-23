"""What the caller says it read, checked against what the payload proves it read.

THE PROBLEM THIS SOLVES
-----------------------
Every answer ``scan_controls`` gives rests on what a model read out of a paper,
and the four things a controls assessment needs sit in different sections: which
figures use antibodies (figures and legends), WHICH antibodies (Methods, where
the catalogue numbers are), which controls and of what kind (Methods, figures and
legends), and whether this antibody was read out against the control material
(figures and legends, supplements included).

A caller that read only the Methods produces a full table of ``absent`` — and
``absent`` means *no genetic manipulation of this target appears anywhere in this
paper*, which is an accusation against the authors. The server has no basis for
it. On screen it is indistinguishable from a paper that genuinely shows none.

``portal._coverage`` already says so in words. Nothing reads those words, and a
caveat in a field the renderer ignores is not a safeguard.

WHAT THIS ADDS, AND THE ONE PROPERTY THAT MATTERS
-------------------------------------------------
A declaration — the caller states which sections it read — cross-checked against
the payload, and used to withhold ONE thing: the ``absent`` verdict.

**A declaration can only ever tighten.** Typing the word ``figure_legends`` never
buys back an accusation the payload has not earned; the payload evidence is
necessary and the declaration is an additional filter on top of it. That
asymmetry is the whole safety of asking a model to self-report, because a model
asked whether it read something is free to say yes. ``absent_is_serveable``
implements exactly that and nothing more.

Everything else is served regardless. ``demonstrated`` and ``present_unlinked``
are findings, not accusations — a paper that shows a knockout still shows it
however little of the paper was read — and the OGA verdicts are database facts
that no amount of unread text makes untrue. Withholding those would punish the
caller for honesty and teach it not to declare.

WHAT COUNTS AS EVIDENCE, AND WHAT MUST NOT
------------------------------------------
``evidenced`` reads only what the CALLER supplied. One exclusion is load-bearing:
a control whose readout the server scraped out of quoted text
(``readout_source == "evidence_text"``) does **not** evidence the legends. That is
the server's own reading, and counting it would let the server certify its
inference as the caller's observation — the machine marking its own homework.

``results`` is never inferred. No field in the payload distinguishes a caller that
read the Results from one that did not, so this says so rather than guessing.
"""

from __future__ import annotations

#: The vocabulary. Deliberately small: these are the four divisions a controls
#: assessment actually depends on, not a bibliography of what a paper contains.
SECTIONS = ("methods", "results", "figure_legends", "supplementary")

#: Free text in, controlled vocabulary out — the same shape as
#: ``portal._APP_ALIASES``, and for the same reason: a term that matches nothing
#: gets DROPPED, and a silently dropped declaration is worse than a substituted
#: one. Anything unrecognised is named back to the caller instead.
_ALIASES = {
    "methods": "methods", "method": "methods",
    "materials and methods": "methods", "materials & methods": "methods",
    "m&m": "methods", "experimental procedures": "methods",
    "star methods": "methods", "methods section": "methods",

    "results": "results", "result": "results",
    "results and discussion": "results", "findings": "results",

    "figure legends": "figure_legends", "figure legend": "figure_legends",
    "legends": "figure_legends", "legend": "figure_legends",
    "figures": "figure_legends", "figures and legends": "figure_legends",
    "captions": "figure_legends", "figure captions": "figure_legends",

    "supplementary": "supplementary", "supplement": "supplementary",
    "supplementary figures": "supplementary",
    "supplementary material": "supplementary",
    "supplementary information": "supplementary",
    "supporting information": "supplementary",
    "si": "supplementary", "extended data": "supplementary",

    # A caller that read everything. Expanded rather than kept as a fifth value:
    # the checks below ask about named sections, and a wildcard would have to be
    # special-cased in each of them.
    "full text": "*", "whole paper": "*", "all": "*",
    "everything": "*", "entire paper": "*",
}

UNKNOWN_SECTION_NOTE = (
    "These `sections_read` entries were not recognised and have been ignored: "
    "{unknown}. The vocabulary is methods, results, figure_legends, "
    "supplementary (or 'full text' for all of them). A term that matches nothing "
    "is dropped, so what was ignored is named here rather than quietly treated "
    "as unread.")

WITHHELD_LIMIT = (
    "NO ROW IN THIS REPLY CAN REPORT `absent`. `absent` means no genetic "
    "manipulation of this target appears anywhere in the paper — a statement "
    "about the authors' work — and nothing in this payload shows the figures and "
    "legends were read. Those rows read `not_assessed` instead, which describes "
    "what this call could establish and makes no claim about the paper. Report "
    "them that way. To get a real `absent`, pass the controls you found (or say "
    "you found none) with the figures they are in, and declare `sections_read`.")

SUPPLEMENT_LIMIT = (
    "The supplement was not among the sections read. A genetic control is "
    "routinely in the supplement — a knockout validation panel is exactly the "
    "kind of figure that gets moved there — so any `absent` below is `absent "
    "from the sections that were read`, not from the paper. Say so.")

#: ── Was it read IN FULL? ────────────────────────────────────────────────────
#:
#: `evidenced` answers "did anything come out of the legends", which one reagent
#: carrying one figure satisfies. That is a fair test of whether a section was
#: OPENED and no test at all of whether it was finished — and `absent` is a claim
#: about the whole paper, so a partial read is exactly the case that produces a
#: confident accusation with a hole in it.
#:
#: Asking is useless: a model asked whether it read the legends in full will say
#: yes. So the caller ENUMERATES instead — which legends, by number — and the
#: server checks the enumeration against the payload's own figure references.
#: Enumeration is falsifiable where a yes is not: a control placed in Fig 4 by a
#: caller that lists legends 1-3 is a contradiction the server can see.
#:
#: Two consequences, both deliberate:
#:   * Enumeration is how `absent` is EARNED. A caller that does not enumerate
#:     gets `not_assessed` — softer, and true. The old bar (one reagent with one
#:     figure) was too weak to carry an accusation.
#:   * It must therefore never PUNISH enumeration. An honest, complete list buys
#:     `absent`; an honest, incomplete one is named and withheld; declining to
#:     answer buys nothing either way. Nothing here withholds a FINDING —
#:     `demonstrated` and `present_unlinked` are served however little was read.
LEGENDS_NOT_ENUMERATED_LIMIT = (
    "`figure_legends_read` was not supplied, so no row can report `absent`. "
    "Whether the legends were read IN FULL is the question `absent` depends on — "
    "it claims no genetic manipulation of the target appears anywhere in the "
    "paper — and a payload cannot show it: one reagent carrying one figure looks "
    "identical whether three legends were read or thirty. List the legends you "
    "read, by number, including supplementary ones (\"1\", \"2\", \"S1\"). Rows "
    "read `not_assessed` until then, which claims nothing about the paper.")

LEGENDS_UNREAD_LIMIT = (
    "These figures are cited in the payload but their legends are not among the "
    "ones you listed as read: {what}. Something was placed in a panel whose "
    "legend was not read — so either the list is incomplete or the placement "
    "came from elsewhere. No row can report `absent` while that is unresolved; "
    "they read `not_assessed`.")

LEGEND_GAP_LIMIT = (
    "The legends you listed skip {what}. That is normal if the paper has no such "
    "figure, and it is what a truncated full text looks like — a PDF extraction "
    "that stopped, a paywalled tail, a supplement that never loaded. Say which. "
    "No row reports `absent` while a legend in the middle of the sequence is "
    "unaccounted for.")

RESULTS_NOTE = (
    "Whether the Results were read cannot be inferred from this payload: no "
    "field distinguishes it. It is reported as declared and never as evidenced.")


def normalise(values):
    """``(declared, unrecognised)`` from whatever the caller passed.

    ``declared`` is ``None`` when nothing arrived — deliberately not ``[]``,
    because "the caller did not answer" and "the caller says it read nothing" are
    different statements and only the second is a declaration. The first falls
    back on the payload alone; the second is a caller telling you its own answer
    is worthless, which is worth being able to see.
    """
    if values is None:
        return None, []
    if isinstance(values, str):
        values = [values]
    declared, unknown = [], []
    for raw in values:
        key = " ".join(str(raw or "").strip().lower().replace("_", " ").split())
        mapped = _ALIASES.get(key)
        if mapped == "*":
            declared = list(SECTIONS)
            continue
        if mapped:
            if mapped not in declared:
                declared.append(mapped)
        elif raw:
            unknown.append(str(raw))
    return declared, unknown


def evidenced(primaries, norm_controls):
    """Which sections the PAYLOAD proves were read. Self-report plays no part.

    This is the half that cannot be talked into anything, which is why the gate
    rests on it and a declaration can only narrow it further.
    """
    found = set()

    # A reagent identified at all came from the Methods: that is where catalogue
    # numbers, suppliers and RRIDs are printed, and a legend routinely names no
    # reagent whatsoever.
    if any(r.get("identifier") or r.get("catalogue") or r.get("rrid")
           for r in primaries):
        found.add("methods")

    if any(r.get("figures") for r in primaries):
        found.add("figure_legends")
    for control in norm_controls:
        if control.get("figures") or control.get("evidence"):
            found.add("figure_legends")
        # The caller STATING the readout means the caller read a legend. The
        # server SCRAPING it out of quoted text means only that the server read
        # the quote — counting that would be the server certifying its own
        # inference as the caller's observation.
        if control.get("readout_source") == "caller":
            found.add("figure_legends")
        if control.get("supplementary"):
            found.add("supplementary")

    # `results` is absent on purpose: nothing in the payload distinguishes it.
    return found


def contradictions(declared, found, counts):
    """Where the declaration and the payload disagree — named, never resolved.

    Both directions are reported and neither wins. This follows how the rest of
    this server handles two sources that disagree (``target_matched_via``,
    ``_linkage_ambiguity``): say what each says and hand the reader the question,
    because the server is not the one who can settle it.
    """
    if declared is None:
        return []
    notes = []
    if ("figure_legends" in declared
            and not counts.get("reagents_with_figures")
            and not counts.get("controls_with_quoted_evidence")
            and not counts.get("controls_with_figures")):
        notes.append(
            "You declared reading the figure legends, but nothing in the payload "
            "reflects them: no reagent carried `figures`, no control carried "
            "`figures`, and no control carried quoted `evidence`. Either the "
            "legends name none of this, or they were not carried across into the "
            "call. The verdicts below rest on what was passed in, not on what was "
            "read.")
    for section in ("figure_legends", "supplementary"):
        if section in found and section not in declared:
            notes.append(
                f"The payload contains something only the "
                f"{section.replace('_', ' ')} could have supplied, but "
                f"`sections_read` does not list it. The declaration is treated as "
                f"incomplete; the payload is what the verdicts rest on.")
    if "methods" in declared and not counts.get("reagents"):
        notes.append(
            "You declared reading the Methods but passed no primary reagents. If "
            "the paper genuinely names none, say so — an empty reagent list is "
            "otherwise indistinguishable from a Methods section nobody read.")
    return notes


def legend_completeness(read_keys, cited_keys):
    """``(uncovered, gaps)`` from two sets of ``(supplementary, number)`` keys.

    ``read_keys`` is what the caller says it read; ``cited_keys`` is every figure
    the payload itself refers to. The caller supplies both halves and neither is
    the server's inference — this is set arithmetic on the caller's own words,
    which is why it can be trusted where a yes/no cannot.

    ``gaps`` looks only at the MAIN sequence. Supplementary numbering is not
    reliably contiguous — papers publish S1, S2 and S5 with no S3 in between, and
    an extended-data set is numbered separately again — so a missing S-number is
    not evidence of anything and is not reported as though it were.
    """
    uncovered = sorted(cited_keys - read_keys)
    main = sorted(int(n) for supp, n in read_keys if not supp and n.isdigit())
    gaps = [n for n in range(main[0], main[-1])
            if n not in main] if len(main) > 1 else []
    return uncovered, gaps


def absent_is_serveable(declared, found):
    """May this reply assert ``absent`` at all?

    Two conditions, and the ORDER of the reasoning is the point:

    1. the payload must evidence both the Methods and the figure legends. This is
       necessary and cannot be talked around.
    2. IF the caller declared, the declaration must cover both as well.

    So a declaration only ever removes an ``absent``, never restores one. Saying
    "I read the legends" while carrying nothing from them buys nothing; not
    declaring at all leaves the payload to decide, which is what every existing
    caller already relies on.
    """
    if not {"methods", "figure_legends"} <= found:
        return False
    if declared is None:
        return True
    return {"methods", "figure_legends"} <= set(declared)


def withheld_note(declared, found, legends=None):
    """The sentence a withheld row carries, naming what is missing.

    Two reasons a row can be withheld and they are not the same sentence: a
    section that was never opened, and a section that was opened and not
    finished. The second is the one a reader would otherwise argue with — the
    legends WERE read, so why is this withheld — so it says which legends.
    """
    if legends and not legends.get("complete"):
        if not legends.get("enumerated"):
            why = ("this call does not say which figure legends were read, so "
                   "whether they were read in full is unknown")
        elif legends.get("cited_but_not_read"):
            why = ("figures are cited here whose legends are not among those "
                   "read (" + ", ".join(legends["cited_but_not_read"]) + ")")
        else:
            why = ("the legends read skip "
                   + ", ".join(str(g) for g in legends["gaps_in_sequence"]))
        return (f"No genetic control was reported for this target — but {why}, "
                f"so this is not a finding about the paper. A control sits in "
                f"one figure and the antibody is used in another as a matter of "
                f"course, and the supplement is where a knockout validation "
                f"panel usually ends up.")
    missing = {"methods", "figure_legends"} - found
    if declared is not None:
        missing |= {"methods", "figure_legends"} - set(declared)
    what = " and ".join(sorted(m.replace("_", " ") for m in missing)) or "the paper"
    return (f"No genetic control was reported for this target — but nothing in "
            f"this call shows the {what} were read, so this is not a finding "
            f"about the paper. Check the figures, legends and supplement before "
            f"concluding anything.")
