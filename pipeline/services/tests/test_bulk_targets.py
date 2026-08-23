"""Bulk Add Targets: preview first, and a deadline instead of a guessed batch size.

This was the one paste box that wrote straight through — a hundred symbols in,
targets out, and the first thing you learned about a typo was a permanent junk
row. It could not have a preview cheaply, because a gene nobody has recorded
costs a UniProt call, and it did those lookups **while writing**, so a slow API
could time the request out mid-batch.

What is pinned here is the shape that fixes both: the preview owns the network
and is bounded by a deadline; the commit owns the writes and makes no call at
all.
"""
from __future__ import annotations

import pytest

DB = "pipeline_db"


@pytest.fixture
def seeded(_pipeline_db):
    from django.contrib.auth.models import User
    from pipeline.models import Member, Site, Target, TargetNomination
    TargetNomination.objects.using(DB).all().delete()
    Target.objects.using(DB).all().delete()

    site, _ = Site.objects.using(DB).get_or_create(
        short_code="LEI", defaults={"name": "Leicester"})
    other, _ = Site.objects.using(DB).get_or_create(
        short_code="MCG", defaults={"name": "McGill"})
    user, _ = User.objects.using(DB).get_or_create(username="carl")
    member, _ = Member.objects.using(DB).get_or_create(
        user_id=user.pk, defaults={"site": site, "role": "admin",
                                   "is_active": True, "display_name": "Carl"})
    # Already on the list, at another site, with a synonym recorded.
    lrrk2 = Target.objects.using(DB).create(
        gene_name="LRRK2", protein_name="Leucine-rich repeat serine/threonine-protein kinase 2",
        uniprot_id="Q5S007", alternative_name="PARK8, DARDARIN")
    TargetNomination.objects.using(DB).create(target_id=lrrk2.pk, site_id=other.pk, funded=False)
    yield {"site": site, "other": other, "member": member, "lrrk2": lrrk2}
    # **Clean up after, not only before.** `conftest._pipeline_db` is one
    # session-scoped database with no per-test rollback, so whatever the last
    # test in this module creates outlives the module — and the next one to
    # create a target of that name fails on the unique constraint, in a file
    # that has nothing to do with this one. It cost `test_cell_lines_resolver`
    # (which creates its own SOD1) the moment a test here left one behind.
    TargetNomination.objects.using(DB).all().delete()
    Target.objects.using(DB).all().delete()


def _uniprot(monkeypatch, table, *, delay=None):
    """Stand in for the network. `table` maps gene → lookup dict."""
    import time as _time
    from pipeline.services import bulk_targets

    def fake(gene):
        if delay:
            _time.sleep(delay)
        return table.get(gene.upper(), {"found": False, "error": "not found"})
    monkeypatch.setattr(bulk_targets.uniprot, "lookup_gene", fake)


def _found(gene, **extra):
    return {"found": True, "gene_name": gene, "protein_name": f"{gene} protein",
            "uniprot_id": f"P{abs(hash(gene)) % 99999:05d}", "mass_kda": 42.0,
            "gene_synonyms": extra.get("synonyms", [])}


# ── parse ────────────────────────────────────────────────────────────────

def test_parse_dedupes_and_keeps_typed_order(_pipeline_db):
    from pipeline.services import bulk_targets
    assert bulk_targets.parse("sod1, TARDBP\nSOD1;  mapt \t") == ["SOD1", "TARDBP", "MAPT"]
    assert bulk_targets.parse("") == []


# ── plan: the database answers first ─────────────────────────────────────

def test_a_gene_already_on_the_list_costs_no_lookup(seeded, monkeypatch):
    from pipeline.services import bulk_targets
    calls = []
    monkeypatch.setattr(bulk_targets.uniprot, "lookup_gene",
                        lambda g: calls.append(g) or _found(g))
    out = bulk_targets.plan(["LRRK2"], member=seeded["member"])
    assert out["ok"]
    assert calls == []                     # answered from the database
    assert out["rows"][0]["status"] == bulk_targets.ON_FILE


def test_a_synonym_of_a_known_target_costs_no_lookup(seeded, monkeypatch):
    """`Target.alternative_name` is UniProt's own synonym list, already stored.

    Pasting `PARK8` used to fetch UniProt, learn it means `LRRK2`, and only then
    notice `LRRK2` was on file — the answer was in the database the whole time.
    """
    from pipeline.services import bulk_targets
    calls = []
    monkeypatch.setattr(bulk_targets.uniprot, "lookup_gene",
                        lambda g: calls.append(g) or _found(g))
    out = bulk_targets.plan(["PARK8"], member=seeded["member"])
    assert calls == []
    row = out["rows"][0]
    assert row["status"] == bulk_targets.SYNONYM
    assert "LRRK2" in row["note"]


def test_a_new_gene_is_previewed_with_what_uniprot_said(seeded, monkeypatch):
    from pipeline.services import bulk_targets
    _uniprot(monkeypatch, {"SOD1": _found("SOD1", synonyms=["ALS1"])})
    out = bulk_targets.plan(["SOD1"], member=seeded["member"])
    row = out["rows"][0]
    assert row["status"] == bulk_targets.CREATES
    assert row["protein_name"] == "SOD1 protein"
    assert row["mass_kda"] == 42.0
    assert row["synonyms"] == "ALS1"


def test_a_symbol_uniprot_does_not_know_is_flagged_not_created(seeded, monkeypatch):
    from pipeline.models import Target
    from pipeline.services import bulk_targets
    _uniprot(monkeypatch, {})
    out = bulk_targets.plan(["SOD1X"], member=seeded["member"])
    assert out["rows"][0]["status"] == bulk_targets.NOT_FOUND
    before = Target.objects.using(DB).count()
    bulk_targets.apply(out["rows"], member=seeded["member"])
    assert Target.objects.using(DB).count() == before


def test_two_synonyms_of_one_gene_only_create_it_once(seeded, monkeypatch):
    """Caught in the preview, not at write time — the old endpoint reported two
    targets added and the database had one."""
    from pipeline.services import bulk_targets
    _uniprot(monkeypatch, {"SOD1": _found("SOD1"), "ALS1": _found("SOD1")})
    out = bulk_targets.plan(["SOD1", "ALS1"], member=seeded["member"])
    statuses = [r["status"] for r in out["rows"]]
    assert statuses == [bulk_targets.CREATES, bulk_targets.SYNONYM]

    res = bulk_targets.apply(out["rows"], member=seeded["member"])
    assert len(res["created"]) == 1


# ── the deadline ─────────────────────────────────────────────────────────

def test_the_budget_stops_the_lookups_and_reports_the_rest(seeded, monkeypatch):
    """The load-bearing guarantee: `plan` returns whatever UniProt is doing.

    A hundred genes at a 10-second timeout apiece is minutes; guessing a chunk
    size is guessing how slow the API is today. Genes the deadline did not reach
    come back `unchecked`, which is a verdict — it refuses to create, and asking
    again is all it needs.
    """
    from pipeline.services import bulk_targets
    genes = [f"GENE{i}" for i in range(12)]
    _uniprot(monkeypatch, {g: _found(g) for g in genes}, delay=0.25)

    out = bulk_targets.plan(genes, member=seeded["member"], budget_seconds=0.05)
    assert out["ok"]
    assert len(out["rows"]) == len(genes)          # every gene has a verdict
    unchecked = [r for r in out["rows"] if r["status"] == bulk_targets.UNCHECKED]
    assert unchecked, "the deadline should have cut some lookups short"
    assert "did not answer in time" in unchecked[0]["note"]


def test_the_budget_bounds_the_wall_clock_not_just_the_verdicts(seeded, monkeypatch):
    """The deadline has to make `plan` **return**, not merely stop recording.

    `with ThreadPoolExecutor(...)` calls `shutdown(wait=True)` on exit, so a
    first draft of this decided what got recorded while the request still sat
    there for the slowest lookup — the exact timeout it exists to prevent, one
    indentation level down. The verdict assertions above pass either way; only
    the clock tells them apart.
    """
    import time
    from pipeline.services import bulk_targets
    genes = [f"GENE{i}" for i in range(8)]
    # Four times the budget, and more genes than workers, so a `wait=True`
    # shutdown would have to sit through at least one full round.
    _uniprot(monkeypatch, {g: _found(g) for g in genes}, delay=0.8)

    started = time.monotonic()
    bulk_targets.plan(genes, member=seeded["member"], budget_seconds=0.2)
    elapsed = time.monotonic() - started
    assert elapsed < 0.6, f"plan() waited {elapsed:.2f}s on a 0.2s budget"


def test_asking_again_for_the_unchecked_ones_finishes_the_job(seeded, monkeypatch):
    from pipeline.services import bulk_targets
    genes = [f"GENE{i}" for i in range(6)]
    _uniprot(monkeypatch, {g: _found(g) for g in genes}, delay=0.2)

    first = bulk_targets.plan(genes, member=seeded["member"], budget_seconds=0.02)
    pending = [r["gene"] for r in first["rows"] if r["status"] == bulk_targets.UNCHECKED]
    assert pending

    _uniprot(monkeypatch, {g: _found(g) for g in genes})   # API recovers
    second = bulk_targets.plan(pending, member=seeded["member"])
    assert all(r["status"] == bulk_targets.CREATES for r in second["rows"])


def test_an_unchecked_gene_is_never_created(seeded, monkeypatch):
    """Deliberately unlike `resolve_or_create_target`, which falls back to a bare
    target so an antibody paste still lands. Here the gene IS the payload."""
    from pipeline.models import Target
    from pipeline.services import bulk_targets
    rows = [{"gene": "SOD1", "status": bulk_targets.UNCHECKED, "note": "timed out"}]
    before = Target.objects.using(DB).count()
    res = bulk_targets.apply(rows, member=seeded["member"])
    assert Target.objects.using(DB).count() == before
    assert res["skipped"][0]["gene"] == "SOD1"


# ── apply: one funder and one project for a whole batch ──────────────────


def _funding(_unused=None):
    """`get_or_create`, because the database in this module is not rolled back
    between tests — `GrantingAgency.name` is UNIQUE and the second test to call
    this would otherwise die on the fixture rather than on its subject."""
    from pipeline.models import GrantingAgency, Project
    cihr, _ = GrantingAgency.objects.using(DB).get_or_create(name="CIHR")
    other, _ = GrantingAgency.objects.using(DB).get_or_create(name="Wellcome")
    proj, _ = Project.objects.using(DB).get_or_create(
        name="ALS 2026", defaults={"granting_agency": cihr})
    return cihr, other, proj


def test_a_batch_carries_one_funder_and_one_project(seeded, monkeypatch):
    """The owner's ask: *"need to be able to add funder and project info for
    bulk targets (just 1 each)"*. A paste is one grant's worth of genes, and
    setting the pair on every row afterwards is the work the box exists to
    avoid."""
    from pipeline.models import TargetNomination
    from pipeline.services import bulk_targets
    cihr, _other, proj = _funding(None)
    _uniprot(monkeypatch, {"SOD1": _found("SOD1"), "TARDBP": _found("TARDBP")})
    rows = bulk_targets.plan(["SOD1", "TARDBP"], member=seeded["member"])["rows"]

    bulk_targets.apply(rows, member=seeded["member"],
                       agency=cihr.pk, project=proj.pk, funded=True)
    noms = TargetNomination.objects.using(DB).filter(site=seeded["site"])
    assert noms.count() == 2
    for nom in noms:
        assert nom.granting_agency_id == cihr.pk
        assert nom.project_id == proj.pk
        assert nom.funded is True


def test_a_project_supplies_its_own_funder(seeded, monkeypatch):
    """A project belongs to a funder, so choosing one is choosing both — the
    third control is not a second thing to remember."""
    from pipeline.models import TargetNomination
    from pipeline.services import bulk_targets
    cihr, _other, proj = _funding(None)
    _uniprot(monkeypatch, {"SOD1": _found("SOD1")})
    rows = bulk_targets.plan(["SOD1"], member=seeded["member"])["rows"]

    bulk_targets.apply(rows, member=seeded["member"], project=proj.pk)
    nom = TargetNomination.objects.using(DB).get(site=seeded["site"])
    assert nom.granting_agency_id == cihr.pk


def test_a_funder_that_contradicts_the_project_is_refused_by_name(seeded):
    """Guessing which half somebody meant is how a gene ends up filed under a
    grant that did not pay for it. Both names, and the way out."""
    import pytest

    from pipeline.services import bulk_targets
    _cihr, other, proj = _funding(None)
    with pytest.raises(ValueError) as e:
        bulk_targets.funding_for(agency=other.pk, project=proj.pk)
    assert "ALS 2026" in str(e.value)
    assert "CIHR" in str(e.value)
    assert "Wellcome" in str(e.value)


def test_an_unknown_funder_is_refused_rather_than_written(seeded):
    """A bad id would otherwise reach a non-null FK and be refused by
    PostgreSQL after the targets had been created."""
    import pytest

    from pipeline.services import bulk_targets
    with pytest.raises(ValueError):
        bulk_targets.funding_for(agency=999999)
    with pytest.raises(ValueError):
        bulk_targets.funding_for(project=999999)


def test_it_fills_blanks_on_an_existing_nomination_and_overwrites_nothing(
        seeded, monkeypatch):
    """**Fill only blanks**, the rule every other write path here follows. A
    second paste of an overlapping list must not re-file somebody else's genes
    under this batch's grant."""
    from pipeline.models import TargetNomination
    from pipeline.services import bulk_targets
    cihr, other, proj = _funding(None)
    _uniprot(monkeypatch, {"SOD1": _found("SOD1")})

    # Already on this site's list, already funded by somebody else.
    rows = bulk_targets.plan(["SOD1"], member=seeded["member"])["rows"]
    bulk_targets.apply(rows, member=seeded["member"], agency=other.pk)
    nom = TargetNomination.objects.using(DB).get(site=seeded["site"])
    assert nom.granting_agency_id == other.pk
    assert nom.project_id is None

    rows = bulk_targets.plan(["SOD1"], member=seeded["member"])["rows"]
    res = bulk_targets.apply(rows, member=seeded["member"],
                             agency=cihr.pk, project=proj.pk)
    nom.refresh_from_db(using=DB)
    assert nom.granting_agency_id == other.pk, "an existing funder was overwritten"
    assert nom.project_id == proj.pk, "the blank project was not filled"
    # And a row that gained something is reported, not called "skipped".
    assert res["funding_filled"] == ["SOD1"]


def test_no_funding_chosen_writes_none(seeded, monkeypatch):
    from pipeline.models import TargetNomination
    from pipeline.services import bulk_targets
    _uniprot(monkeypatch, {"SOD1": _found("SOD1")})
    rows = bulk_targets.plan(["SOD1"], member=seeded["member"])["rows"]
    bulk_targets.apply(rows, member=seeded["member"])
    nom = TargetNomination.objects.using(DB).get(site=seeded["site"])
    assert nom.granting_agency_id is None
    assert nom.project_id is None
    assert nom.funded is False


# ── apply: no network, and it nominates ──────────────────────────────────

def test_an_unreachable_uniprot_is_not_a_misspelt_gene(seeded, monkeypatch):
    """**The shape the outage test below never produced.**

    `lookup_gene` catches `RequestException` itself and *returns*
    `{"found": False, "error": "UniProt API unavailable: …"}` — it does not
    raise. So `_lookup`'s except branch, and the `dead()` test that exercises
    it, never see the failure that actually happens; `plan` read `found` alone
    and filed a blocked proxy as `not_found`.

    What reached the screen was "TRPA1 **not in UniProt** — not added, check the
    spelling" about a real gene, with the raw `ProxyError` and the query URL
    printed underneath for a scientist to read. Two wrong things at once: an
    accusation the reader cannot act on, and exception detail that belongs in
    the log.
    """
    from pipeline.services import bulk_targets
    _uniprot(monkeypatch, {"TRPA1": {
        "found": False, "unavailable": True,
        "error": "UniProt API unavailable: ProxyError('Unable to connect')"}})

    row = bulk_targets.plan(["TRPA1"], member=seeded["member"])["rows"][0]
    assert row["status"] == bulk_targets.UNCHECKED
    assert "could not be reached" in row["note"]
    assert "spelling" not in row["note"].lower()
    assert "ProxyError" not in row["note"], "raw exception detail on screen"


def test_a_gene_that_really_is_not_in_uniprot_still_says_so(seeded, monkeypatch):
    """The other half — a genuinely wrong symbol must keep its refusal, or the
    fix above would hide every typo behind "try again"."""
    from pipeline.services import bulk_targets
    _uniprot(monkeypatch, {"SOD1X": {
        "found": False, "unavailable": False,
        "error": "No human protein found for gene 'SOD1X'"}})
    row = bulk_targets.plan(["SOD1X"], member=seeded["member"])["rows"][0]
    assert row["status"] == bulk_targets.NOT_FOUND


def test_lookup_gene_flags_a_failed_request_rather_than_a_missing_gene(monkeypatch):
    """At the source, because that is where the two answers were collapsed."""
    import requests

    from pipeline.services import uniprot

    def boom(*a, **kw):
        raise requests.exceptions.RequestException("no route to host")
    monkeypatch.setattr(uniprot.requests, "get", boom)

    out = uniprot.lookup_gene("TRPA1")
    assert out["found"] is False
    assert out["unavailable"] is True

    # And a real "no such gene" answer is not flagged.
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"results": []}
    monkeypatch.setattr(uniprot.requests, "get", lambda *a, **kw: _Resp())
    out = uniprot.lookup_gene("NOTAGENE")
    assert out["found"] is False
    assert out["unavailable"] is False


def test_the_commit_makes_no_outbound_call(seeded, monkeypatch):
    """The whole reason preview and commit are separate requests."""
    from pipeline.services import bulk_targets

    def explode(gene):
        raise AssertionError(f"apply() called UniProt for {gene}")
    _uniprot(monkeypatch, {"SOD1": _found("SOD1")})
    rows = bulk_targets.plan(["SOD1"], member=seeded["member"])["rows"]

    monkeypatch.setattr(bulk_targets.uniprot, "lookup_gene", explode)
    res = bulk_targets.apply(rows, member=seeded["member"])
    assert len(res["created"]) == 1


def test_a_created_target_is_nominated_at_your_site(seeded, monkeypatch):
    from pipeline.models import TargetNomination
    from pipeline.services import bulk_targets
    _uniprot(monkeypatch, {"SOD1": _found("SOD1")})
    rows = bulk_targets.plan(["SOD1"], member=seeded["member"])["rows"]
    res = bulk_targets.apply(rows, member=seeded["member"])
    assert TargetNomination.objects.using(DB).filter(
        target_id=res["created"][0]["target_id"], site_id=seeded["site"].pk).exists()


def test_a_gene_another_site_already_has_joins_your_list_too(seeded, monkeypatch):
    """The old endpoint counted this as "skipped" and did nothing at all — so a
    site could paste a list, watch most of it skipped, and still have none of it
    on their own board. A nomination is the repeatable half of a target."""
    from pipeline.models import TargetNomination
    from pipeline.services import bulk_targets
    out = bulk_targets.plan(["LRRK2"], member=seeded["member"])
    assert out["rows"][0]["will_nominate"] is True

    res = bulk_targets.apply(out["rows"], member=seeded["member"])
    assert res["nominated"] == ["LRRK2"]
    assert TargetNomination.objects.using(DB).filter(
        target_id=seeded["lrrk2"].pk, site_id=seeded["site"].pk).exists()
    # …and doing it twice does not make a second nomination.
    again = bulk_targets.apply(bulk_targets.plan(["LRRK2"], member=seeded["member"])["rows"],
                               member=seeded["member"])
    assert again["nominated"] == []


def test_a_gene_added_by_someone_else_between_check_and_save_is_not_duplicated(seeded, monkeypatch):
    from pipeline.models import Target
    from pipeline.services import bulk_targets
    _uniprot(monkeypatch, {"SOD1": _found("SOD1")})
    rows = bulk_targets.plan(["SOD1"], member=seeded["member"])["rows"]

    Target.objects.using(DB).create(gene_name="SOD1", protein_name="raced in")
    res = bulk_targets.apply(rows, member=seeded["member"])
    assert res["created"] == []
    assert "added by someone else" in res["skipped"][0]["reason"]
    assert Target.objects.using(DB).filter(gene_name="SOD1").count() == 1


def test_too_many_genes_is_refused_before_any_lookup(seeded, monkeypatch):
    from pipeline.services import bulk_targets
    calls = []
    monkeypatch.setattr(bulk_targets.uniprot, "lookup_gene",
                        lambda g: calls.append(g) or _found(g))
    out = bulk_targets.plan([f"G{i}" for i in range(bulk_targets.MAX_GENES + 1)],
                            member=seeded["member"])
    assert out["ok"] is False and "most in one go" in out["error"]
    assert calls == []


# ── one check, both doors ────────────────────────────────────────────────

def test_the_preview_carries_the_consortium_context_too(seeded, monkeypatch):
    """The two previews each knew half of the answer.

    The target board's Add panel posted to `nomination_check`, which knows who is
    already doing a gene and whether its knockout can be bought but not whether
    the symbol is real; the feasibility bulk box knew the reverse. Both were
    called "check", on the same act, and neither mentioned the other.
    """
    from pipeline.services import bulk_targets
    out = bulk_targets.plan(["LRRK2"], member=seeded["member"])
    context = out["rows"][0]["context"]
    assert any("already on the list at McGill" in c for c in context), context


def test_an_off_the_shelf_knockout_is_named_before_you_start(seeded, monkeypatch):
    from pipeline.models import HorizonKoLine
    from pipeline.services import bulk_targets
    HorizonKoLine.objects.using(DB).create(
        gene_name="SOD1", item_number="HZGHC001", product_name="SOD1 KO", background="HAP1")
    _uniprot(monkeypatch, {"SOD1": _found("SOD1")})
    out = bulk_targets.plan(["SOD1"], member=seeded["member"])
    assert any("HZGHC001" in c for c in out["rows"][0]["context"])


def test_the_context_costs_a_constant_number_of_queries(seeded, monkeypatch):
    """N+1 is how these pages die, and it dies on the real dataset while looking
    fine on a handful of dev rows.

    `target_board.nomination_check` calls `family_expertise()`, which walks every
    nomination in the database. Looping it over a pasted list would have walked
    that table once per gene — a hundred times for a hundred genes.
    """
    from django.db import connections
    from django.test.utils import CaptureQueriesContext
    from pipeline.models import Target
    from pipeline.services import bulk_targets

    # Enough already-on-file genes that a per-gene query would be obvious.
    many = [f"GENE{i}" for i in range(20)]
    for g in many:
        Target.objects.using(DB).create(gene_name=g, protein_name=f"{g} protein")

    def count_for(genes):
        with CaptureQueriesContext(connections[DB]) as ctx:
            bulk_targets.plan(genes, member=seeded["member"])
        return len(ctx.captured_queries)

    few = count_for(many[:2])
    lots = count_for(many)
    assert lots <= few + 2, (
        f"{len(many)} genes cost {lots} queries against {few} for 2 — "
        "the context is scaling with the row count")


def test_uniprot_being_down_still_returns_the_offline_half(seeded, monkeypatch):
    """The board's Add panel posts here now, and a board must survive an outage.

    Its own preview was offline by construction; routing it through UniProt is
    only safe because an unanswered lookup degrades to a verdict rather than an
    error, and everything the panel used to show still arrives.
    """
    from pipeline.services import bulk_targets

    def dead(gene):
        raise ConnectionError("no route to host")
    monkeypatch.setattr(bulk_targets.uniprot, "lookup_gene", dead)

    out = bulk_targets.plan(["SOD1", "LRRK2"], member=seeded["member"])
    assert out["ok"]
    by_gene = {r["gene"]: r for r in out["rows"]}
    # The unknown gene degrades…
    assert by_gene["SOD1"]["status"] == bulk_targets.UNCHECKED
    # …and the one the database can answer is unaffected, context and all.
    assert by_gene["LRRK2"]["status"] == bulk_targets.ON_FILE
    assert any("already on the list" in c for c in by_gene["LRRK2"]["context"])


# ── the site a batch goes on ─────────────────────────────────────────────
#
# A batch is one bench's worth of genes the same way it is one grant's worth,
# and it was never asked: every nomination went on the *adder's* site. That is
# the complaint the single-gene add already answered — "ideally there would be a
# drop down menu with McGill, Leicester, uOttawa, UBC, Cornell" — and both bulk
# doors kept it, so a coordinator could not put a gene on another bench's list
# from anywhere in the app.

def test_a_chosen_site_gets_the_nomination_not_the_adders(seeded, monkeypatch):
    from pipeline.models import TargetNomination
    from pipeline.services import bulk_targets
    _uniprot(monkeypatch, {"SOD1": _found("SOD1")})

    out = bulk_targets.plan(["SOD1"], member=seeded["member"],
                            site=seeded["other"].pk)
    assert out["site"] == "McGill"          # the preview says whose, by name
    res = bulk_targets.apply(out["rows"], member=seeded["member"],
                             site=seeded["other"].pk)

    target_id = res["created"][0]["target_id"]
    assert TargetNomination.objects.using(DB).filter(
        target_id=target_id, site_id=seeded["other"].pk).exists()
    assert not TargetNomination.objects.using(DB).filter(
        target_id=target_id, site_id=seeded["site"].pk).exists()
    # Who *recorded* it is still the person pressing the button.
    nom = TargetNomination.objects.using(DB).get(target_id=target_id)
    assert nom.created_by_id == seeded["member"].pk


def test_no_site_chosen_is_still_your_own(seeded, monkeypatch):
    """The default has to be unchanged, or every scientist adding their own
    genes pays for a feature aimed at whoever allocates them."""
    from pipeline.models import TargetNomination
    from pipeline.services import bulk_targets
    _uniprot(monkeypatch, {"SOD1": _found("SOD1")})
    rows = bulk_targets.plan(["SOD1"], member=seeded["member"])["rows"]
    res = bulk_targets.apply(rows, member=seeded["member"])
    assert TargetNomination.objects.using(DB).filter(
        target_id=res["created"][0]["target_id"], site_id=seeded["site"].pk).exists()


def test_the_preview_counts_against_the_chosen_site(seeded, monkeypatch):
    """LRRK2 is already McGill's. Asked about McGill it is nothing to do; asked
    about Leicester it is a nomination waiting to be written — one gene, two
    honest answers, and the number on the save button is one of them."""
    from pipeline.services import bulk_targets
    mine = bulk_targets.plan(["LRRK2"], member=seeded["member"],
                             site=seeded["site"].pk)
    theirs = bulk_targets.plan(["LRRK2"], member=seeded["member"],
                               site=seeded["other"].pk)
    assert mine["rows"][0]["will_nominate"] is True
    assert mine["summary"]["nominate"] == 1
    assert theirs["rows"][0]["will_nominate"] is False
    assert theirs["summary"]["nominate"] == 0


def test_a_site_that_is_not_on_file_is_refused_by_name(seeded, monkeypatch):
    """Never fall back to the adder's own site. A stale dropdown quietly filing
    a batch under the wrong institution is the whole failure being fixed, and
    nothing has been written when this is asked."""
    from pipeline.models import Target
    from pipeline.services import bulk_targets
    _uniprot(monkeypatch, {"SOD1": _found("SOD1")})

    out = bulk_targets.plan(["SOD1"], member=seeded["member"], site=999999)
    assert out["ok"] is False
    assert "Leicester" in out["error"] and "McGill" in out["error"]

    rows = bulk_targets.plan(["SOD1"], member=seeded["member"])["rows"]
    with pytest.raises(ValueError) as e:
        bulk_targets.apply(rows, member=seeded["member"], site="Leicster")
    assert "Leicester" in str(e.value)
    assert not Target.objects.using(DB).filter(gene_name="SOD1").exists()


def test_a_site_may_be_named_as_well_as_numbered(seeded, monkeypatch):
    """The pk is what the <select> submits; the name is what a URL, a script or
    a sheet carries. `services/sites.py` already took all three everywhere else."""
    from pipeline.services import bulk_targets
    assert bulk_targets.plan(["LRRK2"], member=seeded["member"],
                             site="McGill")["site"] == "McGill"
    assert bulk_targets.plan(["LRRK2"], member=seeded["member"],
                             site="MCG")["site"] == "McGill"


def test_moving_the_site_after_the_check_is_refused(seeded, monkeypatch):
    """A preview is not a permission slip. The rows say which site they were
    checked against, so a dropdown moved between the two presses is caught even
    if a page forgets to disarm its save."""
    from pipeline.models import Target
    from pipeline.services import bulk_targets
    _uniprot(monkeypatch, {"SOD1": _found("SOD1")})
    rows = bulk_targets.plan(["SOD1"], member=seeded["member"],
                             site=seeded["site"].pk)["rows"]

    with pytest.raises(ValueError) as e:
        bulk_targets.apply(rows, member=seeded["member"], site=seeded["other"].pk)
    assert "Leicester" in str(e.value) and "McGill" in str(e.value)
    assert not Target.objects.using(DB).filter(gene_name="SOD1").exists()

    # …and the same rows against the site they were checked for still write.
    res = bulk_targets.apply(rows, member=seeded["member"], site=seeded["site"].pk)
    assert len(res["created"]) == 1


# ---------------------------------------------------------------------------
# `landed` — where the press just put something, with the ids to go and look
# ---------------------------------------------------------------------------
#
# Adding targets used to end on the panel you pressed. It now ends on the
# targets board narrowed to the genes it added, and that filter has to be built
# from something: `created` carried a target_id and `nominated` carried bare
# gene names, so between them there was no single answer to "which genes did
# this press put on a list, and what are they stored as?"

def test_landed_names_the_targets_a_press_created(seeded, monkeypatch):
    from pipeline.services import bulk_targets
    _uniprot(monkeypatch, {"SOD1": _found("SOD1"), "STMN2": _found("STMN2")})
    rows = bulk_targets.plan(["SOD1", "STMN2"], member=seeded["member"])["rows"]
    res = bulk_targets.apply(rows, member=seeded["member"])

    assert sorted(t["gene"] for t in res["landed"]) == ["SOD1", "STMN2"]
    assert all(t["target_id"] for t in res["landed"])
    assert res["summary"]["landed"] == 2


def test_landed_includes_a_gene_that_only_joined_your_list(seeded, monkeypatch):
    """A nomination is a record. LRRK2 is already on the consortium's list at
    McGill, so nothing is *created* — but it is on Leicester's list now, and it
    is one of the genes the board should be showing you afterwards."""
    from pipeline.services import bulk_targets
    rows = bulk_targets.plan(["LRRK2"], member=seeded["member"])["rows"]
    res = bulk_targets.apply(rows, member=seeded["member"])

    assert res["created"] == []
    assert res["landed"] == [{"gene": "LRRK2", "target_id": seeded["lrrk2"].pk}]


def test_a_gene_that_was_already_yours_did_not_land(seeded, monkeypatch):
    """Nothing was written, so there is nothing new to go and look at — and a
    second press must not re-announce the first one's work."""
    from pipeline.services import bulk_targets
    plan = lambda: bulk_targets.plan(["LRRK2"], member=seeded["member"])["rows"]
    bulk_targets.apply(plan(), member=seeded["member"])
    again = bulk_targets.apply(plan(), member=seeded["member"])
    assert again["landed"] == []


def test_landed_is_the_stored_spelling_not_the_typed_one(seeded, monkeypatch):
    """**The bug this exists to prevent.** `PARK8` is a recorded synonym of
    LRRK2, so pasting it files a nomination against the LRRK2 row — and a board
    filtered to `PARK8` matches nothing. A successful save would have landed on
    an empty board saying no rows match, which reads as the save having failed.
    """
    from pipeline.services import bulk_targets
    rows = bulk_targets.plan(["PARK8"], member=seeded["member"])["rows"]
    assert rows[0]["status"] == bulk_targets.SYNONYM
    res = bulk_targets.apply(rows, member=seeded["member"])
    assert res["landed"] == [{"gene": "LRRK2", "target_id": seeded["lrrk2"].pk}]


def test_a_gene_uniprot_refused_does_not_land(seeded, monkeypatch):
    """It was not created and not nominated, so it is not somewhere to go."""
    from pipeline.services import bulk_targets
    _uniprot(monkeypatch, {})
    rows = bulk_targets.plan(["NOTAGENE"], member=seeded["member"])["rows"]
    res = bulk_targets.apply(rows, member=seeded["member"])
    assert res["landed"] == []


def test_a_member_with_no_site_still_lands_on_what_was_created(seeded, monkeypatch):
    """No site means no nomination is written, so `made` is never true — the
    target is still real and still worth being taken to."""
    from pipeline.services import bulk_targets
    _uniprot(monkeypatch, {"SOD1": _found("SOD1")})
    rows = bulk_targets.plan(["SOD1"])["rows"]
    res = bulk_targets.apply(rows, member=None)
    assert [t["gene"] for t in res["landed"]] == ["SOD1"]
