"""Merge two Target rows that are the same gene.

RAB32 is on file twice. One row is the gene (``RAB32``, Q13637, 4 antibodies);
the other was created because a paste put the **protein** name — *Ras-related
protein Rab-32* — in the gene column, and ``resolve_or_create_target`` matches on
``gene_name``, so it made a second record. Eight more antibodies and a second
HAP1 knockout went onto it. The same thing happened to PTK2B.

Renaming does not fix it. ``Target.gene_name`` is **UNIQUE**, so typing the right
gene onto the second row collides with the first — which is the whole point:
these are two records of one gene, and turning two into one is a merge. That is
also why ``fix_gene_case`` refuses a collision by name rather than doing it.

    python manage.py merge_targets --from id:1203 --into RAB32
    python manage.py merge_targets --from id:1203 --into RAB32 --apply

**A target with no gene name can only be addressed by id**, which is why
``id:<pk>`` is a way in. The RAB32 duplicate has a NULL gene name — somebody
emptied the visibly-wrong value, which took the record's only handle with it —
so ``--from id:1203`` is the only way to name it.

Dry-run by default, as every writing command here is. Nothing moves without
``--apply``, and then it is one transaction: it all lands or none of it does.

Four things it deliberately will not do.

**It finds the foreign keys by introspection**, not from a list, so a model added
later cannot be left behind pointing at a target that no longer exists. Eight
today, and a hand-written list would be wrong within a month.

**It never merges records.** Three constraints include the target
(``unique_antibody_per_site_lot``, ``unique_assignment_per_site_task``,
``unique_target_class_per_source``), so a row whose twin already sits on the
survivor is reported and left where it is. Deciding two rows are one antibody is
``merge_duplicate_antibodies``'s job, with its own dry run and its own evidence.

**It refuses when either side has a published figure.** A ``PublicationImage``
is what makes a figure public, and moving one to a different target changes what
a gene page says about a commercial product without anybody reviewing it.
``--allow-published-figures`` exists for the case where that is genuinely
intended, and it names every gene whose public page would change first.

**It never deletes a target that anything still points at.** ``CellLine.target``
is ``SET_NULL`` while everything else cascades, so a line left behind would not
be deleted — it would be silently blanked, and a cell line with no gene is what a
wild type is. That turns a knockout into a fake parental that then collides by
name with the real knockout when the gene is put back. Held-back rows therefore
keep the loser alive, and the command says so.

**Nothing is copied onto the survivor.** If the loser carries a UniProt id or a
protein name the survivor lacks, that is a curation question for a person — and
``uniprot_id`` is UNIQUE too. The survivor's identity is the one that survives.
"""
from __future__ import annotations

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from pipeline.models import PublicationImage, Target

DB = "pipeline_db"

# The three constraints that include the target. Moving a row onto a target that
# already holds its twin would violate these, so those rows are held back. The
# tuple is the rest of each constraint, with target_id removed.
GUARDED = {
    "Antibody": ("catalogue_number", "company_id", "lot_number", "site_id"),
    "TargetAssignment": ("site_id", "task_type"),
    "TargetClassification": ("label", "source"),
}


def target_fks():
    """Every (model, field name) pointing at Target, found rather than listed."""
    out = []
    for model in apps.get_app_config("pipeline").get_models():
        for field in model._meta.get_fields():
            if getattr(field, "many_to_one", False) and field.related_model is Target:
                out.append((model, field.name))
    return sorted(out, key=lambda mf: mf[0].__name__)


def collisions(model, field, loser, winner):
    """Rows on the loser whose twin already sits on the winner.

    Only for the guarded models; everything else can move freely.
    """
    keys = GUARDED.get(model.__name__)
    if not keys:
        return set()
    winner_keys = set(
        model.objects.using(DB).filter(**{field: winner}).values_list(*keys))
    if not winner_keys:
        return set()
    clash = set()
    for row in model.objects.using(DB).filter(**{field: loser}).values_list("pk", *keys):
        if tuple(row[1:]) in winner_keys:
            clash.add(row[0])
    return clash


def published_figures(target):
    """Published figures hanging off a target's antibodies, newest signal first."""
    return PublicationImage.objects.using(DB).filter(antibody__target=target)


class Command(BaseCommand):
    help = "Move every record from one Target onto another, then delete the empty one."

    def add_arguments(self, parser):
        parser.add_argument("--from", dest="loser", required=True,
                            help="the target that goes away — a gene name or id:<pk>")
        parser.add_argument("--into", dest="winner", required=True,
                            help="the target that survives — a gene name or id:<pk>")
        parser.add_argument("--apply", action="store_true",
                            help="actually move the records (default is a dry run)")
        parser.add_argument("--keep-empty-target", action="store_true",
                            help="move the records but leave the empty Target row")
        parser.add_argument("--allow-published-figures", action="store_true",
                            help="permit a merge that moves figures already on the "
                                 "public site (refused by default)")
        parser.add_argument("--discard-duplicate-labels", action="store_true",
                            help="drop held-back TargetClassification rows whose "
                                 "label the survivor already carries. Only ever "
                                 "classifications, never antibodies.")

    def handle(self, *args, **opts):
        loser = self._target(opts["loser"])
        winner = self._target(opts["winner"])
        if loser.pk == winner.pk:
            raise CommandError("--from and --into are the same target")
        if not (winner.gene_name or "").strip():
            raise CommandError(
                f"the surviving target (id {winner.pk}) has no gene name. "
                "Merge into the row that carries the gene, not away from it — "
                "a target with no gene name is unreachable from every gene page, "
                "filter and search box.")

        apply = opts["apply"]
        self.stdout.write(
            f"\n{'MERGING' if apply else 'DRY RUN — nothing will change'}: "
            f"{self._label(loser)} → {self._label(winner)}\n")

        self._check_published(loser, winner, opts["allow_published_figures"])

        plan, held, total = [], [], 0
        for model, field in target_fks():
            on_loser = model.objects.using(DB).filter(**{field: loser})
            count = on_loser.count()
            if not count:
                continue
            clash = collisions(model, field, loser, winner)
            movable = count - len(clash)
            total += movable
            plan.append((model, field, movable, clash))
            label = model._meta.verbose_name_plural.title()
            if movable:
                self.stdout.write(f"  {movable:>6}  {label}")
            if clash:
                held.append((model, clash))
                self.stdout.write(self.style.WARNING(
                    f"  {len(clash):>6}  {label} held back — "
                    f"{self._label(winner)} already has the same record"))

        if not total and not held:
            self.stdout.write(self.style.SUCCESS(
                f"\nNothing is recorded against {self._label(loser)}. "
                f"{'Deleting it.' if apply else 'It could be deleted.'}"))

        drop_labels = opts["discard_duplicate_labels"]
        for model, clash in held:
            is_label = model.__name__ == "TargetClassification"
            if is_label and drop_labels:
                self.stdout.write(self.style.WARNING(
                    f"\n{len(clash)} duplicate classification(s) will be discarded "
                    f"— the survivor already carries the label:"))
                for row in model.objects.using(DB).filter(pk__in=clash):
                    self.stdout.write(
                        f"    {row.label} ({row.source}) — evidence "
                        f"{row.evidence or '(blank)'!s}")
                continue
            self.stdout.write(self.style.WARNING(
                f"\n{model.__name__} rows staying on {self._label(loser)} "
                f"(ids {sorted(clash)[:20]}{'…' if len(clash) > 20 else ''})"))
            if is_label:
                self.stdout.write(
                    "  The survivor already carries these labels. They are derived "
                    "tags rather than records — `backfill_protein_classes` rebuilds\n"
                    "  them — so --discard-duplicate-labels drops them and lets the "
                    "empty target go. Check the evidence line first if it matters.")
            else:
                self.stdout.write(
                    "  These already exist on the surviving target. Deciding that two "
                    "rows are one record is a merge, not a move — run\n"
                    "  merge_duplicate_antibodies (dry run first), then run this again.")

        if not apply:
            self.stdout.write(self.style.NOTICE(
                f"\nDry run. {total} record(s) would move. "
                f"Re-run with --apply to do it."))
            return

        with transaction.atomic(using=DB):
            for model, field, _movable, clash in plan:
                if clash and drop_labels and model.__name__ == "TargetClassification":
                    model.objects.using(DB).filter(pk__in=clash).delete()
                    clash = set()
                qs = model.objects.using(DB).filter(**{field: loser})
                if clash:
                    qs = qs.exclude(pk__in=clash)
                qs.update(**{f"{field}_id": winner.pk})
            still_there = any(
                model.objects.using(DB).filter(**{field: loser}).exists()
                for model, field in target_fks())
            if still_there:
                self.stdout.write(self.style.WARNING(
                    f"\nMoved {total}. {self._label(loser)} still holds the "
                    f"held-back records, so it has been left in place — deleting "
                    f"it would blank the gene on any cell line still pointing at "
                    f"it rather than remove the row."))
            elif opts["keep_empty_target"]:
                self.stdout.write(self.style.SUCCESS(
                    f"\nMoved {total}. {self._label(loser)} is empty and was kept."))
            else:
                loser.delete(using=DB)
                self.stdout.write(self.style.SUCCESS(
                    f"\nMoved {total}. {self._label(loser)} is empty and has been "
                    f"deleted."))

        self.stdout.write(
            f"\nWorth checking now: /pipeline/target/{winner.pk}/ should list "
            f"everything that was split, and the gene should appear once on the "
            f"target board.")

    # -- refusals ----------------------------------------------------------
    def _check_published(self, loser, winner, allowed):
        """A published figure is on the public site, so moving one is a change to
        what a gene page says about somebody's product."""
        moving = published_figures(loser)
        count = moving.count()
        if not count:
            return
        genes = ", ".join(sorted(
            {f"{t or '(no gene name)'}"
             for t in moving.values_list("antibody__target__gene_name", flat=True)}))
        if not allowed:
            raise CommandError(
                f"{count} published figure(s) hang off {self._label(loser)} "
                f"({genes}). Merging would move them onto "
                f"{self._label(winner)}, changing what a public gene page says "
                f"about a commercial product with nobody reviewing it.\n"
                f"  If that is what you mean, re-run with --allow-published-figures.\n"
                f"  If it is not, discard or re-release the figures on "
                f"/pipeline/review/ first.")
        self.stdout.write(self.style.WARNING(
            f"  {count:>6}  published figure(s) will move onto "
            f"{self._label(winner)} — the public page for {genes} changes."))

    # -- resolving ---------------------------------------------------------
    def _target(self, token):
        """A gene name, or ``id:<pk>`` for a target with no gene name."""
        token = (token or "").strip()
        if token.lower().startswith("id:"):
            raw = token[3:].strip()
            if not raw.isdigit():
                raise CommandError(f"'{token}' is not an id — write it as id:1203")
            found = Target.objects.using(DB).filter(pk=int(raw)).first()
            if found is None:
                raise CommandError(f"no target with id {raw}")
            return found
        # Exact first, and deliberately so: half of what this command is for is
        # a pair that differs *only* by case (PTK2B / PTK2b, the mouse spelling
        # fix_gene_case cannot correct because correcting it is this merge). A
        # case-insensitive lookup resolves both sides of that pair to one row and
        # the command refuses itself as "the same target".
        found = Target.objects.using(DB).filter(gene_name=token).first()
        if found is not None:
            return found
        matches = list(Target.objects.using(DB)
                       .filter(gene_name__iexact=token).order_by("pk")[:6])
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            spelled = ", ".join(f"'{m.gene_name}' (id:{m.pk})" for m in matches)
            raise CommandError(
                f"'{token}' matches {len(matches)} targets differing only by "
                f"case: {spelled}. Say which by its exact spelling, or by id.")
        nameless = Target.objects.using(DB).filter(gene_name__isnull=True).count() \
            + Target.objects.using(DB).filter(gene_name="").count()
        extra = (f" {nameless} target(s) have no gene name and can only be "
                 f"named as id:<pk>." if nameless else "")
        raise CommandError(f"no target with the gene name '{token}'.{extra}")

    def _label(self, target):
        gene = (target.gene_name or "").strip()
        return f"'{gene}' (id {target.pk})" if gene else f"the unnamed target id {target.pk}"
