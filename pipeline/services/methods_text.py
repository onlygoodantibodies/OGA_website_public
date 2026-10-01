"""The methods paragraph a reader copies from under a published figure.

**One writer for the words, whichever surface shows the button** — the gene
page, the data portal (through the API's ``oga_methods``), the embed card. A
paragraph composed twice is two paragraphs that drift.

Three rules, all the owner's (30 Sep 2026):

* **A missing value is not written about.** No `[exposure time]`, no "not
  recorded": a clause whose value is absent is left out and the paragraph reads
  as if it had never been going to say it. This is the opposite of the draft
  report (`report_generator._recorded`), and deliberately so — a draft is
  filled in by the lab before it is published, while this text is pasted into
  somebody else's paper, where a bracket would be printed.
* **Nothing is invented.** Every noun phrase is a value the report stated;
  the words around them are only the verbs of a methods section. The one
  exception is the heading.
* **The report is the source** (`MethodsRecord`), and the paragraph ends by
  citing it.

The values arrive as the extraction wrote them, with the extractor's own notes
to itself mixed in ("per Table 3", "methods text says …"). ``clean`` removes
those at load time, so what is stored is what is printed.
"""
from __future__ import annotations

import re
import time
from datetime import date

_EPOCH = date(1900, 1, 1)

APPLICATIONS = ("WB", "IP", "ICC-IF", "FC", "IHC")

HEADINGS = {
    "WB": "Western blot",
    "IP": "Immunoprecipitation",
    "ICC-IF": "Immunofluorescence",
    "FC": "Flow cytometry",
    "IHC": "Immunohistochemistry",
}

# The run-level facts each application's paragraph can use, in the order the
# paragraph uses them. A key outside this list is kept in the record (and the
# API) but never printed.
CONDITION_KEYS = {
    "WB": ("cell_lines", "lysis_buffer", "protein_loading_ug", "gel_chemistry",
           "membrane", "transfer_method", "blocking", "primary_incubation",
           "secondary_antibody", "secondary_dilution", "ecl_type",
           "imaging_system", "exposure_time"),
    "IP": ("cell_lines", "lysis_buffer", "protein_amount_mg",
           "protein_concentration", "lysate_volume_ml", "bead_type",
           "incubation", "fractions_loaded", "gel", "membrane",
           "detection_antibody", "detection_antibody_dilution",
           "secondary_antibody", "secondary_dilution", "ecl",
           "detection_system"),
    "ICC-IF": ("cell_lines", "mosaic_labelling", "fixation", "permeabilisation",
               "blocking", "dilution_buffer", "primary_condition",
               "secondary_ab", "secondary_dilution", "secondary_condition",
               "nuclear_stain", "microscope", "objective", "analysis"),
    "FC": ("cell_lines", "tracker_dyes", "live_or_fixed", "fixation",
           "permeabilisation", "blocking", "secondary_ab", "secondary_dilution",
           "flow_cytometer", "analysis_software"),
    "IHC": ("tissue_or_cells", "fixation", "antigen_retrieval", "blocking",
            "detection_system", "counterstain", "scanner"),
}


# ── Cleaning ────────────────────────────────────────────────────────────────

# A segment (between semicolons) that is only a note about the report's
# silence carries no method, so it goes whole.
_SILENCE = re.compile(
    r"not stated|not given|not described|does not name|does not state|which was used is not",
    re.I)
_ASIDE = re.compile(r"methods text|refers generally|\(methods\)|\(text\)|\bper methods\b",
                    re.I)
_TABLED = re.compile(r"\bTable\s*\d|secondary (?:antibody )?table|\blisted (?:in|under)\b"
                     r"|working concentration",
                     re.I)
# A segment that only describes another figure's samples ("Fig 1B HAP1 WT
# medium", "Figure 1 panel: …") belongs to the report, not to one antibody's
# methods, so it goes whole.
_FIGURE_ONLY = re.compile(r"^\s*(?:(?:Fig\.?|Figure)\s*\d|(?:figure\s+)?legend\s*:)", re.I)
# Said before the pointers below take a whole bracket away: the part of a
# bracket that is a note, where the rest of it is the method.
_INNER = [
    re.compile(r"\s*;\s*listed in Table\s*\d+[A-Za-z]?", re.I),
    # "(methods: glass coverslips; legend: 96-well plate)" — the methods win.
    re.compile(r"\(methods:\s*([^;()]*?)\s*;\s*legend:[^()]*\)", re.I),
    re.compile(r"\s*(?:,|\b(?:in|per|see)\b)\s*(?:Fig\.?|Figure)\s*\d+[A-Za-z]?"
               r"(?:\s+legend)?(?:\s*/\s*(?:Fig\.?|Figure)\s*\d+[A-Za-z]?[\w ]*)?(?=[;,)]|$)",
               re.I),
]
# What the extractor said about the page rather than the method.
_NOTES = [
    re.compile(r"\s*\blisted in (?:the )?secondary (?:antibody )?table\b", re.I),
    re.compile(r"\s*,?\s*(?:order\s+)?as printed\b", re.I),
    re.compile(r"\s*printed\s+'[^']*'", re.I),
    re.compile(r"\s*\b(?:working concentration\s*(?:(?:in|per)\s+(?:Table\s*\d+|"
               r"(?:the\s+)?secondary (?:antibody )?table))?:?)", re.I),
    re.compile(r"\s*\b(?:per|in)\s+(?:the\s+)?secondary (?:antibody )?table\b", re.I),
    re.compile(r"\s*\blisted (?:in|under)\s+[^;()]*$", re.I),
    re.compile(r"\s*\((?:methods|text)\)|\s*\bper methods\b", re.I),
    re.compile(r"^\s*Table\s*\d+\s*(?:lists|:)?\s*", re.I),
    re.compile(r"^\s*peroxidase[- ]conjugated(?:\s+secondary)?\s*:\s*", re.I),
]
# Pointers into the report's own tables, legends and figures. Meaningful to
# the person who extracted them; in somebody else's paper "(Fig 1A)" would
# point at *their* Figure 1.
_POINTERS = [
    re.compile(r"\s*\((?:[^()]*?\b(?:Table|Fig\.?|Figure|legend)\b[^()]*)\)",
               re.I),
    re.compile(r"\s*,?\s*\b(?:per|listed in)\s+(?:Table|legend)\b[\s\d/A-Za-z]*$",
               re.I),
    re.compile(r"^\s*(?:Fig\.?\s*\d+[A-Za-z]?\s+)?legend\s+(?:says|states|mentions)\s+",
               re.I),
    re.compile(r"^\s*methods text\s+(?:says|states)\s+", re.I),
    re.compile(r"\s*\(see [a-z_]+\)", re.I),
]


def clean(value) -> str:
    """The extraction's value with its notes to itself removed; ``""`` when
    nothing but a note was there."""
    text = str(value or "").strip()
    if not text:
        return ""
    # The part of a bracket that is a note, where the rest of it is method.
    for rx in _INNER:
        text = rx.sub(lambda m: f"({m.group(1)})" if m.groups() else "", text)
    text = re.sub(r"\(\s*[,;]?\s*\)", "", text)
    text = re.sub(r"\(\s*[,;]\s*", "(", text)
    text = re.sub(r"  +", " ", text)
    plain, tabled, methods = [], [], []
    for segment in text.split(";"):
        seg = segment.strip()
        if not seg or _SILENCE.search(seg) or _FIGURE_ONLY.search(seg) \
                or re.search(r"\balso listed\s*$", seg, re.I):
            continue
        # Where a report disagrees with itself, its methods text wins over
        # its tables and legends (owner, 1 Oct 2026): the methods text is the
        # consensus protocol's own prose (Ayoubi et al., Nat Protoc 2024), a
        # table's "working concentration" is a separate record of it. So
        # "methods text says X" replaces a value read off a table, and a
        # table value stands only where the methods text said nothing.
        aside = bool(_ASIDE.search(seg))
        from_table = bool(_TABLED.search(seg))
        for rx in _POINTERS + _NOTES:
            seg = rx.sub("", seg).strip()
        seg = re.sub(r"\(\s*\)", "", seg).strip(" ,")
        if not seg or _ASIDE.search(seg):
            continue
        (methods if aside else tabled if from_table else plain).append(seg)
    return _balanced("; ".join(plain + (methods[:1] or tabled))).strip().rstrip(".")


def _balanced(text: str) -> str:
    """Brackets closed: a note dropped from inside one ("(corresponding
    species; catalogue numbers not stated)") takes its ``)`` with it."""
    out, depth = [], 0
    for i, ch in enumerate(text):
        if ch == "(":
            depth += 1
        elif ch == ")":
            if not depth:
                # "probed with A) AF321 or B) …" — an enumerator, not a bracket.
                if not re.search(r"(?:^|\s)[A-Za-z0-9]$", text[:i]):
                    continue
            else:
                depth -= 1
        out.append(ch)
    result = "".join(out).rstrip(" ,")
    while depth and result.endswith("("):
        result, depth = result[:-1].rstrip(" ,"), depth - 1
    return result + ")" * depth


def clean_conditions(app: str, raw: dict) -> dict:
    """The keys this application's paragraph knows, cleaned, blanks dropped."""
    out = {}
    for key in CONDITION_KEYS.get(app, ()):
        value = clean(raw.get(key))
        if value:
            out[key] = value
    return out


# ── Composing ───────────────────────────────────────────────────────────────

_GENERIC_OPENERS = {
    "peroxidase", "peroxidase-conjugated", "goat", "donkey", "sheep", "rabbit",
    "mouse", "protein", "precast", "large", "cells", "lysate", "antibody-bead",
    "intracellular", "shading", "maximum", "max", "wt", "hrp-conjugated",
    "anti-mouse", "anti-rabbit", "anti-goat", "anti-rat", "anti-sheep",
}


def _lc(text: str) -> str:
    """Lower-case a value's first letter when it is an ordinary word that
    starts a sentence in the report ("Peroxidase-conjugated goat …"); leave
    a brand, a catalogue number or an acronym alone."""
    first = text.split(" ", 1)[0]
    if first.lower() in _GENERIC_OPENERS and first[:1].isupper() and not first.isupper():
        return text[:1].lower() + text[1:]
    return text


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def _series(clauses: list[str]) -> str:
    """Join clauses; a semicolon list once any clause has its own commas, so a
    reader can tell where one condition ends and the next begins."""
    clauses = [c for c in clauses if c]
    if len(clauses) <= 1:
        return "".join(clauses)
    if len(clauses) == 2:
        return f"{clauses[0]} and {clauses[1]}"
    if any("," in c for c in clauses):
        return "; ".join(clauses[:-1]) + "; and " + clauses[-1]
    if len(clauses) == 2:
        return f"{clauses[0]} and {clauses[1]}"
    return ", ".join(clauses[:-1]) + " and " + clauses[-1]


def _is_dilution(value: str) -> bool:
    return bool(re.match(r"^\s*(?:1\s*[:/]\s*[\d ,.]+|1\s*in\s*\d)", value, re.I))


def _used(value: str) -> str:
    """"diluted 1/500" for a ratio, "used at 2 µg/ml" for anything else."""
    return f"diluted {value}" if _is_dilution(value) else f"used at {value}"


def _qualified(value: str) -> bool:
    """A value that opens with its own qualifier ("cell surface: …") cannot
    follow a verb, so it is given its own labelled sentence instead."""
    return bool(re.match(r"^[\w /().-]{2,40}:\s", value))


_PREPOSITION = re.compile(r"^(?:on|in|with|for|at|using|by|after|under)\b", re.I)


def _step(verb: str, value: str, label: str, loose: list[str]) -> str:
    """"fixed in 4% PFA …" — or "fixed on ice …" when the value already opens
    with its own preposition, which "fixed in on ice" would double."""
    if not value:
        return ""
    if _qualified(value):
        loose.append(f"{label}: {value}.")
        return ""
    if _PREPOSITION.match(value):
        verb = verb.split(" ", 1)[0]
    return f"{verb} {_lc(value)}"


def _micrograms(value: str) -> str:
    return value if re.search(r"[µu]g|mg", value) else f"{value} µg"


def _samples(cells: str) -> str:
    return "Samples" if re.search(r"medium|media|serum|tissue|brain|testis", cells, re.I) else "Lysates"


def _secondary(sec: str, dil: str, time_cond: str = "") -> str:
    if not (sec or dil):
        return ""
    how = ""
    if dil:
        how = f", {_used(dil)}" if _is_dilution(dil) else f" at {dil}"
    when = f" for {time_cond}" if time_cond else ""
    if sec and dil and ";" in sec:
        # A list of secondaries has its own semicolons; a dilution hung on
        # the end would read as belonging to the last one only.
        return (f"Detection used {_lc(sec)}. Secondary antibodies were "
                f"{_used(dil)}{when}.")
    if sec:
        return f"Detection used {_lc(sec)}{how}{when}."
    return f"Secondary antibodies were {_used(dil)}{when}."


def _detect(ecl: str, system: str) -> str:
    if ecl and system:
        return f"Signal was developed with {_lc(ecl)} and detected with {_lc(system)}."
    if ecl:
        return f"Signal was developed with {_lc(ecl)}."
    if system:
        return f"Signal was detected with {_lc(system)}."
    return ""


def _primary(label: str, amount: str, extra: list[str], medium: str = "") -> str:
    first = ""
    if amount:
        first = _used(amount) + (f" in {_lc(medium)}" if medium else "")
    elif medium:
        first = f"diluted in {_lc(medium)}"
    parts = ([first] if first else []) + [e for e in extra if e]
    if not label and not parts:
        return ""
    who = f"The primary antibody, {label}," if label else "The primary antibody"
    if parts:
        # " and ", never the semicolon list: a buffer's own commas are inside
        # its parentheses, and "diluted 1/500 in X; and incubated" reads wrong.
        return f"{who} was {' and '.join(parts)}."
    return f"The primary antibody was {label}."


def _prepared(cells: str, lysis: str) -> str:
    """The opening sentence of a blot: what was lysed, and in what. A lysis
    value that opens with its own qualifier ("no cell lysis: culture medium
    centrifuged …") is a description, not a buffer, so it goes in brackets."""
    parts = _top_split(cells, r";") if cells else []
    if len(parts) > 1:
        # "A and B; concentrated culture medium" — the sample type is an
        # aside about the lines, set off by commas, not a second list item.
        cells = f"{parts[0]}, {', '.join(parts[1:])},"
    subject = f"{_samples(cells)} of {cells}" if cells else "Lysates"
    if not lysis:
        return f"{subject} were prepared." if cells else ""
    if _qualified(lysis):
        return f"{subject} were prepared ({lysis})."
    return f"{subject} were prepared in {_lc(lysis)}."


def _wb(c: dict, label: str, amount: str) -> list[str]:
    out = [_prepared(c.get("cell_lines", ""), c.get("lysis_buffer", ""))]

    load, gel = c.get("protein_loading_ug", ""), c.get("gel_chemistry", "")
    mem, transfer = c.get("membrane", ""), c.get("transfer_method", "")
    steps = []
    if gel:
        steps.append(f"resolved on {_lc(gel)}")
    elif load:
        steps.append("resolved by SDS-PAGE")
    if mem:
        steps.append(f"transferred to {_lc(mem)}" + (f" ({transfer})" if transfer else ""))
    if steps:
        subject = f"{_micrograms(load)} of protein was" if load else "Proteins were"
        out.append(_cap(f"{subject} {_series(steps)}."))

    if c.get("blocking"):
        out.append(f"Membranes were blocked with {_lc(c['blocking'])}.")
    out.append(_primary(label, amount, [
        f"incubated {c['primary_incubation']}" if c.get("primary_incubation") else ""]))
    out.append(_secondary(c.get("secondary_antibody", ""), c.get("secondary_dilution", "")))
    out.append(_detect(c.get("ecl_type", ""), c.get("imaging_system", "")))
    if c.get("exposure_time"):
        out.append(f"Exposure time was {c['exposure_time']}.")
    return out


def _ip(c: dict, label: str, amount: str) -> list[str]:
    out = [_prepared(c.get("cell_lines", ""), c.get("lysis_buffer", ""))]

    mg, conc, vol = (c.get("protein_amount_mg", ""), c.get("protein_concentration", ""),
                     c.get("lysate_volume_ml", ""))
    if mg:
        lysate = mg if re.search(r"mg", mg) else f"{mg} mg"
        lysate += " of lysate"
        if vol and conc:
            lysate += f" ({vol} ml at {conc})"
    elif vol and conc:
        lysate = f"{vol} ml of lysate at {conc}"
    else:
        lysate = "Lysate"
    antibody = label or "the antibody"
    if amount:
        antibody = f"{amount} of {antibody}"
    sentence = f"{lysate} was incubated with {antibody}"
    if c.get("bead_type"):
        sentence += f" pre-coupled to {_lc(c['bead_type'])}"
    if c.get("incubation"):
        sentence += f" ({_lc(c['incubation'])})"
    if label or amount or c.get("bead_type") or mg or conc:
        out.append(_cap(sentence) + ".")

    fractions, gel, mem = c.get("fractions_loaded", ""), c.get("gel", ""), c.get("membrane", "")
    det, det_dil = c.get("detection_antibody", ""), c.get("detection_antibody_dilution", "")
    steps = []
    if gel:
        steps.append(f"resolved on {_lc(gel)}")
    if mem:
        steps.append(f"transferred to {_lc(mem)}")
    if det:
        how = ""
        if det_dil:
            how = f", {_used(det_dil)}" if _is_dilution(det_dil) else f" at {det_dil}"
        steps.append(f"probed with {det}{how}")
    if steps:
        # "SM=4% starting material; UB=…; IP=immunoprecipitate" is the
        # report's key to its lanes; the sentence wants the lanes.
        lanes = [re.sub(r"^(?:[A-Z]{2}\s*=\s*|plus\s+)", "", p.strip()).strip()
                 for p in re.split(r"[;,]|\band\b", fractions) if p.strip()]
        subject = (_cap(_series([l for l in lanes if l])) if lanes
                   else "The immunoprecipitates")
        out.append(f"{subject} were {_series(steps)}.")
    out.append(_secondary(c.get("secondary_antibody", ""), c.get("secondary_dilution", "")))
    out.append(_detect(c.get("ecl", ""), c.get("detection_system", "")))
    return out


def _objective(value: str) -> str:
    return value if re.search(r"objective\s*$", value, re.I) else f"{value} objective"


def _if(c: dict, label: str, amount: str) -> list[str]:
    out, loose = [], []
    cells, mosaic = c.get("cell_lines", ""), c.get("mosaic_labelling", "")
    if cells and mosaic:
        out.append(f"The cell lines were {cells}; {_lc(mosaic)}.")
    elif cells:
        out.append(f"The cell lines were {cells}.")
    elif mosaic:
        out.append(f"{_cap(mosaic)}.")
    steps = [
        _step("fixed in", c.get("fixation", ""), "Fixation", loose),
        _step("permeabilised with", c.get("permeabilisation", ""), "Permeabilisation", loose),
        _step("blocked with", c.get("blocking", ""), "Blocking", loose),
    ]
    if any(steps):
        out.append(f"Cells were {_series(steps)}.")
    out.extend(loose)
    out.append(_primary(label, amount, [
        f"incubated {c['primary_condition']}" if c.get("primary_condition") else "",
    ], medium=c.get("dilution_buffer", "")))
    cond = c.get("secondary_condition", "")
    out.append(_secondary(c.get("secondary_ab", ""), c.get("secondary_dilution", ""), cond))
    stain = c.get("nuclear_stain", "")
    if stain and stain.split(" ", 1)[0].lower() not in cond.lower():
        out.append(f"Nuclei were stained with {stain}.")
    mic, obj = c.get("microscope", ""), c.get("objective", "")
    if mic:
        out.append(f"Images were acquired on {_lc(mic)}"
                   + (f" with a {_objective(obj)}" if obj else "") + ".")
    elif obj:
        out.append(f"Images were acquired with a {_objective(obj)}.")
    analysis = c.get("analysis", "")
    if analysis:
        first = analysis.split(" ", 1)[0].strip(",;")
        if first[:1].isupper() and first.lower() not in _GENERIC_OPENERS:
            # A program ("ImageJ", "CellProfiler …") analyses; a list of
            # steps ("shading correction; …") is what analysis included.
            out.append(f"Images were analysed with {analysis}.")
        else:
            out.append(f"Image analysis included {_lc(analysis)}.")
    return out


def _fc(c: dict, label: str, amount: str) -> list[str]:
    out, loose = [], []
    cells, dyes = c.get("cell_lines", ""), c.get("tracker_dyes", "")
    if cells:
        out.append(f"The cell lines were {cells}"
                   + (f", labelled with {_lc(dyes)}" if dyes else "") + ".")
    elif dyes:
        out.append(f"Cells were labelled with {_lc(dyes)}.")
    steps = [
        _step("fixed in", c.get("fixation", ""), "Fixation", loose),
        _step("permeabilised with", c.get("permeabilisation", ""), "Permeabilisation", loose),
        _step("blocked with", c.get("blocking", ""), "Blocking", loose),
    ]
    live = c.get("live_or_fixed", "")
    if not any(steps) and not loose and live:
        out.append(f"Staining: {live}.")
    if any(steps):
        out.append(f"Cells were {_series(steps)}.")
    out.extend(loose)
    out.append(_primary(label, amount, []))
    out.append(_secondary(c.get("secondary_ab", ""), c.get("secondary_dilution", "")))
    cyt, sw = c.get("flow_cytometer", ""), c.get("analysis_software", "")
    if cyt and sw:
        out.append(f"Data were acquired on {cyt} and analysed with {sw}.")
    elif cyt:
        out.append(f"Data were acquired on {cyt}.")
    elif sw:
        out.append(f"Data were analysed with {sw}.")
    return out


def _ihc(c: dict, label: str, amount: str) -> list[str]:
    out, loose = [], []
    if c.get("tissue_or_cells"):
        out.append(f"Staining was performed on {c['tissue_or_cells']}.")
    fix = _step("fixed in", c.get("fixation", ""), "Fixation", loose)
    if fix:
        out.append(f"Samples were {fix}.")
    out.extend(loose)
    if c.get("antigen_retrieval"):
        out.append(f"Antigen retrieval used {_lc(c['antigen_retrieval'])}.")
    if c.get("blocking"):
        out.append(f"Sections were blocked with {_lc(c['blocking'])}.")
    out.append(_primary(label, amount, []))
    if c.get("detection_system"):
        out.append(f"Staining used {_lc(c['detection_system'])}.")
    if c.get("counterstain"):
        out.append(f"Sections were counterstained with {_lc(c['counterstain'])}.")
    if c.get("scanner"):
        out.append(f"Slides were scanned on {_lc(c['scanner'])}.")
    return out


# ── One primary, one secondary ──────────────────────────────────────────────
#
# A report lists every secondary its blots used — anti-rabbit *and*
# anti-mouse — and a paragraph about one rabbit antibody that names both reads
# as if both were used on it. So where the antibody's host is on file and the
# report names a secondary against that host, the paragraph names that one
# alone; anything it cannot place (no host, a host the report does not name, a
# reagent with no species such as Protein A:HRP) is left as the report put it.

#: Secondaries whose species a printed catalogue number settles, so a report
#: that printed two numbers in the wrong order still gets the right one.
SECONDARY_CATALOGUES = {
    "656120": ("goat", "rabbit"), "626520": ("goat", "mouse"),
    "a21429": ("goat", "rabbit"), "a21424": ("goat", "mouse"),
    "a21436": ("donkey", "sheep"), "a21434": ("goat", "rat"),
    "rgar001": ("goat", "rabbit"), "rgam001": ("goat", "mouse"),
    "rgar003": ("goat", "rabbit"), "rgam003": ("goat", "mouse"),
    "rgar005": ("goat", "rabbit"), "rgam005": ("goat", "mouse"),
    "a16041": ("donkey", "sheep"), "a15999": ("donkey", "goat"),
    "31470": ("goat", "rat"),
}
_SPECIES = r"rabbit|mouse|rat|goat|sheep|guinea[- ]pig|chicken|human"
_ANTI = re.compile(rf"\banti-({_SPECIES})\b", re.I)
_RAISED = re.compile(r"\b(goat|donkey|rabbit|mouse|sheep)\s*$", re.I)


def _host(value: str) -> str:
    v = (value or "").strip().lower().replace("-", " ")
    return v if re.fullmatch(_SPECIES.replace("[- ]", " "), v) else ""


def _species(text: str) -> str:
    m = _ANTI.search(text)
    return m.group(1).lower().replace("-", " ") if m else ""


def _catkey(token: str) -> str:
    return re.sub(r"[^a-z0-9]", "", token.lower())


def _top_split(text: str, seps=r";|\s/\s|,\s|\s+(?:and|or)\s+"):
    """Split on separators outside parentheses."""
    out, depth, start = [], 0, 0
    i = 0
    rx = re.compile(seps)
    while i < len(text):
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(depth - 1, 0)
        elif depth == 0:
            m = rx.match(text, i)
            if m and m.end() > i:
                out.append(text[start:i])
                start = i = m.end()
                continue
        i += 1
    out.append(text[start:])
    return [p.strip() for p in out if p.strip()]


def _parens(text: str) -> list[str]:
    return re.findall(r"\(([^()]*)\)", text)


def _split_supplier(paren: str):
    """``Thermo Fisher 65-6120`` → (``Thermo Fisher``, ``65-6120``); a paren
    with no catalogue-like token is all supplier."""
    words = paren.split()
    cats = [w for w in words if re.search(r"\d", w)]
    if not cats:
        return paren.strip(), ""
    first = words.index(cats[0])
    return " ".join(words[:first]).strip(), " ".join(words[first:]).strip()


def choose_secondary(text: str, host: str) -> str:
    """The one secondary of ``text`` raised against ``host``, or ``text``."""
    host = _host(host)
    if not host or not text:
        return text
    # "… secondary antibodies (Thermo A-21429 goat anti-rabbit, A-21424 goat
    # anti-mouse, …)": the list is inside one bracket.
    inside = re.fullmatch(r"([^()]*?)\s*\(([^()]*)\)\s*", text)
    if inside and len({_species(m.group(0)) for m in _ANTI.finditer(inside.group(2))}) > 1:
        elements = _top_split(inside.group(2), r",\s|\s+and\s+")
        mine = [e for e in elements if _species(e) == host]
        if len(mine) != 1:
            return text
        pick = mine[0]
        lead = elements[0].split(" ", 1)[0]
        if lead[:1].isalpha() and not re.search(r"\d", lead) and not pick.startswith(lead):
            pick = f"{lead} {pick}"  # the supplier, printed once for the list
        return f"{inside.group(1)} ({pick})"
    segments = [seg.strip() for seg in text.split(";") if seg.strip()]
    antibody = [seg for seg in segments if _ANTI.search(seg)]
    context = [seg for seg in segments if not _ANTI.search(seg)]
    whole = "; ".join(antibody)
    named = {_species(m.group(0)) for m in _ANTI.finditer(whole)}
    if len(named) < 2 or host not in named:
        return text
    items, trailing = [], []
    for part in _top_split(whole):
        if _ANTI.search(part) or not items:
            items.append(part)
            trailing = []
        elif not part.startswith("(") and not re.match(r"all\s", part, re.I):
            trailing.append(part)  # "…, in 150 µL of PBS with 1% BSA": about all of them
        else:
            items[-1] += " " + part
    tail_clause = ", ".join(trailing)
    chosen_i = next((i for i, it in enumerate(items) if _species(it) == host), None)
    if chosen_i is None:
        return text  # the species is named only inside a bracket
    chosen = items[chosen_i]

    # Every catalogue printed anywhere, and which species each one is.
    all_cats = [tok for p in _parens(whole) for tok in re.split(r",|\s+and\s+|\s", p)
                if re.search(r"\d", tok)]
    known = [tok for tok in all_cats
             if SECONDARY_CATALOGUES.get(_catkey(tok), ("", ""))[1] == host]

    # The words before "anti-": its own, or the first item's when it has none
    # ("… goat anti-rabbit (…) and anti-mouse (…)").
    first_prefix = items[0][:_ANTI.search(items[0]).start()].strip()
    own = chosen[:_ANTI.search(chosen).start()].strip()
    if not own:
        prefix = first_prefix
    elif _RAISED.fullmatch(own) and first_prefix.lower() != own.lower():
        conj = _RAISED.sub("", first_prefix).strip()
        prefix = f"{conj} {own}".strip()
    else:
        prefix = own
    m = _ANTI.search(chosen)
    head = f"{prefix} {m.group(0)}".strip()

    # Its catalogue: its own bracket, else the one the items around it share
    # ("anti-rabbit and anti-mouse (Thermo Fisher 65-6120 and 62-6520)").
    has_paren = [bool(_parens(it)) for it in items]
    supplier = cat = ""
    for p in _parens(chosen):
        sup, c = _split_supplier(p)
        supplier = supplier or sup
        if len([t for t in re.split(r",|\s+and\s+", c) if t.strip()]) == 1:
            cat = cat or c  # one number is its own; several are the list's
    if not cat:
        j = next((k for k in range(chosen_i, len(items)) if has_paren[k]), None)
        has_paren[chosen_i] = has_paren[chosen_i] and j != chosen_i
        start = next((k + 1 for k in range(chosen_i - 1, -1, -1) if has_paren[k]), 0)
        if j is not None:
            sup, c = _split_supplier(_parens(items[j])[0])
            supplier = supplier or sup
            cats = [t.strip() for t in re.split(r",|\s+and\s+", c) if t.strip()]
            # Read by position only where the printed order checks out: a
            # bracket whose known numbers sit against the wrong species was
            # printed out of order, and its unknown ones are left unsaid.
            in_order = len(cats) == j - start + 1 and all(
                SECONDARY_CATALOGUES.get(_catkey(c), ("", _species(items[start + k])))[1]
                == _species(items[start + k]) for k, c in enumerate(cats))
            if in_order:
                cat = cats[chosen_i - start]
    if not supplier:
        # Printed once for the whole list: in the first bracket, or "all X".
        for p in _parens(whole):
            sup, _ = _split_supplier(p)
            if sup and not _ANTI.search(sup):
                supplier = sup
                break
        m_all = re.search(r",\s*all\s+([A-Z][\w ]+?)\s*$", whole)
        supplier = supplier or (m_all.group(1) if m_all else "")
    if known:
        cat = known[0]
    cat = re.sub(r"\s*\bas printed\b", "", cat).strip()
    tail = " ".join(x for x in (supplier, cat) if x)
    out = f"{head} ({tail})" if tail else head
    if tail_clause:
        out += f", {tail_clause}"
    return "; ".join([out] + context)


def choose_dilution(text: str, host: str) -> str:
    """``0.05 µg/mL (anti-rabbit); 0.5 µg/mL (anti-mouse)`` → the host's."""
    host = _host(host)
    if not host or not text:
        return text
    parts = _top_split(text, r";|,\s")
    tagged = [(p, _species(p)) for p in parts]
    # "1 hr at room temperature" goes with whichever is chosen; an amount with
    # no species is for a reagent that has none (Protein A), so the list is
    # left as it is.
    context = [p for p, sp in tagged if not sp and not re.search(r"\d\s*(?:[µu]g|ng|mg|/)", p)]
    tagged = [(p, sp) for p, sp in tagged if sp or p not in context]
    if not tagged or not all(sp for _, sp in tagged):
        return text
    species = {sp for _, sp in tagged if sp}
    if len(species) == 1:
        # One secondary's concentration given: it is this antibody's, or it
        # says nothing about this antibody's.
        if host not in species:
            return "; ".join(context)
        mine = [p for p, _ in tagged]
    else:
        mine = [p for p, sp in tagged if sp == host]
        if len(mine) != 1:
            return text
    chosen = re.sub(r"\s*\((?:HRP\s+)?anti-[^()]*\)", "", mine[0]).strip()
    return "; ".join([chosen] + context)


_COMPOSERS = {"WB": _wb, "IP": _ip, "ICC-IF": _if, "FC": _fc, "IHC": _ihc}


def antibody_label(antibody) -> str:
    """"Abcam ab76040 (RRID:AB_1523361)" — the supplier as the public site
    names it, the catalogue number, and the RRID when there is one."""
    company = getattr(antibody, "company", None)
    supplier = (company.display_name or company.name) if company else ""
    rrid = (antibody.rrid or "").strip()
    if rrid and not rrid.upper().startswith("RRID:"):
        rrid = f"RRID:{rrid}"
    label = " ".join(p for p in (supplier, antibody.catalogue_number) if p)
    return f"{label} ({rrid})" if rrid else label


def paragraph(app: str, conditions: dict, label: str = "", amount: str = "",
              host: str = "") -> str:
    compose = _COMPOSERS.get(app)
    if not compose:
        return ""
    c = dict(conditions or {})
    for key in ("secondary_antibody", "secondary_ab"):
        if c.get(key):
            c[key] = choose_secondary(c[key], host)
    if c.get("secondary_dilution"):
        c["secondary_dilution"] = choose_dilution(c["secondary_dilution"], host)
    sentences = [s for s in compose(c, label, amount) if s]
    return " ".join(sentences)


def text(app: str, conditions: dict, label: str = "", amount: str = "",
         source: str = "", host: str = "") -> str:
    """The whole of what the button copies: a heading, the paragraph and, when
    there is one, the report it came from. ``host`` is the primary's host
    species, which picks its secondary out of the report's list."""
    body = paragraph(app, conditions, label, amount, host)
    if not body:
        return ""
    out = f"{HEADINGS[app]}\n{body}"
    if source:
        out += f"\nMethod as published in {source}."
    return out


# ── Reading ─────────────────────────────────────────────────────────────────

_TABLES = {"checked_at": 0.0, "ok": False}


def available() -> bool:
    """Whether the two tables exist yet.

    The MCP service runs this code with no migrate of its own, and the three
    cron jobs run a merge to `beta` before the Manual Deploy migrates, so a
    query here can arrive before the tables do. Asked of the catalogue, never
    by catching a failed query — on PostgreSQL that poisons the transaction.
    On PostgreSQL it also asks whether this role may *read* them. A "no" is
    re-asked every ten minutes; a "yes" is final.
    """
    if _TABLES["ok"]:
        return True
    now = time.monotonic()
    if _TABLES["checked_at"] and now - _TABLES["checked_at"] < 600:
        return False
    _TABLES["checked_at"] = now
    from django.db import connections, router
    from pipeline.models import AntibodyMethod, MethodsRecord
    alias = router.db_for_read(MethodsRecord)
    connection = connections[alias]
    tables = (MethodsRecord._meta.db_table, AntibodyMethod._meta.db_table)
    ok = set(tables) <= set(connection.introspection.table_names())
    if ok and connection.vendor == "postgresql":
        # Existing is not the same as readable: the MCP's `mcp_readonly` role
        # reads only the tables `mcp_servers/common/grants.py` names, and a
        # query on any other is "permission denied" — the 29 Aug 2026 outage.
        with connection.cursor() as cursor:
            cursor.execute("SELECT has_table_privilege(%s, 'SELECT') "
                           "AND has_table_privilege(%s, 'SELECT')", tables)
            ok = bool(cursor.fetchone()[0])
    _TABLES["ok"] = ok
    return ok


def conditions_of(record) -> dict:
    """A record's conditions as the current rules clean them. Cleaned again
    on read, so a rule added after a record was loaded (figure references,
    1 Oct 2026) reaches every paragraph without a reload. Memoised per record
    and per version of its stored conditions."""
    key = (record.pk, record.updated_at)
    hit = _CLEANED.get(key)
    if hit is None:
        hit = clean_conditions(record.application, record.conditions or {})
        if len(_CLEANED) > 2000:
            _CLEANED.clear()
        _CLEANED[key] = hit
    return hit


_CLEANED: dict = {}


def stamp() -> str:
    """A fingerprint of everything the paragraphs are made from — the count
    and newest change of both tables — for a caller holding an ETag
    (`core/api_manifest._fingerprint`). Empty before the tables exist."""
    if not available():
        return ""
    from django.db.models import Count, Max
    from pipeline.models import AntibodyMethod, MethodsRecord
    parts = []
    for model in (MethodsRecord, AntibodyMethod):
        agg = model.objects.aggregate(n=Count("id"), at=Max("updated_at"))
        parts.append(f"{agg['n']}@{agg['at'].isoformat() if agg['at'] else ''}")
    return "|".join(parts)


def source_for(reports) -> str:
    """The DOI the paragraph cites: the Zenodo report the methods were read
    from, the F1000 paper when there is no Zenodo record. ``reports`` is one
    gene's `Report` rows."""
    from pipeline.services import doi
    for report in sorted(reports, key=lambda r: (r.zenodo_date or r.f1000_date
                                                 or _EPOCH), reverse=True):
        for value in (report.zenodo_doi, report.f1000_doi):
            if doi.link(value):
                return doi.link(value)
    return ""


def for_antibodies(antibodies, axes=None, curated=None) -> dict:
    """``{antibody_id: {application: payload}}`` for every application where
    the antibody's published result is **supportive** and there is a methods
    record.

    **Offered only under a supportive result** (owner, 30 Sep 2026): a methods
    paragraph beside *Not supportive* or *Limited support* reads as an
    invitation to repeat an experiment that did not support the antibody. The
    rows are stored for every published antibody regardless, and this asks the
    verdict at read time (`core/recommendations.describe`, the one reader) —
    so a result regraded upwards gets its button on the next page render, with
    nothing to reload or re-run.

    Six queries whatever the number of antibodies — figures, records,
    per-antibody values, reports, and the verdict's two inputs — or four when
    the caller already holds ``axes`` (`capability_axes`) and ``curated``
    (`curated_gene_ids`). ``payload`` is ``text`` (what the button copies),
    ``amount``, ``basis``, ``source`` and ``conditions``.
    """
    antibodies = list(antibodies)
    if not antibodies or not available():
        return {}
    from pipeline.models import (AntibodyMethod, MethodsRecord,
                                 PublicationImage, Report)
    ids = [a.pk for a in antibodies]
    target_ids = {a.target_id for a in antibodies}
    figures = set(PublicationImage.objects.filter(antibody_id__in=ids)
                  .values_list("antibody_id", "application_type"))
    if not figures:
        return {}
    records = {(r.target_id, r.application): r
               for r in MethodsRecord.objects.filter(target_id__in=target_ids)}
    if not records:
        return {}
    values = {}
    for m in (AntibodyMethod.objects.filter(antibody_id__in=ids)
              .select_related("record").order_by("updated_at", "id")):
        values[(m.antibody_id, m.record.application)] = m
    reports = {}
    for r in Report.objects.filter(target_id__in=target_ids):
        reports.setdefault(r.target_id, []).append(r)
    sources = {t: source_for(rs) for t, rs in reports.items()}

    from core import recommendations as R
    if curated is None:
        curated = R.curated_gene_ids(target_ids)
    if axes is None:
        axes = R.capability_axes(ids)
    tested = {}
    for ab_id, app in figures:
        tested.setdefault(ab_id, set()).add(app)

    out = {}
    for ab in antibodies:
        for app in APPLICATIONS:
            if (ab.pk, app) not in figures:
                continue
            record = records.get((ab.target_id, app))
            if record is None:
                continue
            support = R.describe(ab, app, tested[ab.pk], ab.target_id in curated,
                                 axes.get((ab.pk, app)))["support"]
            if support != R.SUPPORTIVE:
                continue
            value = values.get((ab.pk, app))
            amount = value.amount if value else ""
            source = sources.get(ab.target_id, "")
            body = text(app, conditions_of(record), antibody_label(ab), amount, source,
                        ab.host_species)
            if not body:
                continue
            out.setdefault(ab.pk, {})[app] = {
                "text": body,
                "amount": amount or None,
                "basis": (value.basis or None) if value else None,
                "source": source or None,
                "conditions": conditions_of(record),
            }
    return out

