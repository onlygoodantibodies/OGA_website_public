"""check_manuscript — resolving the reagents a caller read out of a paper.

There is no server-side text parsing: the caller identifies the reagents, this
resolves them against the knockout-controlled dataset. What is defended here is
the resolution and the framing — dedup, per-application verdicts, untested being
reported as untested rather than as a quality judgement, and the public boundary.
"""
from __future__ import annotations

from mcp_servers.common import portal


def _ab(identifier, target=None, role="primary", **kw):
    r = {"identifier": identifier, "role": role}
    if target:
        r["target"] = target
    r.update(kw)
    return r


# ── resolution ───────────────────────────────────────────────────────────────

def test_recommended_antibody_resolves_with_per_application_verdict():
    res = portal.check_manuscript(reagents=[_ab("ab212184", "SNCA")])
    hit = res["antibody_hits"]["recommended"][0]
    assert hit["identifier"] == "ab212184"
    assert hit["gene"] == "SNCA"
    # verdicts are per application, never a single overall grade
    assert isinstance(hit["applications"], dict)
    assert set(hit["applications"]) & {"WB", "IP", "IF", "FC"}


def test_not_recommended_antibody():
    res = portal.check_manuscript(reagents=[_ab("2642", "SNCA")])
    assert {h["identifier"] for h in res["antibody_hits"]["not_recommended"]} == {"2642"}


def test_absent_antibody_listed_not_in_dataset_not_as_validated():
    res = portal.check_manuscript(reagents=[_ab("ZZ99999", "NOTAGENE")])
    assert res["not_in_dataset"][0]["identifier"] == "ZZ99999"
    assert all(not b for b in res["antibody_hits"].values())
    # untested is NOT a verdict on quality, and the note must say so
    assert "untested" in res["note"].lower()
    assert "not a verdict" in res["note"].lower()


def test_rrid_and_catalogue_for_same_antibody_dedupe_to_one_hit():
    res = portal.check_manuscript(reagents=[
        _ab("ab212184", "SNCA", rrid="AB_2895247")])
    hits = [h for b in res["antibody_hits"].values() for h in b]
    assert len(hits) == 1


def test_cst_pack_size_suffix_resolves_to_bare_catalogue():
    # OGA stores CST catalogues without the pack-size letter; 2642T is 2642.
    res = portal.check_manuscript(reagents=[_ab("2642T", "SNCA")])
    hits = [h for b in res["antibody_hits"].values() for h in b]
    assert hits and hits[0]["identifier"] == "2642T"


# ── publisher typesetting must not read as absence ───────────────────────────
#
# Five of 59 antibodies in the 100-pair benchmark were reported absent from the
# dataset when the dataset held a not-recommended result for them, on nothing but
# how the publisher set the number. That is worse here than in the extension: the
# extension shows nothing, whereas this server states `oga_tested: "no"` and its
# own note tells the reader absence is not evidence about quality — so a reader was
# actively directed to treat as unknown an antibody with a documented failure.

def test_identifiers_as_publishers_print_them_resolve():
    from mcp_servers.common.manuscript import resolution_variants

    # (as printed, what it must reach) — every one from the benchmark run.
    for printed, stored in [
        ("14 060-1-AP", "14060-1-AP"),        # BMJ: space as thousands separator
        ("14,060-1-AP", "14060-1-AP"),        # comma
        ("14,060–1-AP", "14060-1-AP"),   # Springer: comma AND an en-dash
        ("23,274–1-AP", "23274-1-AP"),
        ("11 306-1-AP", "11306-1-AP"),
        ("14 060-1-AP", "14060-1-AP"),   # thin space
        ("#ab74140", "ab74140"),              # leading hash
        ("PA1–914", "PA1-914"),          # en-dash for the hyphen
    ]:
        assert stored in resolution_variants(printed), printed
        # as-given is always tried first, so normalising can only ever ADD a match
        assert resolution_variants(printed)[0] == printed


def test_ordinary_identifiers_are_not_rewritten():
    from mcp_servers.common.manuscript import resolution_variants

    for plain in ("sc-138763", "NBP2-23490", "ab15954", "AB_2039791",
                  "13927-1-AP", "ARP54635_P050"):
        assert resolution_variants(plain) == [plain], plain


def test_a_full_stop_between_digits_is_never_stripped():
    """It is a decimal point far more often than a separator, and stripping it
    turns a concentration into a catalogue number."""
    from mcp_servers.common.manuscript import normalise_identifier

    assert normalise_identifier("1.500") == "1.500"


def test_every_lookup_door_normalises_not_just_check_manuscript():
    """The re-run reported this fix as never having landed, and it was half right.

    `resolution_variants` was wired into `check_manuscript` only, so the
    confirmation fixtures — which go through `antibody_validation` and come back
    `in_dataset: false` — met a bare `catalogue_number__iexact` and saw the raw
    string. A fix on one of three doors is a fix for one door.
    """
    typeset = "#ab212184"

    rows, _ = portal.antibody_validation(catalogue=typeset)
    assert [r["antibody_name"] for r in rows] == ["ab212184"], "antibody_validation"

    rows, _ = portal.search_antibodies(typeset)
    assert "ab212184" in {r["antibody_name"] for r in rows}, "search_antibodies"

    res = portal.check_manuscript(reagents=[_ab(typeset, "SNCA")])
    assert not res["not_in_dataset"], "check_manuscript"


def test_a_catalogue_stored_with_a_space_is_reachable_without_one():
    """The other side of the same coin, and the half nobody had looked at.

    Seven published catalogue numbers are STORED with a digit-group space —
    Synaptic Systems' house style. Cleaning only the incoming string cannot reach
    them from a paper that closed the number up, which is how most journals set it.
    """
    for printed in ("107 011", "107011"):
        rows, _ = portal.antibody_validation(catalogue=printed)
        assert [r["antibody_name"] for r in rows] == ["107 011"], printed

        res = portal.check_manuscript(reagents=[_ab(printed, "SYT1")])
        assert not res["not_in_dataset"], printed
        hits = [h for b in res["antibody_hits"].values() for h in b]
        assert hits[0]["gene"] == "SYT1", printed


# ── the LABEL around the number, and the number's own punctuation ────────────
#
# The second measured identifier failure, and unrelated to controls. Eight
# benchmark antibodies that ARE in the dataset came back `not_in_dataset` because
# they arrived wrapped in what the paper printed around them — `cat. no.
# 14060-1-AP`, `Proteintech #66140-1-Ig`, `Affinity BioReagents, PA1-914` — or with
# the number's own hyphen moved: `MA5-11154` printed `MA511154`.

def test_a_catalogue_wrapped_in_its_label_still_resolves():
    from mcp_servers.common.manuscript import resolution_variants

    for printed, stored in [
        ("cat. no. 18420-1-AP", "18420-1-AP"),
        ("Cat. No. ab171939", "ab171939"),
        ("catalogue #ab212184", "ab212184"),
        ("Proteintech #66184-1-Ig", "66184-1-Ig"),
        ("Affinity BioReagents, GTX100685", "GTX100685"),
        ("Cell Signaling Technology, 2642", "2642"),
        ("#ab74140", "ab74140"),
    ]:
        assert stored in resolution_variants(printed), printed
        # …and never at the cost of the string the reader will search for.
        assert resolution_variants(printed)[0] == printed


def test_a_label_word_that_is_really_the_catalogue_number_survives():
    """`NO-1234` starts with "no". Stripping it would leave `-1234`, so a strip is
    only accepted when what remains still holds a digit AND starts on an
    alphanumeric."""
    from mcp_servers.common.manuscript import strip_printing

    assert strip_printing("NO-1234") == "NO-1234"
    assert strip_printing("REF12345") == "REF12345"
    # A digit-group space is closed up BEFORE the supplier is peeled, so Synaptic
    # Systems' own house style keeps both halves instead of losing the first.
    assert strip_printing("Synaptic Systems, 107 011") == "107011"


def test_a_hyphen_the_publisher_moved_still_resolves():
    """The one direction cleaning the input cannot fix: a hyphen the paper DROPPED
    cannot be put back, so both sides collapse to alphanumerics and meet there."""
    for printed in ("18420-1AP", "184201AP", "Proteintech #18420-1AP"):
        res = portal.check_manuscript(reagents=[_ab(printed, "SQSTM1")])
        assert not res["not_in_dataset"], printed
        hits = [h for b in res["antibody_hits"].values() for h in b]
        assert hits[0]["gene"] == "SQSTM1", printed
        # echoed back exactly as printed, never tidied
        assert hits[0]["identifier"] == printed, printed


def test_a_short_number_never_matches_on_punctuation_alone():
    """The collapsed form is the weakest there is, and catalogue numbers are not
    unique across suppliers. Below MIN_COLLAPSED it is not offered at all."""
    from mcp_servers.common.manuscript import collapse_identifier

    assert collapse_identifier("2642") == ""
    assert collapse_identifier("A-5-4") == ""
    assert collapse_identifier("18420-1-AP") == "184201AP"


def test_a_pack_size_suffix_reaches_the_bare_number():
    """OGA stores Cell Signaling catalogues without the size letter and Novus
    without the sample-size `SS`."""
    from mcp_servers.common.manuscript import resolution_variants

    assert "2642" in resolution_variants("2642S")
    assert "NB120-1234" in resolution_variants("NB120-1234SS")
    assert "NBP2-24630" in resolution_variants("NBP2-24630SS")

    res = portal.check_manuscript(reagents=[_ab("NB120-1234SS", "SYT1")])
    assert not res["not_in_dataset"]


def test_a_label_stripped_form_never_outranks_a_real_exact_match():
    """As-given is always first, so every added form can only ADD a match."""
    from mcp_servers.common.manuscript import resolution_variants

    assert resolution_variants("107 011")[0] == "107 011"
    rows, _ = portal.antibody_validation(catalogue="cat. no. 107 011")
    assert [r["antibody_name"] for r in rows] == ["107 011"]


def test_a_typeset_catalogue_reaches_the_same_record_as_the_plain_one():
    plain = portal.check_manuscript(reagents=[_ab("ab212184", "SNCA")])
    typeset = portal.check_manuscript(reagents=[_ab("#ab212184", "SNCA")])
    assert not typeset["not_in_dataset"]
    got = [h for b in typeset["antibody_hits"].values() for h in b]
    want = [h for b in plain["antibody_hits"].values() for h in b]
    assert got and got[0]["gene"] == want[0]["gene"]
    # …and it is echoed back exactly as the paper printed it, never tidied.
    assert got[0]["identifier"] == "#ab212184"


# ── roles: the field a text parser could not determine ───────────────────────

def test_non_primary_roles_are_excluded_from_the_antibody_results():
    res = portal.check_manuscript(reagents=[
        _ab("M5909", role="isotype_control"),
        _ab("F0313", role="secondary"),
        _ab("A5441", role="loading_control"),
        _ab("ab212184", "SNCA"),
    ])
    hits = [h["identifier"] for b in res["antibody_hits"].values() for h in b]
    assert hits == ["ab212184"]
    assert res["not_in_dataset"] == []


def test_reagent_with_no_role_is_treated_as_a_primary():
    res = portal.check_manuscript(reagents=[{"identifier": "ab212184"}])
    assert [h for b in res["antibody_hits"].values() for h in b]


# ── the caller's own reading survives for anything the DB cannot supply ──────

def test_caller_target_and_figures_are_kept_for_an_unresolved_reagent():
    res = portal.check_manuscript(reagents=[
        _ab("AV35098", "alpha-smooth muscle actin", figures=["5a"])])
    entry = res["not_in_dataset"][0]
    assert entry["target"] == "alpha-smooth muscle actin"
    assert entry["figures"] == ["5a"]


# ── genes ────────────────────────────────────────────────────────────────────

def test_gene_hits_are_separate_and_public_only():
    res = portal.check_manuscript(reagents=[], genes=["SNCA", "MAPT"])
    names = {g["gene"] for g in res["gene_hits"]}
    assert "SNCA" in names          # public: has a published figure
    assert "MAPT" not in names      # non-public: no publication image


def test_matched_on_is_present_only_when_it_has_something_to_say():
    # It used to be an explicit null on every hit, which reads as a field that never
    # works — especially next to the `target_matched_on` on an unresolved reagent,
    # which is absent unless it has something to report.
    canonical = portal.check_manuscript(reagents=[], genes=["SNCA"])["gene_hits"][0]
    assert canonical["gene"] == "SNCA"
    assert "matched_on" not in canonical         # nothing to report

    alias = portal.check_manuscript(reagents=[], genes=["PARK1"])["gene_hits"][0]
    assert alias["gene"] == "SNCA"
    assert alias["matched_on"] == "PARK1"        # the name the caller used


def test_a_gene_written_the_way_a_paper_writes_it_still_resolves():
    # The genes list went through the whole-string resolver while a reagent's target
    # label went through the name-by-name one, so the identical string resolved on a
    # reagent and vanished from gene_hits.
    hits = portal.check_manuscript(reagents=[], genes=["SQSTM1/p62"])["gene_hits"]
    assert [h["gene"] for h in hits] == ["SQSTM1"]
    assert hits[0]["matched_on"] == "SQSTM1/p62"     # the caller's own string


def test_truncation_flag_when_capped():
    res = portal.check_manuscript(
        reagents=[_ab(f"CAT{i:05d}") for i in range(10)], cap=5)
    assert res["truncated"] is True


def test_empty_call_is_safe():
    res = portal.check_manuscript(reagents=[])
    assert res["counts"]["recommended"] == 0
    assert res["not_in_dataset"] == []


# ── our data must be found even when the paper names the protein ─────────────

def test_gene_resolves_from_protein_name_and_alias_not_just_the_symbol():
    from pipeline.models import Target
    t = Target.objects.filter(gene_name="SNCA").first()
    assert t is not None
    for name in filter(None, [t.gene_name, t.protein_name,
                              (t.aliases or "").split(",")[0].strip() or None]):
        res = portal.check_manuscript(reagents=[], genes=[name])
        assert [g["gene"] for g in res["gene_hits"]] == ["SNCA"], name


def test_unknown_gene_name_is_simply_absent():
    res = portal.check_manuscript(reagents=[], genes=["NOTAREALGENE"])
    assert res["gene_hits"] == []


# ── one reagent resolves once, across every identifier form it was given ─────

def test_reagent_with_one_good_form_is_not_also_reported_as_untested():
    # A correct catalogue plus an RRID we do not hold used to produce BOTH a
    # validated hit and an "untested" row for the same antibody.
    res = portal.check_manuscript(reagents=[
        {"identifier": "ab212184", "rrid": "AB_NOTOURS", "target": "SNCA"}])
    hits = [h["identifier"] for b in res["antibody_hits"].values() for h in b]
    assert hits == ["ab212184"]
    assert res["not_in_dataset"] == []


def test_reagent_with_no_resolvable_form_is_reported_once():
    res = portal.check_manuscript(reagents=[
        {"identifier": "ZZ99999", "rrid": "AB_NOTOURS", "target": "NOTAGENE"}])
    assert len(res["not_in_dataset"]) == 1


# ── every gene entry point resolves the same way ─────────────────────────────

def test_all_gene_entry_points_accept_protein_name_and_alias():
    from pipeline.models import Target
    t = Target.objects.filter(gene_name="SNCA").first()
    names = [t.gene_name, t.protein_name] + [
        a.strip() for a in (t.aliases or "").split(",") if a.strip()]
    for name in names:
        assert portal.gene_detail(name)["found"] is True, f"target_report: {name}"
        assert portal.antibody_validation(gene=name)[0], f"antibody_validation: {name}"
        assert portal.search_antibodies(name)[0], f"search_antibodies: {name}"
        assert portal.antibodies_by_recommendation("WB", gene=name)[0], name
        assert [g["gene"] for g in portal.check_manuscript(
            reagents=[], genes=[name])["gene_hits"]] == ["SNCA"], name


def test_unknown_gene_is_still_absent_everywhere():
    assert portal.gene_detail("NOTAREALGENE")["found"] is False
    assert portal.search_antibodies("NOTAREALGENE")[0] == []
    assert portal.antibody_validation(gene="NOTAREALGENE")[0] == []


# ── untested reagent, characterised target: the reader's actual decision ─────
# The case that prompted this: a paper used antibodies against genes OGA HAS
# worked on, but those particular antibodies were never tested. Reporting them as
# "not in the dataset" and stopping there withholds the one thing that reader can
# act on — that characterised alternatives exist for the same target.

def test_untested_reagent_gets_its_target_gene_page_and_alternative_count():
    res = portal.check_manuscript(reagents=[
        _ab("ZZ-UNTESTED-1", "SNCA")])
    entry = res["not_in_dataset"][0]
    assert entry["gene"] == "SNCA"
    assert entry["gene_page_url"].endswith("/antibodies/SNCA/")
    assert entry["target_is_characterised"] is True
    assert entry["recommended_alternatives"] >= 1


def test_alternatives_are_listed_once_per_gene_not_per_reagent():
    res = portal.check_manuscript(reagents=[
        _ab("ZZ-UNTESTED-1", "SNCA"), _ab("ZZ-UNTESTED-2", "SNCA")])
    alts = res["characterised_alternatives"]
    assert list(alts) == ["SNCA"]
    a = alts["SNCA"][0]
    assert a["antibody"] and a["supplier"] and a["recommended_for"]
    # only genuinely recommended antibodies are offered as alternatives
    assert "2642" not in {x["antibody"] for x in alts["SNCA"]}


def test_target_named_by_protein_name_still_finds_alternatives():
    res = portal.check_manuscript(reagents=[
        _ab("ZZ-UNTESTED-1", "Alpha-synuclein")])
    assert res["not_in_dataset"][0]["gene"] == "SNCA"
    assert "SNCA" in res["characterised_alternatives"]


def test_uncurated_target_offers_no_alternatives():
    # QPRT is public but uncurated — there is nothing to recommend, and we must not
    # imply there is.
    res = portal.check_manuscript(reagents=[_ab("ZZ-UNTESTED-3", "QPRT")])
    entry = res["not_in_dataset"][0]
    assert entry["target_is_characterised"] is False
    assert entry["recommended_alternatives"] == 0
    assert res["characterised_alternatives"] == {}


def test_unknown_target_adds_no_gene_context():
    res = portal.check_manuscript(reagents=[_ab("ZZ-UNTESTED-4", "NOTAGENE")])
    assert "gene_page_url" not in res["not_in_dataset"][0]


# ── the target label as papers actually write it ─────────────────────────────
# The feature above worked and then never fired on a real methods section, because
# the label was matched whole: "NFE2L2" resolved, "Nrf2 (NFE2L2)" did not, and
# "SQSTM1/p62" did not — while both genes came back characterised in the same
# response. Every string here contains a name that already resolved on its own.

def _alt_entry(target, genes=("SQSTM1",), **kw):
    res = portal.check_manuscript(reagents=[_ab("ZZ-UNTESTED-9", target, **kw)],
                                  genes=list(genes))
    return res, res["not_in_dataset"][0]


def test_gene_in_parentheses_resolves():
    _, entry = _alt_entry("p62 (SQSTM1)")
    assert entry["gene"] == "SQSTM1"
    assert entry["recommended_alternatives"] >= 1


def test_slash_separated_names_resolve():
    _, entry = _alt_entry("SQSTM1/p62")
    assert entry["gene"] == "SQSTM1"


def test_the_name_that_resolved_is_reported():
    # Attaching a reagent to a gene must be auditable, not asserted.
    _, entry = _alt_entry("p62 (SQSTM1)")
    assert entry["target_matched_on"] in {"p62", "SQSTM1"}
    # a label that resolved whole claims no sub-name
    _, plain = _alt_entry("SQSTM1")
    assert "target_matched_on" not in plain


def test_label_joins_against_the_callers_own_gene_list():
    # SQSTM1 is only reachable here through the caller's `genes` list, which is the
    # cheapest and safest vocabulary to match a label against.
    res = portal.check_manuscript(
        reagents=[_ab("ZZ-UNTESTED-9", "autophagy receptor (SQSTM1)")],
        genes=["SQSTM1"])
    assert res["not_in_dataset"][0]["gene"] == "SQSTM1"


def test_greek_letter_prefix_is_transliterated_not_stripped():
    # "α-synuclein" is Alpha-synuclein; it is NOT "synuclein" with a prefix removed.
    res = portal.check_manuscript(reagents=[_ab("ZZ-UNTESTED-9", "α-synuclein")])
    assert res["not_in_dataset"][0]["gene"] == "SNCA"


def test_a_word_lifted_out_of_a_phrase_must_clear_a_higher_bar():
    """`"Apotrack" -> CD44` in the benchmark, and the alias table was not at fault.

    `_target_names` splits a label into single words as a last resort, and
    `"ApoTrack (Cytochrome c in Apoptosis)"` emitted `in` — which is a correct,
    current HGNC alias for CD44, the Indian blood group antigen. The lookup then
    did exactly what it was asked. So the guard belongs on the tokeniser: a word
    taken out of a phrase is our reading, not the caller's, and a preposition can
    never be a gene.
    """
    names = {n.lower() for n in portal._target_names(
        "ApoTrack (Cytochrome c in Apoptosis)")}
    assert "in" not in names
    assert "c" not in names
    # the caller's own words survive — only the split is narrowed
    assert "apotrack" in names

    for label in ("antibodies raised against the light chain",
                  "anti-SNCA and the total protein control"):
        for word in ("and", "the", "against", "light", "chain", "total"):
            assert word not in {n.lower() for n in portal._target_names(label)}, (
                label, word)


def test_a_real_short_alias_still_resolves():
    """The guard must not cost `p62` -> SQSTM1, which is the same mechanism
    working correctly and the reason the alias path exists at all."""
    _, entry = _alt_entry("p62")
    assert entry["gene"] == "SQSTM1"
    _, split = _alt_entry("SQSTM1/p62 autophagy receptor")
    assert split["gene"] == "SQSTM1"


def test_a_match_made_on_an_alias_says_so():
    """No rule separates `p62` -> SQSTM1 from `p65` -> SYT1: both are short, real
    and exact, and RELA is not in the dataset to compete for the second. The
    server cannot pick correctly, so it discloses instead.

    `genes=()` is the case that needs it. When the caller DOES name the gene, the
    label is resolved against their own list and there is nothing to disclose —
    which is why the caveat stays rare enough to be read.
    """
    _, entry = _alt_entry("p62", genes=())
    assert entry["gene"] == "SQSTM1"
    assert entry["target_matched_via"] == "alias"


def test_an_alias_shared_with_a_gene_we_do_not_hold_resolves_to_nothing():
    """`p65` is the everyday name for RELA and an old name for SYT1, and six
    reagents across four benchmark papers meant RELA.

    Disclosure was not enough: the reply still named SYT1 and still offered five
    SYT1 antibodies. A recommendation is what turns a resolution error into a
    reagent someone might buy, so the gene — and with it the alternatives — goes.
    """
    for label in ("p65", "NF-κB p65"):
        _, entry = _alt_entry(label, genes=())
        assert "gene" not in entry, label
        assert "recommended_alternatives" not in entry, label
        assert entry["target_ambiguous"]["candidates"] == ["RELA", "SYT1"], label

    res = portal.check_manuscript(
        reagents=[_ab("ZZ-UNTESTED-9", "p65")], genes=[])
    assert res["characterised_alternatives"] == {}


def test_the_ambiguity_guard_is_not_over_tight():
    """The brief's other fixtures. A guard that also breaks ordinary resolution
    has cost more than it saved.

    The brief names `Parkin` -> PRKN; PRKN is not in this seed, so the same two
    paths are exercised on genes that are — a protein name and a stored alias.
    """
    _, protein = _alt_entry("Alpha-synuclein", genes=())
    assert protein["gene"] == "SNCA"               # protein-name path

    _, alias = _alt_entry("PARK1", genes=())
    assert alias["gene"] == "SNCA"                 # ordinary alias path

    _, p62 = _alt_entry("p62", genes=())
    assert p62["gene"] == "SQSTM1"                 # a short alias still resolves

    _, apotrack = _alt_entry("Apotrack Cytofluorometric Apoptosis Kit", genes=())
    assert "gene" not in apotrack
    assert "target_ambiguous" not in apotrack      # unresolvable, not ambiguous


def test_a_clean_name_in_the_label_outranks_an_ambiguous_one():
    """`SQSTM1/p65` has a name that resolves outright, so the collision never
    comes up — ambiguity is the answer of last resort, not the first."""
    _, entry = _alt_entry("SQSTM1/p65", genes=())
    assert entry["gene"] == "SQSTM1"
    assert "target_ambiguous" not in entry


def test_a_match_on_the_symbol_itself_carries_no_caveat():
    """The caveat has to stay rare, or it stops being read."""
    _, entry = _alt_entry("SQSTM1")
    assert "target_matched_via" not in entry

    # The caller's own gene list is a vocabulary they supplied for this paper, so a
    # hit there is not an alias guess and is not flagged either.
    res = portal.check_manuscript(
        reagents=[_ab("ZZ-UNTESTED-9", "autophagy receptor (SQSTM1)")],
        genes=["SQSTM1"])
    assert "target_matched_via" not in res["not_in_dataset"][0]


def test_a_label_with_no_gene_in_it_still_resolves_to_nothing():
    # Splitting a label must not turn a miss into a false positive.
    for label in ("some random protein", "recombinant human antibody", "12"):
        res = portal.check_manuscript(reagents=[_ab("ZZ-UNTESTED-9", label)],
                                      genes=["SNCA"])
        assert "gene" not in res["not_in_dataset"][0], label


# ── the shortlist must not misreport itself ──────────────────────────────────
# SQSTM1 has seven recommended antibodies across three suppliers. The shortlist
# holds five, and used to report that five as the total with nothing to say it had
# been shortened — and picked those five by supplier name, so one supplier's
# catalogue crowded out everyone else's.

def test_alternative_count_is_the_gene_total_not_the_shortlist_length():
    res, entry = _alt_entry("SQSTM1")
    assert entry["recommended_alternatives"] == 7               # the gene's total
    assert entry["alternatives_listed"] == 5                    # what is listed
    assert len(res["characterised_alternatives"]["SQSTM1"]) == 5
    assert entry["alternatives_truncated"] is True


def test_the_count_is_named_for_what_it_holds_recommended_not_tested():
    # SQSTM1 has eight published antibodies; seven are recommended for at least one
    # application and one was tested and not recommended. The field counts the
    # RECOMMENDED ones, and the connector keeps tested and recommended apart
    # everywhere else, so the name has to as well.
    res, entry = _alt_entry("SQSTM1")
    assert "tested_alternatives" not in entry
    assert entry["recommended_alternatives"] == 7
    offered = {a["antibody"] for a in res["characterised_alternatives"]["SQSTM1"]}
    assert "66184-1-Ig" not in offered          # tested, not recommended


def test_a_complete_shortlist_is_not_flagged_as_truncated():
    res = portal.check_manuscript(reagents=[_ab("ZZ-UNTESTED-9", "SNCA")])
    entry = res["not_in_dataset"][0]
    assert entry["alternatives_listed"] == entry["recommended_alternatives"]
    assert entry["alternatives_truncated"] is False


def test_the_shortlist_spans_suppliers_rather_than_one_catalogue():
    res = portal.check_manuscript(reagents=[_ab("ZZ-UNTESTED-9", "SQSTM1")])
    suppliers = {a["supplier"] for a in res["characterised_alternatives"]["SQSTM1"]}
    assert len(suppliers) >= 3, suppliers


def test_rank_leads_and_the_supplier_round_robin_only_breaks_ties():
    # Truncation makes ordering a claim: a reader told they are seeing "5 of 7" will
    # assume they are the best 5. Interleaving suppliers FIRST made that untrue — it
    # could promote a single-application antibody over a broader one purely to give
    # the next supplier a turn.
    res = portal.check_manuscript(reagents=[_ab("ZZ-UNTESTED-9", "SQSTM1")])
    offered = res["characterised_alternatives"]["SQSTM1"]
    breadth = [len(a["recommended_for"]) for a in offered]
    assert breadth == sorted(breadth, reverse=True), offered
    assert offered[0]["antibody"] == "ab109012"     # WB+IP+IF
    assert offered[1]["antibody"] == "GTX100685"    # WB+IF, a different supplier


def test_equal_breadth_still_needs_applications_to_separate_them():
    # The limit of ranking without the schema, stated so it is not a surprise:
    # breadth cannot tell an FC-only antibody from a WB-only one — both cover one
    # application. Only `applications` decides between them, which is why the note
    # tells the caller to check `recommended_for` before offering a row.
    res = portal.check_manuscript(reagents=[_ab("ZZ-UNTESTED-9", "SQSTM1")])
    offered = res["characterised_alternatives"]["SQSTM1"]
    assert "ab207305" in {a["antibody"] for a in offered}     # FC-only, breadth 1
    # …and naming the application is what removes it.
    res = portal.check_manuscript(reagents=[
        _ab("ZZ-UNTESTED-9", "SQSTM1", applications=["WB"])])
    assert "ab207305" not in {a["antibody"]
                              for a in res["characterised_alternatives"]["SQSTM1"]}


def test_alternatives_are_ranked_for_the_application_the_paper_used():
    # ab207305 is recommended for FC only. It is not an alternative for a western
    # blot, and must not displace a WB-recommended antibody in a five-row shortlist.
    res = portal.check_manuscript(reagents=[
        _ab("ZZ-UNTESTED-9", "SQSTM1/p62", applications=["western blot"])])
    offered = res["characterised_alternatives"]["SQSTM1"]
    assert all("WB" in a["recommended_for"] for a in offered), offered
    assert "ab207305" not in {a["antibody"] for a in offered}


def test_applications_filter_the_shortlist_they_do_not_merely_rank_it():
    # An antibody recommended for FC is not an alternative for a western blot, so it
    # does not take a slot the caller is told to discard.
    res = portal.check_manuscript(reagents=[
        _ab("ZZ-UNTESTED-9", "SQSTM1", applications=["WB"])])
    offered = res["characterised_alternatives"]["SQSTM1"]
    assert all("WB" in a["recommended_for"] for a in offered), offered


def test_both_counts_are_carried_and_n_of_m_uses_the_scoped_one():
    # SQSTM1: 7 recommended for something, 6 of them for WB, 5 listed. The gene total
    # keeps saying how much data exists; the scoped count is the honest denominator
    # once the caller has said what they need.
    res = portal.check_manuscript(reagents=[
        _ab("ZZ-UNTESTED-9", "SQSTM1", applications=["WB"])])
    entry = res["not_in_dataset"][0]
    assert entry["recommended_alternatives"] == 7                  # any application
    assert entry["recommended_for_requested_applications"] == 6     # WB only
    assert entry["alternatives_listed"] == 5
    assert entry["alternatives_truncated"] is True                  # 5 < 6


def test_the_scoped_count_is_absent_when_no_application_was_named():
    res = portal.check_manuscript(reagents=[_ab("ZZ-UNTESTED-9", "SQSTM1")])
    entry = res["not_in_dataset"][0]
    assert "recommended_for_requested_applications" not in entry
    assert entry["alternatives_truncated"] is True                  # 5 < 7


# ── characterised, but nothing for THIS application ──────────────────────────
# SYT1 is characterised for IF and FC and has nothing recommended for WB. Ranking
# hid that behind IF/FC rows sorted to the top; filtering makes it sayable.

def test_a_gene_with_nothing_for_the_requested_application_says_so():
    res = portal.check_manuscript(reagents=[
        _ab("ZZ-UNTESTED-9", "SYT1", applications=["WB"])])
    entry = res["not_in_dataset"][0]
    assert entry["target_is_characterised"] is True         # OGA HAS worked here
    assert entry["recommended_alternatives"] >= 1           # …for something
    assert entry["recommended_for_requested_applications"] == 0   # …but not for WB
    assert entry["alternatives_listed"] == 0
    assert entry["alternatives_truncated"] is False         # nothing withheld
    # and nothing is offered that the caller would have to discard
    assert "SYT1" not in res["characterised_alternatives"]


def test_the_same_gene_does_offer_alternatives_for_an_application_it_covers():
    res = portal.check_manuscript(reagents=[
        _ab("ZZ-UNTESTED-9", "SYT1", applications=["IF"])])
    assert res["characterised_alternatives"]["SYT1"]
    assert res["not_in_dataset"][0]["recommended_for_requested_applications"] >= 1


def test_applications_are_pooled_across_every_reagent_for_that_gene():
    # Two reagents against the same gene, one WB and one FC: the shortlist has to
    # serve both, so the FC-only antibody is back in scope.
    res = portal.check_manuscript(reagents=[
        _ab("ZZ-UNTESTED-8", "SQSTM1", applications=["WB"]),
        _ab("ZZ-UNTESTED-9", "SQSTM1/p62", applications=["flow cytometry"])])
    assert "ab207305" in {a["antibody"]
                          for a in res["characterised_alternatives"]["SQSTM1"]}


def test_unstated_applications_still_return_a_shortlist():
    res = portal.check_manuscript(reagents=[_ab("ZZ-UNTESTED-9", "SQSTM1")])
    assert len(res["characterised_alternatives"]["SQSTM1"]) == 5


# ── F1000 supersedes Zenodo ──────────────────────────────────────────────────

def test_only_one_doi_family_is_cited_per_gene():
    rows, _ = portal.antibody_validation(catalogue="ab212184")
    dois = rows[0]["provenance"]["report_dois"]
    assert dois == list(dict.fromkeys(dois))            # no duplicates
    if any("f1000" in d.lower() for d in dois):
        assert not any("zenodo" in d.lower() for d in dois), dois
