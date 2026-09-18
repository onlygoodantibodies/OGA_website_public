"""
Django management command: backfill_cell_line_parents

Links a knockout to the wild type it was made from, from the reference the row
already carries.

    python manage.py backfill_cell_line_parents            # dry run
    python manage.py backfill_cell_line_parents --apply
    python manage.py backfill_cell_line_parents --gene ARF6 --apply

### The gap

**A knockout only means something alongside the wild type it came from**, and
which WT is not guessable from the name: `HeLa` covers 17 separate parental
stocks in the Access export, from five sources (abcam ×12, ATCC, Synthego, two
academic partners). Reading the WT off the background name would pick an
arbitrary one.

The lab records it, and records it well: `CellLines.ParentalLine` holds a
**C-number**, which names a specific freeze-down batch. That column came across
at import into `CellLine.parental_line_name` — **193 knockouts carry one** — but
the `parent_line` ForeignKey was set on only **20**. So the fact was on file, in
the column the board draws, and nothing pointed at the row it names. The same
dark-field shape as `clone`, `InventoryLocation` and `received_date`: recorded,
resolvable, and joined to nothing.

Live, 4 Sep 2026: 157 of the 193 are a single C-number and **all 157 resolve to
exactly one line**, 156 of them a wild type — of which **122 are a wild type of
the knockout's own background**, which is the only kind this writes. Nothing is
guessed and nothing near-enough is accepted.

### What it will not link, and why

Four shapes are refused rather than resolved, each named in the output:

  * **A wild type of a different background.** The load-bearing one, and the one
    the first version of this got wrong: it checked the genotype and not the
    name, which would have written 14 links including `HeLa FUS KO → HAP1`,
    `U2OSn UBQLN2 KO → HCT116` and `HCT116 CSNK2A1 KO → HEK293T`. A mismatched
    control is read as the matched one by every session planned afterwards and
    every report generated from them, with nothing on any screen dissenting.
    The match is **exact**: `HEK293` and `HEK293T` are different cell lines, so
    no prefix rule can separate them from `U2OSn` and `U2OSn clone FM109`, which
    are the same one. Both go to a person, and so do the four
    `HEK293 → Jump In T-REx HEK 293`, which are arguable rather than wrong.
  * **A reference that lands on something that is not a wild type.** One does:
    the SK-N-AS GCG knockout records `C-744`, which resolves to a HAP1 knockout.
    A knockout cannot be its own parental and SK-N-AS cannot come from HAP1, so
    the number is wrong somewhere — and writing it would put a false control on
    a row that every future session reads.
  * **Several references that disagree.** `C-591/C-262` names a KO and a WT.
    Most multi-reference cells (`C-590/C-262`, `C-16/C-613` — 30 rows) resolve
    to *one* live line, because the parentals were themselves merged by name,
    and those link.
  * **A reference naming nothing**, or naming more than one line.
  * **A row that already has a parent.** Never overwritten: 20 rows carry a link
    somebody or something already made, and this is a backfill.

`parental_line_name` is left exactly as written in every case. It is the record
of what the bench wrote; the FK is derived from it and can be re-derived, which
is what makes this safe to run again after any later change to the parentals.

### What this does not fix

The parentals were merged by `(name, site)` too, so the 17 HeLa stocks are two
live rows carrying 20 vials, and the origin, catalogue and lot of the merged-away
ones went the way the clones did. This links a knockout to the right *line*; it
cannot link it to the right *stock*, because the stocks are no longer separate
rows. Recovering those is a separate decision — see DECISIONS.md — and it needs
a curation call this command must not make: two lots of one catalogue number are
almost certainly one line received twice, and splitting on lot would invent 16
HeLa lines where the bench has far fewer.

Live data — read `.claude/skills/production-data`, take the backup, dry run
first. Every write is one FK on one row, so the way back is to null it.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from pipeline.models import CellLine
from pipeline.services import cell_lines as cell_line_svc
from pipeline.services import targets as target_svc
from pipeline.services.bulk_cell_lines import _parent_refs

DB = "pipeline_db"


class Command(BaseCommand):
    help = "Link knockouts to their parental wild type from the recorded C-number"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Write. Without it this reports and changes nothing.")
        parser.add_argument("--gene", action="append", default=[],
                            help="Only these genes. Repeatable.")

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        if not apply_changes:
            self.stdout.write(self.style.WARNING(
                "DRY RUN — nothing is written. Re-run with --apply.\n"))

        rows = (CellLine.objects.using(DB)
                .select_related("target", "site")
                .filter(genotype="KO", parent_line__isnull=True)
                .exclude(parental_line_name="")
                .order_by("target__gene_name", "name"))
        genes = {g.strip().upper() for g in options["gene"]}
        if genes:
            rows = [cl for cl in rows
                    if (getattr(cl.target, "gene_name", "") or "").upper() in genes]

        stats = {"linked": 0, "not_a_wild_type": 0, "different_background": 0,
                 "disagreed": 0, "unresolved": 0}
        refusals = []
        with transaction.atomic(using=DB):
            for line in rows:
                parent, why = self._parent_for(line)
                if parent is None:
                    stats[why[0]] += 1
                    refusals.append(why[1])
                    continue
                if apply_changes:
                    CellLine.objects.using(DB).filter(pk=line.pk).update(
                        parent_line_id=parent.pk)
                stats["linked"] += 1
                self.stdout.write(
                    f"  {cell_line_svc.label(line)}  →  {cell_line_svc.label(parent)}"
                    f"  (recorded as {line.parental_line_name})")
            if not apply_changes:
                transaction.set_rollback(True, using=DB)

        self.stdout.write("")
        # One writer for the labels and their width, so a new counter cannot
        # quietly break the column the reader scans down.
        summary = (("linked", "linked"),
                   ("not_a_wild_type", "left — not a wild type"),
                   ("different_background", "left — other background"),
                   ("disagreed", "left — references disagree"),
                   ("unresolved", "left — nothing on file"))
        width = max(len(label) for _key, label in summary)
        self.stdout.write(self.style.SUCCESS("\n".join(
            f"  {label:<{width}}  {stats[key]}" for key, label in summary)))
        # **A count with no list under it invents the noun.** Everything left
        # alone is named, because each one is a row somebody has to look at.
        if refusals:
            self.stdout.write("")
            for text in refusals:
                self.stdout.write(f"  {text}")
        if not apply_changes:
            self.stdout.write(self.style.WARNING(
                "\nDRY RUN — nothing was written."))

    # ------------------------------------------------------------------
    def _looks_wrong(self, line) -> str:
        """`Looks wrong. … may be C-43.` — the sentence a person can act on.

        The hint is `cell_lines.parental_hint`, the one reader for "what should
        this row have named instead", shared with the paste door and the
        identity dialog since the nineteenth field test found the check living
        here and nowhere else.
        """
        return (f"Looks wrong. "
                f"{cell_line_svc.parental_hint(line.name, site_id=line.site_id, db=DB)}")

    def _parent_for(self, line):
        """`(parent, None)` or `(None, (stat_key, sentence))`.

        Every reference in the cell has to agree, and the thing they agree on
        has to be a wild type. `_parent_refs` is `bulk_cell_lines`' own splitter
        — the shape `C-153/C-420/C-421` is nineteen rows on file and it already
        knows how to read it — and `by_c_number` is the one reader for a number,
        which looks in the line table *and* the vial table because 102 of the
        145 numbered parents are a vial's.
        """
        raw = (line.parental_line_name or "").strip()
        here = cell_line_svc.label(line)
        found = set()
        for ref in _parent_refs(raw):
            hits = cell_line_svc.by_c_number(ref, db=DB)
            if not hits:
                # Not a number, or a number nothing answers to. A bare name is
                # legitimate — three rows write `HAP1` and `HEK293T` — so it is
                # resolved the way any typed name is, against wild types only.
                hit, _err = cell_line_svc.resolve_wt(ref, site_id=line.site_id, db=DB)
                hits = [hit] if hit is not None else []
            found |= {h.pk for h in hits if h is not None and h.pk != line.pk}

        if not found:
            # **A gap, said as a gap.** Nothing resolves, so as far as the link
            # goes this row has no parental — the text is a note somebody wrote,
            # not a reference. Saying "unresolved" invites somebody to go hunting
            # for the row it names; saying the field is empty is what is true.
            return None, ("unresolved",
                          f"{here}: parental recorded as '{raw}', which names no "
                          f"line — an empty field as far as the link goes. "
                          f"{cell_line_svc.parental_hint(line.name, site_id=line.site_id, db=DB)}")
        if len(found) > 1:
            names = ", ".join(sorted(
                cell_line_svc.label(cl) for cl in
                CellLine.objects.using(DB).select_related("site", "target")
                .filter(pk__in=found)))
            return None, ("disagreed",
                          f"{here}: '{raw}' names {len(found)} different lines "
                          f"({names}) — left as it is")

        parent = (CellLine.objects.using(DB).select_related("site", "target")
                  .filter(pk=found.pop()).first())
        # **A wild type is not enough; it must be a wild type of that
        # background** — `cell_lines.wrong_background` is the one reader, so
        # the paste door and the identity dialog refuse the same links.
        wrong = cell_line_svc.wrong_background(
            line.name, parent, gene=target_svc.gene_of(line.target),
            site_id=line.site_id, db=DB)
        if wrong:
            return None, ("different_background",
                          f"{here}: '{raw}' is {cell_line_svc.label(parent)}. {wrong}")
        if parent is None or parent.genotype != CellLine.Genotype.WILD_TYPE:
            # The one live case is SK-N-AS recording `C-744`, which lands on a
            # HAP1 knockout. A knockout is not a parental and SK-N-AS does not
            # come from HAP1, so the reference is wrong somewhere — and a false
            # control written here is read by every session that follows.
            what = cell_line_svc.label(parent) if parent else raw
            return None, ("not_a_wild_type",
                          f"{here}: '{raw}' is {what}, which is a knockout. "
                          f"{self._looks_wrong(line)}")
        return parent, None
