"""Where a gene has got to, as a sequence of steps.

A target's characterisation has a natural order — somebody has to want it, there
has to be a knockout to test against, there have to be antibodies in hand, the
western blot runs and **confirms the knockout**, the remaining applications
follow, and then it is written up. The app already knows every one of those
facts; they were just spread across four boards, so the question a PI actually
asks — *what is blocking STMN2?* — could not be answered by looking at anything.

The position of **KO confirmed** is the one worth stating, because it was wrong.
It sat third, before the antibodies, so a gene with a knockout line and nothing
else was told its next step was to confirm the knockout — which cannot be done
until there is an antibody to blot with and a WB to run it in. Confirmation is
an *output* of the first western blot, not a prerequisite for it (owner, 4 Aug).

Nothing here is a new status field, and nothing is typed. Every step is derived
from records that already exist, the same way ``target_board.completed_report_q``
derives "completed" from a Report rather than believing a dropdown. A step is
done because the evidence for it is on file.

Deliberately *not* a percentage. Many targets will never have all four
applications — Harvinder's point — so "3 of 7 steps" would call a finished piece
of work incomplete. It reports what is done, what is next, and stays quiet about
the rest.
"""
from __future__ import annotations

from pipeline.models import Antibody, CellLine, Report, Target, TargetNomination
from pipeline.services import ko_validation
from pipeline.services.target_board import (APPLICATIONS, application_coverage,
                                            completed_report_q)

DB = "pipeline_db"


def _step(key, label, done, detail, hint, url=""):
    return {"key": key, "label": label, "done": bool(done), "detail": detail,
            "hint": hint, "url": url}


def steps_for(target: Target) -> list[dict]:
    """The characterisation sequence for one gene, each step with its evidence.

    One query per step, all counts or existence checks — this renders on a page
    that already loads the same objects, and it must not grow with the dataset.
    """
    pk = target.pk
    gene = target.gene_name or ""
    q = f"?gene={gene}" if gene else ""

    noms = list(TargetNomination.objects.using(DB)
                .filter(target_id=pk).select_related("site"))
    sites = sorted({n.site.name for n in noms if n.site_id})
    # The Access-era targets predate TargetNomination, so for them the legacy
    # Target.site FK is the only record of whose target this is. Without this,
    # every imported target would report step one as not started — hundreds of
    # genes told they were nobody's.
    from_import = bool(not sites and target.site_id)
    if from_import:
        sites = [target.site.name]

    # Through `ko_validation`, never off the tick. `views/dashboard.py` learned
    # this one field over — the gene page's TARGET INFORMATION box counts
    # `confirmed()`, so a row whose tick and reason disagree is not counted and
    # the box says so — and this strip, a few centimetres below it on the same
    # page, went on counting the bit alone. So a gene with one disputed line read
    # **No — none confirmed yet** in the box and **KO confirmed ✓ · 1 of 1
    # confirmed** in the strip, with the summary above both saying everything on
    # the list was done. Six live rows carry a tick over a reason recording
    # `Failed-…` or `Inconclusive-…`; the strip was publishing those as confirmed
    # knockouts, which is the direction this module must never lean.
    ko_lines = list(CellLine.objects.using(DB)
                    .filter(target_id=pk, genotype="KO")
                    .only("pk", "name", "ko_validated", "ko_validation_notes"))
    validated = [c for c in ko_lines if ko_validation.confirmed(c)]
    disputed = [c for c in ko_lines if ko_validation.disagrees(c)]

    antibody_count = Antibody.objects.using(DB).filter(target_id=pk).count()
    coverage = application_coverage([pk]).get(pk, {})
    report = (Report.objects.using(DB)
              .filter(completed_report_q(), target_id=pk).first())

    out = [
        _step("nominated", "Nominated", sites or noms,
              (", ".join(sites) + (" (from the import)" if from_import else ""))
              if sites else ("nominated, no site set" if noms else ""),
              "No site is pursuing this gene yet. Set one on the target board.",
              f"/pipeline/targets/board/{q}"),
        _step("ko_line", "KO line", ko_lines,
              f"{len(ko_lines)} knockout line{'' if len(ko_lines) == 1 else 's'}"
              if ko_lines else "",
              "A knockout is what makes a result specific. Add the KO line and "
              "its wild-type parent on the cell lines board.",
              f"/pipeline/cell-lines/board/{q}"),
        _step("antibodies", "Antibodies", antibody_count,
              f"{antibody_count} on file" if antibody_count else "",
              "Nothing to test yet. Add the antibodies on the antibodies board.",
              f"/pipeline/antibodies/board/{q}"),
    ]

    # **KO confirmation comes after the western blot, because the blot is how
    # the knockout is confirmed** (owner, 4 Aug). The strip had it third, so a
    # gene with a knockout line and no antibodies was told its next step was
    # "KO confirmed" — a thing that cannot be done until there is an antibody to
    # blot with and a WB to run. It read as the app not understanding the bench.
    #
    # The order the sequence now states is the order the work happens in: a
    # site wants the gene, somebody makes the knockout, antibodies arrive, the
    # WB runs, that WB confirms the knockout, and the remaining applications
    # follow. The cell lines and the antibodies genuinely are needed first, so
    # they stay where they were.
    # A line left out of the count is a line nobody knows to fix, so the detail
    # says how many disagreed rather than quietly narrowing the numerator — the
    # same half-sentence the TARGET INFORMATION box carries.
    ko_detail = f"{len(validated)} of {len(ko_lines)} confirmed" if ko_lines else ""
    if disputed:
        ko_detail += (f" · {len(disputed)} to check"
                      if ko_detail else f"{len(disputed)} to check")

    ko_confirmed = _step(
        "ko_validated", "KO confirmed", validated,
        ko_detail,
        "An unconfirmed knockout makes every result that used it provisional. "
        "The western blot above is normally what confirms it — record how, on "
        "the cell lines board.",
        f"/pipeline/cell-lines/board/{q}")

    for app in APPLICATIONS:
        where = coverage.get(app) or []
        out.append(_step(
            f"app_{app}", app, where,
            ", ".join(where),
            f"No {app} session recorded. Many targets never need all four — "
            f"this is only worth chasing if {app} is part of the plan.",
            f"/pipeline/sessions/board/{q}&procedure={app}" if q
            else f"/pipeline/sessions/board/?procedure={app}"))
        # Straight after WB, wherever WB sits in APPLICATIONS — reading the
        # position out of the list rather than hard-coding an index, so a fifth
        # application added later cannot silently move it.
        if app == "WB":
            out.append(ko_confirmed)

    if ko_confirmed not in out:      # WB is not in APPLICATIONS — keep the step.
        out.append(ko_confirmed)

    out.append(_step(
        "reported", "Reported", report,
        ("Zenodo" if report and report.zenodo_doi else "F1000") if report else "",
        "Not written up. A gene counts as completed once it has a Zenodo DOI or "
        "a published F1000 date — there is nothing to tick.",
        f"/pipeline/target/{pk}/"))
    return out


def headline(steps) -> dict:
    """One phrase for where this gene has got to, derived like everything else.

    The gene page carried a badge reading the stored ``Target.status``, which is
    set to "not started" when the row is created and then advanced by nothing at
    all — the same shape of bug as ``Target.site``. So the badge said **Not
    Started** in the same viewport as a strip showing two procedures run and six
    antibodies on file. Two answers to one question, one of them wrong.

    Deliberately coarse: the strip below it already says which step is which, and
    a second detailed summary would just be another thing to disagree with.
    """
    done = {s["key"] for s in steps if s["done"]}
    if "reported" in done:
        return {"label": "Reported", "tone": "bg-green-100 text-green-800"}
    if any(k.startswith("app_") for k in done):
        return {"label": "Testing under way", "tone": "bg-blue-100 text-blue-800"}
    if "antibodies" in done or "ko_line" in done:
        return {"label": "Reagents being gathered", "tone": "bg-blue-100 text-blue-800"}
    if "nominated" in done:
        return {"label": "Nominated, not started", "tone": "bg-gray-100 text-gray-800"}
    return {"label": "Not started", "tone": "bg-gray-100 text-gray-800"}


def next_step(steps) -> dict | None:
    """The first step with no evidence behind it.

    The four applications are skipped when *any* of them has been run: once a
    gene has WB and IF, the missing IP is a choice rather than a gap, and
    pointing at it would nag about work nobody planned.
    """
    apps = [s for s in steps if s["key"].startswith("app_")]
    any_app = any(s["done"] for s in apps)
    for s in steps:
        if s["done"]:
            continue
        if any_app and s["key"].startswith("app_"):
            continue
        return s
    return None
