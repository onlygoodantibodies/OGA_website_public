"""Server A — read-only, per-antibody / per-gene lookups over the live pipeline.

Trust level: LOW. Lets an assistant answer FACTUAL questions about a specific
antibody or a specific gene ("is this paper's antibody in the dataset, and
how did it do for WB?", "what does the data support for SNCA?"). It can only
read.

**Antibody data only.** This connector exposes the per-antibody / per-gene data
tools below and nothing else. The interactive-education (tutor) tools once shared
this endpoint via ``server_a2_academy.register_tools``; that registration was
removed while the conversational tutor is reworked, so the public connector is now
the antibody DATABASE only. The tutor implementation is retained in
``server_a2_academy`` and can be re-attached here when it's ready.

**Scope by policy — no whole-database analytics.** The tools answer per-antibody
and per-gene questions only. There is deliberately NO free-form SQL tool and NO
cross-gene antibody list, so the server cannot be used to compare vendors by how
often their antibodies are supported across the database. Looking up a single
antibody (e.g. one cited in a paper) is fully supported; whole-DB benchmarking is
not. ``check_manuscript`` only resolves the identifiers and genes that appear in
the pasted text, so it is a convenience over the per-antibody lookups — not a way
to enumerate the database.

**Output matches the public data portal / website exactly**, plus a factual
interpretation layer. The structured tools serialise through the SAME code the
public API uses (``core/api_views.py::_serialise_antibody`` etc., via
``common/portal.py``), then add a controlled-vocabulary, provenance-anchored
result per application — supportive / limited_support / not_supportive /
not_tested — so a connected assistant reports facts, never inferences. The
three-value `status` is still carried beside it for callers written against it,
and it cannot express the middle rung.

The guarantee is at the DATABASE, not the prompt:
  * In production the owner runs ``roles.sql`` → a dedicated ``mcp_readonly``
    role: ``GRANT SELECT`` on the lab tables only, ``default_transaction_read_only
    = on``, a ``statement_timeout``, and NO access to ``auth_user``.
  * This process runs the Django ORM with ``pipeline_db`` pointed at that role's
    DSN (``MCP_READONLY_DATABASE_URL``), so every query is SELECT-only.
  * The PUBLIC boundary is the app-level filter ``publication_images__isnull=
    False`` — the exact filter the public API applies. Server A only ever returns
    antibodies with a published figure (and the targets that own them).

Locally (no env var set) it connects to the SQLite ``pipeline_db`` fallback.
"""
from __future__ import annotations

import os
from typing import List, Optional

from mcp_servers.common import audit, content_pack, portal, tutor_guidance
from mcp_servers.common.controls_rubric import (CONTROLS_RUBRIC,
                                                CONTROLS_RUBRIC_VERSION)

ROW_CAP = int(os.environ.get("MCP_A_ROW_CAP", "500"))


def build_server(auth_settings=None, token_verifier=None, http_path=None,
                 transport_security=None):
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    # Every tool here is a pure read of a fixed internal dataset — declare that
    # (title + readOnlyHint) so clients and the connector-directory review can see
    # it. openWorldHint=False: the "world" is the closed OGA database, not the web.
    # Title is set both top-level and in annotations so any client reads it.
    def _ro(title):
        return dict(title=title, annotations=ToolAnnotations(
            title=title, readOnlyHint=True, idempotentHint=True,
            openWorldHint=False))

    # Boot Django (pipeline_db → the read-only role) so the tools can serialise
    # through the canonical public API code.
    portal.setup()

    kwargs = {}
    if http_path:
        kwargs["streamable_http_path"] = http_path
    if auth_settings is not None:
        kwargs["auth"] = auth_settings
        kwargs["token_verifier"] = token_verifier
    if transport_security is not None:
        kwargs["transport_security"] = transport_security
    # `instructions` nudges a connecting BYO client (claude.ai / ChatGPT / …) toward
    # the same natural, grounded behaviour as the hosted chat — one shared source.
    # Data-only: this connector exposes the antibody database, not the tutor.
    #
    # **`stateless_http` is what lets the service run on more than one instance.**
    # FastMCP's streamable-HTTP transport otherwise keeps session state in the
    # instance's own memory, keyed by `Mcp-Session-Id`, and Render load-balances
    # without session affinity — so a second instance answers a client that
    # initialised on the first with "session not found". That trades the rare
    # outage a second instance is there to prevent for constant intermittent
    # breakage for everybody, which is the worse of the two. It is safe here
    # because every tool is a pure read of a fixed dataset and holds nothing
    # between calls: there is no session state to lose. The cost is the optional
    # GET SSE stream, which a stateless server does not offer and the spec lets
    # a server decline. **So a tool must not start keeping state between calls** —
    # doing so would work perfectly on one instance and fail on two.
    mcp = FastMCP("oga-pipeline-readonly",
                  instructions=tutor_guidance.DATA_ONLY_INSTRUCTIONS,
                  stateless_http=True, **kwargs)

    @mcp.tool(**_ro("Check a paper's antibodies against the OGA dataset"))
    def check_manuscript(reagents: List[dict], genes: Optional[List[str]] = None,
                         paper_doi: Optional[str] = None,
                         paper_pmid: Optional[str] = None,
                         paper_title: Optional[str] = None,
                         paper_year: Optional[int] = None) -> dict:
        """Look up the antibodies a paper uses in YCharOS's independent,
        knockout-controlled dataset, and report how each performed.

        YOU read the paper; this resolves what you found. There is no server-side
        text parsing — you identify the reagents and pass them. That division is
        deliberate: a model reading a manuscript spots catalogue numbers of any
        format, keeps a target name whole, and can say what a reagent IS, none of
        which pattern matching did reliably.

        ``reagents`` — one dict per reagent the paper uses:

            {"identifier": "AV35098",        # catalogue number as printed
             "rrid":       "AB_2039791",     # optional
             "target":     "KCa3.1",         # full target name, not a fragment
             "role":       "primary",        # or isotype_control / secondary /
                                             # loading_control / tag / dye
             "applications": ["WB", "IF"],   # optional; what the PAPER used it for
             "figures":    ["5a", "5b"]}     # optional

        Include EVERY reagent, and set ``role`` honestly — non-primary roles are
        excluded from the antibody results here, exactly as the controls rubric
        requires, so an isotype control must be marked as one rather than omitted.

        ``applications`` is worth filling in wherever the methods say: when a
        reagent turns out to be untested, it is what decides whether the
        alternatives offered for its target are alternatives for the job the paper
        actually did. Without it a flow-cytometry-only antibody can be offered for a
        western blot. Free text is accepted ("western blot", "immunofluorescence").

        ``genes`` — the gene symbols the paper studies; each is checked for a public
        OGA gene page.

        ``paper_doi`` / ``paper_pmid`` / ``paper_title`` — the paper's OWN identity.
        Pass whichever you have; a DOI or a PubMed id is exact, a title is checked
        against ``paper_year``. With one of these the reply also carries what
        CiteAb's citation record says this paper used each reagent for — an
        independent source, recorded by somebody who was not reading the paper
        just now. Where it disagrees with what you read, that disagreement is a
        finding: report it, and do not pick a side.

        The `citeab` block has three states and only one is about the paper.
        `not_covered` means CiteAb's record does not hold it — their coverage has
        documented gaps and their join is on catalogue number, since their schema
        has no RRID — and is NOT evidence the paper cites nothing. `unavailable`
        means no snapshot is deployed here, which is a fact about this server.

        CALL WHEN THE USER ASKS — in words, or by asking for the underlying thing
        ("which of these antibodies are validated?", "has this one been
        knockout-tested?"). Being asked to summarise, review or critique a paper is
        NOT a request for this.

        Returns antibody hits grouped as recommended / not_recommended / not_tested
        / not_in_dataset, each with its PER-APPLICATION verdict (WB/IP/IF/FC), RRID,
        product link, report DOI and gene page URL.

        NEVER state or imply that an antibody or gene is absent from the dataset
        without calling this (or another OGA tool) first — memory is not evidence.
        ``not_in_dataset`` means untested, NOT unreliable. Verdicts are per
        application: never generalise a pass in one application to another.

        Pass each identifier EXACTLY as the paper prints it — do not tidy it up.
        Publisher typesetting (``14 060-1-AP``, ``14,060–1-AP``, ``#ab74140``) is
        normalised server-side, and an identifier you have silently corrected is one
        neither of us can check against the page.

        When an entry comes back with ``target_matched_via: "alias"``, the reagent
        reached that gene through a stored synonym rather than its own symbol. Name
        both sides when you report it (*matched via the alias "p65" → SYT1*): short
        synonyms are shared between unrelated proteins, and only the reader knows
        which one they meant."""
        result = portal.check_manuscript(reagents=reagents, genes=genes, cap=ROW_CAP,
                                         paper_doi=paper_doi, paper_pmid=paper_pmid,
                                         paper_title=paper_title, paper_year=paper_year)
        c = result.get("counts", {})
        audit.record("A", "check_manuscript", reagents=len(reagents or []),
                     recommended=c.get("recommended"),
                     not_recommended=c.get("not_recommended"),
                     not_tested=c.get("not_tested"),
                     not_in_dataset=c.get("not_in_dataset"), genes=c.get("genes"))
        return result

    @mcp.tool(**_ro("Antibody characterisation lookup (one antibody)"))
    def antibody_validation(rrid: Optional[str] = None, catalogue: Optional[str] = None,
                            gene: Optional[str] = None) -> dict:
        """Look up ONE antibody by RRID, catalogue number, or gene and report,
        factually, how it performed when YCharOS tested it independently with
        knockout controls, under community consensus protocols.

        Call this whenever an antibody or reagent is cited in a paper, preprint,
        methods section, or figure legend you are reading or summarising — even if
        the user did not ask about antibodies. (To sweep a whole manuscript at
        once, prefer ``check_manuscript``.) NEVER assert an antibody is absent from
        the dataset without calling a tool first; memory is not evidence.

        Each match is serialised exactly as the public data portal, plus:
          * ``assessment``: per application (WB/IP/IF/FC), ``support`` is one of
            ``supportive``, ``limited_support`` (tested with KO controls, not
            supported overall, and the antibody was still seen to do what the
            application is for), ``not_supportive`` (tested, nothing on-target
            seen), or ``not_tested`` (no independent assessment — untested, NOT
            unreliable). Quote ``verdict`` / ``verdict_sentence`` for the words
            a reader will find on the gene page. ``status`` beside it is the
            older three-value form and reports both negatives as
            ``not_recommended``, so read ``support``. Never generalise one
            application's result to another;
          * ``summary``: a plain-fact sentence with the result per application and
            the consensus-protocol DOI it was assessed under;
          * ``provenance``: RRID registry link + F1000/Zenodo report DOIs;
          * ``assessment[app].tested_from``: which public signals support the
            result — the curated recommendation flag and/or a published figure.
            The pipeline's internal per-session lab records are NOT exposed: this
            connector carries what the live site and portal API carry.

        If the antibody is not present, returns ``in_dataset: false`` — state that
        as a fact; absence is NOT evidence about the antibody's quality. The ONE
        exception is a reply carrying ``target_confusion``: that product is
        documented as an antibody to a different protein from the one it shares a
        name with, which is not an absence and must not be reported as one."""
        rows, truncated = portal.antibody_validation(
            rrid=rrid, catalogue=catalogue, gene=gene, cap=ROW_CAP)
        audit.record("A", "antibody_validation", rrid=rrid, catalogue=catalogue,
                     gene=gene, n=len(rows))
        # Asked whether or not the lookup hit, so that a product which ever does
        # enter the dataset is not served a verdict with the mismatch missing.
        confusion = portal.confusion_lookup(rrid=rrid, catalogue=catalogue)
        if not rows:
            # THE REPLY MOST AT RISK, and the reason this feature exists. The
            # message below is exactly right for an untested antibody and was
            # being served about reagents whose own manufacturer states they do
            # not bind the protein the reader asked about — a documented problem
            # handed over as an open question, on the one tool that answers about
            # a single reagent with no paper around it to soften it.
            if confusion:
                return {
                    "in_dataset": False, "count": 0, "antibodies": [],
                    "target_confusion": confusion,
                    "message": (
                        "OGA has not characterised this antibody, but it is "
                        f"documented as an antibody to {confusion['declared_target']}"
                        f" and NOT to {confusion['commonly_bought_for']}, which is "
                        "the protein it shares a name with. Do not report this as "
                        "a plain absence: report the declared target, name and "
                        "link the review in `documented_in`, and say that this is "
                        "about which protein the reagent is raised against rather "
                        "than how well it works."),
                }
            return {"in_dataset": False, "count": 0, "antibodies": [],
                    "message": "This antibody is not in the dataset. Absence is "
                    "not a judgement about the antibody — it simply has not been "
                    "independently characterised."}
        reply = {"in_dataset": True, "antibodies": rows, "count": len(rows),
                 "truncated": truncated}
        if confusion:
            reply["target_confusion"] = confusion
        return reply

    @mcp.tool(**_ro("Gene characterisation report"))
    def target_report(gene: str) -> dict:
        """Full characterisation report for ONE gene (target), in the same shape as
        the public data portal: every published antibody with its per-application,
        knockout-controlled result, a supplier summary, the gene page
        URL, and the F1000/Zenodo report DOIs. Use this when a gene is named in a
        paper, manuscript, or methods section to state, factually, what the data
        shows and why. Do not claim a gene is absent from memory — call
        ``list_targets`` or this tool first. Results are per application; never
        carry one application's result over to another.

        Each row's ``assessment[app].support`` is one of ``supportive``,
        ``limited_support``, ``not_supportive`` or ``not_tested``. Report the
        middle rung as itself — it is tested, not supported overall, and the
        antibody was still seen to do what the application is for — rather than
        folding it into the negative."""
        result = portal.gene_detail(gene)
        audit.record("A", "target_report", gene=gene, found=result.get("found"))
        return result

    @mcp.tool(**_ro("Antibodies at one support level for a gene"))
    def antibodies_by_support(gene: str, application: str,
                              support: str = "supportive") -> dict:
        """Within ONE gene, the antibodies at one support level for one application.

        ``gene`` is REQUIRED (this is a per-gene question, not a whole-database
        list). ``application`` is one of WB, IP, IF (a.k.a. ICC-IF), FC —
        results are per application, so never carry one application's result
        over to another.

        ``support`` is one of:
          * ``supportive`` — the characterisation data supports this application;
          * ``limited_support`` — tested, not supported overall, and the antibody
            was still seen to do what the application is for (detect, enrich, or
            give a selective signal). A real and common middle rung: on the live
            dataset it is 491 of the 1,833 negative results;
          * ``not_supportive`` — tested, and nothing on-target was seen;
          * ``not_tested`` — nobody has run it. NOT a negative result.

        Ask for one rung at a time. There is deliberately no boolean form: a
        yes/no question cannot separate ``limited_support`` from
        ``not_supportive``, and answering it would hand back an antibody the
        bench watched work alongside one that showed nothing.

        Antibodies are serialised exactly as the public API, with the factual
        assessment layer."""
        rows, truncated = portal.antibodies_by_support(
            application, support, gene, cap=ROW_CAP)
        audit.record("A", "antibodies_by_support", application=application,
                     support=support, gene=gene, n=len(rows))
        return {"antibodies": rows, "count": len(rows), "gene": gene,
                "application": application, "support": support,
                "truncated": truncated}

    @mcp.tool(**_ro("List characterised genes"))
    def list_targets(only_with_recommendations: bool = False,
                     limit: Optional[int] = None) -> dict:
        """List every gene (target) YCharOS has publicly characterised with
        knockout-controlled antibody characterisation — i.e. the genes that have a public
        gene page. Use it to check whether a gene named in a paper or manuscript is
        covered BEFORE saying anything about its presence — never assert a gene is
        absent from memory. Returns gene name + protein/UniProt identity only; it
        does NOT expose (or filter by) internal pipeline workflow status.
        ``only_with_recommendations`` keeps genes with at least one antibody
        whose data is supportive for some application. (The parameter keeps its
        older name so existing calls still work.) ``limit`` caps the list — the full set is a few hundred genes and
        is rarely all needed at once; ``count`` always reports the true total so a
        capped call is never mistaken for a short dataset."""
        rows = portal.list_targets(only_with_recommendations)
        total = len(rows)
        if limit and limit > 0:
            rows = rows[:limit]
        audit.record("A", "list_targets", n=len(rows), total=total)
        return {"targets": rows, "count": total, "returned": len(rows),
                "truncated": len(rows) < total}

    @mcp.tool(**_ro("Find one antibody in the dataset"))
    def search_antibodies(text: str, limit: int = 50) -> dict:
        """Locate a specific published antibody by catalogue number, RRID, or
        gene — the "is this antibody in the dataset?" lookup for a single reagent
        cited in a paper. Does NOT search by company (so it can't enumerate a
        vendor's catalogue). Each hit carries the factual, per-application
        KO-controlled assessment. To scan a whole manuscript at once, use
        ``check_manuscript``. Never report an antibody as absent without running
        this (or ``check_manuscript``) first — memory is not evidence."""
        rows, truncated = portal.search_antibodies(text, limit, cap=ROW_CAP)
        audit.record("A", "search_antibodies", text=text, n=len(rows))
        return {"antibodies": rows, "count": len(rows), "truncated": truncated}

    # -- Controls-assessment surface. The controls VERDICT is the connected
    #    model's job, guided by the rubric scan_controls returns inline in
    #    `rubric.text`. There is no separate rubric prompt: a second copy the user
    #    had to select and paste was the awkward step this replaced, and leaving it
    #    registered only invited people to pay for the rubric twice. -------------

    @mcp.tool(**_ro("Assess a paper's antibody controls"))
    def scan_controls(reagents: List[dict],
                      controls: Optional[List[dict]] = None,
                      genes: Optional[List[str]] = None,
                      sections_read: Optional[List[str]] = None,
                      legends_read: Optional[List[str]] = None,
                      what_shows_selectivity: Optional[str] = None,
                      paper_doi: Optional[str] = None,
                      paper_pmid: Optional[str] = None,
                      paper_title: Optional[str] = None,
                      paper_year: Optional[int] = None) -> dict:
        """Assess whether a paper CONTROLLED its antibodies.

        RUN ONLY WHEN THE USER ASKS. Being asked to summarise, explain, review or
        critique a paper is NOT a request for this; "are the antibodies controlled?",
        "is figure 3 trustworthy?" or "run the controls scan" is. If you notice a
        paper uses antibodies, OFFER this and wait — do not run it unbidden.

        YOU read the paper; this resolves and assembles. Pass:

        ``reagents`` — as for ``check_manuscript`` (identifier / rrid / target /
        role / applications / figures). Include isotype controls and secondaries
        with their real role so they can be excluded properly, and give
        ``applications`` where the methods say, so any alternatives offered for an
        untested reagent match the application it was actually used for.

        ``controls`` — the controls the paper actually SHOWS, one dict each:

            {"type": "knockout",          # knockout | knockdown | crispr |
                                          # peptide_competition | secondary_only |
                                          # isotype | overexpression |
                                          # positive_control | orthogonal | …
             "target": "KCNN4",           # the gene REMOVED (selectivity controls)
             "figures": ["S1"],           # where the CONTROL is, main or supplement
             "performed_in_paper": true,  # false if the paper only CITES it
             "readout": "WB",             # what the manipulated material was
                                          # measured with, if the paper says
             "detected_with": "ab212184", # which reagent read it out, if named
             "evidence": "quoted FIGURE LEGEND"}

        Report what you SAW; the server decides what it COUNTS AS. You do not supply
        the control class — whether an isotype control can evidence selectivity is a
        property of the method (it cannot), not a judgement call. Three things
        matter most and are yours to get right: ``performed_in_paper`` (a knockout
        the paper merely cites in its discussion is NOT a control for its figures),
        ``target`` (a knockout of gene X is a control for the anti-X antibody and
        nothing else), and the ``evidence`` you quote. A selectivity control with no
        target named cannot validate anything and will not be counted.

        **READ BOTH THE METHODS AND THE FIGURE LEGENDS — neither holds all of it.**
        The four questions sit in different sections: which figures use antibodies
        (figures + legends); WHICH antibodies (Methods — that is where catalogue
        numbers, suppliers and applications are, and a legend often names no
        reagent at all); which controls and of what kind (Methods + figures +
        legends); and whether this antibody was read out against the control
        material (figures + legends, SUPPLEMENTS INCLUDED). Quote the panel's
        legend as ``evidence``, and fill in ``readout`` / ``detected_with`` from
        wherever the paper says it. A siRNA sequence out of the Methods answers
        none of the four.

        The control does NOT have to be in the same figure as the antibody's use,
        and it never did have to be scientifically: papers establish a reagent in
        one figure or the supplement and then use it in another. Report where the
        control actually is — the same PANEL is taken as linkage evidence, a
        different figure is reported and never held against the paper.

        ``paper_doi`` / ``paper_pmid`` / ``paper_title`` — as for
        ``check_manuscript``. With one of them, a row whose applications disagree
        with CiteAb's record of the same paper carries ``application_conflict``,
        naming both sides and choosing neither. A conflict usually means the paper
        used the reagent more than once, or CiteAb attributed the wrong product,
        or the use is in a section you did not read — say which you cannot rule
        out rather than reconciling them.

        ``what_shows_selectivity`` — ANSWER THIS BEFORE YOU SCAN, in your own
        words, from your own knowledge: what would show that an antibody is
        selectively detecting its target, and what would not?

        It is not scored and there is no right form of words. It is asked for two
        reasons. A reader who has just stated the principle applies it better than
        one handed it in a rubric — so answering makes your own scan better,
        whatever this server sends back. And the answer decides how much this
        reply explains: it can only ever ADD explanation, never withhold it, so a
        thorough answer earns a shorter reply and a poor one, or none, earns the
        full rubric. Nothing you say here can affect any verdict about the paper.

        Your answer is echoed back in ``briefing`` so a human reading the output
        can see what understanding the assessment was made against.

        ``legends_read`` — WHICH FIGURE LEGENDS YOU READ, BY NUMBER, including
        supplementary ones: ``["1", "2", "3", "S1"]``. Required to earn an
        `absent`.

        Enumerate rather than assert, because "did you read the legends in full?"
        is a question anything will answer yes to, and `absent` is the one verdict
        that depends on the answer — it claims no genetic manipulation of the
        target appears ANYWHERE in the paper. A list is checkable where a yes is
        not: if you place a reagent in Fig 4 while listing legends 1-3, the server
        can see it without reading the paper, and says so. Gaps in the main
        sequence are named too — that is what a truncated full text looks like, a
        PDF extraction that stopped or a supplement that never loaded. (Gaps in
        supplementary numbering are ignored: S1, S2, S5 is ordinary publishing.)

        Without it every row reads `not_assessed`. That withholds an accusation
        and nothing else: `demonstrated` and `present_unlinked` are FINDINGS and
        are served however little of the paper was read, because a paper that
        shows a knockout shows it either way.

        ``sections_read`` — WHICH PARTS OF THE PAPER YOU ACTUALLY READ. Any of
        ``methods``, ``results``, ``figure_legends``, ``supplementary``, or
        ``"full text"``. Answer honestly and answer narrowly: this is not a
        formality and it is not scored.

        It exists because `absent` is an accusation. It means no genetic
        manipulation of this target appears ANYWHERE in the paper, and a caller
        that read only the Methods produces a whole table of it that the server has
        no basis for and that looks identical to a paper genuinely showing none.
        So unless the Methods AND the figure legends are both evidenced, those rows
        come back `not_assessed` instead — a statement about this call, not about
        the authors — and `coverage.limits` says so.

        Declaring cannot buy you an `absent`. The declaration is checked against
        what you passed: saying you read the legends while carrying nothing out of
        them is contradicted in `coverage.sections.contradictions`, and the payload
        is what the verdict rests on either way. What the declaration CAN do is
        withhold a verdict you should not be given. If the supplement was not among
        them, every `absent` is caveated — a knockout panel is exactly the kind of
        figure that gets moved there.

        Returns the ``rubric`` INLINE (``rubric.text``) — apply it; no prompt to
        fetch. Plus a ready-made ``table`` of ONLY the antibodies that have
        something to say (a candidate control, or independent OGA data), an
        ``others`` list (untested AND uncontrolled — the ~85% default), and
        ``focus``.

        RENDER THE ``table`` AS-IS; render ``others`` as ONE tidy line — do NOT add
        them to the table or enumerate their figures. Use the direct media files
        (``row.image`` / ``row.images``), NOT embed-card URLs. Expand ONLY on
        ``focus``. This makes NO controls verdict: ``control_status`` is a CANDIDATE
        the reader confirms at the figure (presence is not proof), while
        ``oga_result`` is a database fact — keep the two axes separate. not_tested /
        not_in_dataset means untested, not unreliable; never carry one application's
        verdict to another.

        **``control_status`` is THREE-VALUED — do not collapse it to yes/no.**
        ``demonstrated`` (a genetic control for this target, and the paper says this
        antibody was read out against it), ``present_unlinked`` (the control is
        there; the text does not establish THIS antibody was tested against it) and
        ``absent`` (no genetic manipulation of this target anywhere in the paper).
        ``present_unlinked`` is a real and common state and must be reported in its
        own words — calling it "no control" is a false accusation against a paper
        that may be perfectly well controlled. ``paper_control`` carries the same
        answer as ``yes`` / ``unlinked`` / ``no`` for older renderers. Every row with
        a control also carries ``control_note`` (where the control is, what read it
        out, where the antibody was used, and what to check at the panel),
        ``controls[].linkage_basis`` (WHY it counts: ``named`` / ``same_panel`` /
        ``stated_readout``) and, where it applies, ``application_caveat``. Render
        them. **Never say a control worked** — this server reads text, not images.

        **Read ``coverage.limits`` before reporting any ``absent``** and state what
        it says. It reports what YOU passed in — with no controls supplied, every
        row reads ``absent``, and that describes the input, not the paper.

        **SCOPE — state it whenever you report a ``control_status`` of ``absent``.**
        This counts the GENETIC pillar only: knockout, knockdown, CRISPR, a naturally
        null line. ``absent`` therefore means *no genetic selectivity control was
        found*, NOT that the paper showed no validation. Overexpression and
        recombinant protein (Uhlen pillar 4) are classed ``detection`` and orthogonal
        methods (pillar 2) ``orthogonal``; neither sets ``paper_control``, and both
        appear in that row's ``other_controls`` as ``{figure, type, class}`` when the
        paper performed one. Report them by name alongside the "no" — a paper showing
        a blocking peptide and a paper showing nothing are not the same paper, and
        ``other_controls`` is the only thing that tells them apart."""
        result = portal.scan_controls(reagents=reagents, controls=controls,
                                      genes=genes, cap=ROW_CAP,
                                      sections_read=sections_read,
                                      legends_read=legends_read,
                                      what_shows_selectivity=what_shows_selectivity,
                                      paper_doi=paper_doi, paper_pmid=paper_pmid,
                                      paper_title=paper_title, paper_year=paper_year)
        c = result.get("counts", {})
        audit.record("A", "scan_controls", reagents=len(reagents or []),
                     recommended=c.get("recommended"),
                     not_recommended=c.get("not_recommended"),
                     not_tested=c.get("not_tested"),
                     not_in_dataset=c.get("not_in_dataset"),
                     controls_reported=c.get("controls_reported"),
                     legends_enumerated=bool(legends_read),
                     briefing_answered=bool(what_shows_selectivity),
                     rubric_version=CONTROLS_RUBRIC_VERSION)
        return result

    @mcp.tool(**_ro("The full antibody controls rubric"))
    def controls_rubric() -> dict:
        """The whole controls rubric, both halves, on request.

        `scan_controls` always carries the rules that BIND — what each kind of
        control can establish, what may not be asserted, the three-valued verdict.
        Those are not negotiable and every caller gets them on every call.

        What it may leave out is the HOW-TO half: how a competent reader gets
        through a paper, where each answer hides, why `present_unlinked` is so
        common. That is support, not law, and support nobody needs costs
        something — it anchors a way of reading in place of one you might have
        chosen better for the paper in front of you. So it is sent when your
        answer to `what_shows_selectivity`, or your payload, suggests it is
        wanted, and named rather than pushed otherwise.

        This is how you get it anyway, any time, no reason required. Nothing in
        `scan_controls` is ever withheld from you — a shorter reply is a shorter
        reply, not a smaller entitlement.
        """
        return {"version": CONTROLS_RUBRIC_VERSION, "text": CONTROLS_RUBRIC}

    @mcp.tool(**_ro("How to work a paper for its antibodies"))
    def how_to_read_a_paper() -> dict:
        """How to get through a paper looking for antibodies and their controls.

        Call it before scanning a paper you have not worked through yet, or when
        you are unsure a paper uses antibodies at all — the first step it gives
        you is how to find that out cheaply and stop.

        It is guidance and not law: where to look, in what order, what tends to
        hide where, and which arguments to `scan_controls` earn their keep. If
        you have a better way through the paper in front of you, use yours. What
        a control METHOD can establish is the part that does not bend, and that
        comes back from `scan_controls` on every call.

        MCP has no skills primitive — checked against the SDK, not assumed — so
        this is the same document Claude loads as a skill, served as a tool so
        every connected model can reach it. One file, not a copy.
        """
        text = content_pack.reading_guidance()
        if text is None:
            # A deploy that did not carry the file says so. An empty string here
            # would read as guidance that has nothing to say.
            return {"available": False,
                    "note": "The reading guidance is not present in this "
                            "deployment. The rules that bind are unaffected — "
                            "they ship with every scan_controls reply."}
        return {"available": True, "text": text}

    @mcp.prompt(name="how_to_read_a_paper",
                description="How to work a paper for its antibodies and their "
                            "controls — where to look, and when to stop.")
    def how_to_read_a_paper_prompt() -> str:
        """The same document, offered where a client lists prompts for a person
        to pick. A tool is reached by a model deciding it needs help; a prompt is
        reached by a reader deciding they want it, and those are different doors
        to the same room."""
        return content_pack.reading_guidance() or (
            "The reading guidance is not present in this deployment.")

    # The interactive-education (tutor) tools are deliberately NOT registered here:
    # the conversational tutor is being reworked, so this connector is the antibody
    # DATABASE only. The tutor implementation still lives in
    # ``server_a2_academy.register_tools`` for that rework; re-add the call here to
    # surface it again once it's ready.
    return mcp


def main():
    build_server().run()


if __name__ == "__main__":
    main()
