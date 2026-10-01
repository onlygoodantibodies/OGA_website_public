"""Load the methods read out of the published reports, and backfill the lab
records from them.

    python manage.py load_methods_record                  # dry run: what would change
    python manage.py load_methods_record --gene TGM2      # one gene
    python manage.py load_methods_record --list           # every line, not only counts
    python manage.py load_methods_record --apply          # write it

Dry-run by default, like every command that writes here (production-data
skill): take a Render export of ycharos-pipeline-db first, read the dry run,
then ``--apply``. Safe to run twice — a second run reports everything as
already on file.

What it writes, and what it deliberately does not, is in
`pipeline/services/methods_backfill.py`. In short: a methods record per gene
and application with a published figure (what the *Copy methods* button
prints); blank session conditions and blank result cells filled; the lab
values the extraction found to be entry errors corrected, where the cell
still holds what the extraction saw. Then `methods_sessions`: a session for a
report run no session holds, a split where one session holds two runs, and a
result row for each antibody a run tested that it has no row for — each
created record saying in its comments how it was made.
"""
from __future__ import annotations

from collections import Counter

from django.core.management.base import BaseCommand

from pipeline.services import methods_backfill as B
from pipeline.services import methods_sessions as MS


class Command(BaseCommand):
    help = "Load report methods into the methods records and backfill lab records (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write the changes.")
        parser.add_argument("--gene", help="Only this gene.")
        parser.add_argument("--list", action="store_true",
                            help="Print every change and every item left alone.")
        parser.add_argument("--file", default=str(B.DATA_FILE), help="The data file.")

    def handle(self, *args, **opts):
        data = B.load(opts["file"])
        p = B.plan(data, gene=opts.get("gene"))
        out, show = self.stdout.write, opts["list"]

        rec = Counter(r["status"] for r in p.records)
        out(f"Methods records (one per gene and application with a published figure): "
            f"{rec['new']} new, {rec['changed']} changed, {rec['same']} already on file.")
        genes = sorted({r["target"].gene_name for r in p.records})
        out(f"  {len(genes)} genes: {', '.join(genes[:40])}{' …' if len(genes) > 40 else ''}")
        if show:
            for r in p.records:
                others = f"; {r['others']} other report(s) not used" if r["others"] else ""
                out(f"    {r['status']:8} {r['target'].gene_name} {r['application']} "
                    f"← {r['report_file']} ({len(r['conditions'])} conditions{others})")

        abm = Counter(m["status"] for m in p.antibody_methods)
        basis = Counter(m["basis"] for m in p.antibody_methods)
        out(f"Antibody amounts (the dilution or amount printed for each published "
            f"antibody): {abm['new']} new, {abm['changed']} changed, {abm['same']} already on file.")
        out("  from: " + ", ".join(f"{basis[k]} {k.replace('_', ' ')}" for k in
                                   ("report_named", "report_protocol", "report_general", "lab_record")))
        if show:
            for m in p.antibody_methods:
                if m["status"] != "same":
                    out(f"    {m['status']:8} {m['target'].gene_name} {m['application']} "
                        f"{m['antibody'].catalogue_number}: {m['amount']} ({m['basis']})")

        sessions = {s.pk for s, *_ in p.session_fills} | {s.pk for s, _ in p.session_loading}
        out(f"Session conditions filled where blank: {len(p.session_fills)} values "
            f"on {len(sessions)} sessions"
            + (f", plus protein loading on {len(p.session_loading)}" if p.session_loading else "") + ".")
        if show:
            for s, key, value in p.session_fills:
                out(f"    session #{s.pk} {s.target} {s.procedure_type} {key}: {value[:90]}")

        why = Counter(f[4] for f in p.row_fills)
        out(f"Result cells: {len(p.row_fills)} — " + ", ".join(f"{n} {w}" for w, n in why.items()) + ".")
        for row, col, old, new, reason in p.row_fills:
            if show or reason.startswith("corrected"):
                out(f"    {type(row).__name__} #{row.pk} {row.antibody.catalogue_number} "
                    f"{col}: {old or '(blank)'} → {new}  [{reason}]")

        s = MS.plan(data, gene=opts.get("gene"))
        out(f"Sessions split (one session held two runs): {len(s.splits)}, "
            f"moving {sum(len(x['rows']) for x in s.splits)} result rows.")
        for x in s.splits:
            stays = sorted(MS._acc(r) or 0 for r in x["stays"])
            moves = sorted(MS._acc(r) or 0 for r in x["rows"])
            out(f"    session #{x['session'].pk} {x['target'].gene_name} {x['app']}: keeps "
                f"{len(stays)} rows (Access {stays[0]}–{stays[-1]}); "
                f"{len(moves)} rows (Access {moves[0]}–{moves[-1]}) → new session for "
                f"{x['group'].chosen['report_file']}")
        out(f"Sessions created (a published run no session holds): {len(s.creates)}, "
            f"with {sum(len(c['rows']) for c in s.creates)} result rows.")
        for c in s.creates:
            versions = f", {c['versions']} versions of the report" if c["versions"] > 1 else ""
            out(f"    {c['target'].gene_name} {c['app']} at {c['site']}, dated {c['date']}: "
                f"{len(c['rows'])} antibodies ← {c['group'].chosen['report_file']}{versions}")
        out(f"Result rows added to a session that lacked them: {len(s.row_adds)}.")
        if show:
            for a in s.row_adds:
                where = (f"session #{a['session'].pk}" if a["session"]
                         else f"the session split from #{s.splits[a['split_index']]['session'].pk}")
                out(f"    {where} {a['app']} {a['antibody'].catalogue_number}: "
                    f"{a['value']['report_value']}")
        out(f"And on those sessions: {len(s.session_fills)} blank session conditions "
            f"and {len(s.row_fills)} blank result cells filled.")
        for reason, items in s.left.items():
            p.left.setdefault(reason, []).extend(items)

        if p.left:
            out("Left alone (named, not written):")
            for reason, items in sorted(p.left.items()):
                out(f"  {len(items)} {reason}")
                if show:
                    for item in items:
                        out(f"    {item}")

        if not opts["apply"]:
            out(self.style.WARNING("Dry run — nothing written. Add --apply to write."))
            return
        counts = B.apply(p)
        out(self.style.SUCCESS(
            "Written: " + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in counts.items()) + "."))
        counts = MS.apply(s)
        out(self.style.SUCCESS(
            "Sessions: " + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in counts.items()) + "."))
        # Every public gene page draws the new buttons, and the edge holds a
        # public page for seven days — so the write is not visible until the
        # cache is dropped. Never under a test runner (`edge_cache`).
        from OGA_website import edge_cache
        purged, note = edge_cache.purge_public_pages()
        out((self.style.SUCCESS if purged else self.style.WARNING)(note))
