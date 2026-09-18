"""What OGA recommends for one antibody in one application — the one reader.

OGA tests an antibody under the community consensus protocols, in one knockout
cell line, for one application, and recommends it or does not under those
conditions.

``SCOPE_NOTE`` is what a reader is owed about that, and it travels with the
data. It is stated once per surface rather than repeated on every value — a
caveat on every row is a caveat nobody reads.

The three values
----------------
``Antibody.wb_recommended`` and its three siblings are plain booleans, so
``False`` on its own answers two completely different questions with one value:

    * we tested this antibody for western blot and do not recommend it, and
    * nobody has tested it.

Only the first says anything. Reporting the second as if it were the first tells
a manufacturer their product failed testing that was never run on it, which is
the one output of this dataset that must never be wrong by accident.

Two signals disambiguate it, and **both are needed**:

``PublicationImage``
    A published figure for that application is the record that the application
    was tested at all.

The gene having any recommendation
    A gene nobody has curated yet can carry published figures and no flags, and
    that is the normal state of a gene mid-pipeline — figures go up as sessions
    are cropped, and the recommendations are set later, in one pass, on
    ``/pipeline/recommendations/``. Reading the figure alone turns every antibody
    on every uncurated gene into a documented failure the moment its first figure
    is published.

So::

    flag set                                     -> recommended
    no flag + figure for this application
                + the gene has been curated      -> not_recommended
    anything else                                -> not_tested

Three copies of this question exist in the repo and **they did not all agree**:

``mcp_servers/common/portal.py::_assessment``
    Three-valued with the gene gate. The reasoning above is its docstring's, and
    this module is that logic lifted out so the API can share it rather than
    import from an MCP server.

``core/extension_index.py::_verdicts``
    Lacked the gene gate until 5 Aug 2026, so on an uncurated gene it reported
    ``NOT_RECOMMENDED`` where this module reports ``NOT_TESTED``. It asks this
    module now.

``core/api_views.py::_serialise_antibody``
    Two-valued — the raw booleans, under ``recommendations``. Kept as it is,
    because the portal front end and every existing integration read that shape.
    New surfaces carry ``oga_recommendations`` beside it, not instead of it.

The fourth rung
---------------
The three values above answer *did OGA recommend it*. Since 29 Aug 2026 every
screen answers a different question — *what does the characterisation data
support* — in four rungs, because ``not_recommended`` had been carrying two
findings that a manufacturer reading about their own product needs told apart.
``SUPPORT_VALUES`` is that scale as controlled values, published as
``oga_support``; ``support()`` is its one reader and ``words()`` derives from it.

**Both vocabularies ship, and the old one is not going anywhere.** The API's
keyless callers are 269 of 357 all-time requests and are never identified, so
there is nobody to tell and no way to learn whether a removal broke them —
which makes *additive* the only honest move here, not merely the polite one.
The rule for every surface is: **switch on ``support``, print ``words``, and
keep emitting ``verdict`` for whoever is already reading it.**
"""
from __future__ import annotations

# The four applications tested under the consensus protocols, in the order every
# OGA surface prints them.
APPLICATIONS = ('WB', 'IP', 'ICC-IF', 'FC')

#: The original three-valued vocabulary. **Legacy, and staying** — see
#: ``SUPPORT_VALUES`` below for the one to write new code against.
#:
#: Published as ``oga_recommendations`` on the API since this API was written,
#: encoded as the ints 0/1/2 in the extension index, and switched on by six
#: named organisations plus an unidentifiable keyless population that is 269 of
#: the API's 357 all-time requests (impact page, 14 Sep 2026). Nothing here is
#: removable, and the sentence to keep is that it is not *wrong* — it is one
#: rung short.
RECOMMENDED = 'recommended'
NOT_RECOMMENDED = 'not_recommended'
NOT_TESTED = 'not_tested'

#: The vocabulary every OGA surface prints, as controlled values — four rungs,
#: one per printed rung, so *switch on what you print* is true for the first
#: time.
#:
#: **The reason this exists is that ``NOT_RECOMMENDED`` carries two findings.**
#: An antibody that showed nothing at all and one that did the job the
#: application is *for* and fell short on the rest are the same value, and on
#: live data the second group is **491 of the 1,833 negatives** (29 Aug 2026).
#: A consumer switching on the three values files better than a quarter of every
#: negative this dataset publishes with the outright failures — and the six
#: organisations doing so are the manufacturers whose own products those are.
#: ``words()`` has drawn the distinction since 29 Aug 2026 and no machine-
#: readable field carried it, so the only clients getting it right were the ones
#: that read a paragraph of API.md and re-derived it.
#:
#: **``NOT_TESTED`` is deliberately the same string in both vocabularies.** The
#: fourth rung is the one value the two agree about exactly, and giving it a
#: second spelling would invent a difference where there is none — a consumer
#: migrating field by field would have to handle two untested values that mean
#: one thing.
#:
#: **Additive, and that is the whole safety argument.** A keyless caller cannot
#: be told, surveyed or checked up on, so nothing they already read may move: a
#: new key is invisible to every parser that does not ask for it, where a
#: changed value is a silent mis-bucketing on somebody's dashboard.
SUPPORTIVE = 'supportive'
LIMITED_SUPPORT = 'limited_support'
NOT_SUPPORTIVE = 'not_supportive'

#: The four, in the order every surface prints them — best-supported first.
SUPPORT_VALUES = (SUPPORTIVE, LIMITED_SUPPORT, NOT_SUPPORTIVE, NOT_TESTED)

#: Which legacy value each rung reports as, for a consumer holding both fields
#: or migrating between them. Derived from nothing — it *is* the mapping, and
#: the two negatives collapsing onto one legacy value is the fact the whole
#: field exists to state.
LEGACY_VERDICT_OF = {
    SUPPORTIVE: RECOMMENDED,
    LIMITED_SUPPORT: NOT_RECOMMENDED,
    NOT_SUPPORTIVE: NOT_RECOMMENDED,
    NOT_TESTED: NOT_TESTED,
}

# Which boolean on Antibody carries the recommendation for each application.
_FLAG = {
    'WB': 'wb_recommended',
    'IP': 'ip_recommended',
    'ICC-IF': 'if_recommended',
    'FC': 'fc_recommended',
}

#: The consensus protocols the results come from: Ayoubi et al., 2024, *Nature
#: Protocols*, written with YCharOS, the industry–academic consortium. One copy,
#: because the public pages, the API and the framework page all cite it and a
#: second URL is the one that rots.
#:
#: **Not the Delphi study** (owner, 7 Aug 2026). That is a separate piece of
#: work — 32 experts rating interventions for funders, publishers and
#: institutions — and it is what the roadmap pages are built on. The protocols
#: were not written by that method, and the homepage card said they were.
CONSENSUS_PROTOCOL_URL = 'https://www.nature.com/articles/s41596-024-01095-8'

#: Said once, with the data, and not repeated per value.
#:
#: This is the owner's wording, 7 Aug 2026, and it is deliberately the whole of
#: the scientific caveat — two facts a reader can act on. What it replaced ran
#: to a paragraph per surface: that a recommendation is "not a verdict", that
#: `not_tested` "is not a negative result — map it to no data, never to a
#: failure", that absence from the dataset means the same. Untested is untested,
#: and a caveat long enough to need its own section is one nobody finishes.
#:
#: The second fact was sharpened on 12 Aug 2026, on the owner's instruction. It
#: had read "may not apply to other protocols and samples", which says the
#: result might not carry over — true, and weaker than what OGA means. A reader
#: holding a *recommended* verdict and a different sample type was being left to
#: infer whether their own experiment was covered, and the honest answer is that
#: it is neither supported nor contradicted. Both directions are stated, because
#: the harmful reading runs both ways: a pass here is not a licence for another
#: assay system, and a fail here is not a mark against somebody's working IHC.
#:
#: **Two changes on 11 Sep 2026, on the owner's instruction, and both make the
#: same move: say what a reader should DO.** The dependence is *assay*, protocol
#: and sample — the assay is the axis a reader most often crosses (an OGA
#: western blot result and their IHC are different assays before they are
#: different protocols), and it was the one word missing from a list the
#: extension page had already been printing in full. And "do not validate or
#: invalidate experiments in other assay systems or sample types" is replaced by
#: "performance in your own experimental context may differ and needs its own
#: controls": both directions survive in *may differ* — a pass here is still not
#: a licence and a fail here is still not a mark against somebody's working IHC
#: — and the clause that follows is the half a reader can act on, which the
#: sentence had never said. Every HTML surface draws this beside a link to the
#: Validation Planning Framework (`_recommendation_caveat.html`), so the reader
#: is told what to do and where to do it in one breath.
SCOPE_NOTE = (
    'Results are based on consensus protocols. Antibody performance is assay, '
    'protocol and sample dependent, so performance in your own experimental '
    'context may differ and needs its own controls.')

#: The clause inside ``SCOPE_NOTE`` that every HTML surface sets in bold.
#:
#: Owner, 11 Sep 2026, reading the deployed page and highlighting exactly these
#: words. The sentence carries four facts and a reader skimming a notice above a
#: table takes one of them; this is the one that is about *their* experiment
#: rather than about ours, so it is the one that has to survive the skim.
#:
#: **A fragment of the sentence, not a second copy of it.** It is asserted to be
#: a substring of ``SCOPE_NOTE`` (``tests_recommendation_caveat``), because the
#: failure otherwise is silent in the worst way: reword the note, the fragment
#: matches nothing, the emphasis quietly stops being drawn, and the page still
#: renders a complete and correct sentence. Nothing on screen would say so.
#:
#: Plain text, and the markup is applied in ``context_processors`` — this module
#: is Django-free, and the string itself travels into JSON on the API, the
#: archive and the extension index, where a ``<strong>`` would be a tag in a
#: data field.
SCOPE_EMPHASIS = 'performance in your own experimental context may differ'

#: The words that bind a verdict to what produced it, inside the sentence.
#:
#: Owner, 27 Aug 2026, reading the packaged 0.2.5 build: the card said "Tested
#: and *not* recommended for WB" with the scope note *underneath* it. Two
#: sentences, and the strong one stands alone — a reader who takes only the
#: headline takes an unqualified verdict, which is the whole thing this work was
#: about. The qualifier belongs in the claim.
#:
#: A fragment, not a sentence, and the only one here: it is appended to
#: "recommended for WB, IP" and to "not recommended for WB", never printed
#: alone. ``SCOPE_SHORT`` still follows underneath and says the wider thing —
#: this says *these applications, these conditions*, that says *performance
#: depends on protocol and sample*.
#:
#: **It names WHICH conditions** (owner, 3 Sep 2026). It read "in the conditions
#: tested", which is passive about the two things a reader most needs: whose
#: conditions, and which. A reader taking the headline alone was left to infer
#: both from a footer line two paragraphs down. Naming the protocols inside the
#: claim is the same move that put the qualifier inside the claim in the first
#: place, one step further.
#:
#: No trailing "tested": "the consensus protocols tested" reads as *the
#: protocols that were tested*, and the word buys nothing — "Tested;" already
#: opens the negative headline, and "supports" / "limited support" carry it in
#: the others.
#:
#: Two consequences, and both are the point rather than side effects. **The card
#: hyperlinks this phrase** to ``CONSENSUS_PROTOCOL_URL`` — the way to check a
#: result is now attached to the claim it qualifies, and reaches every verdict
#: rather than only the three levels that draw a scope line. And **``SCOPE_SHORT``
#: dropped its own "Result from consensus protocols."**: with the fact inside the
#: verdict, repeating it one line below is the third caveat on one card, which is
#: what teaches a reader to skip all of them.
#:
#: Never appended to "not tested": an application nobody ran is not a result
#: under any conditions, and qualifying it would imply one.
CONDITIONS_QUALIFIER = 'under the consensus protocols'

#: What is specific to ONE application, and is a fact about the application
#: rather than about the vocabulary — so it can be drawn anywhere.
#:
#: ``SCOPE_NOTE`` is the caveat for the dataset; this is the caveat for a single
#: value in it, and the two are drawn in different places for that reason — a
#: fact about immunofluorescence belongs beside the immunofluorescence result,
#: not in a sentence a reader meets once per page and applies to nothing in
#: particular.
#:
#: Owner's instruction, 27 Aug 2026. ICC-IF is the application where "protocol
#: dependent" has a specific, actionable meaning: whether an epitope survives
#: depends on how the sample was fixed and permeabilised, so a result obtained
#: under one fixation says less about another than a western blot result does
#: about another lysis buffer. It is the same axis the extension already refuses
#: to cross for tissue — OGA's IF verdict is cultured cells, and IHC-IF is
#: tissue despite the name — stated one step earlier, for the case where the
#: preparation matches and the chemistry does not.
#:
#: Keyed by the application values in ``APPLICATIONS``. An application with
#: nothing specific to say is absent rather than carrying an empty string, so a
#: reader is never shown a caveat-shaped blank.
#:
#: **Split out of ``APPLICATION_SCOPE`` on 11 Sep 2026**, which composes it back
#: below. The other half of every one of those sentences ends "the gene page
#: says which" — a pointer, and the right one on a card floating over somebody
#: else's tab. On the gene page it sends a reader where they already are, and it
#: is redundant there besides: ``cell_caption`` prints the capability under
#: *every* tested cell, which is what "says which" means. So the gene page draws
#: this constant alone (``context_processors.application_facts``) and the
#: extension page goes on drawing the whole sentence.
APPLICATION_FACT = {
    'ICC-IF': ('Immunofluorescence results are fixation and permeabilisation '
               'dependent.'),
}

#: What ‘not supportive’ leaves open, per application — the half that defers to
#: the gene page, and so is never drawn on it.
#:
#: Added 29 Aug 2026 as the **interim** half of the capability layer, and it is
#: here rather than anywhere else for a mechanical reason: this string is
#: shipped inside ``index.json`` and drawn by the extension's card, so rewording
#: it reaches every installed build at the next daily refresh with no store
#: review. The per-antibody caveat cannot — ``q`` is data the shipped build does
#: not read, and colours live in its JavaScript — so until a release lands, this
#: is the only thing that stops 491 qualified negatives being read as outright
#: failures.
#:
#: Phrased as a fact about the **vocabulary** rather than about the antibody in
#: front of the reader, because the card draws this under whichever application
#: is open and cannot tell a supportive verdict from a negative one. "A 'not
#: supportive' result may still mean…" is true beside either; "this antibody
#: detected its target" would not be.
#:
#: The quoted word moved from ‘not recommended’ to ‘not supportive’ on 30 Aug
#: 2026, because it QUOTES the verdict the reader is looking at and that verdict
#: changed: extension 0.3.0 made the chips *supportive* / *not supportive*, and
#: the gene page says “Characterisation data supports”. A sentence quoting a word
#: no surface prints any more reads as being about something else — which is the
#: opposite of what it is for. Only the quoted verdict changed; the shape, the
#: “may still mean” hinge and the per-application capability words are as they
#: were, and the controlled values in ``oga_recommendations`` are untouched
#: (they are API surface, not words a reader sees).
#:
#: Private: every surface draws it through ``APPLICATION_SCOPE``.
_APPLICATION_VOCABULARY = {
    'WB': ('A ‘not supportive’ result may still mean the antibody detected '
           'its target but was not selective; the gene page says which.'),
    'IP': ('A ‘not supportive’ result may still mean the antibody enriched '
           'its target; the gene page says which.'),
    'ICC-IF': ('A ‘not supportive’ result may still mean the antibody gave a '
               'selective signal; the gene page says which.'),
}

#: The fact and the vocabulary note, in that order, for the surfaces that draw
#: both — the extension page and every installed extension build, which reads it
#: out of ``index.json``. Composed rather than typed out, so the split above
#: cannot let the two halves drift; the strings are byte-identical to what this
#: dict held before it.
#:
#: FC is absent: nothing records a flow cytometry outcome, so there is no
#: qualified negative for it to warn about and a line here would promise a
#: nuance the data cannot deliver.
APPLICATION_SCOPE = {
    app: ' '.join(part for part in (APPLICATION_FACT.get(app), note) if part)
    for app, note in _APPLICATION_VOCABULARY.items()
}

#: ``SCOPE_NOTE`` compressed for a surface that cannot hold the whole sentence.
#:
#: Written for the extension's hover card, which is 330px wide and already
#: carries a verdict, a per-application chip strip, a figure, provenance and a
#: links row. The full note runs to four lines there, directly under the words
#: it qualifies, and a caveat that long in that position is one a reader skips —
#: the failure ``SCOPE_NOTE``'s own docstring was written against.
#:
#: It is a compression and not a second opinion: both facts survive, in the
#: owner's own vocabulary, and it lives in this file beside the sentence it
#: shortens so the two cannot drift. Every surface with room for the full note
#: uses the full note — the API, the archive, the JSON-LD and all six pages are
#: unchanged.
#:
#: **Both facts still survive, but no longer both in this string** (owner, 3 Sep
#: 2026). It opened "Result from consensus protocols.", which
#: ``CONDITIONS_QUALIFIER`` now says *inside* the verdict — a stronger position,
#: since a reader who takes only the headline takes it too. Left here as well it
#: was the same fact twice on a 330px card, one line apart. So the pair is what
#: carries the caveat now: the qualifier says which protocols, this says what
#: they do not cover, and ``tests_extension_scope`` asserts the pair rather than
#: this string alone.
#:
#: ``assay`` joined the list on 11 Sep 2026 with ``SCOPE_NOTE``'s, and has to:
#: this is a compression of that sentence, and a compression that drops an axis
#: the full note names is a second opinion. One word on a 330px card.
SCOPE_SHORT = 'Antibody performance is assay, protocol and sample dependent.'

#: What the four verdicts collectively ARE, said before any one of them is read.
#:
#: Owner's instruction, 27 Aug 2026: "recommended", "not recommended" and
#: "failed" are very strong words for what is a measurement, and the extension's
#: own promotional images led with the strongest of them ("Tested against a
#: knockout. It failed."). The words themselves stay — they are what the
#: consortium publishes, and they are the API's enum — so the nuance is carried
#: by framing the set as a *result* rather than as advice, and by ``SCOPE_NOTE``
#: saying what it does not cover.
#:
#: A title, not a sentence: it heads a group of verdicts. ``SCOPE_NOTE`` is the
#: sentence, and the two are drawn together or not at all.
RESULTS_TITLE = 'Results of independent antibody characterisation'

#: The licence the data is under, stated once for the same reason ``SCOPE_NOTE``
#: is: it has to travel with the data, and it is now on four surfaces (the gene
#: pages' JSON-LD, the gene pages themselves, ``/data-access/`` and ``API.md``).
#: A second copy is the one that ends up naming a different licence.
LICENCE_NAME = 'CC BY 4.0'
LICENCE_URL = 'https://creativecommons.org/licenses/by/4.0/'

#: **What must be cited is the report DOI on the gene's own page**, not this
#: site (owner, 9 Aug 2026). Every gene that has a report carries one, and it is
#: what makes a reuse traceable back to the experiments rather than to a URL
#: that can move. A gene with no report yet has nothing to cite, which is why
#: every reader of this has to cope with the DOI being absent.
CITATION_NOTE = (
    'Free to reuse under CC BY 4.0. Cite the DOI of the report for the gene '
    'you used, shown on that gene’s page.')

# What each value means. Short on purpose: the scope is stated once above, and
# repeating a caveat on every row makes readers skip all of them.
#: What each value means, in the frame every reader-facing surface now uses.
#:
#: **The keys do not move.** ``recommended``/``not_recommended``/``not_tested``
#: are the API's published enum and the ints the extension index encodes, and
#: consumers switch on them. What changed on 29 Aug 2026 is the *prose*: a gene
#: page is headed "characterisation data", and in that context these describe
#: evidence rather than issue advice — the same instruction that produced
#: ``SCOPE_NOTE``'s framing, that "recommended" and "failed" are very strong
#: words for what is a measurement.
#:
#: Two surfaces deliberately keep the older wording, and they are not
#: inconsistency: ``data_access.html`` and ``connect_your_ai.html`` document the
#: enum *values*, where naming them anything else would make the documentation
#: wrong about the strings a client receives.
MEANINGS = {
    RECOMMENDED: ('The characterisation data supports this application in the '
                  'conditions tested.'),
    NOT_RECOMMENDED: ('Tested; the characterisation data does not support this '
                      'application in the conditions tested.'),
    NOT_TESTED: 'Not tested for this application.',
}

# ── The capability layer ────────────────────────────────────────────────────
#
# **A recommendation is one bit and the bench recorded two.** "Not recommended"
# has been carrying two quite different antibodies: one that showed nothing at
# all, and one that did the job the application is *for* and fell short on the
# rest. On live data (29 Aug 2026) that second group is **491 of the 1,833
# not-recommended verdicts** — 313 WB, 104 IP, 74 ICC-IF — so better than a
# quarter of every negative this dataset publishes deserves a qualifier it did
# not have.
#
# The layering is deliberately **not a fourth verdict value**. Four surfaces
# switch on the three above — the public API, the gene page, the MCP and the
# extension index, which encodes them as the small ints 0/1/2 in a file every
# install re-downloads daily. A new value would have reached the *shipped*
# extension build within about a day of being written, with no case for it. So
# the verdict is unchanged and the capability rides beside it; a surface draws
# the middle state as "NOT_RECOMMENDED and capable" when it is ready to, and one
# that has not been taught about it goes on behaving exactly as before.
#
# FC is absent by construction: `histogram_shift` is blank on all 22 live rows,
# so there is no capability to read and its verdicts stay two-state. That is a
# gap in the data rather than a judgement about flow cytometry, and it is worth
# saying out loud wherever the counts are shown.

#: What "capable" means for each application — the question its whole assay is
#: for. `pipeline.services.outcomes` owns the axis; this owns the words.
CAPABILITY_WORDS = {
    'WB': 'detect the target',
    'IP': 'enrich the target',
    'ICC-IF': 'give a selective signal',
}


def capability_axes(antibody_ids, applications=APPLICATIONS):
    """``{(antibody_id, application): axes}`` for a batch, in bulk.

    Two queries per application whatever the size of the batch — the callers
    resolve a whole gene page or a whole snapshot at once, and this must not
    become an N+1 on the extension index, which walks every published antibody.
    """
    from pipeline.services import outcomes as outcome_svc

    ids = list(antibody_ids)
    if not ids:
        return {}
    out = {}
    for app in applications:
        for ab_id, axes in outcome_svc.for_antibodies(ids, app).items():
            out[(ab_id, app)] = axes
    return out


def outcome_stamp():
    """One value that moves whenever a review judgement does — one query.

    For a caller that caches on a fingerprint. The capability axes are a third
    input to what a surface *says* about an antibody, beside the recommendation
    flag and the curated-gene set, and nothing about them shows in either: a
    reviewer filling in `detects` moves a verdict from *Not supportive* to
    *Limited support* without touching a flag or a file.

    A stamp rather than the derived words, because the caller that needs it
    (`core/api_manifest.py::_fingerprint`) is answering a conditional request
    and must not materialise the scope to do it. `assessed_at` is `auto_now`
    and the count catches a deletion, which between them cover every edit to
    this table.

    **It does not cover a session result row**, which `services/outcomes.py`
    also reads and merges. That would need the four result models stamped as
    well, and none of them carries a modified timestamp — so it is stated here
    rather than implied by a function that looks complete.

    Here rather than in the caller because `AntibodyOutcome` has exactly one
    public reader and this module is it — `pipeline/tests_outcomes.py::
    ThePublicLayerGoesThroughTheOneReaderTests` derives that from the source
    and fails on a fifth copy.
    """
    from django.db.models import Count, Max

    from pipeline.models import AntibodyOutcome

    row = AntibodyOutcome.objects.aggregate(last=Max('assessed_at'),
                                            rows=Count('pk'))
    return f"{row['rows']}:{row['last']}"


def is_capable(axes, application):
    """``True``/``False``/``None`` — did it do what the application is for?"""
    from pipeline.services import outcomes as outcome_svc

    answer = outcome_svc.capability(axes, application)
    if answer is None:
        return None
    return answer == outcome_svc.YES


#: What a gene-page cell says the **characterisation data** shows.
#:
#: Not the verdict vocabulary, deliberately. ``RECOMMENDED`` and
#: ``NOT_RECOMMENDED`` are what the API publishes and what the extension
#: switches on, and they do not move. But a gene page is headed *characterisation
#: data*, and in that context the cells describe evidence rather than issue
#: advice — the same instruction that produced ``SCOPE_NOTE``'s framing on
#: 27 Aug 2026: "recommended" and "failed" are very strong words for what is a
#: measurement.
CELL_WORDS = {
    RECOMMENDED: 'Supportive',
    NOT_RECOMMENDED: 'Not supportive',
}

#: The middle rung. A negative the data still says something on-target about is
#: **not** "not supportive with a footnote" — it is its own degree of support,
#: and naming it that way is the owner's (29 Aug 2026). It reads as a statement
#: about how much weight the evidence carries, rather than as a fault in the
#: antibody, which is the register the rest of this file already uses.
LIMITED_WORDS = 'Limited support'

#: The middle rung's entry in ``MEANINGS``, kept beside the word it explains.
#:
#: Not folded into ``MEANINGS`` itself, which is keyed by the three controlled
#: verdict values and must stay that way — this rung is not one of them, and a
#: fourth key in that dict would be a fourth value for every caller that walks
#: it. A surface drawing all four rungs (the bulk archive's README is the first)
#: reads ``MEANINGS`` and this.
#:
#: Same shape as the ``NOT_RECOMMENDED`` sentence it sits under, because that is
#: what it is: the negative, with the half the verdict could not carry.
LIMITED_MEANING = (
    'Tested; the characterisation data does not support this application '
    'overall, and the antibody was still seen to do what the application is '
    'for.')

#: Said once, wherever the middle rung appears. **The axes are inputs to a
#: judgement, not a formula that produces one** — an antibody can detect its
#: target and still carry enough non-selective signal to be hard to use with
#: confidence, and there is no threshold anybody can write down for that. So the
#: clause states what was *seen* and this states what it is worth, which is the
#: honest division of labour between them.
LIMITED_SUPPORT_NOTE = (
    'Support is an overall judgement, not a sum of the parts. An antibody can '
    'detect its target and still carry enough non-selective signal to be hard '
    'to use with confidence — there is no fixed threshold, and whether it is '
    'usable depends on what you need it for.'
)

#: The heading above the list of applications the data supports, on a gene page
#: and on an embed card. Same frame as the cells beneath it, from the same file,
#: because those two disagreeing on one screen is exactly the drift this module
#: was written to stop.
SUPPORTED_APPLICATIONS_LABEL = 'Characterisation data supports:'

#: What a surface colours by. **The verdict sets the colour and the qualifier is
#: a modifier on top of it** — three well-separated colours instead of four
#: competing ones, which is what stopped amber and red reading as the same
#: weight. They were competing on one axis, so red had to be muted to keep 1,300
#: cells from drowning the page, which put it right next to the amber. Doing
#: different jobs, red can be a proper red, and amber means one thing wherever
#: it appears: there is nuance here.
TONE_SUPPORTIVE = 'supportive'
#: Not supportive, **but the antibody did the thing the application is for.**
#: Its own colour rather than a mark on the negative — see ``tone``.
TONE_QUALIFIED = 'qualified'
TONE_NOT_SUPPORTIVE = 'not-supportive'
TONE_UNTESTED = ''
_TONE = {RECOMMENDED: TONE_SUPPORTIVE, NOT_RECOMMENDED: TONE_NOT_SUPPORTIVE,
         NOT_TESTED: TONE_UNTESTED}


def tone(verdict, tempers=False):
    """The colour family for one cell — the verdict **and** its qualifier.

    **One colour per cell, which is the whole of the fix** (owner, 29 Aug 2026).
    This drew the verdict's colour and laid an amber edge on top for the
    qualifier, so a caveated negative was a red border with an amber bar inside
    it: two neighbouring hues a couple of millimetres apart, indistinguishable
    on a phone. Now the combination picks one of three, and amber and red are
    only ever compared across rows.

    Amber is exactly *not supportive, and yet it did the thing the application
    is for* — a traffic light's amber, its own state rather than a footnote on
    the red. A **supportive** verdict stays green whatever its qualifier says:
    "but not selective" is a real limitation and it belongs in the caption, but
    the cell is still a supportive one and colouring it otherwise would
    contradict the word printed under it.
    """
    if verdict == NOT_RECOMMENDED and tempers:
        return TONE_QUALIFIED
    return _TONE.get(verdict, TONE_UNTESTED)


def caveat_tab(verdict, tempers):
    """Does this cell carry the small yellow tab?

    **Only on a supportive verdict** (owner, 29 Aug 2026). Green stays green
    when the data falls short of it — the antibody *is* supported for the
    application — and the shortfall rides as a small tab rather than by
    repainting the cell. On a negative the same qualifier is the colour itself:
    ``tone`` returns amber, "upgraded from red because the reagent has
    potential", and a tab as well would say one thing twice.

    Yellow beside green is the pair that reads at a glance; yellow beside red
    was the pair that did not, which is why the tab survives on one side and
    not the other.
    """
    return bool(tempers) and verdict == RECOMMENDED


def qualified(application, verdict, axes):
    """``(clause, tempers)`` — what follows the dash, and whether it pulls against.

    The clause belongs on **either** side of the verdict. The layer started life
    attached only to negatives, and that was half the job: 312 of the 879
    supportive western blot verdicts — 36% of the green on the site — are *not
    selective*, and they were drawn identically to the 566 that are clean.

    **``tempers`` is the other half, and shipping without it was a defect.** A
    qualifier either holds the verdict back or reinforces it, and only the first
    is a caveat. Drawn as one thing, *Supportive — strongly selective* came out
    with the amber edge the site uses for a reservation — the best result this
    dataset can record, marked as though something were wrong with it (owner,
    29 Aug 2026, on a live ATP2B1 page). So the clause and the colour are two
    facts and this returns both: an amber edge means the data fell short of the
    verdict, never that there is something extra to say.

    The clause states what was recorded and never a reason the data does not
    carry, so each appears only where the axis behind it was actually answered.
    """
    from pipeline.services import outcomes as outcome_svc

    if verdict == NOT_TESTED or not axes:
        return '', False
    value = lambda axis: (axes.get(axis) or {}).get('value')

    if verdict == RECOMMENDED:
        if application == 'ICC-IF':
            band = value('selective')
            # The band is the grade: how strongly, not whether. Both values
            # reinforce a supportive verdict, so neither is a caveat.
            return {'strongly_selective': 'strongly selective',
                    'selective': 'selective'}.get(band, ''), False
        if application == 'WB':
            # The one axis a supportive western blot can fall short on, and the
            # only clause on this side that holds the verdict back.
            if outcome_svc.binary_of(value('selective')) == outcome_svc.NO:
                return 'detects the target, but is not selective', True
        return '', False

    # A negative that did the thing the application is for. Every clause on this
    # side softens the verdict, which is what it is for.
    if is_capable(axes, application) is not True:
        return '', False
    clause = {'WB': 'detects the target',
              'IP': 'enriches the target, but not significantly',
              'ICC-IF': 'some selective signal'}.get(application, '')
    return clause, bool(clause)


def qualifier(application, verdict, axes):
    """The clause alone — what the public API and the extension index publish.

    They carry the sentence and leave the colour to whoever draws it, so the
    ``tempers`` half of ``qualified`` is not part of that contract.
    """
    return qualified(application, verdict, axes)[0]


#: Every qualifier this module can produce, as a two-letter code, with the
#: clause it stands for and whether it holds the verdict back.
#:
#: **The codes exist for the extension index and nothing else.** That file is a
#: megabyte before gzip and every install re-downloads it daily, so the
#: sentences cannot travel in it — one code per qualified record does, and the
#: client holds the wording. `core/tests_extension_scope.py` pins that the
#: client's table and this one carry the same codes, the same way the two tools
#: are held to one spelling of a catalogue number: a code the client cannot
#: read draws nothing, silently.
QUALIFIER_CODES = {
    'ns': ('detects the target, but is not selective', True),
    'sd': ('detects the target', True),
    'se': ('enriches the target, but not significantly', True),
    'si': ('some selective signal', True),
    'sl': ('selective', False),
    'xs': ('strongly selective', False),
}
_CODE_OF = {clause: code for code, (clause, _) in QUALIFIER_CODES.items()}


def qualifier_code(application, verdict, axes):
    """The two-letter code for this cell's qualifier, or ``''``.

    Derived from ``qualified`` rather than from a second set of rules, so a
    clause cannot exist that the codes do not cover.
    """
    clause, _ = qualified(application, verdict, axes)
    return _CODE_OF.get(clause, '')


def cell_caption(application, verdict, axes):
    """The line printed under a figure on a gene page.

    **Every tested cell says what the data shows, in words.** Ring colour alone
    was not enough and the amber ring made that plain: a plain negative carried
    no marking whatsoever, so "tested and did not perform" was drawn identically
    to "nobody has run this" — the one distinction this dataset must never blur,
    and the reason ``recommendation()`` has a gene gate at all. Colour also fails
    a reader who cannot see it.

    ``NOT_TESTED`` is the deliberate blank: the cell already carries a "No data
    available" image, and a caption saying so twice is noise.
    """
    if verdict == NOT_TESTED:
        return ''
    clause, tempers = qualified(application, verdict, axes)
    # The rung leads and the observation follows, so a reader who stops after
    # two words has still read how much weight the evidence carries.
    lead = words(verdict, tempers)
    return f'{lead} — {clause}' if clause else lead


def verdict(antibody, application, tested_applications, gene_is_curated,
            axes=None):
    """The recommendation, with the immunofluorescence veto applied.

    **A ratio below the floor cannot be supportive** (owner, 29 Aug 2026): for
    ICC-IF the measurement decides that much, whatever the flag says. It is a
    veto and not a promotion — clearing the floor never *confers* a supportive
    verdict, it earns the qualifier on a negative.

    Only ICC-IF, and only where the axis was actually answered: an unjudged
    figure is not evidence against itself.
    """
    value = recommendation(antibody, application, tested_applications,
                           gene_is_curated)
    if (value == RECOMMENDED and application == 'ICC-IF'
            and is_capable(axes, 'ICC-IF') is False):
        return NOT_RECOMMENDED
    return value


def verdict_with_qualifier(antibody, application, tested_applications,
                           gene_is_curated, axes=None):
    """``(verdict, clause, tempers)`` — everything a surface needs for one cell.

    ``axes`` comes from ``capability_axes``, resolved in bulk by the caller.
    ``tempers`` is the amber modifier: see ``qualified``.
    """
    value = verdict(antibody, application, tested_applications,
                    gene_is_curated, axes)
    clause, tempers = qualified(application, value, axes)
    return value, clause, tempers


#: What an untested application is called where a surface has to name it. The
#: gene page draws nothing (its cell already carries a "No data available"
#: image and saying it twice is noise), but an API row, a portal pill and a
#: model reading a paper all need the words.
UNTESTED_WORDS = 'Not tested'

#: The printed rung for each of the four controlled values.
#:
#: Built from the constants above rather than typed out, so the word a surface
#: prints and the value a consumer switches on cannot drift apart — which is
#: the entire promise ``oga_support`` makes and the one that would fail
#: silently. ``tests_recommendations`` asserts the round trip over every
#: ``(verdict, tempers)`` pair.
SUPPORT_WORDS = {
    SUPPORTIVE: CELL_WORDS[RECOMMENDED],
    LIMITED_SUPPORT: LIMITED_WORDS,
    NOT_SUPPORTIVE: CELL_WORDS[NOT_RECOMMENDED],
    NOT_TESTED: UNTESTED_WORDS,
}

#: What each rung means, for a surface documenting the vocabulary.
#:
#: The three that have a legacy equivalent reuse ``MEANINGS`` and
#: ``LIMITED_MEANING`` verbatim rather than restating them: two sentences for
#: one rung is the drift this module exists to stop, one level down from where
#: it usually stops it.
SUPPORT_MEANINGS = {
    SUPPORTIVE: MEANINGS[RECOMMENDED],
    LIMITED_SUPPORT: LIMITED_MEANING,
    NOT_SUPPORTIVE: MEANINGS[NOT_RECOMMENDED],
    NOT_TESTED: MEANINGS[NOT_TESTED],
}


def support(verdict, tempers=False):
    """The rung as a controlled value — the one reader for the four.

    ``(verdict, tempers)`` is exactly the pair ``words()`` takes, and that is
    deliberate: this is the same decision, published instead of printed. A
    surface that wants the word asks ``words()``, one that wants to switch asks
    this, and ``SUPPORT_WORDS`` is the bridge — so there is no third place where
    a rung could be decided differently.

    ``tempers`` comes from ``qualified()`` and means *the qualifier holds the
    verdict back*, which on a negative is precisely the middle rung. A
    supportive verdict the data fell short on is still ``SUPPORTIVE``: the
    antibody **is** supported for the application, the shortfall rides as the
    qualifier, and colouring or naming it otherwise would contradict the word
    printed under it (see ``caveat_tab``).
    """
    if verdict == RECOMMENDED:
        return SUPPORTIVE
    if verdict == NOT_RECOMMENDED:
        return LIMITED_SUPPORT if tempers else NOT_SUPPORTIVE
    if verdict == NOT_TESTED:
        return NOT_TESTED
    return ''


def words(verdict, tempers=False):
    """The rung a reader is shown — the one reader for all four.

    Four rungs, and the middle one needs the qualifier to exist at all:

    ==================  ================================================
    Supportive          the data supports the application
    Limited support     not supportive, and the antibody was still seen
                        to do something on-target
    Not supportive      tested, and nothing on-target was seen
    Not tested          nobody has run it
    ==================  ================================================

    Derived from ``support()`` since 14 Sep 2026, when the four rungs stopped
    being display-only and became ``oga_support`` on the API. Before that this
    function held the branch itself and the published field held three values;
    two copies of *which rung is this* would have been the same drift as the
    five verdicts ``describe`` was written to end.

    ``verdict`` keeps its three controlled values whatever this says — the
    legacy ``oga_recommendations`` publishes them and the extension encodes them
    as 0/1/2 in a file every install re-downloads daily.
    """
    return SUPPORT_WORDS.get(support(verdict, tempers), '')


def describe(antibody, application, tested_applications, gene_is_curated,
             axes=None):
    """Everything a surface needs to state one verdict — the one reader.

    The gene page, the public API, the portal, the MCP connector and the
    browser extension all answer the same question about the same antibody, and
    each of them used to compose its own sentence and pick its own colour out
    of the three verdict values. That is the drift this module exists to stop,
    one level up from where it was stopping it: `recommendation()` kept the
    *verdict* from splitting into five answers, and the words and the colour
    then split into five anyway.

    Seven keys, and every surface takes the ones it can draw:

    ``support``
        The controlled value to switch on — ``supportive``/``limited_support``/
        ``not_supportive``/``not_tested``, one per printed rung. **This is the
        one to write new code against**, and it is the only one of the two that
        can express the middle rung.
    ``verdict``
        The legacy controlled value — ``recommended``/``not_recommended``/
        ``not_tested``. **Unchanged and unchanging**: the API publishes it, the
        extension encodes it as 0/1/2 in a file every install re-downloads
        daily, and a shipped build must go on reading it. It cannot tell
        *Limited support* from *Not supportive*; ``support`` is what fixes that,
        and neither replaces the other in the wire format.
    ``words``
        What a person is shown for that verdict — *Supportive*, *Not
        supportive*, *Not tested*. Deliberately not the verdict's own spelling:
        a gene page is headed "characterisation data", and in that frame the
        answer describes evidence rather than issuing advice.
    ``qualifier``
        The clause after the dash, on either side of the verdict, or ``''``.
    ``sentence``
        The two composed — *Supportive — but not selective*. One writer, so no
        surface assembles its own dash.
    ``tone``
        The colour family: ``supportive`` (green), ``qualified`` (amber),
        ``not-supportive`` (red), or ``''`` for untested. A negative that did
        the thing the application is for is amber rather than red, which is the
        colour carrying the qualifier on that side.
    ``tab``
        True where the qualifier rides as a small yellow tab instead: a
        supportive verdict the data fell short of keeps its green, because the
        antibody *is* supported for the application.
    """
    value, clause, tempers = verdict_with_qualifier(
        antibody, application, tested_applications, gene_is_curated, axes)
    return {
        'support': support(value, tempers),
        'verdict': value,
        'words': words(value, tempers),
        'qualifier': clause,
        'sentence': (cell_caption(application, value, axes)
                     or words(value, tempers)),
        'tone': tone(value, tempers),
        'tab': caveat_tab(value, tempers),
    }


def describe_all(antibody, tested_applications, gene_is_curated, axes=None,
                 applications=APPLICATIONS):
    """``describe`` for each application, keyed as the verdict maps are.

    ``axes`` is the bulk map from ``capability_axes``, resolved by the caller —
    resolving it per antibody is the N+1 that only shows as a slow feed, which
    is what `TheFeedsDoNotQueryPerGeneTests` exists to catch.
    """
    if axes is None:
        axes = capability_axes([antibody.pk], applications)
    return {
        app: describe(antibody, app, tested_applications, gene_is_curated,
                      axes.get((antibody.pk, app)))
        for app in applications
    }


def recommendation(antibody, application, tested_applications, gene_is_curated):
    """What OGA recommends for one antibody in one application.

    ``tested_applications`` is the set of ``PublicationImage.application_type``
    values that exist for this antibody; ``gene_is_curated`` is whether any
    antibody against the same gene carries any recommendation.

    Both are passed in rather than looked up, because the callers resolve them
    in bulk — one query for the whole page, not one per antibody.
    """
    if getattr(antibody, _FLAG[application], False):
        return RECOMMENDED
    if application in tested_applications and gene_is_curated:
        return NOT_RECOMMENDED
    return NOT_TESTED


def recommendations_for(antibody, tested_applications, gene_is_curated):
    """All four recommendations for one antibody, keyed by application."""
    return {
        app: recommendation(antibody, app, tested_applications, gene_is_curated)
        for app in APPLICATIONS
    }


def curated_gene_ids(target_ids=None):
    """Which targets have had their recommendations set.

    One query for the whole batch. ``_target_has_recommendations`` in
    ``api_views`` answers the same question one target at a time, which is fine
    for a gene page and is an N+1 on a feed — this is the bulk form.

    ``target_ids=None`` means every target, for callers that build a snapshot of
    the whole dataset (the extension index) and would otherwise pass a few
    hundred ids into an ``IN`` clause to learn the same thing.
    """
    from pipeline.models import Antibody
    from django.db.models import Q

    any_flag = (Q(wb_recommended=True) | Q(ip_recommended=True)
                | Q(if_recommended=True) | Q(fc_recommended=True))

    qs = Antibody.objects.all()
    if target_ids is not None:
        ids = list(target_ids)
        if not ids:
            return set()
        qs = qs.filter(target_id__in=ids)

    return set(
        qs.filter(any_flag)
          .values_list('target_id', flat=True)
          .distinct()
    )
