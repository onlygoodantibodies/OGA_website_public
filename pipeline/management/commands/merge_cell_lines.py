"""Merge two CellLine rows that are the same line.

`RPEI` and `RPE-1` are one line entered twice: both wild-type hTERT RPE-1 from
the Fon lab at McGill, one filed under the ATCC catalogue (CRL-4000, C-100) and
one as an academic-partner receipt in May 2025 (C-690). The August 2026 Access
delta wanted to normalise `RPEI` to the Cellosaurus spelling and
`fix_cell_line_identity` refused, correctly: renaming onto a name already in use
is a merge, not a rename, and doing it would leave two rows for one line with
the freezer split between them and both drawn as complete.

    python manage.py merge_cell_lines --from 901 --into 1434
    python manage.py merge_cell_lines --from C-100 --into C-690 --apply

Dry-run by default, and one transaction when it writes.

**Both C-numbers survive.** That is the point, not a side effect: a freeze-down
batch is a C-number and the vials in it share it, so two receipts of one line
are two `CellLineVial` rows under one `CellLine`. Nothing written on a tube
stops resolving — `cell_lines.by_c_number` reads both tables, and it read them
before this merge too.

### Why this cannot be a delete

Eight foreign keys point at `CellLine` and **four of them cascade**:
`CellLineVial`, `InventoryLocation`, `CellCultureEvent` and `Sample`. Deleting
the losing row before moving them destroys the freeze-down batch that carries
its C-number — the number this merge exists to preserve — and says nothing
while it does. Two more are `SET_NULL` (`ExperimentSession.cell_line_wt` /
`cell_line_ko`, and `CellLine.parent_line`), which are worse: a delete count
never mentions them, and a session quietly loses the line it was run against.

So everything moves first, the row is re-checked as empty, and only then is it
deleted. The foreign keys are found **by introspection** rather than from a
list, so a model added later cannot be left behind.

### What it refuses

**A different genotype, gene or bench is not the same line.** A wild type and a
knockout are different reagents however alike the names look; two knockouts of
different genes likewise; and two sites' lines that share a name are the
ordinary case this database is built around, not a duplicate — `HAP1` names
hundreds of rows and the site is what tells them apart. Each is refused by name
rather than merged, because a wrong merge here is not visible afterwards: the
rows are gone and the freezer reads as one line.

### What it carries across

The survivor keeps everything it already has. Anything it has *no* value for is
filled in from the losing row — catalogue number, Cellosaurus id, medium, origin
— so the ATCC number on the older RPE-1 record is not lost by keeping the newer
one. **Fill-only-blank**: nothing already recorded is overwritten, and the
carry-over is listed cell by cell. Booleans are never filled, only reported: on
a tick, "not set" and "false" are the same value and there is no way to tell a
blank from a decision.
"""
from __future__ import annotations

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from pipeline.services import cell_lines as cell_lines_svc
from pipeline.services import lab_numbers
from pipeline.models import CellLine, CellLineVial

DB = "pipeline_db"

# Filled on the survivor only where it has nothing. Identity — name, genotype,
# target, site — is never carried: a mismatch there is refused, not merged.
CARRY = [
    "catalogue_number", "lot_number", "cellosaurus_id", "species", "origin",
    "origin_comments", "clone", "growth_properties", "medium",
    "parental_line_name", "ko_validation_notes", "location_original_vial",
    "received_date", "company_id", "parent_line_id", "arrived_with_ko_id",
]
# Reported, never filled — on a tick there is no telling a blank from a "no".
REPORT_ONLY = ["ko_validated", "received", "thawed"]

# `acquisition_method` has a default rather than a blank, so "unknown" is what
# "nothing recorded" looks like on it.
_EMPTY = ("", None, "unknown")


def _cell_line_fks():
    """Every (model, field name) pointing at CellLine, found rather than listed."""
    out = []
    for model in apps.get_app_config("pipeline").get_models():
        for field in model._meta.get_fields():
            if getattr(field, "many_to_one", False) and field.related_model is CellLine:
                out.append((model, field.name))
    return sorted(out, key=lambda mf: (mf[0].__name__, mf[1]))


class Command(BaseCommand):
    help = ("Move every record from one CellLine onto another, keeping both "
            "C-numbers as freeze-down batches, then delete the empty row. "
            "Dry run unless --apply.")

    def add_arguments(self, parser):
        parser.add_argument("--from", dest="loser", required=True,
                            help="the cell line that goes away — a pk or a C-number")
        parser.add_argument("--into", dest="winner", required=True,
                            help="the cell line that survives — a pk or a C-number")
        parser.add_argument("--apply", action="store_true",
                            help="actually merge (default is a dry run)")

    # ------------------------------------------------------------------
    def handle(self, *args, **opts):
        loser = self._line(opts["loser"])
        winner = self._line(opts["winner"])
        if loser.pk == winner.pk:
            raise CommandError("--from and --into are the same cell line")
        self._refuse_if_not_one_line(loser, winner)

        apply = opts["apply"]
        self.stdout.write(
            f"\n{'MERGING' if apply else 'DRY RUN — nothing will change'}: "
            f"{self._label(loser)} → {self._label(winner)}\n")

        # 1. The C-numbers, which are the reason this is a merge and not a delete.
        rescue = self._unbatched_number(loser, winner)
        batches = list(CellLineVial.objects.using(DB)
                       .filter(cell_line_id=loser.pk)
                       .order_by("c_number").values_list("c_number", flat=True))
        keeping = sorted(
            n for n in (batches + cell_lines_svc.batch_numbers(winner, db=DB)
                        + ([rescue] if rescue else [])) if n)
        self.stdout.write(
            f"  C-numbers after the merge: "
            f"{cell_lines_svc.format_batches(keeping) or 'none'}")
        if rescue:
            self.stdout.write(self.style.WARNING(
                f"  C-{rescue} is the losing row's own number and no batch "
                f"carries it — a batch will be created for it, or the number "
                f"would go when the row does."))

        # 2. What moves.
        plan, total = [], 0
        for model, field in _cell_line_fks():
            count = model.objects.using(DB).filter(**{field: loser}).count()
            if not count:
                continue
            total += count
            plan.append((model, field, count))
            cascade = model._meta.get_field(field).remote_field.on_delete.__name__
            self.stdout.write(
                f"  {count:>6}  {model._meta.verbose_name_plural.title()} "
                f"({field}, {cascade})")
        if not total:
            self.stdout.write("  Nothing points at the losing row.")

        # 3. What gets filled in.
        fills = self._fills(loser, winner)
        for field, value in fills.items():
            self.stdout.write(f"  fill  {field} ← {value!r}")
        for field in REPORT_ONLY:
            a, b = getattr(loser, field), getattr(winner, field)
            if a != b:
                self.stdout.write(self.style.WARNING(
                    f"  differ  {field}: losing row {a!r}, survivor {b!r} — "
                    f"left as the survivor has it"))

        if not apply:
            self.stdout.write(self.style.NOTICE(
                f"\nDry run. {total} record(s) would move and "
                f"{len(fills)} field(s) would be filled in. "
                f"Re-run with --apply to do it."))
            return

        # No number is issued here: every one of them was written on a tube
        # years before this command ran.
        with lab_numbers.suspended(), transaction.atomic(using=DB):
            if rescue:
                # Created on the **survivor**, not on the row about to go: a
                # batch made on the losing row after the move plan was built
                # is a batch nothing moves, and the merge would then refuse
                # itself for the row not being empty.
                CellLineVial.objects.using(DB).create(
                    cell_line_id=winner.pk, c_number=rescue,
                    site_id=winner.site_id,
                    notes=f"Kept from {loser.name} when it was merged in.")
            for model, field, _count in plan:
                (model.objects.using(DB).filter(**{field: loser})
                 .update(**{f"{field}_id": winner.pk}))
            if fills:
                for field, value in fills.items():
                    setattr(winner, field, value)
                winner.save(using=DB, update_fields=list(fills))

            still_there = [
                f"{model.__name__}.{field}"
                for model, field in _cell_line_fks()
                if model.objects.using(DB).filter(**{field: loser}).exists()
            ]
            if still_there:
                # Never delete over something that did not move: four of these
                # cascade, so the row would take it with it.
                raise CommandError(
                    f"{', '.join(still_there)} still points at "
                    f"{self._label(loser)}. Nothing has been deleted and the "
                    f"whole merge is rolled back.")
            loser.delete(using=DB)

        self.stdout.write(self.style.SUCCESS(
            f"\nMerged. {total} record(s) moved onto {self._label(winner)} and "
            f"the duplicate row is gone."))
        self.stdout.write(
            f"  Its batches came too, so "
            f"{cell_lines_svc.format_batches(keeping)} all still resolve.")

    # ------------------------------------------------------------------
    def _refuse_if_not_one_line(self, loser, winner):
        if loser.genotype != winner.genotype:
            raise CommandError(
                f"{self._label(loser)} is {loser.genotype} and "
                f"{self._label(winner)} is {winner.genotype}. A wild type and a "
                f"knockout are different reagents however alike the names look.")
        if loser.target_id != winner.target_id:
            gene = lambda l: (l.target.gene_name if l.target_id and l.target
                              else "no gene")
            raise CommandError(
                f"{self._label(loser)} is a knockout of {gene(loser)} and "
                f"{self._label(winner)} of {gene(winner)}. Two knockouts of "
                f"different genes are two lines.")
        if loser.site_id != winner.site_id:
            raise CommandError(
                f"{self._label(loser)} and {self._label(winner)} are at "
                f"different benches. Two sites' lines sharing a name is the "
                f"ordinary case here, not a duplicate — HAP1 names hundreds of "
                f"rows and the site is what tells them apart. If one bench's "
                f"row really should go, move it with the boards first.")

    def _unbatched_number(self, loser, winner):
        """The losing row's own C-number, when no batch on either row has it.

        `CellLine.c_number` and `CellLineVial.c_number` are both written on
        tubes and 219 of 746 live vial numbers sit on no `CellLine` row, so the
        two tables genuinely disagree — a row whose own number is on no batch
        would lose it when the row goes.
        """
        if not loser.c_number:
            return None
        taken = set(CellLineVial.objects.using(DB)
                    .filter(cell_line_id__in=[loser.pk, winner.pk])
                    .values_list("c_number", flat=True))
        return None if loser.c_number in taken else loser.c_number

    @staticmethod
    def _fills(loser, winner):
        out = {}
        for field in CARRY:
            theirs = getattr(loser, field, None)
            ours = getattr(winner, field, None)
            if ours in _EMPTY and theirs not in _EMPTY:
                out[field] = theirs
        if (winner.acquisition_method in _EMPTY
                and loser.acquisition_method not in _EMPTY):
            out["acquisition_method"] = loser.acquisition_method
        return out

    def _line(self, value):
        text = str(value).strip()
        if text.isdigit():
            line = CellLine.objects.using(DB).filter(pk=int(text)).first()
            if line is not None:
                return line
        matches = cell_lines_svc.by_c_number(text, db=DB)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise CommandError(
                f"'{text}' names more than one line: "
                + ", ".join(f"{m.name} (id {m.pk})" for m in matches)
                + ". Give the id instead.")
        raise CommandError(
            f"No cell line with id or C-number '{text}'. Ids are on the cell "
            f"lines board; a C-number is written on the tube.")

    def _label(self, line):
        numbers = cell_lines_svc.batch_numbers(line, db=DB)
        bits = [f"id {line.pk}", repr(line.name), line.genotype]
        if line.target_id and line.target and line.target.gene_name:
            bits.append(line.target.gene_name)
        if numbers:
            bits.append(cell_lines_svc.format_batches(numbers))
        elif line.c_number:
            bits.append(f"C-{line.c_number}")
        return " ".join(str(b) for b in bits)
