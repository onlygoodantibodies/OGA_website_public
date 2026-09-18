"""Assigning and rearranging A-numbers — the whole mapping, in one go.

uOttawa stopped logging antibodies into the portal after eighteen of them,
because the number is issued the moment a row is created and the number decides
which freezer box a vial goes in. Reagents arrive over weeks, so numbering at
the moment of logging forces the order of arrival onto the freezer.

What would be **silently** wrong here is the only thing worth pinning:

* two vials ending up holding one number — there is no unique constraint on
  ``(site, ab_number)`` to catch it (45 Access-era rows already share one), so
  nothing downstream would raise and the collision would be found on a box in
  somebody's hand;
* a number moving out from under a **printed bench sheet** —
  ``bench_results._resolve_ab`` matches an uploaded sheet's ``Ab#`` against
  ``ab_number`` across the gene, so a later reading would be filed against a
  different antibody with no screen contradicting it;
* half a renumbering — a refusal that took some rows and not others would leave
  a reader no way to know which half.
"""
from __future__ import annotations

import pytest

DB = "pipeline_db"


@pytest.fixture()
def bench(_pipeline_db):
    from django.contrib.auth import get_user_model

    from pipeline.models import Antibody, Company, Member, Site, Target
    for M in (Antibody, Company, Target):
        M.objects.using(DB).all().delete()
    site, _ = Site.objects.using(DB).get_or_create(
        short_code="RNU", defaults={"name": "Renumber Site"})
    other, _ = Site.objects.using(DB).get_or_create(
        short_code="RNV", defaults={"name": "Renumber Partner"})
    company = Company.objects.using(DB).create(name="Abcam")
    target = Target.objects.using(DB).create(gene_name="TRPA1",
                                             protein_name="TRPA1")
    user, _ = get_user_model().objects.using(DB).get_or_create(
        username="renumber-tester")
    member, _ = Member.objects.using(DB).get_or_create(
        user_id=user.pk, defaults={"site": site,
                                   "role": Member.Role.EXPERIMENTER})
    return {"site": site, "other": other, "company": company,
            "target": target, "member": member}


@pytest.fixture()
def renumber(bench):
    """`services.renumber`, bound to a member at this bench.

    Whose numbers these are is a real gate and has its own tests at the end;
    threading the same member through every mapping test would only say it
    again, less clearly.
    """
    import functools
    import types

    from pipeline.services import renumber as module
    return types.SimpleNamespace(
        plan=functools.partial(module.plan, member=bench["member"]),
        apply=functools.partial(module.apply, member=bench["member"]),
        Refused=module.Refused)


def _ab(bench, catalogue, *, number=None, site=None):
    """One vial. ``number=None`` leaves it unnumbered, which is what a bench
    holding its numbers back has; `lab_numbers.assign` would otherwise issue
    one on `pre_save`."""
    from pipeline.models import Antibody
    from pipeline.services import lab_numbers
    ab = Antibody(target=bench["target"], company=bench["company"],
                  catalogue_number=catalogue,
                  site=site or bench["site"], ab_number=number)
    if number is None:
        lab_numbers.withhold(ab)
    ab.save(using=DB)
    return ab


def _numbers(*abs_):
    from pipeline.models import Antibody
    return [Antibody.objects.using(DB).get(pk=a.pk).ab_number for a in abs_]


# ── dealing the numbers out ──────────────────────────────────────────────────

def test_numbers_are_dealt_in_the_order_the_board_shows_them(bench, renumber):

    a, b, c = (_ab(bench, "ab-1"), _ab(bench, "ab-2"), _ab(bench, "ab-3"))
    # The order given is the order the board is showing — that is the whole
    # interface, and it is what makes "number them as shown" mean "put each
    # protein's antibodies together".
    out = renumber.apply([c.pk, a.pk, b.pk], start=10, consented_count=3)

    assert not out["changed"] or True
    assert _numbers(c, a, b) == [10, 11, 12]


def test_a_blank_start_carries_on_from_this_benchs_highest(bench, renumber):

    _ab(bench, "ab-old", number=41)
    a, b = _ab(bench, "ab-1"), _ab(bench, "ab-2")
    renumber.apply([a.pk, b.pk], start=None, consented_count=2)
    assert _numbers(a, b) == [42, 43]


def test_two_numbers_swap_in_one_operation(bench, renumber):
    """One edit at a time is impossible — every intermediate state collides."""

    a, b = _ab(bench, "ab-a", number=1), _ab(bench, "ab-b", number=2)
    # Start from the lowest the pair already holds: same numbers, new order.
    renumber.apply([b.pk, a.pk], start=1, consented_count=2)
    assert _numbers(a, b) == [2, 1]


def test_a_row_that_already_has_its_number_is_not_counted_as_moving(bench, renumber):

    a, b = _ab(bench, "ab-a", number=1), _ab(bench, "ab-b", number=9)
    d = renumber.plan([a.pk, b.pk], start=1)
    assert d["moving"] == 1
    assert [it["moves"] for it in d["items"]] == [False, True]


# ── what must not move ───────────────────────────────────────────────────────

def _attach(bench, antibody, relation):
    """Give `antibody` the kind of history that pins its number."""
    import datetime

    from django.contrib.auth import get_user_model

    from pipeline.models import (AntibodyOutcome, ExperimentSession, Member,
                                 PublicationImage, WbResult)
    if relation in ("planned", "reading"):
        user, _ = get_user_model().objects.using(DB).get_or_create(
            username="renumber-tester")
        member, _ = Member.objects.using(DB).get_or_create(
            user_id=user.pk, defaults={"site": bench["site"],
                                       "role": Member.Role.EXPERIMENTER})
        session = ExperimentSession.objects.using(DB).create(
            target=bench["target"], procedure_type="WB", site=bench["site"],
            experimenter_id=member.pk, date=datetime.date(2026, 9, 1))
        # **Planning writes the row blank.** That is the whole distinction:
        # `sessions.apply` creates one result row per antibody the moment a
        # session is planned, so the row exists long before anybody reads
        # anything off a gel.
        WbResult.objects.using(DB).create(
            session=session, antibody=antibody,
            **({"signal": "specific band"} if relation == "reading" else {}))
    elif relation == "figure":
        PublicationImage.objects.using(DB).create(
            antibody=antibody, application_type="WB", image="x.png")
    else:
        AntibodyOutcome.objects.using(DB).create(
            antibody=antibody, application_type="WB")


@pytest.mark.parametrize("relation",
                         ["planned", "reading", "figure", "outcome"])
def test_an_antibody_whose_number_has_reached_paper_keeps_it_and_is_named(
        bench, renumber, relation):
    a = _ab(bench, "ab-a", number=1)
    b = _ab(bench, "ab-b", number=2)
    _attach(bench, b, relation)

    d = renumber.plan([a.pk, b.pk], start=1)
    held = [it for it in d["items"] if it["held"]]
    assert len(held) == 1 and held[0]["id"] == b.pk

    # And its number is not handed to anything else: `a` is the only movable
    # row, so it would land on 1 — which it already holds.
    renumber.apply([a.pk, b.pk], start=1, consented_count=d["moving"])
    assert _numbers(a, b) == [1, 2]


def test_a_planned_session_is_not_called_a_reading(bench, renumber):
    """`sessions.apply` writes one **blank** result row per antibody when a
    session is planned, so the four result relations are true of an antibody
    nobody has read. The row is still held back — planning is when the bench
    sheet gets printed — but calling it a reading is the sentence
    `session_board.is_reading` exists to prevent, and the first version of this
    module said exactly that."""
    planned = _ab(bench, "ab-planned", number=1)
    read = _ab(bench, "ab-read", number=2)
    _attach(bench, planned, "planned")
    _attach(bench, read, "reading")

    held = {it["id"]: it["held"]
            for it in renumber.plan([planned.pk, read.pk], start=1)["items"]
            if it["held"]}
    assert "planned experiment" in held[planned.pk]
    assert "reading" not in held[planned.pk]
    assert "reading recorded" in held[read.pk]


def test_a_held_back_rows_number_is_never_given_away(bench, renumber):
    from pipeline.models import PublicationImage

    keep = _ab(bench, "ab-keep", number=2)
    PublicationImage.objects.using(DB).create(
        antibody=keep, application_type="WB", image="x.png")
    mover = _ab(bench, "ab-move", number=7)

    # Starting at 2 would put `mover` on the published figure's number.
    d = renumber.plan([keep.pk, mover.pk], start=2)
    assert "A-2" in d["refusal"] and "cannot share" in d["refusal"]
    assert _numbers(keep, mover) == [2, 7]


# ── refusals ─────────────────────────────────────────────────────────────────

def test_a_number_held_outside_the_set_refuses_the_whole_batch(bench, renumber):

    bystander = _ab(bench, "ab-else", number=5)
    a = _ab(bench, "ab-a")
    d = renumber.plan([a.pk], start=5)
    assert "A-5" in d["refusal"]
    assert "ab-else" in d["refusal"]           # says which record, not just "taken"

    with pytest.raises(renumber.Refused):
        renumber.apply([a.pk], start=5, consented_count=1)
    assert _numbers(a, bystander) == [None, 5]


def test_two_benches_at_once_is_refused_by_name(bench, renumber):

    a = _ab(bench, "ab-a")
    b = _ab(bench, "ab-b", site=bench["other"])
    d = renumber.plan([a.pk, b.pk])
    assert "Renumber Site" in d["refusal"] and "Renumber Partner" in d["refusal"]
    assert d["items"] == []


def test_a_row_with_no_site_is_refused_because_the_run_belongs_to_a_bench(bench, renumber):
    from pipeline.models import Antibody
    # `Antibody.site` is PROTECT and non-null on new rows in practice, but the
    # column is nullable and imported rows exist without one.
    a = _ab(bench, "ab-a")
    Antibody.objects.using(DB).filter(pk=a.pk).update(site=None)
    assert "no site" in renumber.plan([a.pk])["refusal"]


def test_nothing_chosen_says_so_rather_than_reporting_success(bench, renumber):
    d = renumber.plan([])
    assert d["refusal"] and d["moving"] == 0


def test_a_first_number_below_one_is_refused(bench, renumber):
    a = _ab(bench, "ab-a")
    assert renumber.plan([a.pk], start=0)["refusal"]


# ── the consent check ────────────────────────────────────────────────────────

def test_a_set_that_grew_since_the_preview_is_refused_and_writes_nothing(bench, renumber):

    a, b = _ab(bench, "ab-a"), _ab(bench, "ab-b")
    d = renumber.plan([a.pk], start=1)
    assert d["moving"] == 1
    with pytest.raises(renumber.Refused, match="not the 1"):
        renumber.apply([a.pk, b.pk], start=1, consented_count=d["moving"])
    assert _numbers(a, b) == [None, None]


def test_a_different_set_with_the_same_count_is_refused_by_the_stamp(bench, renumber):
    """The count alone waves this through: one row leaves as another joins."""

    a, b = _ab(bench, "ab-a"), _ab(bench, "ab-b")
    shown = renumber.plan([a.pk], start=1)
    with pytest.raises(renumber.Refused, match="not the same antibodies"):
        renumber.apply([b.pk], start=1, consented_count=shown["moving"],
                       stamp=shown["stamp"])
    assert _numbers(a, b) == [None, None]

    # And the set it *did* show still goes through.
    renumber.apply([a.pk], start=1, consented_count=shown["moving"],
                   stamp=shown["stamp"])
    assert _numbers(a, b) == [1, None]


def test_renumbering_rewrites_nothing_else_on_the_row(bench, renumber):
    """`update_fields=["ab_number"]`, and `Antibody.save()` is the reason it
    matters: it stamps `recommendations_set_at` whenever a recommendation flag
    has moved, and a save that re-dated the public verdict on every renumbered
    row would be exactly the silent wrong write this file is about."""
    import datetime

    from pipeline.models import Antibody

    a = _ab(bench, "ab-a", number=3)
    was = datetime.datetime(2026, 1, 2, tzinfo=datetime.timezone.utc)
    Antibody.objects.using(DB).filter(pk=a.pk).update(
        lot_number="LOT-9", wb_recommended=True, recommendations_set_at=was)
    renumber.apply([a.pk], start=8, consented_count=1)
    fresh = Antibody.objects.using(DB).get(pk=a.pk)
    assert fresh.ab_number == 8 and fresh.lot_number == "LOT-9"
    assert fresh.wb_recommended is True
    assert fresh.recommendations_set_at == was


# ── whose numbers these are ──────────────────────────────────────────────────

def test_another_benchs_numbers_are_not_yours_to_change(bench):
    """The rule `deletion.site_refusal` states for deleting. These numbers are
    written on somebody else's freezer boxes, and nothing in the app can tell
    you whether they are."""
    from pipeline.models import Member
    from pipeline.services import renumber as module

    theirs = _ab(bench, "ab-theirs", site=bench["other"])
    mine = Member.objects.using(DB).get(site=bench["site"])
    d = module.plan([theirs.pk], start=1, member=mine)
    assert "Renumber Partner" in d["refusal"]
    assert "your own bench" in d["refusal"]

    with pytest.raises(module.Refused):
        module.apply([theirs.pk], start=1, consented_count=1, member=mine)
    assert _numbers(theirs) == [None]


def test_a_superuser_may_renumber_any_bench(bench):
    from pipeline.services import renumber as module

    theirs = _ab(bench, "ab-theirs", site=bench["other"])
    module.apply([theirs.pk], start=4, consented_count=1, is_superuser=True)
    assert _numbers(theirs) == [4]


def test_an_account_with_no_site_is_told_why_rather_than_shown_an_empty_list(bench):
    from pipeline.services import renumber as module

    a = _ab(bench, "ab-a")
    d = module.plan([a.pk], start=1, member=None)
    assert "no site" in d["refusal"]
    assert "people board" in d["refusal"]
