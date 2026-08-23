"""Tests for duplicate-antibody detection and the scoped merge.

Runs against the isolated SQLite pipeline_db from conftest. The fixture mirrors
the real SOD1 case that prompted this: Bio-Techne MAB3418 (clone 348808) listed
twice under two RRIDs, one copy carrying the WB recommendation and all the
knockout-controlled evidence, the other carrying only publication images — so
both showed on the public SOD1 page as if they were different reagents.

Proves:
  * the catalogue/clone signals catch a product listed twice under two RRIDs,
    including a punctuation/case variant of the catalogue number
  * genuinely different antibodies, and the same catalogue against a different
    gene, are never grouped
  * a shared RRID across two suppliers is reported but NEVER merged
  * --gene / --ids confine the merge to the rows you named
  * the merge keeps the row holding the evidence and the recommendation, keeps
    its own RRID and supplier link, and never backfills a unique-key field
"""
from __future__ import annotations

import pytest

DB = "pipeline_db"


# ── seeding ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def seeded(_pipeline_db):
    from django.contrib.auth.models import User
    from pipeline.models import (
        Antibody, Company, ExperimentSession, Member, PublicationImage, Site,
        Target, WbResult,
    )
    for M in (PublicationImage, WbResult, ExperimentSession, Antibody, Company,
              Target, Member, Site):
        M.objects.using(DB).all().delete()

    site = Site.objects.using(DB).create(name="Dup Site", short_code="DUP")
    # Users are NOT deleted between tests: cascading a User delete reaches
    # allauth tables that this pipeline-only test DB does not migrate.
    user, _ = User.objects.using(DB).get_or_create(username="dup-tester")
    # cross-DB FK: assign via _id, never the object (CLAUDE.md rule + the router)
    member = Member.objects.using(DB).create(user_id=user.pk, site=site, role="admin")
    sod1 = Target.objects.using(DB).create(protein_name="Superoxide dismutase 1",
                                           gene_name="SOD1")
    syt1 = Target.objects.using(DB).create(protein_name="Synaptotagmin-1",
                                           gene_name="SYT1")
    biotechne = Company.objects.using(DB).create(name="Bio-Techne")
    genetex = Company.objects.using(DB).create(name="GeneTex")

    # The pair: same product, two RRIDs. `keeper` holds the evidence + verdict.
    keeper = Antibody.objects.using(DB).create(
        target=sod1, company=biotechne, catalogue_number="MAB3418",
        rrid="AB_2193899", host_species="mouse", clonality="monoclonal",
        clone_id="348808", wb_recommended=True,
        supplier_url="https://www.rndsystems.com/products/mab3418")
    # Catalogue differs only in case/punctuation; site differs so both rows can
    # coexist under the (catalogue, company, target, lot, site) unique key.
    dupe = Antibody.objects.using(DB).create(
        target=sod1, company=biotechne, catalogue_number="mab-3418", site=site,
        rrid="AB_10680079", host_species="Mouse", clonality="monoclonal",
        clone_id="348808", isotype="IgG2a",
        supplier_url="https://www.bio-techne.com/p/mab3418")
    # A different antibody on the same gene — must never be grouped with them.
    other = Antibody.objects.using(DB).create(
        target=sod1, company=genetex, catalogue_number="GTX100554",
        rrid="AB_10618670", clonality="polyclonal", wb_recommended=True)
    # Same catalogue + clone, DIFFERENT gene — must never be grouped.
    cross_gene = Antibody.objects.using(DB).create(
        target=syt1, company=biotechne, catalogue_number="MAB3418",
        rrid="AB_9999999", clonality="monoclonal", clone_id="348808")
    # Same RRID as `keeper` under another supplier — a wrong RRID, not a dup.
    wrong_rrid = Antibody.objects.using(DB).create(
        target=sod1, company=genetex, catalogue_number="GTX100659",
        rrid="https://www.antibodyregistry.org/AB_2193899", clonality="polyclonal")

    session = ExperimentSession.objects.using(DB).create(
        target=sod1, procedure_type="WB", date="2021-06-01", site=site,
        experimenter=member)
    WbResult.objects.using(DB).create(session=session, antibody=keeper,
                                      dilution="1:1000", signal="YES", rating="YES")
    for ab in (keeper, dupe):
        for app in ("WB", "IP", "ICC-IF"):
            PublicationImage.objects.using(DB).create(
                antibody=ab, application_type=app,
                image=f"experiments/SOD1_{app}_MAB3418.png")
    for ab in (other, wrong_rrid):
        PublicationImage.objects.using(DB).create(
            antibody=ab, application_type="WB",
            image=f"experiments/SOD1_WB_{ab.catalogue_number}.png")

    return {"keeper": keeper, "dupe": dupe, "other": other,
            "cross_gene": cross_gene, "wrong_rrid": wrong_rrid, "sod1": sod1}


def _rows():
    from pipeline.models import Antibody
    return list(Antibody.objects.using(DB).all())


# ── the signals ─────────────────────────────────────────────────────────────

def test_canonical_code_ignores_case_and_punctuation():
    from pipeline.services.duplicates import canonical_code
    assert canonical_code("MAB-3418 ") == canonical_code("mab3418") == "mab3418"


@pytest.mark.parametrize("junk", ["", "  ", "?", "n/a", "unknown", None])
def test_canonical_code_rejects_placeholders(junk):
    from pipeline.services.duplicates import canonical_code
    assert canonical_code(junk) is None


def test_catalogue_and_clone_signals_catch_the_pair(seeded):
    from pipeline.services.duplicates import group_by_signal
    for signal in ("catalogue", "clone"):
        groups = group_by_signal(_rows(), signal)
        assert len(groups) == 1, signal
        assert {r.id for r in groups[0][1]} == {seeded["keeper"].id, seeded["dupe"].id}


def test_rrid_signal_normalises_url_form(seeded):
    from pipeline.services.duplicates import group_by_signal
    groups = group_by_signal(_rows(), "rrid")
    # keeper's bare AB_2193899 and wrong_rrid's full registry URL are one key.
    assert len(groups) == 1
    assert {r.id for r in groups[0][1]} == {seeded["keeper"].id, seeded["wrong_rrid"].id}


def test_same_catalogue_on_a_different_gene_is_not_a_duplicate(seeded):
    from pipeline.services.duplicates import find_duplicate_groups
    for entry in find_duplicate_groups(_rows()):
        assert seeded["cross_gene"].id not in {r.id for r in entry["rows"]}


def test_distinct_antibody_is_never_grouped(seeded):
    from pipeline.services.duplicates import find_duplicate_groups
    for entry in find_duplicate_groups(_rows()):
        assert seeded["other"].id not in {r.id for r in entry["rows"]}


def test_find_duplicate_groups_reports_each_pair_once_with_all_signals(seeded):
    from pipeline.services.duplicates import find_duplicate_groups
    entries = find_duplicate_groups(_rows())
    assert len(entries) == 2
    by_ids = {tuple(sorted(r.id for r in e["rows"])): e for e in entries}
    pair = by_ids[tuple(sorted((seeded["keeper"].id, seeded["dupe"].id)))]
    # caught by both catalogue and clone, but listed as ONE group
    assert sorted(pair["signals"]) == ["catalogue", "clone"]


# ── the report command (read-only) ──────────────────────────────────────────

def test_report_changes_nothing(seeded, capsys):
    from django.core.management import call_command
    from pipeline.models import Antibody
    before = Antibody.objects.using(DB).count()
    call_command("find_duplicate_antibodies", "--all")
    out = capsys.readouterr().out
    assert Antibody.objects.using(DB).count() == before
    assert "READ-ONLY" in out
    assert "SPANS MULTIPLE SUPPLIERS" in out   # the wrong-RRID pair is flagged
    assert "groups the merge REFUSES         : 1" in out


# ── the merge ───────────────────────────────────────────────────────────────

def test_dry_run_writes_nothing(seeded):
    from django.core.management import call_command
    from pipeline.models import Antibody
    call_command("merge_duplicate_antibodies", "--strategy", "cat", "--gene", "SOD1")
    assert Antibody.objects.using(DB).filter(pk=seeded["dupe"].pk).exists()


def test_gene_scope_confines_the_merge(seeded):
    """--gene SYT1 must not touch the SOD1 duplicate."""
    from django.core.management import call_command
    from pipeline.models import Antibody
    call_command("merge_duplicate_antibodies", "--strategy", "cat", "--gene", "SYT1",
                 "--apply", "--include-risky")
    assert Antibody.objects.using(DB).filter(pk=seeded["dupe"].pk).exists()


def test_ids_scope_confines_the_merge(seeded):
    """Naming only the untouched rows leaves the duplicate pair alone."""
    from django.core.management import call_command
    from pipeline.models import Antibody
    call_command("merge_duplicate_antibodies", "--strategy", "cat",
                 "--ids", f"{seeded['other'].id},{seeded['cross_gene'].id}",
                 "--apply", "--include-risky")
    assert Antibody.objects.using(DB).filter(pk=seeded["dupe"].pk).exists()


def test_shared_rrid_across_suppliers_is_never_merged(seeded, capsys):
    from django.core.management import call_command
    from pipeline.models import Antibody
    call_command("merge_duplicate_antibodies", "--strategy", "rrid",
                 "--apply", "--include-risky")
    assert "SKIP: multiple vendors" in capsys.readouterr().out
    assert Antibody.objects.using(DB).filter(pk=seeded["wrong_rrid"].pk).exists()


def test_merge_keeps_the_row_holding_the_evidence(seeded):
    from django.core.management import call_command
    from pipeline.models import Antibody, PublicationImage
    call_command("merge_duplicate_antibodies", "--strategy", "cat", "--gene", "SOD1",
                 "--apply", "--include-risky")

    assert not Antibody.objects.using(DB).filter(pk=seeded["dupe"].pk).exists()
    kept = Antibody.objects.using(DB).get(pk=seeded["keeper"].pk)
    assert kept.rrid == "AB_2193899"                  # survivor keeps its own RRID
    assert kept.host_species == "mouse"               # and its own spelling
    assert kept.supplier_url == "https://www.rndsystems.com/products/mab3418"
    assert kept.wb_recommended is True                # the verdict survives
    assert kept.wb_results.count() == 1               # evidence still attached
    assert kept.isotype == "IgG2a"                    # blank field filled from loser
    assert kept.site_id is None                       # unique-key field NOT backfilled
    # one image per application, no orphans left behind
    assert sorted(kept.publication_images.values_list("application_type", flat=True)) \
        == ["ICC-IF", "IP", "WB"]
    assert PublicationImage.objects.using(DB).filter(
        antibody_id=seeded["dupe"].pk).count() == 0


def test_merge_is_idempotent(seeded):
    from django.core.management import call_command
    from pipeline.models import Antibody
    for _ in range(2):
        call_command("merge_duplicate_antibodies", "--strategy", "cat", "--gene", "SOD1",
                     "--apply", "--include-risky")
    assert Antibody.objects.using(DB).filter(
        target=seeded["sod1"], catalogue_number__iexact="MAB3418").count() == 1


# ── the image signal ────────────────────────────────────────────────────────

def test_image_signal_groups_rows_sharing_a_file(seeded):
    from pipeline.services.duplicates import group_by_shared_image
    groups = group_by_shared_image(_rows())
    assert len(groups) == 1
    assert {r.id for r in groups[0]} == {seeded["keeper"].id, seeded["dupe"].id}


def test_image_signal_ignores_rows_with_their_own_figures(seeded):
    from pipeline.services.duplicates import group_by_shared_image
    ids = {r.id for g in group_by_shared_image(_rows()) for r in g}
    assert seeded["other"].id not in ids
    assert seeded["wrong_rrid"].id not in ids


def test_image_signal_catches_a_pair_no_metadata_signal_can(_pipeline_db, seeded):
    """
    The real BECN1 MA5-15825 shape: same figure, but the second row has a
    different lot and no RRID, so catalogue+lot says "two vials" and rrid says
    nothing. Only the shared image file proves it is one antibody.
    """
    from django.core.management import call_command
    from pipeline.models import Antibody, PublicationImage, Target, Company
    target = Target.objects.using(DB).create(protein_name="Beclin-1", gene_name="BECN1")
    company = Company.objects.using(DB).create(name="Thermo Fisher Scientific")
    a = Antibody.objects.using(DB).create(
        target=target, company=company, catalogue_number="MA5-15825",
        lot_number="AC4649936", rrid="AB_11153717", clonality="monoclonal",
        clone_id="2A4", wb_recommended=True)
    b = Antibody.objects.using(DB).create(
        target=target, company=company, catalogue_number="MA5-15825",
        lot_number="160520-2025", rrid="", clonality="monoclonal", clone_id="2A4")
    for ab in (a, b):
        PublicationImage.objects.using(DB).create(
            antibody=ab, application_type="WB", image="p/BECN1_MA5-15825_WB.png")

    from pipeline.services.duplicates import group_by_shared_image
    rows = list(Antibody.objects.using(DB).filter(target=target))
    assert {r.id for r in group_by_shared_image(rows)[0]} == {a.id, b.id}

    call_command("merge_duplicate_antibodies", "--strategy", "image", "--gene", "BECN1",
                 "--prefer-recommended", "--apply", "--include-risky")
    assert not Antibody.objects.using(DB).filter(pk=b.pk).exists()
    kept = Antibody.objects.using(DB).get(pk=a.pk)
    assert kept.wb_recommended is True
    assert kept.rrid == "AB_11153717"


def test_prefer_recommended_keeps_the_verdict_and_inherits_the_rrid(_pipeline_db, seeded):
    """
    The real NFE2L2 80593-1-RR shape: the RECOMMENDED row is the one missing an
    RRID, and the row with the RRID owns more records. Deleting the unrecommended
    row would lose the RRID; the merge must keep the verdict AND inherit it.
    """
    from django.core.management import call_command
    from pipeline.models import Antibody, PublicationImage, Target, Company
    target = Target.objects.using(DB).create(protein_name="NRF2", gene_name="NFE2L2")
    company = Company.objects.using(DB).create(name="Proteintech")
    rich = Antibody.objects.using(DB).create(      # more images, no verdict
        target=target, company=company, catalogue_number="80593-1-RR",
        lot_number="23019843", rrid="AB_2918904", clonality="recombinant")
    verdict = Antibody.objects.using(DB).create(   # the verdict, no RRID
        target=target, company=company, catalogue_number="80593-1-RR",
        lot_number="", rrid="", clonality="recombinant", wb_recommended=True)
    for app in ("WB", "FC"):
        PublicationImage.objects.using(DB).create(
            antibody=rich, application_type=app, image=f"p/NFE2L2_80593_{app}.png")
    PublicationImage.objects.using(DB).create(
        antibody=verdict, application_type="WB", image="p/NFE2L2_80593_WB.png")

    call_command("merge_duplicate_antibodies", "--strategy", "image", "--gene", "NFE2L2",
                 "--prefer-recommended", "--apply", "--include-risky")

    assert not Antibody.objects.using(DB).filter(pk=rich.pk).exists()
    kept = Antibody.objects.using(DB).get(pk=verdict.pk)
    assert kept.wb_recommended is True          # verdict survived
    assert kept.rrid == "AB_2918904"            # RRID inherited from the loser
    assert sorted(kept.publication_images.values_list("application_type", flat=True)) \
        == ["FC", "WB"]                          # the FC figure was not lost


def test_default_survivor_rule_is_unchanged_without_the_flag(seeded):
    """Without --prefer-recommended the most-data row still wins."""
    from pipeline.management.commands.merge_duplicate_antibodies import Command
    cmd = Command()
    rows = [seeded["keeper"], seeded["dupe"]]
    assert cmd.choose_survivor(rows).id == seeded["keeper"].id
    assert cmd.choose_survivor(rows, prefer_recommended=True).id == seeded["keeper"].id


def test_report_counts_safe_to_remove(seeded, capsys):
    from django.core.management import call_command
    call_command("find_duplicate_antibodies", "--all")
    out = capsys.readouterr().out
    assert "ROWS SHARING A PUBLICATION IMAGE FILE" in out
    assert "...exactly one row recommended   : 1" in out
    assert "...rows removable in those groups: 1" in out


# ── guards added after the first live report ────────────────────────────────

def test_different_clone_ids_are_never_merged(_pipeline_db, seeded, capsys):
    """
    Real case: Abcam ab2730 carries AB_303255 on both clone AP6 and
    EPR2688(2). One RRID on two different products is a wrong RRID, not a
    duplicate — merging would fuse two reagents.
    """
    from django.core.management import call_command
    from pipeline.models import Antibody, Target, Company
    target = Target.objects.using(DB).create(protein_name="AP-2 alpha", gene_name="AP2A2")
    company = Company.objects.using(DB).create(name="Abcam AP2")
    a = Antibody.objects.using(DB).create(
        target=target, company=company, catalogue_number="ab2730", lot_number="1091513-6",
        rrid="AB_303255", clonality="monoclonal", clone_id="AP6")
    b = Antibody.objects.using(DB).create(
        target=target, company=company, catalogue_number="ab2730", lot_number="1091513-4",
        rrid="AB_303255", clonality="monoclonal", clone_id="EPR2688(2)")

    call_command("merge_duplicate_antibodies", "--strategy", "cat", "--gene", "AP2A2",
                 "--apply", "--include-risky")
    out = capsys.readouterr().out
    assert "SKIP: clone conflict" in out
    assert Antibody.objects.using(DB).filter(pk=a.pk).exists()
    assert Antibody.objects.using(DB).filter(pk=b.pk).exists()


def test_a_blank_clone_is_not_a_clone_conflict(seeded):
    """One row naming the clone and one leaving it blank is still a duplicate."""
    from django.core.management import call_command
    from pipeline.models import Antibody
    Antibody.objects.using(DB).filter(pk=seeded["dupe"].pk).update(clone_id="")
    call_command("merge_duplicate_antibodies", "--strategy", "cat", "--gene", "SOD1",
                 "--apply", "--include-risky")
    assert not Antibody.objects.using(DB).filter(pk=seeded["dupe"].pk).exists()


def test_only_single_recommended_skips_a_group_with_no_verdict(_pipeline_db, seeded, capsys):
    """The 18 'no row recommended' groups must not be merged by the safe rule."""
    from django.core.management import call_command
    from pipeline.models import Antibody, PublicationImage, Target, Company
    target = Target.objects.using(DB).create(protein_name="GCase", gene_name="GBA1")
    company = Company.objects.using(DB).create(name="ABCD Antibodies GBA")
    a = Antibody.objects.using(DB).create(
        target=target, company=company, catalogue_number="ABCD_AR148",
        lot_number="1", rrid="AB_3076343", clonality="unknown")
    b = Antibody.objects.using(DB).create(
        target=target, company=company, catalogue_number="ABCD_AR148",
        lot_number="2", rrid="", clonality="unknown")
    for ab in (a, b):
        PublicationImage.objects.using(DB).create(
            antibody=ab, application_type="WB", image="p/ABCD_AR148_WB.png")

    call_command("merge_duplicate_antibodies", "--strategy", "image", "--gene", "GBA1",
                 "--only-single-recommended", "--prefer-recommended",
                 "--apply", "--include-risky")
    out = capsys.readouterr().out
    assert "SKIP: no recommended rows" in out
    assert Antibody.objects.using(DB).filter(pk=a.pk).exists()
    assert Antibody.objects.using(DB).filter(pk=b.pk).exists()


def test_only_single_recommended_skips_a_group_with_two_verdicts(_pipeline_db, seeded, capsys):
    """The 2 'several rows recommended' groups must not be merged by the safe rule."""
    from django.core.management import call_command
    from pipeline.models import Antibody, PublicationImage, Target, Company
    target = Target.objects.using(DB).create(protein_name="Two verdicts", gene_name="TWOV")
    company = Company.objects.using(DB).create(name="Two Verdict Co")
    a = Antibody.objects.using(DB).create(
        target=target, company=company, catalogue_number="TV1", lot_number="1",
        clonality="unknown", wb_recommended=True)
    b = Antibody.objects.using(DB).create(
        target=target, company=company, catalogue_number="TV1", lot_number="2",
        clonality="unknown", ip_recommended=True)
    for ab in (a, b):
        PublicationImage.objects.using(DB).create(
            antibody=ab, application_type="WB", image="p/TV1_WB.png")

    call_command("merge_duplicate_antibodies", "--strategy", "image", "--gene", "TWOV",
                 "--only-single-recommended", "--prefer-recommended",
                 "--apply", "--include-risky")
    out = capsys.readouterr().out
    assert "SKIP: 2 recommended rows" in out
    assert Antibody.objects.using(DB).filter(pk=a.pk).exists()
    assert Antibody.objects.using(DB).filter(pk=b.pk).exists()


def test_only_single_recommended_still_merges_the_safe_case(seeded):
    """The SOD1 shape — one verdict, one blank — is exactly what it should merge."""
    from django.core.management import call_command
    from pipeline.models import Antibody
    call_command("merge_duplicate_antibodies", "--strategy", "image", "--gene", "SOD1",
                 "--only-single-recommended", "--prefer-recommended",
                 "--apply", "--include-risky")
    assert not Antibody.objects.using(DB).filter(pk=seeded["dupe"].pk).exists()
    assert Antibody.objects.using(DB).get(pk=seeded["keeper"].pk).wb_recommended is True


def test_report_refuses_rows_with_separate_figures(_pipeline_db, seeded, capsys):
    """
    Real TMEM175 case: Abcam ab300457 and ab309572 are the same clone
    (EPR24415-47) but different products — the second is the BSA/azide-free
    format, separately registered and separately characterised. The clone signal
    groups them; the report must say the merge refuses, not offer a keeper.
    """
    from django.core.management import call_command
    from pipeline.models import Antibody, PublicationImage, Target, Company
    target = Target.objects.using(DB).create(protein_name="TMEM175", gene_name="TMEM175")
    company = Company.objects.using(DB).create(name="Abcam TMEM")
    a = Antibody.objects.using(DB).create(
        target=target, company=company, catalogue_number="ab300457",
        rrid="AB_3101786", clonality="recombinant", clone_id="EPR24415-47",
        wb_recommended=True)
    b = Antibody.objects.using(DB).create(
        target=target, company=company, catalogue_number="ab309572",
        rrid="AB_3105952", clonality="recombinant", clone_id="EPR24415-47",
        wb_recommended=True)
    # each carries its OWN figure — the thing that proves they are two reagents
    PublicationImage.objects.using(DB).create(
        antibody=a, application_type="WB", image="p/TMEM175_ab300457_WB.png")
    PublicationImage.objects.using(DB).create(
        antibody=b, application_type="WB", image="p/TMEM175_ab309572_WB.png")

    call_command("find_duplicate_antibodies", "--gene", "TMEM175")
    out = capsys.readouterr().out
    assert "SEPARATE FIGURES for the same application" in out
    assert "REVIEW" in out and "DROP" not in out          # no keeper offered
    assert "groups the merge would accept    : 0" in out

    # and the merge itself refuses, for the same reason
    call_command("merge_duplicate_antibodies", "--strategy", "clone", "--gene", "TMEM175",
                 "--apply", "--include-risky")
    assert Antibody.objects.using(DB).filter(pk=a.pk).exists()
    assert Antibody.objects.using(DB).filter(pk=b.pk).exists()
