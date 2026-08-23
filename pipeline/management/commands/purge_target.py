"""Remove a target and everything recorded against it — for clearing test data.

There is no delete UI for targets, deliberately: deleting a gene throws away
every antibody, session and result recorded against it, and nothing in the app
should make that a click. This exists so a *test* target can be cleared without
hand-written SQL in a production shell, where a mistyped WHERE clause is
unrecoverable.

Dry-run unless --apply. It names what it would remove in terms of antibodies,
cell lines and sessions rather than row counts, so the person approving it can
see what they are approving.

Two things it will not do, both of which a plain ``Target.delete()`` gets wrong:

  * **WT cell lines are never touched.** A WT line is shared across every gene
    and is recorded with no gene of its own, so it is not "part of" any target.
    Only KO lines carrying this gene go.
  * **KO lines are deleted explicitly, before the target.** ``CellLine.target``
    is SET_NULL, so cascading would leave them behind with no gene — which is
    exactly what a WT parental line looks like. Two KO lines would quietly become
    two fake parental lines nobody could tell from the real thing.

Everything else really does cascade: antibodies (with their results, images and
inventory), sessions (with their results and attachments), nominations, reports,
classifications, assignments and reagent requests.

**The gap that leaving WT lines alone opens, and what to do about it.** "A WT line
is shared and was already there" is true of the real ones and false of a test run
that created its own parent. Run 4 added ``SH-SY5Y WT [COWORK RUN4]``, its KO was
purged with the gene, and the parent stayed — an orphan with the run's tag in its
name, which a purge *by gene* can never reach because a WT has no gene.

``--orphan-wt`` removes those, and it **requires ``--check-tag``**. That is not
ceremony: relations alone cannot tell the two apart. Purge every gene a real HAP1
parents and it is left parentless too, with nothing pointing at it — identical, by
relation, to a wild type a test run invented. The only thing that distinguishes
them is that the run *tagged what it made*, so the tag is the evidence and nothing
untagged is ever a candidate. The relation checks are the second gate: no remaining
knockouts, no sessions, no vials, no pairing.

**A tag makes the genes optional.** By the time anyone goes looking for a leftover,
the purge has usually already happened — so "none of those genes are in the
pipeline" used to abort the run and take the tag sweep and the orphan pass with it,
at exactly the moment both are wanted. With ``--check-tag`` the command carries on
and works off the tag; without one it still refuses, because then there really is
nothing to do. Naming **no genes at all** is the way to say "clear only what this
tag can reach, and touch nothing that is on those genes now".

**And a tag that names a different run from the genes is refused.** The tag says
*which run*; the purge goes by *gene*; every run reuses the same genes. So the two
can disagree, and the dry run reads as though it is doing what you asked. The run-4
cleanup was attempted after run 5 had been done on STMN2 and ELP3: it listed ten of
run 5's antibodies and five of its sessions as "would be removed" under a command
whose tag said RUN4. The trigger is narrow — a gene carrying a *different tag of the
same family*, not merely a gene with no tag, because a run may tag only some of what
it touches and sweeping for a tag while purging a gene is the original point of
``--check-tag``.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import CharField, Q, TextField

from pipeline.models import (Antibody, CellLine, ExperimentSession, Report,
                             Target, TargetNomination)

DB = "pipeline_db"


class Command(BaseCommand):
    help = ("Remove a target and everything recorded against it. "
            "Dry-run unless --apply.")

    def add_arguments(self, parser):
        parser.add_argument(
            "genes", nargs="*",
            help="Gene symbols to purge, e.g. STMN2 ELP3 (case-insensitive). "
                 "May be omitted entirely when --check-tag is given, which is "
                 "how you clear a leftover a purge by gene cannot reach without "
                 "touching whatever is on those genes now.")
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually delete. Without this, nothing is written.")
        parser.add_argument(
            "--check-tag", default="",
            help='Afterwards, report any record anywhere still carrying this '
                 'text — e.g. --check-tag "[COWORK RUN2]". Read-only. Use it to '
                 'confirm a clean slate before repeating a test run.')
        parser.add_argument(
            "--orphan-wt", action="store_true",
            help="Also remove a WT parental line this purge has left with no "
                 "knockouts, no sessions and no vials — a wild type the test run "
                 "created for itself. REQUIRES --check-tag: the tag is the only "
                 "thing that tells such a line from the lab's own, so an untagged "
                 "line is never a candidate. Named in the dry run first.")

    def handle(self, *args, **opts):
        genes = [g.strip() for g in opts["genes"] if g.strip()]
        apply_ = opts["apply"]
        tag = (opts["check_tag"] or "").strip()
        if opts["orphan_wt"] and not tag:
            raise CommandError(
                "--orphan-wt needs --check-tag \"<the run's tag>\".\n"
                "Purging every gene a real HAP1 parents leaves it parentless too, "
                "with nothing pointing at it — by relation alone it is identical "
                "to a wild type a test run invented. The tag is the only evidence "
                "of which is which, so nothing untagged is ever removed.")

        targets = []
        for gene in genes:
            t = Target.objects.using(DB).filter(gene_name__iexact=gene).first()
            if t is None:
                self.stdout.write(self.style.WARNING(
                    f"No target called '{gene}' — nothing to remove."))
                continue
            targets.append(t)

        if not targets and not tag:
            raise CommandError(
                "Nothing to do. Name at least one gene that is in the pipeline, "
                'or give --check-tag "<a run\'s tag>".'
                if genes else
                'Name a gene to purge, or give --check-tag "<a run\'s tag>" to '
                'clear what a purge by gene cannot reach.')

        # ── the guard that matters most ──────────────────────────────────────
        #
        # A tag says *which run* you mean. The purge goes by gene, and a gene is
        # reused by every run — so naming last run's tag alongside this run's genes
        # deletes the wrong run's work, and the dry run reads as though it is doing
        # what you asked.
        #
        # This is not hypothetical. The run-4 cleanup was attempted after run 5 had
        # already been done on STMN2 and ELP3: the dry run listed ten of run 5's
        # antibodies and five of its sessions under a command whose tag said RUN4,
        # and --apply would have destroyed the records run 5's reports were about
        # to be triaged against.
        # The trigger is deliberately narrow: a gene whose records carry a
        # *different tag of the same family*. "These records carry no tag" is no
        # evidence at all — a run may tag only some of what it touches, and the
        # read-only sweep is the whole reason --check-tag exists. "These records
        # carry [COWORK RUN5] and you said [COWORK RUN4]" is unambiguous.
        conflicts = {}
        if tag and targets:
            for t in targets:
                if self._target_carries(t, tag):
                    continue
                others = self._other_tags(t, tag)
                if others:
                    conflicts[t.gene_name or f"target {t.pk}"] = sorted(others)
        if conflicts:
            lines = "\n".join(f"  {gene} carries {', '.join(others)}"
                              for gene, others in sorted(conflicts.items()))
            raise CommandError(
                f"Refusing: you named '{tag}', but these genes hold another run's "
                f"records.\n{lines}\n"
                "The purge goes by gene and every run reuses the same genes, so "
                "this would delete that run's work while the output read as though "
                "it were clearing yours.\n"
                f'  · to clear only what "{tag}" can reach, drop the gene names: '
                f'purge_target --orphan-wt --check-tag "{tag}"\n'
                "  · to purge these genes deliberately, pass the tag their own "
                "records carry, or omit --check-tag.")
        if not targets:
            # No genes to purge, which is the normal case for the two jobs a tag
            # unlocks: confirming a clean slate, and clearing what a purge by gene
            # could not reach. Bailing out here made both unreachable at exactly
            # the moment they are wanted — the second half of the run-4 cleanup hit
            # this and could not get at its own leftover wild type.
            self.stdout.write(
                (f"No genes named. Looking only for what a purge by gene cannot "
                 f"reach, carrying '{tag}'."
                 if not genes else
                 f"None of those genes are in the pipeline. Looking for what a "
                 f"purge by gene cannot reach, carrying '{tag}'."))

        # The WT parents this purge is about to orphan, worked out before anything
        # is deleted so the dry run can name them — and before the per-target
        # reports, so "LEFT ALONE" does not claim a line the next section is about
        # to remove. Two answers to one question is the bug this file keeps having
        # to avoid.
        orphans = self._would_orphan(targets, tag)
        going_too = ({o.pk for o in orphans} if opts["orphan_wt"] else set())

        for t in targets:
            self._report(t, going_too)

        self._report_orphans(orphans, opts["orphan_wt"], tag)

        if not apply_:
            self.stdout.write(self.style.WARNING(
                "\nDRY RUN — nothing was deleted. Re-run with --apply once the "
                "above is what you expect."))
            if opts["check_tag"]:
                self._check_tag(opts["check_tag"])
            return

        with transaction.atomic(using=DB):
            for t in targets:
                gene = t.gene_name
                # Before the target, or SET_NULL leaves these as fake WT lines.
                kos = CellLine.objects.using(DB).filter(target_id=t.pk)
                n_ko = kos.count()
                kos.delete()
                t.delete()
                self.stdout.write(self.style.SUCCESS(
                    f"Removed {gene} and {n_ko} knockout line(s)."))

            if opts["orphan_wt"] and orphans:
                # Re-check every guard now the knockouts are actually gone: the
                # decision was made against the pre-delete state, and this is the
                # only place it can be confirmed against the post-delete one.
                for line in orphans:
                    holds = self._what_still_points_at(line)
                    if holds:
                        self.stdout.write(self.style.WARNING(
                            f"  KEPT {line.name} — {'; '.join(holds)}"))
                        continue
                    name = line.name
                    line.delete()
                    self.stdout.write(self.style.SUCCESS(
                        f"Removed orphaned wild type: {name}"))

        self.stdout.write(self.style.SUCCESS(
            "\nDone. WT parental lines were left alone — they are shared."
            if not opts["orphan_wt"] else
            "\nDone. Any WT line still used by something was left alone."))

        if opts["check_tag"]:
            self._check_tag(opts["check_tag"])

    # ── wild types this purge would leave behind ──────────────────────────
    def _what_still_points_at(self, line, *, ignore_lines=(), ignore_sessions=()):
        """Every reason this cell line must stay. Empty means nothing needs it.

        Checked by relation rather than by name or tag, because "it looks like test
        data" is not a safe basis for deleting a shared parental line. A real WT —
        one HAP1 serving nine genes — fails the first check on every one of them.

        ``ignore_lines``/``ignore_sessions`` are the records *this same purge is
        about to delete*. Counting them is how run 5's own parent survived its own
        cleanup: the knockouts were discounted but the **sessions were not**, and a
        session names the WT it was run against, so the two STMN2 sessions going
        with the gene still read as "2 session(s) used it". The line was dropped
        from the candidate list before the dry run could name it, and ``--apply``
        never considers a line that was not a candidate — so the run-4 leftover
        happened again, silently, under a flag added to prevent exactly that.

        The post-delete re-check passes nothing, deliberately: by then the records
        are really gone, so anything still pointing at the line is a real hold.
        """
        from pipeline.models import CellLineVial
        holds = []
        kids = (CellLine.objects.using(DB)
                .filter(parent_line_id=line.pk)
                .exclude(pk=line.pk).exclude(pk__in=ignore_lines))
        if kids.exists():
            holds.append(f"{kids.count()} knockout line(s) still name it as parent")
        used = (ExperimentSession.objects.using(DB)
                .filter(Q(cell_line_wt_id=line.pk) | Q(cell_line_ko_id=line.pk))
                .exclude(pk__in=ignore_sessions))
        if used.exists():
            holds.append(f"{used.count()} session(s) used it")
        vials = CellLineVial.objects.using(DB).filter(cell_line_id=line.pk)
        if vials.exists():
            holds.append(f"{vials.count()} vial(s) recorded against it")
        if line.arrived_with_ko_id and line.arrived_with_ko_id not in set(ignore_lines):
            holds.append("it is paired with a knockout it arrived alongside")
        return holds

    def _would_orphan(self, targets, tag=""):
        """WT lines nothing needs any more, and that a purge by gene cannot reach.

        With a `tag`, candidates are **tagged WT lines** — not lines derived from
        the targets. That is the difference between working and not: by the time
        you want this, the genes are usually already gone, so there are no targets
        to derive anything from and the run's own parent is already parentless. The
        tag is what identifies it either way, and it is the only thing that can:
        purge every gene a real HAP1 parents and it is left parentless too.

        Without a tag this reports what *this* purge is about to orphan, which is
        worth knowing on its own. `--orphan-wt` cannot be reached without a tag, so
        nothing untagged is ever deleted.
        """
        target_pks = [t.pk for t in targets]
        going = set(CellLine.objects.using(DB)
                    .filter(target_id__in=target_pks)
                    .values_list("pk", flat=True))
        # The sessions going with those genes. A session records which WT it was
        # run against, so without this the run's own parent is held by the very
        # sessions about to be deleted alongside it.
        going_sessions = set(ExperimentSession.objects.using(DB)
                             .filter(target_id__in=target_pks)
                             .values_list("pk", flat=True))
        if tag:
            candidates = self._tagged(
                CellLine.objects.using(DB).filter(genotype="WT"), tag
            ).exclude(pk__in=going)
        elif going:
            candidates = (CellLine.objects.using(DB)
                          .filter(ko_derivatives__pk__in=going, genotype="WT")
                          .exclude(pk__in=going).distinct())
        else:
            return []

        out = []
        for line in candidates:
            # Everything except what this purge is about to remove. A vial, or a
            # session belonging to some *other* gene, is real use and keeps the
            # line. The exclusions are passed in rather than filtered out of the
            # result afterwards: matching on the wording of a message is how the
            # session hold went unnoticed, since only the parent one was stripped.
            holds = self._what_still_points_at(
                line, ignore_lines=going, ignore_sessions=going_sessions)
            if holds:
                continue
            out.append(line)
        return out

    def _target_carries(self, t, tag) -> bool:
        """Does anything recorded against this gene mention `tag`?

        The target row itself, its antibodies, its cell lines, its sessions. If
        none of them do, the tag on the command line is describing a different run
        from the one whose records are actually on this gene.
        """
        if self._tagged(Target.objects.using(DB).filter(pk=t.pk), tag).exists():
            return True
        for model, field in ((Antibody, "target_id"), (CellLine, "target_id"),
                             (ExperimentSession, "target_id")):
            qs = model.objects.using(DB).filter(**{field: t.pk})
            if self._tagged(qs, tag).exists():
                return True
        return False

    def _other_tags(self, t, tag):
        """Other tags *of the same family* on this gene's records. Read-only.

        Same family means sharing the tag's first word: given `[COWORK RUN4]`, a
        record carrying `[COWORK RUN5]` is a conflict and one carrying
        `[see fig 2]` is a scientist writing prose. Narrow on purpose — this
        function's output refuses a command, so a false positive blocks legitimate
        work, and ordinary comments are full of square brackets.

        Returns an empty set for a tag with no bracketed word to key on, which
        means the guard simply does not apply to a free-text tag.
        """
        import re
        family = re.findall(r"[A-Za-z0-9]+", tag)
        if not family:
            return set()
        head = family[0].lower()
        pattern = re.compile(r"\[[^\]\[]{1,40}\]")
        seen = set()
        for model, field in ((Antibody, "target_id"), (CellLine, "target_id"),
                             (ExperimentSession, "target_id")):
            for row in model.objects.using(DB).filter(**{field: t.pk})[:200]:
                for f in row._meta.fields:
                    if not isinstance(f, (CharField, TextField)) or f.choices:
                        continue
                    for hit in pattern.findall(getattr(row, f.name, "") or ""):
                        if head in hit.lower() and hit.lower() != tag.lower():
                            seen.add(hit)
        return seen

    def _tagged(self, qs, tag):
        """`qs` narrowed to rows mentioning `tag` in any of their text fields."""
        q = Q()
        for f in qs.model._meta.fields:
            if isinstance(f, (CharField, TextField)) and not f.choices:
                q |= Q(**{f"{f.name}__icontains": tag})
        return qs.filter(q) if q else qs.none()

    def _report_orphans(self, orphans, will_remove, tag=""):
        if not orphans:
            return
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\nWT parental lines carrying '{tag}' with nothing pointing at them:"
            if tag else
            "\nWT parental lines this purge would leave with nothing pointing at "
            "them:"))
        for line in orphans:
            site = f" [{line.site.name}]" if line.site_id else ""
            self.stdout.write(f"  - {line.name} (id {line.pk}){site}")
        if will_remove:
            self.stdout.write(self.style.WARNING(
                f"  --orphan-wt is set and each of these carries '{tag}', so they "
                f"will be REMOVED. Read the names first — that is the whole check."))
        else:
            self.stdout.write(
                "  Left alone. A purge by gene cannot reach a WT line — it has no "
                "gene — so if the run created its own parent it stays behind with "
                "the run's tag on it. To remove the tagged ones, add --orphan-wt "
                "alongside --check-tag.")

    # ── is the slate actually clean? ──────────────────────────────────────
    def _check_tag(self, tag):
        """Report anything anywhere still carrying a tag. Read-only.

        A test run leaves two kinds of trace: records hanging off its genes, which
        the purge above removes, and free text typed onto records that were
        already there — a note on somebody else's antibody, a comment on a shared
        cell line. The second kind survives a purge by gene, and it is exactly
        what makes a repeat run confusing: you cannot tell this run's `[COWORK]`
        note from the last one's.

        Every text field on every pipeline model is searched rather than a list of
        the likely ones, because the whole point is finding the place nobody
        thought of.
        """
        from django.apps import apps
        from django.db.models import CharField, Q, TextField

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\nAnything still carrying '{tag}':"))
        found = 0
        for model in apps.get_app_config("pipeline").get_models():
            fields = [f.name for f in model._meta.fields
                      if isinstance(f, (CharField, TextField)) and not f.choices]
            if not fields:
                continue
            q = Q()
            for name in fields:
                q |= Q(**{f"{name}__icontains": tag})
            try:
                rows = list(model.objects.using(DB).filter(q)[:20])
            except Exception:
                # A model on another database, or one with no table yet. Not
                # worth failing a cleanup check over.
                continue
            for row in rows:
                found += 1
                where = ", ".join(
                    name for name in fields
                    if tag.lower() in (getattr(row, name, "") or "").lower())
                self.stdout.write(
                    f"  {model.__name__} #{row.pk} — {where}: {row}")
        if found:
            self.stdout.write(self.style.WARNING(
                f"\n{found} record(s) still mention '{tag}'. Two different things "
                f"look the same here, so check which each one is:"))
            self.stdout.write(
                "  · free text typed onto a record that was already there — clear "
                "the text on the board;\n"
                "  · a record the purge could not reach by gene, which for a WT "
                "parental line means it has no gene. If the run created it, remove "
                "it: re-run with --orphan-wt, or delete it from the cell lines "
                "board.\n"
                "  Either way, do it before repeating the run — otherwise the next "
                "run's notes are indistinguishable from this one's.")
        else:
            self.stdout.write(self.style.SUCCESS(
                f"  Nothing. No record mentions '{tag}' — the slate is clean and "
                f"the run can be repeated from scratch."))

    # ── what would go ────────────────────────────────────────────────────
    def _report(self, t, going_too=frozenset()):
        gene = t.gene_name or f"target {t.pk}"
        abs_ = list(Antibody.objects.using(DB).filter(target_id=t.pk)
                    .select_related("company"))
        kos = list(CellLine.objects.using(DB).filter(target_id=t.pk))
        sess = list(ExperimentSession.objects.using(DB).filter(target_id=t.pk))
        noms = TargetNomination.objects.using(DB).filter(target_id=t.pk).count()
        reports = Report.objects.using(DB).filter(target_id=t.pk).count()

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n{gene} (id {t.pk}) — would be removed, with:"))

        self.stdout.write(f"  {len(abs_)} antibody record(s):")
        for a in abs_:
            supplier = a.company.name if a.company_id else "no supplier"
            lot = f", lot {a.lot_number}" if a.lot_number else ""
            site = f" [{a.site.name}]" if a.site_id else ""
            self.stdout.write(f"    - {a.catalogue_number} ({supplier}{lot}){site}")

        self.stdout.write(f"  {len(kos)} knockout cell line(s):")
        for c in kos:
            self.stdout.write(f"    - {c.name} ({c.genotype})")

        self.stdout.write(f"  {len(sess)} session(s):")
        for s in sess:
            self.stdout.write(
                f"    - #{s.pk} {s.procedure_type} {s.date} "
                f"({s.status}) — results go with it")

        self.stdout.write(f"  {noms} nomination(s), {reports} report(s), "
                          "plus classifications and any reagent requests.")

        # The reassurance that matters: the shared lines survive. Anything
        # --orphan-wt is taking is excluded, or this line would contradict the
        # section below it about the same record.
        shared = sorted({c.name for c in CellLine.objects.using(DB)
                         .filter(ko_derivatives__target_id=t.pk).distinct()
                         if c.pk not in going_too})
        if shared:
            self.stdout.write(self.style.SUCCESS(
                "  LEFT ALONE — parental lines are shared, not this target's: "
                + ", ".join(shared)))
        if reports:
            self.stdout.write(self.style.WARNING(
                "  WARNING: this target has a report (Zenodo/F1000) against it. "
                "That is published work, not test data — check before applying."))
