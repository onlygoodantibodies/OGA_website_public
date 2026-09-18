"""Antibodies whose declared target is not the target people buy them for.

**This module holds no opinion about how well any antibody works.** It answers a
different question, one step before the verdicts: *is this an antibody to the
protein the paper is talking about at all?* A supplier's own datasheet says what
the reagent was raised against, and where a published review has documented that
the reagent is routinely bought for a different protein with a similar name, that
is a fact about identity — not about performance, and nothing here touches a
recommendation.

It exists because the two reader-facing tools had nothing to say about the case,
and one of them said something actively wrong. ``ab51243`` is an ARPC5 (p16-ARC)
antibody; 317 of the 406 papers on Sholto David's list used it as a p16-INK4a
(CDKN2A) antibody, including papers in Nature, Nature Medicine, Cancer Cell and
eLife. None of the product codes on any of the lists is in the OGA
dataset — checked, 12 Sep 2026 — so the MCP filed every one of them under
``not_in_dataset``, whose standing note tells the reader that absence "is
untested, NOT unreliable; absence is not a verdict on quality". True of an
untested antibody and misleading here: the reader is not waiting on evidence
about this reagent, they are looking at a reagent raised against another protein.

WHAT IS A NOTICE, AND WHAT IS NOT
---------------------------------
A notice is **the supplier's declared target, plus a published list of papers**.
Both halves are required, and they answer different questions:

* the **product** half is true on any page, including the supplier's own, and is
  a restatement of what the manufacturer says the antibody binds;
* the **paper** half says what the review found *for that paper* — the half that
  can say "may have used the wrong antibody" — and is keyed by whichever of a
  **DOI**, a **PubMed id** or a **title and year** the page in front of the
  reader declares. It was the DOI alone until 0.4.2; see ``paper_keys``.

THE PAPER VERDICT IS THREE-VALUED AND THE MIDDLE ONE IS NOT A HEDGE
-------------------------------------------------------------------
``mistaken``     the review records this paper as using it against the mistaken
                 target.
``as_declared``  the review records this paper as using it correctly, for the
                 target it is sold against. **Seventeen of the p16 papers are
                 these**, and telling their authors they used the wrong antibody
                 would be a false accusation against a competent paper — the same
                 shape as ``present_unlinked`` in the controls rubric. The tools
                 draw NO mark on these.
``not_checked``  on the list, full text not reachable, so the reviewer could not
                 say. Seventy-two of the p16 papers. Named as unknown, never
                 folded into ``mistaken``.

A paper that is not on a list at all is a fourth state and belongs to the tools,
not here: the product fact still holds, and the strongest thing either tool may
say is *may have*.

ONE READER, TWO TOOLS
---------------------
``core/extension_index.py`` folds ``index_payload()`` into the snapshot every
install downloads; ``mcp_servers/common/confusions.py`` reads the same functions.
That is the same arrangement as ``core/citations.py`` and for the same reason —
the extension and the connector must not answer one question differently. DOIs
are keyed through ``citations.normalise_doi``, which is already mirrored in
``browser-extension/src/paper.js``, so the key a page computes is the key stored
here.

ADDING A THIRD LIST IS DATA
---------------------------
A JSON definition and a CSV in ``core/data/target_confusions/``, and nothing in
any of the three codebases changes. Sholto's articles say a third is coming.

WHAT IS DELIBERATELY LEFT OUT
-----------------------------
Codes named in the prose of the articles but carrying no paper list: ``ab118459``
and ``ab220800`` (ARPC5), ``sc-166630`` (p21-ARC, a different confusion of the
same shape) and ``PA1-30670`` (p19-ARF, which shares the CDKN2A *gene* but is a
different reading frame). The product fact is probably true of each; the evidence
here is a list with DOIs in it, and these have none, so they wait for the source
that gives them one.
"""

from __future__ import annotations

import csv
import json
from functools import lru_cache
from pathlib import Path

from .citations import normalise_doi, title_key

DATA_DIR = Path(__file__).resolve().parent / "data" / "target_confusions"

#: The extension release whose code first DRAWS a notice. The data reaches every
#: install on a site deploy, but a build that has never heard of
#: ``target_confusions`` ignores it, so nothing is visible to a reader until this
#: version clears store review.
#:
#: Named once, here, because ``/extension/`` advertises the feature as coming and
#: **that page becomes false the day the stores approve**. ``core.W006`` compares
#: this against the version actually being served and says so, rather than
#: leaving a public page to rot into a promise about something that arrived weeks
#: ago. Same shape as ``core.W001``/``W002`` for the signed Firefox build.
EXTENSION_RELEASE = "0.4.0"

#: Bumped when the shape of ``index_payload`` changes in a way a shipped client
#: could not read. The extension gates on ``index.json``'s own ``schema``, which
#: does NOT move for this — the key is additive and a build that has never heard
#: of it ignores it — so this is the finer-grained one, read by the client before
#: it trusts the contents.
SCHEMA = 1

#: The whole vocabulary, spelled the same in Python, in the snapshot, in the
#: extension's JavaScript and in the connector's replies. Deliberately words
#: rather than the small integers the verdict codes use: there are a few hundred
#: rows, not a megabyte, and a code needs a mapping table at each end — which is
#: four chances for two tools to disagree about what the middle value means.
MISTAKEN = "mistaken"
AS_DECLARED = "as_declared"
NOT_CHECKED = "not_checked"
VERDICTS = (MISTAKEN, AS_DECLARED, NOT_CHECKED)

#: What a reviewer's spreadsheet column says, mapped onto the vocabulary above.
#: Anything else is dropped and COUNTED (see ``problems``), never guessed at: a
#: verdict nobody recognised must not become the strongest one by default.
_USE_WORDS = {
    "wrong": MISTAKEN,
    "mistaken": MISTAKEN,
    "correct": AS_DECLARED,
    "as declared": AS_DECLARED,
    "as_declared": AS_DECLARED,
    "cant check": NOT_CHECKED,
    "can't check": NOT_CHECKED,
    "cannot check": NOT_CHECKED,
    "not checked": NOT_CHECKED,
    "unknown": NOT_CHECKED,
}

#: Below this, an alphanumerics-only identifier is too easily another supplier's
#: catalogue number. Mirrors ``manuscript.MIN_COLLAPSED`` and
#: ``matcher.js::MIN_COLLAPSED`` — the same number for the same reason.
MIN_COLLAPSED = 5


def _key(identifier):
    """The lookup key for a product code: trimmed and case-folded.

    Case is not part of this. Suppliers print ``ab9361`` and papers print
    ``AB9361``; both are the same product, and the extension's ``normaliseKey``
    already folds case on the catalogue map beside this one.
    """
    return (identifier or "").strip().lower()


def _collapsed(identifier):
    """Alphanumerics only, or ``""`` when that is too short to be safe.

    The form that survives a publisher moving punctuation — ``14-6773-81`` set as
    ``14 6773 81``. Same rule as the catalogue lookup in both tools.
    """
    text = "".join(ch for ch in (identifier or "") if ch.isalnum()).lower()
    return text if len(text) >= MIN_COLLAPSED else ""


#: How far a declared publication year may be from the listed one and still be
#: the same paper. Online-first and print dates routinely differ by one, and
#: rejecting a real match over that would be a defect of our own making. Mirrors
#: ``paper.js::YEAR_SLACK``, which the citation layer has used since it shipped —
#: the same number for the same reason, like ``MIN_COLLAPSED`` above.
YEAR_SLACK = 1


def _year(value):
    """A four-digit year from a spreadsheet cell, or ``None``.

    Never raises and never guesses: a cell holding ``2019 (online 2018)`` gives
    2019 and a cell holding prose gives nothing, which costs the row its title
    key and leaves its DOI and PMID keys untouched.
    """
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())[:4]
    return int(digits) if len(digits) == 4 else None


def paper_keys(doi=None, pmid=None, title=None, year=None):
    """Every key a paper can be looked up under, most certain first.

    THREE KEYS, BECAUSE NO ONE KEY IS ON EVERY PAGE — the same three
    ``paper.js`` has always used for the citation record, in the same order and
    with the same normalisers, because "which paper is this" is one question and
    must not get two answers.

    * **DOI** — the precise key on an ordinary publisher page, and the only one
      these lists used until 0.4.2. Emitted bare, so every key already in the
      snapshot keeps its exact spelling and a shipped client reading
      ``papers[doi]`` is unaffected.
    * **PMID** — exact, and free on pubmed.ncbi.nlm.nih.gov, PMC and
      europepmc.org, where it is in the URL. It is what reaches a paper whose
      publisher registers no DOI at all: four of the PERK papers are e-Century
      titles, which Crossref has no record of, so before this they were listed
      and unreachable.
    * **title + year** — the fallback that works on a publisher's own page,
      which is where a reader usually is and which rarely declares a PMID. The
      year is part of the key rather than a separate check, so a title that
      matches with the wrong year simply finds nothing; the caller widens by
      ``YEAR_SLACK`` rather than this guessing.

    A title alone is refused. A title is a string two papers can share, and the
    year is what confirms it — the rule ``paper.js::confirmPaper`` enforces for
    the citation layer, kept here rather than reinvented.
    """
    keys = []
    doi = normalise_doi(doi)
    if doi:
        keys.append(doi)
    digits = "".join(ch for ch in str(pmid or "") if ch.isdigit())
    if digits:
        keys.append(f"pmid:{digits}")
    folded = title_key(title) if title else ""
    if folded and year:
        keys.append(f"title:{folded}:{int(year)}")
    return keys


def _lookup_keys(doi=None, pmid=None, title=None, year=None):
    """``paper_keys`` widened by ``YEAR_SLACK`` on the title key.

    The asymmetry is deliberate and is the whole of the year rule: a LIST row is
    stored under the one year the reviewer recorded, and a PAGE is looked up
    under the three years it could honestly be. Storing three would put a
    tolerance into the data, where it reads as three different papers.
    """
    keys = paper_keys(doi=doi, pmid=pmid)
    folded = title_key(title) if title else ""
    if folded and year:
        for offset in range(-YEAR_SLACK, YEAR_SLACK + 1):
            keys.append(f"title:{folded}:{int(year) + offset}")
    return keys


def _read_papers(path, notice_id, problems):
    """``{paper_key: {identifier_key: verdict}}`` from one reviewer's list.

    A row may identify its paper by ``doi``, by ``pmid``, or by ``title`` and
    ``year`` — see ``paper_keys`` — and a row carrying more than one is stored
    under each, because which of them a page declares is a fact about the
    publisher and not about the paper. Until 0.4.2 only ``doi`` was read, and a
    row whose journal registers none was dropped: four of the PERK papers, every
    one of them established by the reviewer and unreachable by the tools.

    A row this cannot use is appended to ``problems`` and left out. Counting the
    losses is the point: a list that silently shrank would make a documented
    paper read as a paper nobody had looked at, which is the direction that costs
    something.
    """
    papers, by_paper = {}, {}
    try:
        with path.open(encoding="utf-8", newline="") as fh:
            for line, row in enumerate(csv.DictReader(fh), start=2):
                keys = paper_keys(doi=row.get("doi"), pmid=row.get("pmid"),
                                  title=row.get("title"),
                                  year=_year(row.get("year")))
                identifier = _key(row.get("identifier"))
                raw_use = (row.get("use") or "").strip().lower()
                verdict = _USE_WORDS.get(raw_use)
                if not keys or not identifier:
                    problems.append(
                        f"{notice_id}: {path.name} line {line} has no "
                        + ("product code" if keys else
                           "DOI, PubMed id, or title and year"))
                    continue
                if verdict is None:
                    problems.append(
                        f"{notice_id}: {path.name} line {line} records the use as "
                        f"{raw_use!r}, which is not one of {', '.join(sorted(set(_USE_WORDS)))}")
                    continue
                # COUNTED ONCE PER PAPER, WHATEVER IT IS KEYED UNDER AND HOWEVER
                # MANY CODES IT NAMES. `papers` is a lookup index: a row keyed by
                # both a DOI and a PMID occupies two slots, and a paper naming
                # two products contributes two verdicts. Counting either would
                # print a number bigger than the review's own — "191 papers" over
                # a review that says 187, on the card, under the review's link.
                # `keys[0]` is the row's most certain identifier and so its
                # identity here.
                by_paper.setdefault(keys[0], {})
                for key in keys:
                    entry = papers.setdefault(key, {})
                    # A paper listed twice for one product code keeps the stronger
                    # claim's OPPOSITE: `not_checked` never overwrites a verdict
                    # somebody reached, and `as_declared` is never overwritten by
                    # `mistaken`, because a reviewer who read the paper and found a
                    # correct use outranks a duplicate row.
                    if entry.get(identifier) in (MISTAKEN, AS_DECLARED):
                        continue
                    entry[identifier] = verdict
                if by_paper[keys[0]].get(identifier) not in (MISTAKEN, AS_DECLARED):
                    by_paper[keys[0]][identifier] = verdict
    except OSError as exc:
        problems.append(f"{notice_id}: {path.name} could not be read ({exc.strerror})")
    tally = {v: 0 for v in VERDICTS}
    tally["papers"] = len(by_paper)
    for verdicts in by_paper.values():
        for verdict in set(verdicts.values()):
            tally[verdict] += 1
    return papers, tally


@lru_cache(maxsize=1)
def _loaded():
    """``(notices, problems)``. Never raises; a bad file costs its own notice."""
    notices, problems = [], []
    if not DATA_DIR.is_dir():
        return (), ()
    for path in sorted(DATA_DIR.glob("*.json")):
        try:
            with path.open(encoding="utf-8") as fh:
                notice = json.load(fh)
        except (OSError, ValueError) as exc:
            problems.append(f"{path.name} could not be read ({exc})")
            continue
        missing = [f for f in ("id", "declared_target", "mistaken_for", "short",
                               "source", "antibodies") if not notice.get(f)]
        if missing:
            problems.append(f"{path.name} is missing {', '.join(missing)}")
            continue
        if not (notice.get("source") or {}).get("url"):
            # The link is the whole of the reader's way to check this, so a
            # notice without one is not servable.
            problems.append(f"{notice['id']}: the source carries no url")
            continue
        # Mark only a paper this list names, never a page that merely mentions
        # one of the codes. See `papers_only` in `index_payload`.
        notice["papers_only"] = bool(notice.get("papers_only"))
        papers_file = notice.get("papers_file")
        notice["papers"], notice["paper_tally"] = (
            _read_papers(path.parent / papers_file, notice["id"], problems)
            if papers_file else ({}, {v: 0 for v in VERDICTS} | {"papers": 0}))
        notice["antibodies"] = [a for a in notice["antibodies"] if a.get("identifier")]
        if not notice["antibodies"]:
            problems.append(f"{notice['id']}: no product codes")
            continue
        notices.append(notice)
    return tuple(notices), tuple(problems)


def load():
    """Every notice on file, or ``()``.

    An empty answer is a real one and both tools render it as nothing at all —
    which is exactly the behaviour they had before this existed, so a missing or
    unreadable data directory costs the feature and never a reply.
    """
    return _loaded()[0]


def public_totals():
    """The figures ``/extension/`` quotes about the lists, counted rather than typed.

    The page carried "eleven product codes across the two reviews" and was
    shipped beside four lists and twenty-nine codes three days later. That is the
    shape of every stale figure on this site: right on the day it was written,
    quoted for a year afterwards, and nothing on the page to contradict it. The
    same reason the hero's two numbers are derived.

    ``reviews`` counts distinct **sources**, not notices, because a collision
    that runs both ways is two notices out of one article — a notice holds one
    ``declared_target`` — and a reader counting articles would get four.
    """
    notices = load()
    return {
        "codes": sum(len(n["antibodies"]) for n in notices),
        "lists": len(notices),
        "reviews": len({(n.get("source") or {}).get("url") for n in notices}),
    }


def problems():
    """Rows and files ``load()`` could not use, as sentences.

    Surfaced by ``core.W005`` rather than raised: a system check runs inside the
    pre-deploy ``migrate``, and an error there takes the site down over a
    reference file.
    """
    return _loaded()[1]


@lru_cache(maxsize=1)
def _by_identifier():
    """``({key: (notice, antibody)}, {collapsed: (notice, antibody)})``.

    The collapsed map is built second and first-key-wins, so it can only ever ADD
    a match after the exact form has failed — the same ordering the catalogue
    lookup uses in both tools.
    """
    exact, collapsed = {}, {}
    for notice in load():
        for antibody in notice["antibodies"]:
            key = _key(antibody["identifier"])
            exact.setdefault(key, (notice, antibody))
            folded = _collapsed(antibody["identifier"])
            if folded:
                collapsed.setdefault(folded, (notice, antibody))
    return exact, collapsed


def for_identifier(identifier):
    """``(notice, antibody)`` for one product code, or ``(None, None)``.

    Exact case-folded form first, then punctuation-insensitive. Callers holding
    several spellings of one identifier (the connector's
    ``manuscript.resolution_variants``) should ask about each in their own
    most-certain-first order rather than expecting this to guess.
    """
    exact, collapsed = _by_identifier()
    hit = exact.get(_key(identifier))
    if hit is None:
        folded = _collapsed(identifier)
        hit = collapsed.get(folded) if folded else None
    return hit if hit is not None else (None, None)


def verdict_for(doi, identifier, *, pmid=None, title=None, year=None):
    """What a list records for one paper and one product code, or ``None``.

    ``None`` means *this pair is not on a list*, which is not a verdict and must
    never be drawn as one. It covers both "the paper is on no list" and "the paper
    is listed for a different product".

    The paper may be named any of the three ways ``paper_keys`` describes and the
    first that hits wins, most certain first. ``doi`` stays positional because
    that is how every caller written before 0.4.2 asks.
    """
    notice, antibody = for_identifier(identifier)
    if notice is None:
        return None
    wanted = _key(antibody["identifier"])
    for key in _lookup_keys(doi=doi, pmid=pmid, title=title, year=year):
        found = (notice["papers"].get(key) or {}).get(wanted)
        if found is not None:
            return found
    return None


def for_paper(doi, *, pmid=None, title=None, year=None):
    """Every (notice, product code, verdict) a list records for one paper.

    Answers without being handed an antibody, so a caller that has only a paper
    can still be told it is documented — and which reagent the list names. Any of
    the three identifiers will do; within one notice the first key that hits is
    used, so a paper listed under both a DOI and a PMID is reported once.
    """
    keys = _lookup_keys(doi=doi, pmid=pmid, title=title, year=year)
    if not keys:
        return []
    found = []
    for notice in load():
        verdicts = next((notice["papers"][k] for k in keys if k in notice["papers"]), {})
        for identifier, verdict in sorted(verdicts.items()):
            declared = next((a for a in notice["antibodies"]
                             if _key(a["identifier"]) == identifier), None)
            found.append({
                "notice": notice,
                # The supplier's spelling, not the reviewer's: the reviewer's
                # sheets disagree about case on the same product (`AB9361` and
                # `ab9361`), and what a reader will search for is the one on the
                # datasheet.
                "identifier": declared["identifier"] if declared else identifier,
                "supplier": (declared or {}).get("supplier"),
                "verdict": verdict,
            })
    return found


_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")


def printed_date(value):
    """``2026-06-02`` -> ``2 June 2026``, or the value back unchanged.

    Stored ISO because that is the one unambiguous way to write a date down, and
    printed long because this sentence is a citation on a hover card and in a
    reply a model quotes. ``%-d %B %Y`` would do it and is not portable, so the
    month table is spelled out. Anything that does not parse is passed through
    rather than dropped -- a date we cannot reformat is still a date.
    """
    try:
        year, month, day = (int(part) for part in str(value).split("-", 2))
        return f"{day} {_MONTHS[month - 1]} {year}"
    except (AttributeError, IndexError, TypeError, ValueError):
        return value or ""


def source_sentence(notice):
    """One sentence naming who documented this and when."""
    source = notice.get("source") or {}
    parts = [p for p in (source.get("author"), source.get("publication")) if p]
    who = ", ".join(parts)
    when = printed_date(source.get("date"))
    return f"{source.get('title')} — {who}, {when}" if who else source.get("title", "")


def paper_counts(notice):
    """``{verdict: n}`` over the notice's list, plus ``papers``.

    The totals a surface quotes. Counted here so the extension, the connector and
    any page all quote one number.

    Read off the tally the reader built, NOT off ``notice["papers"]``, which is a
    lookup index: since 0.4.2 one row can sit under a DOI key and a PMID key at
    once, so counting that map counts some papers twice — and the figure it
    feeds is the "a published review found N papers" line on the card.
    """
    return dict(notice.get("paper_tally") or {v: 0 for v in VERDICTS} | {"papers": 0})


def summary():
    """What a human needs to know about which notices are deployed."""
    return [
        {"id": n["id"],
         "declared_target": n["declared_target"],
         "mistaken_for": n["mistaken_for"],
         "product_codes": [a["identifier"] for a in n["antibodies"]],
         "source": n["source"],
         "counts": paper_counts(n)}
        for n in load()
    ]


@lru_cache(maxsize=1)
def index_payload():
    """The part of a notice the browser extension needs, or ``None``.

    Three maps rather than one nested structure, because the client does three
    different lookups: a product code it found in the text, a DOI it read out of
    the page's metadata, and the wording to draw. Keys are sorted so the snapshot
    stays byte-identical while the data is unchanged — the property
    ``extension_index._dataset_stamp`` exists to protect, and this file is read
    from disk so its content only moves on a deploy.

    ``None`` when nothing is on file, so the key is absent from the snapshot
    entirely rather than present and empty. An empty table and a missing one are
    the same to a client, but a reader looking at the JSON should be able to tell
    "no notices are deployed" from "notices are deployed and this paper is clean".
    """
    notices = load()
    if not notices:
        return None
    kinds, antibodies, papers = {}, {}, {}
    for notice in notices:
        declared, mistaken = notice["declared_target"], notice["mistaken_for"]
        kinds[notice["id"]] = {
            # Pre-composed, because both halves are drawn in one phrase on a
            # 330px card and joining two fields in JavaScript is a second place
            # for the wording to live.
            "declared": f"{declared['gene']} ({declared['protein']})",
            "mistaken": f"{mistaken['gene']} ({mistaken['protein']})",
            "declared_protein": declared["protein"],
            "mistaken_protein": mistaken["protein"],
            "short": notice["short"],
            "url": notice["source"]["url"],
            "source": source_sentence(notice),
            "mistaken_papers": paper_counts(notice)[MISTAKEN],
        }
        if notice["papers_only"]:
            # THE UNLISTED-PAPER FALLBACK IS A BET ON THE BASE RATE, and this
            # turns it off for a list where the bet loses.
            #
            # A notice is looked up by the product code, so by default a page
            # naming one of these codes draws a mark whatever paper it is, and
            # the card leads with "This paper MAY have used the wrong antibody"
            # plus the review's tally. That is a fair caution where the product
            # is mostly misused: 317 of the 406 p16 papers used `ab51243` as a
            # p16-INK4a antibody, so on an unlisted paper the odds are with the
            # warning.
            #
            # For PERK it is the other way round. `ab65142` is a perfectly good
            # PERK antibody and the overwhelming majority of papers citing it
            # used it correctly for the unfolded protein response — so marking
            # every one of them would be a false accusation against a correct
            # paper, which is the failure `as_declared` exists to prevent,
            # arriving through a different door. 187 wrong papers is a lot in
            # absolute terms and a small fraction of that product's use.
            #
            # So the flag is per notice and not a global change of behaviour:
            # the p16 and beta-galactosidase lists keep the caution they were
            # built with, on the owner's decision that naming that collision
            # helps on any page.
            kinds[notice["id"]]["papers_only"] = True
        for antibody in notice["antibodies"]:
            # The supplier's own spelling, carried on the entry as well as being
            # the map key: the key is case-folded, and the paper lists are keyed
            # the same way, so a client that matched by the punctuation-
            # insensitive form still needs the canonical key to look a DOI up.
            entry = {"kind": notice["id"], "id": antibody["identifier"]}
            if antibody.get("supplier"):
                entry["supplier"] = antibody["supplier"]
            if antibody.get("supplier_statement"):
                entry["statement"] = antibody["supplier_statement"]
            if antibody.get("supplier_required"):
                # The client must see the SUPPLIER's name beside the number
                # before marking it. Set on the codes whose shape belongs to no
                # one supplier — Millipore's `AB986` is typeset exactly like an
                # Abcam number, and marking an Abcam product with a Millipore
                # notice would be a claim about the wrong reagent. The cues
                # are what a paper actually PRINTS, which is rarely the
                # supplier's full legal name: `Invitrogen` and `Thermo` both
                # mean Thermo Fisher Scientific, and testing the stored string
                # would recognise neither.
                entry["supplier_required"] = True
                entry["cues"] = sorted(
                    {c.strip().lower()
                     for c in (antibody.get("supplier_cues") or []) if c.strip()}
                    or {(antibody.get("supplier") or "").strip().lower()})
            antibodies[_key(antibody["identifier"])] = entry
            # AND UNDER THE KEY A PAGE WOULD ACTUALLY PRODUCE.
            #
            # Cell Signaling prints its catalogue numbers `#3179`, and that is
            # the spelling on the datasheet and so the one stored — but
            # `matcher.js::TOKEN_RE` begins `[A-Za-z0-9]`, so a page saying
            # "#3179" only ever yields the token `3179`, which matched nothing.
            # The punctuation-insensitive map could not save it either: the
            # collapsed form is four characters, below `MIN_COLLAPSED`. So all
            # three Cell Signaling codes sat in the payload unreachable, which
            # is this file's own failure shape — the data is there and nothing
            # can draw it.
            #
            # ONLY FOR A CODE WHOSE SUPPLIER MUST BE NAMED, which is what makes
            # a four-digit key safe: `MIN_COLLAPSED` exists because a short
            # number belongs to every supplier at once, and `supplier_required`
            # answers exactly that by demanding the manufacturer's name beside
            # it before anything is marked. `entry["id"]` keeps the supplier's
            # own spelling, so the card still prints `#3179`.
            #
            # ONLY WHERE THE COLLAPSED MAP CANNOT ALREADY DO IT. Thermo's
            # `A-11132` needs nothing: the token keeps its dash, and its
            # collapsed form is seven characters, so `A11132` on a page resolves
            # through the punctuation-insensitive map that exists for exactly
            # that. It is the codes BELOW `MIN_COLLAPSED` that fall through
            # both, which is the three CST numbers and nothing else today.
            bare = _key("".join(ch for ch in antibody["identifier"]
                                if ch.isalnum()))
            if (antibody.get("supplier_required") and bare
                    and len(bare) < MIN_COLLAPSED and bare not in antibodies):
                antibodies[bare] = entry
        for doi, verdicts in notice["papers"].items():
            slot = papers.setdefault(doi, {})
            for identifier, verdict in verdicts.items():
                slot[identifier] = verdict
    return {
        "schema": SCHEMA,
        "kinds": {k: kinds[k] for k in sorted(kinds)},
        "antibodies": {k: antibodies[k] for k in sorted(antibodies)},
        "papers": {d: {i: papers[d][i] for i in sorted(papers[d])}
                   for d in sorted(papers)},
    }
