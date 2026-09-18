"""
Django management command: restore_cell_line_clones

Puts back the knockout clones `restructure_cell_lines` merged away.

    python manage.py restore_cell_line_clones            # dry run
    python manage.py restore_cell_line_clones --apply
    python manage.py restore_cell_line_clones --gene ACSL5 --apply

### What went wrong

**A gene and a background define a knockout; a clone is one instance of it** —
a separate single-cell origin, often a separate guide. `CellLine` had no room
for that: `clone` was one text column on one row. So when
`restructure_cell_lines._phase4_dedup_ko` merged knockouts by
`(name, site, target)` — a key the clone is not in — every clone of one KO
collapsed into a single row. `_merge_into` moved the losers' vials to the
survivor, repointed their sessions, and **deleted the loser copying no scalar
field at all**, so each merged-away clone's identity went with it.

Measured against `access_csvs/CellLines.csv`, the export the original import
read (3 Sep 2026): **26 knockout groups carry more than one distinct clone id**,
77 Access rows behind 26 live rows, **47 clone identities destroyed**. The
worst is `HCT116 ACSL5 KO` — clones 2.3, 2.5, 2.6, 2.8, 4.2, 4.5 and 4.11,
which are two guide series, drawn today as one row labelled clone 2.3 with
C-472…C-478 listed under it as freeze-down batches. Clone 2.8 is the one the
export marks `Confirmed`.

The signature is visible without the export: 60 of the 93 multi-batch KO lines
hold a *perfectly consecutive* run of C-numbers, against 3 of 35 wild types. A
parental frozen down repeatedly over years gets scattered numbers (HeLa's 17
batches run C-15 to C-742); a consecutive run of seven is a set of clones
numbered in one sitting.

### Why it is recoverable

`CellLineVial.access_id` survived the merge one-for-one: the vial now carrying
C-472 is Access row 487, which is clone 2.3, and so on down the run. So each
lost clone can be read straight out of the export — no guessing, no heuristic —
and given back its own row with its own vial.

### What this writes

For each Access row in a split group that is not the survivor's own:

  * a **new `CellLine`** carrying that row's clone, its C-number and its own
    `access_id`, with the structure (name, gene, genotype, site, parent,
    supplier) copied from the survivor, because that is the half the merge did
    not damage;
  * the **vial** whose `access_id` is that row's, moved onto it, so the number
    on the tube and the record agree again;
  * the row's own **KO verdict** (`KOInfo`), read through
    `services/ko_validation.py`'s own vocabulary rather than trusted as a tick.

Sessions are repointed only where the answer is unambiguous. A live result row
keeps its `access_id`, and the Access `Wb`, `IP` and `IF` tables name the cell
lines each experiment used (`lane1..4CellLineID`, `CellLineID`,
`CellLine1/2ID`), so a session whose readings all name **one** clone of the
group is moved to that clone. One naming two is left where it is and **printed**
— the historical import grouped sessions by (procedure, target) rather than by
day, so a session really can span two clones, and inventing an answer there
would be the same class of mistake as the merge itself.

Three things it will not do. It never touches a group whose Access rows record
**one** clone or none — those really are freeze-downs of one line, which is what
the vial table already says. It never deletes or merges anything; every write is
an insert or a repoint, so the way back is to delete the rows it created. And it
is **idempotent**: a clone whose `access_id` is already on a live row is
skipped, so a second run reports zero and writes nothing.

Live data — read `.claude/skills/production-data`, take the backup, and run the
dry run first.
"""

import csv
import os
from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from pipeline.models import (CellLine, CellLineVial, ExperimentSession,
                             IfResult, IpResult, WbResult)
from pipeline.services import ko_validation

DB = "pipeline_db"

# The export the original import read. Committed, so the default needs no
# argument — the recovery is not a thing anybody should have to locate a file to
# run.
DEFAULT_CSV = os.path.join("access_csvs", "CellLines.csv")

# Where each Access result table names the cell lines an experiment used. A
# western blot names up to four lanes because a blot is WT and KO side by side;
# only the ids that fall inside the split group are read, so the WT lanes filter
# themselves out without this having to know which lane was which.
_RESULT_SOURCES = (
    (WbResult, "Wb.csv", ("lane1CellLineID", "lane2CellLineID",
                          "lane3CellLineID", "lane4CellLineID")),
    (IpResult, "IP.csv", ("CellLineID",)),
    (IfResult, "IF.csv", ("CellLine1ID", "CellLine2ID")),
)


def _clone_of(row) -> str:
    """The clone a row records, or `""`.

    `NA` is the Access placeholder for "not recorded" — the same shape as `NA`
    in a gene column — so it is an absence and never an identity.
    """
    clone = (row.get("Clone") or "").strip()
    return "" if clone.upper() == "NA" else clone


def _int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


class Command(BaseCommand):
    help = "Restore the KO clones the Access cell-line merge collapsed into one row"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Write. Without it this reports and changes nothing.")
        parser.add_argument("--csv", default=DEFAULT_CSV,
                            help=f"Access CellLines export (default {DEFAULT_CSV})")
        parser.add_argument("--gene", action="append", default=[],
                            help="Only these genes. Repeatable.")

    # ── read ────────────────────────────────────────────────────────────────
    def _groups(self, path, genes):
        """`[(name, gene, [access_row])]` — the KO groups holding several clones.

        Grouped on `(CellLine, ProteinsID)`, which is the key
        `restructure_cell_lines` merged on minus the site: the Access export is
        one site's database, so every row in it is McGill's and adding the site
        would narrow nothing while making the group key disagree with the merge
        it is undoing.
        """
        directory = os.path.dirname(os.path.abspath(path))
        proteins = os.path.join(directory, "Proteins.csv")
        gene_of = {}
        if os.path.exists(proteins):
            with open(proteins, newline="", encoding="utf-8-sig") as fh:
                gene_of = {r["ID"]: (r.get("Gene") or "").strip()
                           for r in csv.DictReader(fh)}

        with open(path, newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))

        buckets = defaultdict(list)
        for r in rows:
            if (r.get("WTorKO") or "").strip().upper() != "KO":
                continue
            buckets[((r.get("CellLine") or "").strip(),
                     (r.get("ProteinsID") or "").strip())].append(r)

        wanted = {g.strip().upper() for g in genes}
        out = []
        for (name, pid), group in sorted(buckets.items()):
            clones = {_clone_of(r) for r in group if _clone_of(r)}
            # **One clone is not a split.** A group whose rows record one clone
            # or none is what the vial table already says it is: repeated
            # freeze-downs of one line. Only a group that recorded *different*
            # clones lost anything in the merge.
            if len(group) < 2 or len(clones) < 2:
                continue
            gene = gene_of.get(pid, "")
            if wanted and gene.upper() not in wanted:
                continue
            out.append((name, gene, group))
        return out

    def _result_map(self, directory, keep_ids):
        """`{(model, live_result_access_id): {access cell line id}}`, narrowed to
        the ids in the split groups.

        Read once for the whole run rather than per group: three files, and the
        alternative is opening each of them 26 times.
        """
        out = {}
        for model, filename, columns in _RESULT_SOURCES:
            path = os.path.join(directory, filename)
            if not os.path.exists(path):
                continue
            with open(path, newline="", encoding="utf-8-sig") as fh:
                for row in csv.DictReader(fh):
                    rid = _int(row.get("ID"))
                    if rid is None:
                        continue
                    used = {_int(row.get(c)) for c in columns}
                    used = {u for u in used if u in keep_ids}
                    if used:
                        out[(model, rid)] = used
        return out

    # ── write ───────────────────────────────────────────────────────────────
    def handle(self, *args, **options):
        path = options["csv"]
        if not os.path.exists(path):
            raise CommandError(
                f"No Access cell-line export at {path}. It is the file the "
                f"original import read — pass --csv to point at your copy.")
        apply_changes = options["apply"]
        groups = self._groups(path, options["gene"])
        if not groups:
            self.stdout.write("No split knockout groups found. Nothing to do.")
            return

        every_id = {_int(r.get("ID")) for _n, _g, grp in groups for r in grp}
        every_id.discard(None)
        results = self._result_map(os.path.dirname(os.path.abspath(path)), every_id)

        if not apply_changes:
            self.stdout.write(self.style.WARNING(
                "DRY RUN — nothing is written. Re-run with --apply.\n"))

        stats = {"groups": 0, "clones_restored": 0, "vials_moved": 0,
                 "sessions_moved": 0, "sessions_ambiguous": 0, "skipped": 0}
        notes = []
        with transaction.atomic(using=DB):
            for name, gene, group in groups:
                self._restore_group(name, gene, group, results, stats, notes,
                                    apply_changes)
            if not apply_changes:
                transaction.set_rollback(True, using=DB)

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"  groups split      {stats['groups']}\n"
            f"  clones restored   {stats['clones_restored']}\n"
            f"  vials moved       {stats['vials_moved']}\n"
            f"  sessions repointed{stats['sessions_moved']:>4}\n"
            f"  sessions left     {stats['sessions_ambiguous']} "
            f"(readings name more than one clone)\n"
            f"  rows skipped      {stats['skipped']} (already restored, or no "
            f"live row to split)"))
        # **A count with no list under it invents the noun.** Every number above
        # that is not a plain success has its rows named here.
        if notes:
            self.stdout.write("")
            for line in notes:
                self.stdout.write(f"  {line}")
        if not apply_changes:
            self.stdout.write(self.style.WARNING(
                "\nDRY RUN — nothing was written."))

    def _restore_group(self, name, gene, group, results, stats, notes, apply_changes):
        access_ids = [i for i in (_int(r.get("ID")) for r in group) if i is not None]
        vials = {v.access_id: v for v in
                 CellLineVial.objects.using(DB).filter(access_id__in=access_ids)}
        line_ids = {v.cell_line_id for v in vials.values()}
        if len(line_ids) != 1:
            # Either nothing survived to split, or the group is already split.
            # Both are "leave it alone", and both are named rather than counted
            # silently — a group this cannot place is exactly the case somebody
            # needs to look at by hand.
            stats["skipped"] += len(group)
            notes.append(
                f"{name} {gene}: skipped — its {len(access_ids)} Access rows sit "
                f"on {len(line_ids)} live cell lines, not 1")
            return
        survivor = (CellLine.objects.using(DB)
                    .select_related("site", "target").filter(pk=line_ids.pop()).first())
        if survivor is None:
            stats["skipped"] += len(group)
            return

        stats["groups"] += 1
        self.stdout.write(
            f"\n{name} {gene} — survivor pk={survivor.pk} "
            f"clone {survivor.clone or '(none)'}")

        # The survivor's own Access row keeps the survivor. Identified by
        # `CellLine.access_id` where there is one and by its clone otherwise,
        # because a row whose clone matches is the row this line already is.
        keep = survivor.access_id
        if keep is None:
            keep = next((_int(r.get("ID")) for r in group
                         if _clone_of(r) and _clone_of(r) == (survivor.clone or "").strip()),
                        None)

        made = {}          # access id -> the CellLine that row belongs to
        # Clone -> the row that already holds it, so a clone naming an existing
        # one is recognised as another freeze-down of it rather than a second
        # line. Seeded with the survivor's own.
        by_clone = {}
        if (survivor.clone or "").strip():
            by_clone[survivor.clone.strip().lower()] = survivor
        for row in sorted(group, key=lambda r: _int(r.get("ID")) or 0):
            rid = _int(row.get("ID"))
            clone = _clone_of(row)
            if rid is None or rid == keep:
                if rid == keep and clone and not (survivor.clone or "").strip():
                    # The merge left the survivor's own clone blank on 12 of the
                    # 26 groups. Filling it is the same recovery as creating the
                    # siblings, and leaving it would draw the row as the one
                    # clone with no identity among its own.
                    survivor.clone = clone
                    if apply_changes:
                        survivor.save(using=DB, update_fields=["clone"])
                    self.stdout.write(f"    survivor gains clone {clone}")
                continue
            if not clone:
                stats["skipped"] += 1
                continue
            if CellLine.objects.using(DB).filter(access_id=rid).exists():
                stats["skipped"] += 1
                continue

            # **A clone this group already holds is that clone frozen again, not
            # a second line.** HeLa NR4A2 is five Access rows and three clones:
            # D15 at C-605 and C-636, E6 at C-606 and C-637, M20 at C-638. A
            # C-number is a freeze-down batch *of a clone*, so the second row is
            # a batch — and giving it a `CellLine` of its own would put two rows
            # under one clone with its vials and its readings split between
            # them, both drawn as complete. That is the shape `identity.py`
            # refuses by name on the dialog, and the shape this whole command
            # exists to undo; recreating it here would be the merge's mistake
            # made backwards.
            twin = by_clone.get(clone.lower())
            if twin is not None:
                vial = vials.get(rid)
                if vial is not None:
                    if apply_changes:
                        vial.cell_line_id = twin.pk
                        vial.save(using=DB, update_fields=["cell_line"])
                    stats["vials_moved"] += 1
                    self.stdout.write(
                        f"    clone {clone} frozen again, vial C-{vial.c_number} "
                        f"kept as a batch of it")
                # Named for the sessions pass: a reading against this Access row
                # is a reading against that clone.
                made[rid] = twin
                continue

            line = self._new_line(survivor, row, clone, rid)
            if apply_changes:
                line.save(using=DB)
            made[rid] = line
            by_clone[clone.lower()] = line
            stats["clones_restored"] += 1

            vial = vials.get(rid)
            moved = ""
            if vial is not None:
                if apply_changes:
                    vial.cell_line_id = line.pk
                    vial.save(using=DB, update_fields=["cell_line"])
                stats["vials_moved"] += 1
                moved = f", vial C-{vial.c_number}"
            self.stdout.write(f"    + clone {clone}{moved}")

        self._move_sessions(survivor, made, results, stats, notes, apply_changes)

    def _new_line(self, survivor, row, clone, access_id) -> CellLine:
        """A restored clone: its own identity, the survivor's structure.

        The structure is copied because the merge never damaged it — the name,
        the gene, the genotype, the bench and the parental are the key it merged
        *on*, so every row in the group agreed about them. What is taken from
        the Access row is what the merge threw away: the clone, its own
        freeze-down number, the notes written about it and its own KO verdict.
        """
        verdict = (row.get("KOInfo") or "").strip()
        line = CellLine(
            name=survivor.name,
            target_id=survivor.target_id,
            genotype=CellLine.Genotype.KNOCKOUT,
            site_id=survivor.site_id,
            parent_line_id=survivor.parent_line_id,
            parental_line_name=survivor.parental_line_name,
            company_id=survivor.company_id,
            species=survivor.species,
            growth_properties=survivor.growth_properties,
            medium=survivor.medium,
            clone=clone,
            access_id=access_id,
            catalogue_number=(row.get("CatNumber") or "").strip(),
            lot_number=(row.get("Lot") or "").strip(),
            origin=(row.get("Origin") or "").strip(),
            origin_comments=(row.get("OriginComments") or "").strip(),
            ko_validation_notes=verdict,
            # **The tick is derived from the lab's own verdict words, never set
            # because a note exists.** `ko_validation` is the one reader, and a
            # note it does not recognise leaves the box unticked — which is the
            # row being drawn as a question rather than as a confirmation
            # nobody made.
            ko_validated=ko_validation.note_verdict(verdict) == ko_validation.CONFIRMED,
            # **Logistics come from the row and the survivor, never from an
            # assertion here.** This said `received=True`, which put six restored
            # ACSL5 clones on screen reading *received / not thawed* beside their
            # own sibling reading *not received / thawed* — seven tubes from one
            # freeze-down run disagreeing in one column, which is the shape of
            # every "two readers, one fact" defect in this codebase. `Thawed` is
            # a real column in the export and is read; `received` has no column
            # (only `ReceivedDate`) so it is copied from the survivor, which is
            # whatever the original import decided for this group.
            thawed=(row.get("Thawed") or "").strip() in ("1", "-1", "True", "true"),
            received=survivor.received,
        )
        # **`ReceivedDate` is deliberately not read.** The export writes it
        # `06/01/23`, which is two different days and the cell does not say
        # which — the ambiguity `services/received.py` refuses rather than
        # guesses. A wrong arrival date is worse than none: it would print as
        # fact on a screen with nothing to contradict it.
        # The number written on this clone's own tubes. Set explicitly so
        # `lab_numbers` does not issue a fresh one on `pre_save` — a historical
        # row must come back under the number it has always had, not the next
        # one free at that bench.
        line.c_number = _int(row.get("LabLabel"))
        return line

    def _move_sessions(self, survivor, made, results, stats, notes, apply_changes):
        """Repoint a session onto the clone its own readings name.

        Only where the readings agree. The historical import grouped sessions by
        (procedure, target) rather than by day, so one session genuinely can hold
        readings against two clones — and there is no honest single answer for
        it. Those are counted **and named**, because a session pointing at the
        wrong clone is the defect this whole command exists to undo and
        replacing it with a quieter version of itself would be worse than
        leaving it visible.
        """
        if not made:
            return
        by_access = {survivor.access_id: survivor, **made}
        # `made` can map an Access row onto the survivor itself, when that row
        # was the survivor's clone frozen a second time. Harmless here — the
        # move is skipped below when the chosen row is the one it is already on.
        sessions = (ExperimentSession.objects.using(DB)
                    .filter(cell_line_ko_id=survivor.pk)
                    .prefetch_related("wb_results", "ip_results", "if_results"))
        for session in sessions:
            named = set()
            for model, relation in ((WbResult, "wb_results"),
                                    (IpResult, "ip_results"),
                                    (IfResult, "if_results")):
                for result in getattr(session, relation).all():
                    if result.access_id is None:
                        continue
                    named |= results.get((model, result.access_id), set())
            # **Counted by `access_id`, never by primary key.** A dry run does
            # not save the restored rows, so every one of them has `pk = None` —
            # and a session whose readings name *two* clones then collapsed to
            # the single value `{None}`, which read as agreement. The preview
            # reported two clean repoints where the write would have found an
            # ambiguity and left the session alone, and it printed
            # `→ cell line None` as the destination. A preview is only true if
            # it is compared against the write, so the identity used here has to
            # be one that exists in both modes: the Access id does, from the
            # moment the group is read.
            targets = {a for a in named if a in by_access}
            if len(targets) != 1:
                if targets:
                    stats["sessions_ambiguous"] += 1
                    notes.append(
                        f"session #{session.pk} ({survivor.name} "
                        f"{getattr(survivor.target, 'gene_name', '')}) left as it "
                        f"is — its readings name {len(targets)} of the clones: "
                        + ", ".join(sorted(by_access[a].clone or "(none)"
                                           for a in targets)))
                continue
            chosen = by_access[targets.pop()]
            if chosen.pk is not None and chosen.pk == survivor.pk:
                continue
            if apply_changes:
                ExperimentSession.objects.using(DB).filter(pk=session.pk).update(
                    cell_line_ko_id=chosen.pk)
            stats["sessions_moved"] += 1
            # The clone, not the primary key: a row id is not something anybody
            # can check against a freezer, and in a dry run there is not one yet.
            self.stdout.write(f"    session #{session.pk} → clone {chosen.clone}")
