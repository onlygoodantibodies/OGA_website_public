"""
Recover missing antibody RRIDs from the Antibody Registry by exact catalogue.

For antibodies whose ``rrid`` is blank AND whose ``rrid_link`` carries no
recoverable id (rows whose ``rrid_link`` *does* hold an ``AB_<n>`` are gap-filled
straight from the ``/pipeline/data/`` download → upload round-trip, not here),
look the catalogue number up in the Antibody Registry (SciCrunch) — one exact
query per catalogue, ``q=vendors.catalogNumber:"<cat>"`` — and read the RRID off
the matching record. The registry's catalogue is authoritative, so this is a
lookup, not a guess.

Because an RRID is antibody *identity*, the match is strictly gated, using two
independent signals read off the registry record — the target **gene**
(``antibodies.primary[].targets[].name``) and the **vendor**:
  * HIGH  — the record's catalogue matches ours (exact, or ignoring separators),
            at least one signal (gene or vendor) agrees, neither contradicts, and
            exactly one RRID survives → filled. The gene check is the robust one:
            it catches a catalogue that resolves to a different antibody.
  * REVIEW — catalogue matches but the target gene differs, the vendor/gene can't
            be confirmed, or matches disagree on the RRID → reported, NEVER
            written (a human reconciles);
  * NOMATCH — no registry record with that catalogue.

The RRID itself is read from ``item.identifier`` (bare ``AB_<n>``; ``item.curie``
is ``ab:<n>`` and is NOT used), with ``rrid.curie`` as a fallback.

Only HIGH matches are written, into blank fields only (gap-fill):
  * ``rrid`` <- the registry RRID (canonical AB_<n>); ``rrid_link`` set to match;
  * ``product url`` <- the registry vendor URL, only if present and ours is blank.

VERIFY-FIRST: run a **dry-run with --show-raw in the Render shell first** — it
prints the raw registry JSON + what was parsed for the first N lookups (and stops
after them), so the parse can be confirmed before any --apply. A parse surprise
degrades to REVIEW/NOMATCH, never a wrong write.

SAFETY MODEL
  * Defaults to --dry-run: prints proposed changes + REVIEW cases, writes nothing.
  * --apply writes HIGH matches only, and commits each one as it is found — so a
    long run is RESUMABLE: if the shell drops, re-running continues where it left
    off (a filled row is no longer blank, so it is skipped). Each fill is an
    independent gap-fill, so per-row commits are safe.
  * Guarded to pipeline_db only. Fully reversible with a pg_dump backup.

RUNTIME: ~4-5s per catalogue (SciCrunch latency), so the full ~430 takes ~30 min.
Run it as a **Render One-Off Job** (or `nohup … &`) so it survives the web shell
dropping; `--scope published` is a smaller, faster first pass. Needs
SCICRUNCH_API_KEY + outbound HTTPS to api.scicrunch.io.

USAGE (Render One-Off Job, or the Render shell)
  python manage.py backfill_rrid_from_registry --show-raw 3     # dry-run, stops after 3
  python manage.py backfill_rrid_from_registry --scope published
  python manage.py backfill_rrid_from_registry --apply
"""

import json
import time

from django.core.management.base import BaseCommand, CommandError

from pipeline.models import Antibody
from pipeline.services import scicrunch
from pipeline.rrid_utils import normalize_rrid, registry_url


class Command(BaseCommand):
    help = ("Recover blank antibody RRIDs from the Antibody Registry by exact catalogue "
            "(dry-run by default; writes only high-confidence matches).")

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Write HIGH-confidence matches. Without this it is a dry-run.")
        parser.add_argument("--scope", choices=["all", "published"], default="all",
                            help="Which targets' antibodies to consider (default: all).")
        parser.add_argument("--limit", type=int, default=0,
                            help="Only process the first N blank-RRID antibodies (0 = no limit).")
        parser.add_argument("--sleep", type=float, default=0.3,
                            help="Seconds to pause between registry calls (default 0.3).")
        parser.add_argument("--show", type=int, default=40,
                            help="How many example matches / reviews to print (default 40).")
        parser.add_argument("--show-raw", type=int, default=0,
                            help="Print the raw registry _source for the first N lookups "
                                 "(schema check — use on the first dry-run).")

    def handle(self, *args, **opts):
        apply = opts["apply"]
        scope = opts["scope"]
        show = opts["show"]
        show_raw = opts["show_raw"]
        sleep = opts["sleep"]
        limit = opts["limit"]

        if Antibody.objects.db != "pipeline_db":
            raise CommandError(
                f"Antibody routes to '{Antibody.objects.db}', expected 'pipeline_db'. "
                f"Aborting to avoid touching the wrong database."
            )

        banner = "APPLY (writing HIGH matches)" if apply else "DRY-RUN (no changes)"
        self.stdout.write("=" * 70)
        self.stdout.write(f"  backfill_rrid_from_registry — {banner}")
        self.stdout.write(f"  scope={scope}")
        self.stdout.write("=" * 70)

        qs = Antibody.objects.select_related("target", "company").filter(rrid="")
        if scope == "published":
            qs = qs.filter(target__status="published")
        # Only rows with a catalogue and no recoverable link (link case is handled elsewhere).
        candidates = [ab for ab in qs
                      if (ab.catalogue_number or "").strip()
                      and not normalize_rrid(ab.rrid_link)]

        # A --show-raw probe stops after N lookups unless an explicit --limit is set,
        # so a schema check doesn't grind through every catalogue.
        eff_limit = limit or (show_raw if show_raw else 0)
        self.stdout.write(f"  {len(candidates)} blank-RRID antibodies to check"
                          + (f"; stopping after {eff_limit}" if eff_limit else "") + "\n")

        stats = dict(candidates=0, high=0, review=0, nomatch=0, unreachable=0, urls_filled=0)
        high_ex, review_ex = [], []
        raw_shown = 0

        for ab in candidates:
            if eff_limit and stats["candidates"] >= eff_limit:
                break
            stats["candidates"] += 1
            gene = ab.target.gene_name if ab.target_id else ""
            company = (ab.company.display_name or ab.company.name) if ab.company_id else ""
            reg = _safe_lookup(ab.catalogue_number)
            if sleep:
                time.sleep(sleep)

            if show_raw and raw_shown < show_raw:
                raw_shown += 1
                rec0 = reg["records"][0] if reg.get("records") else None
                src = rec0["source"] if rec0 else {}
                peek = {
                    "item.identifier": (src.get("item") or {}).get("identifier"),
                    "rrid": src.get("rrid"),
                    "vendors": src.get("vendors") or (src.get("item") or {}).get("vendors"),
                    "antibodies": src.get("antibodies"),
                    "PARSED": {k: rec0[k] for k in ("rrid", "vendors", "genes")} if rec0 else None,
                }
                self.stdout.write(f"\n  [raw] our={ab.catalogue_number} [{company}] gene={gene} "
                                  f"error={reg.get('error')!r}")
                self.stdout.write("  " + json.dumps(peek, indent=2, default=str)[:4000])

            if reg.get("error") and not reg.get("records"):
                # Distinguish "no such catalogue" (nomatch) from transport failure.
                if "unavailable" in (reg["error"] or "") or "not set" in (reg["error"] or ""):
                    stats["unreachable"] += 1
                    continue
                stats["nomatch"] += 1
                continue

            if stats["candidates"] % 25 == 0:
                self.stdout.write(f"  ... {stats['candidates']} checked "
                                  f"(high={stats['high']} review={stats['review']} "
                                  f"nomatch={stats['nomatch']})")

            status, rrid, url, note = match_registry(ab.catalogue_number, company, gene, reg["records"])
            if status == "high":
                stats["high"] += 1
                fields = ["rrid", "rrid_link"]
                ab.rrid = rrid
                ab.rrid_link = registry_url(rrid)
                if (url and str(url).lower().startswith(("http://", "https://"))
                        and not (ab.supplier_url or "").strip()):
                    ab.supplier_url = url
                    fields.append("supplier_url")
                    stats["urls_filled"] += 1
                # Commit each match as it is found (not one big transaction at the
                # end): a long --apply run is then RESUMABLE — if it is interrupted,
                # re-running continues, since a filled row is no longer blank.
                if apply:
                    ab.save(using="pipeline_db", update_fields=fields + ["updated_at"])
                    self.stdout.write(f"  [filled] {gene}  {ab.catalogue_number}  [{company}] "
                                      f"-> {rrid}" + ("  (+url)" if "supplier_url" in fields else ""))
                if len(high_ex) < show:
                    high_ex.append((gene, ab.catalogue_number, company, rrid,
                                    "url" if "supplier_url" in fields else "", note))
            elif status == "review":
                stats["review"] += 1
                if len(review_ex) < show:
                    review_ex.append((gene, ab.catalogue_number, company, note))
            else:
                stats["nomatch"] += 1

        if high_ex:
            self.stdout.write("\n  HIGH-confidence matches (gene / catalogue / vendor -> RRID):")
            for gene, cat, comp, rrid, urltag, note in high_ex:
                self.stdout.write(f"    {gene}  {cat}  [{comp}] -> {rrid}"
                                  + (f"  ({note})" if note else "")
                                  + ("  (+product url)" if urltag else ""))
        if review_ex:
            self.stdout.write("\n  REVIEW (catalogue matched but not auto-filled — check by hand):")
            for gene, cat, comp, note in review_ex:
                self.stdout.write(f"    {gene}  {cat}  [{comp}]: {note}")

        self.stdout.write("\n" + "=" * 70)
        self.stdout.write("  SUMMARY")
        self.stdout.write(f"    blank-RRID antibodies checked : {stats['candidates']}")
        self.stdout.write(f"    {'filled' if apply else 'to fill'} (HIGH confidence)"
                          f"{'':<9}: {stats['high']}")
        self.stdout.write(f"      of which product url filled : {stats['urls_filled']}")
        self.stdout.write(f"    REVIEW (reported, untouched)  : {stats['review']}")
        self.stdout.write(f"    no registry catalogue match   : {stats['nomatch']}")
        self.stdout.write(f"    skipped — registry unreachable: {stats['unreachable']}")
        if not apply:
            self.stdout.write("\n  This was a DRY-RUN. No data changed. "
                              "Back up, then rerun with --apply.")
        self.stdout.write("=" * 70)


# ---------------------------------------------------------------------------
# Matching lives in the service now (shared with antibody entry). Re-exported
# here so this module's public name — and its tests — keep working.
# ---------------------------------------------------------------------------

from pipeline.services.scicrunch import match_registry  # noqa: E402,F401


def _safe_lookup(catalogue):
    try:
        return scicrunch.lookup_by_catalogue(catalogue)
    except Exception as e:
        return {"found": False, "records": [], "error": f"SciCrunch API unavailable: {e}"}
