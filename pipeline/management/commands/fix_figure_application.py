"""Relabel published figures that were cropped under the wrong application.

A figure is filed under one of ``WB``/``IP``/``ICC-IF``/``FC``, and the cropper
takes that from whoever pressed the button. Get it wrong and the mistake is
loud on the gene page and silent everywhere it matters: the antibody is
recommended *for the wrong assay*, the extension and the MCP report it that
way, and the readings the figure came from sit under a heading with no figure
beside them.

**SLC2A6, 29 Aug 2026, is the case this was written for.** Two crops — both
plainly WT/KO western blots across HAP1 and HCT116 — were filed as
immunoprecipitation, and ``ab119272`` carried an *IP* recommendation because of
it. The corroboration was in the database rather than in the picture: the gene
has **no ``IpResult`` row at all**, and every reading on it belongs to a session
whose ``procedure_type`` is ``WB``. That is the check this command prints, and
it is the one worth reading before ``--apply``: if the readings do not agree
with where you are moving the figure, the figure may not be the thing that is
wrong.

What moves with the figure:

* **The recommendation.** ``ab119272`` was recommended for IP; it must come out
  of IP and go into WB, or the correction leaves a public verdict about an assay
  nobody ran. ``core/recommendations.py`` owns which boolean carries which
  application, so it is asked rather than copied.
* **A queued crop**, if one exists at the same key. The oldest figures predate
  the review queue and have no pending row — SLC2A6's two are exactly that — so
  this is a no-op more often than not.
* **A judgement on Judge outcomes**, which is keyed the same way.

What does **not** move: the bytes. The object keeps its key, because a key is
not read for its application anywhere (checked: nothing parses it), a rename is
a storage move against a live public URL, and the older figures are named from
a convention that predates the current one — ``experiments/Glut-IP-ab119272.png``
does not become truer by being renamed. It does mean a corrected figure can keep
a filename naming the old application; say so rather than let somebody discover
it.

Dry run unless ``--apply``.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.recommendations import APPLICATIONS, _FLAG
from pipeline.models import (Antibody, AntibodyOutcome, PendingPublicationImage,
                             PublicationImage)

DB = "pipeline_db"

#: Which result model records a reading for each application — used only to say
#: whether the readings agree with the move. It is evidence for a person, never
#: a gate: a figure can legitimately exist for an application whose readings were
#: never typed up, which is 129 antibodies on WB alone.
_RESULT_REL = {
    "WB": "wb_results",
    "IP": "ip_results",
    "ICC-IF": "if_results",
    "FC": "fc_results",
}


class Command(BaseCommand):
    help = ("Move published figures from one application to another, taking "
            "the recommendation with them. Dry run unless --apply.")

    def add_arguments(self, parser):
        parser.add_argument(
            "--gene", help="Gene symbol whose figures to move, e.g. SLC2A6.")
        parser.add_argument(
            "--catalogue", action="append", default=[],
            help="Narrow to one catalogue number. Repeatable.")
        parser.add_argument(
            "--ids", help="Comma-separated PublicationImage ids, instead of "
                          "--gene. Use when only some of a gene's figures are "
                          "wrong.")
        parser.add_argument(
            "--from", dest="from_app", required=True,
            help=f"The application they are filed under now. One of "
                 f"{', '.join(APPLICATIONS)}.")
        parser.add_argument(
            "--to", dest="to_app", required=True,
            help="The application they should be filed under.")
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually write. Without it nothing is saved.")

    def handle(self, *args, **opts):
        from_app, to_app = opts["from_app"], opts["to_app"]
        for name, value in (("--from", from_app), ("--to", to_app)):
            if value not in APPLICATIONS:
                raise CommandError(
                    f"{name}={value!r} is not an application. "
                    f"Use one of {', '.join(APPLICATIONS)} — note the exact "
                    f"casing, and that immunofluorescence is 'ICC-IF' in the "
                    f"database even though a crop's filename says 'IF'.")
        if from_app == to_app:
            raise CommandError(f"--from and --to are both {from_app}; "
                               "there is nothing to move.")
        if not opts["gene"] and not opts["ids"]:
            raise CommandError("Name what to move: --gene SLC2A6, or "
                               "--ids 804,805.")

        images = (PublicationImage.objects.using(DB)
                  .filter(application_type=from_app)
                  .select_related("antibody", "antibody__target",
                                  "antibody__company"))
        if opts["ids"]:
            try:
                wanted = [int(v) for v in opts["ids"].split(",") if v.strip()]
            except ValueError:
                raise CommandError(f"--ids must be numbers: {opts['ids']!r}")
            images = images.filter(pk__in=wanted)
        if opts["gene"]:
            images = images.filter(antibody__target__gene_name__iexact=opts["gene"])
        if opts["catalogue"]:
            images = images.filter(antibody__catalogue_number__in=opts["catalogue"])

        images = list(images.order_by("antibody__catalogue_number"))
        if not images:
            where = opts["gene"] or opts["ids"]
            self.stdout.write(self.style.WARNING(
                f"No {from_app} figures found for {where}. Nothing to do — "
                f"check the gene spelling, or whether they are already {to_app}."))
            return

        # A figure already filed under the destination is the one thing that
        # cannot be resolved here: the pair is unique per antibody, so moving
        # would collide. Named, with both figures, rather than raised as an
        # IntegrityError at the write.
        taken = set(
            PublicationImage.objects.using(DB)
            .filter(antibody_id__in=[i.antibody_id for i in images],
                    application_type=to_app)
            .values_list("antibody_id", flat=True))

        moving, refused = [], []
        for img in images:
            (refused if img.antibody_id in taken else moving).append(img)

        self._report(moving, refused, from_app, to_app)
        if not moving:
            return

        if not opts["apply"]:
            self.stdout.write(self.style.WARNING(
                "\nDry run — nothing was written. Re-run with --apply once the "
                "readings above look right."))
            return

        with transaction.atomic(using=DB):
            counts = self._apply(moving, from_app, to_app)
        self.stdout.write(self.style.SUCCESS(
            f"\nApplied. {counts['figures']} figure(s) moved to {to_app}, "
            f"{counts['recommendations']} recommendation(s) moved with them, "
            f"{counts['pending']} queued crop(s) and {counts['outcomes']} "
            f"judgement(s) followed."))

    # ── what a person needs to see before pressing --apply ──────────────────
    def _report(self, moving, refused, from_app, to_app):
        self.stdout.write(
            f"\nMoving {len(moving)} figure(s) from {from_app} to {to_app}.\n")
        for img in moving:
            ab = img.antibody
            gene = ab.target.gene_name if ab.target_id else "(no gene)"
            supplier = ab.company.name if ab.company_id else "(no supplier)"
            self.stdout.write(f"  {ab.catalogue_number} · {gene} · {supplier}")

            # Whether the readings agree — the evidence that the figure, and not
            # the label on the readings, is the thing that is wrong.
            here = getattr(ab, _RESULT_REL[from_app]).using(DB).count()
            there = getattr(ab, _RESULT_REL[to_app]).using(DB).count()
            self.stdout.write(
                f"      readings on file: {there} {to_app}, {here} {from_app}")
            if here and not there:
                self.stdout.write(self.style.WARNING(
                    f"      the readings are all {from_app} — check the figure "
                    f"really is a {to_app} before applying"))

            if getattr(ab, _FLAG[from_app]):
                already = getattr(ab, _FLAG[to_app])
                note = (f"      recommendation moves {from_app} → {to_app}"
                        + (f" ({to_app} was already recommended)" if already else ""))
                self.stdout.write(self.style.SUCCESS(note))
            else:
                self.stdout.write(f"      not recommended for {from_app}; "
                                  "nothing to move")

            if img.image and img.image.name:
                self.stdout.write(f"      file stays at {img.image.name} "
                                  "(the bytes are not moved or renamed)")

        for img in refused:
            ab = img.antibody
            self.stdout.write(self.style.ERROR(
                f"  REFUSED {ab.catalogue_number}: it already has a {to_app} "
                f"figure, and one antibody has at most one figure per "
                f"application. Decide which of the two is the {to_app} blot "
                f"and withdraw the other first."))

    def _apply(self, moving, from_app, to_app):
        counts = {"figures": 0, "recommendations": 0, "pending": 0,
                  "outcomes": 0}
        for img in moving:
            ab = img.antibody

            # The queued crop behind it, and any judgement recorded against it —
            # both keyed (antibody, application) like the figure, so both would
            # otherwise be left pointing at an application the antibody no
            # longer has a figure for.
            for model, key in ((PendingPublicationImage, "pending"),
                               (AntibodyOutcome, "outcomes")):
                if model.objects.using(DB).filter(
                        antibody_id=ab.pk, application_type=to_app).exists():
                    continue
                counts[key] += model.objects.using(DB).filter(
                    antibody_id=ab.pk, application_type=from_app
                ).update(application_type=to_app)

            img.application_type = to_app
            img.save(using=DB, update_fields=["application_type"])
            counts["figures"] += 1

            if getattr(ab, _FLAG[from_app]):
                setattr(ab, _FLAG[from_app], False)
                setattr(ab, _FLAG[to_app], True)
                ab.save(using=DB,
                        update_fields=[_FLAG[from_app], _FLAG[to_app]])
                counts["recommendations"] += 1
        return counts
