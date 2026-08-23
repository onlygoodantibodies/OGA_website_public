"""
Gap-fill target metadata from UniProt (theoretical mass, aliases, alt name).

Every pipeline Target stores its exact UniProt accession, but many rows are
missing fields that UniProt gives authoritatively:
  * ``theoretical_mass_kda`` — the predicted MW (UniProt sequence molWeight/1000),
    i.e. the expected Western-blot band size;
  * ``aliases``              — alternative gene names/symbols (UniProt gene
    synonyms), e.g. ACE → "ACE1, CD143, DCP1";
  * ``alternative_name``     — the alternative PROTEIN name (UniProt alternative
    full name), e.g. ACSL5 → "LACS 5".

Fetches each target's entry BY ACCESSION (no gene-name search ambiguity) via
``uniprot.lookup_accession`` and fills only the fields that are currently blank.

This is a GAP-FILL ONLY, LOSSLESS command:
  * only blank fields are written — a populated value is NEVER overwritten;
  * a target with no accession, or one UniProt can't return, is skipped and
    reported (network failures degrade gracefully, never raise);
  * ``protein_type`` is deliberately NOT auto-filled — it is a curated 4-way
    classification (Intracellular / Secreted / ...), not a 1:1 UniProt field.

SAFETY MODEL (identical to standardize_rrid_format)
  * Defaults to --dry-run: prints every change it would make, writes nothing.
  * --apply writes, inside a single transaction (all-or-nothing).
  * Guarded to pipeline_db only.
  * Fully reversible with a pg_dump backup taken before --apply.

NOTE: needs outbound HTTPS to rest.uniprot.org — run it in the Render shell
(dev / Codespaces network is usually blocked). Be polite: --sleep throttles calls.

USAGE (run in the Render shell)
  python manage.py enrich_targets_from_uniprot                       # dry-run, all targets
  python manage.py enrich_targets_from_uniprot --scope published     # dry-run, published only
  python manage.py enrich_targets_from_uniprot --apply
  python manage.py enrich_targets_from_uniprot --apply --fields mass
"""

import time

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from pipeline.models import Target
from pipeline.services import uniprot

# field key -> (model attr, human label)
_FIELDS = {
    "mass": ("theoretical_mass_kda", "theoretical_mass_kda"),
    "aliases": ("aliases", "aliases"),
    "alt_name": ("alternative_name", "alternative_name"),
}


class Command(BaseCommand):
    help = ("Gap-fill Target theoretical_mass_kda / aliases / alternative_name "
            "from UniProt by accession (dry-run by default).")

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Write changes. Without this it is a read-only dry-run.")
        parser.add_argument("--scope", choices=["all", "published"], default="all",
                            help="Which targets to consider (default: all).")
        parser.add_argument("--fields", default="mass,aliases,alt_name",
                            help="Comma list of fields to fill: mass,aliases,alt_name "
                                 "(default: all three).")
        parser.add_argument("--limit", type=int, default=0,
                            help="Only process the first N candidate targets (0 = no limit).")
        parser.add_argument("--sleep", type=float, default=0.2,
                            help="Seconds to pause between UniProt calls (default 0.2).")
        parser.add_argument("--batch", type=int, default=25,
                            help="Commit every N filled targets (default 25). A "
                                 "dropped shell then loses at most one batch, and "
                                 "re-running resumes where it stopped. Use 0 for "
                                 "the old all-or-nothing single transaction.")
        parser.add_argument("--show", type=int, default=25,
                            help="How many example fills to print (default 25).")

    def handle(self, *args, **opts):
        apply = opts["apply"]
        scope = opts["scope"]
        show = opts["show"]
        sleep = opts["sleep"]
        limit = opts["limit"]

        want = [f.strip() for f in opts["fields"].split(",") if f.strip()]
        bad = [f for f in want if f not in _FIELDS]
        if bad:
            raise CommandError(f"Unknown --fields {bad}; choose from {list(_FIELDS)}.")

        if Target.objects.db != "pipeline_db":
            raise CommandError(
                f"Target routes to '{Target.objects.db}', expected 'pipeline_db'. "
                f"Aborting to avoid touching the wrong database."
            )

        banner = "APPLY (writing changes)" if apply else "DRY-RUN (no changes)"
        self.stdout.write("=" * 70)
        self.stdout.write(f"  enrich_targets_from_uniprot — {banner}")
        self.stdout.write(f"  scope={scope}  fields={want}")
        self.stdout.write("=" * 70)

        qs = Target.objects.all().order_by("gene_name")
        if scope == "published":
            qs = qs.filter(status="published")

        stats = dict(scanned=0, considered=0, looked_up=0, targets_filled=0,
                     no_accession=0, unreachable=0, no_new_data=0)
        field_counts = {k: 0 for k in want}
        to_save = []          # (target, update_fields)
        examples = []         # (gene, [(label, value), ...])

        batch = opts.get("batch", 25)
        stats["committed"] = 0

        def flush():
            """Write what's accumulated. Called per batch so a dropped shell
            costs one batch of lookups, not the whole run — the next run skips
            everything already filled, because it only ever considers blanks."""
            if not (apply and to_save):
                return
            with transaction.atomic(using="pipeline_db"):
                for target, fields in to_save:
                    target.save(using="pipeline_db",
                                update_fields=fields + ["updated_at"])
            stats["committed"] += len(to_save)
            to_save.clear()
            self.stdout.write(
                f"    … saved {stats['committed']} target(s); "
                f"{stats['looked_up']} lookups done", ending="\n")
            self.stdout.flush()

        for t in qs:
            stats["scanned"] += 1
            accession = (t.uniprot_id or "").strip()

            # Which requested fields are blank on this target?
            blank_here = [k for k in want if _is_blank(getattr(t, _FIELDS[k][0]))]
            if not blank_here:
                continue
            stats["considered"] += 1
            if limit and stats["considered"] > limit:
                stats["considered"] -= 1
                break

            if not accession:
                stats["no_accession"] += 1
                continue

            up = _safe_lookup(accession)
            stats["looked_up"] += 1
            if sleep:
                time.sleep(sleep)
            if not up.get("found"):
                stats["unreachable"] += 1
                continue

            fields, filled_pairs = [], []
            for k in blank_here:
                attr, label = _FIELDS[k]
                val = _value_for(k, up)
                if val in (None, ""):
                    continue
                setattr(t, attr, val)
                fields.append(attr)
                field_counts[k] += 1
                filled_pairs.append((label, val))

            if fields:
                stats["targets_filled"] += 1
                to_save.append((t, fields))
                if len(examples) < show:
                    examples.append((t.gene_name, filled_pairs))
                if batch and len(to_save) >= batch:
                    flush()
            else:
                stats["no_new_data"] += 1

        if examples:
            self.stdout.write("\n  Example fills (gene: field -> value):")
            for gene, pairs in examples:
                self.stdout.write(f"    {gene}")
                for label, val in pairs:
                    self.stdout.write(f"        {label} -> {val!r}")

        flush()

        self.stdout.write("\n" + "=" * 70)
        self.stdout.write("  SUMMARY")
        self.stdout.write(f"    targets scanned              : {stats['scanned']}")
        self.stdout.write(f"    with a blank requested field : {stats['considered']}")
        self.stdout.write(f"    UniProt lookups performed    : {stats['looked_up']}")
        self.stdout.write(f"    {'targets filled' if apply else 'targets to fill'}"
                          f"{'':<14}: {stats['targets_filled']}")
        for k in want:
            self.stdout.write(f"      {_FIELDS[k][1]:<26}: {field_counts[k]}")
        self.stdout.write(f"    skipped — no accession       : {stats['no_accession']}")
        self.stdout.write(f"    skipped — UniProt unreachable: {stats['unreachable']}")
        self.stdout.write(f"    looked up, no new data       : {stats['no_new_data']}")
        if not apply:
            self.stdout.write("\n  This was a DRY-RUN. No data changed. "
                              "Back up, then rerun with --apply.")
        self.stdout.write("=" * 70)


def _is_blank(v):
    return v is None or (isinstance(v, str) and v.strip() == "")


def _safe_lookup(accession):
    try:
        return uniprot.lookup_accession(accession)
    except Exception as e:  # network / parsing — never fatal
        return {"found": False, "error": str(e)}


def _value_for(key, up):
    if key == "mass":
        return up.get("mass_kda")
    if key == "aliases":
        return ", ".join(up.get("gene_synonyms") or [])
    if key == "alt_name":
        # First real alternative protein name; never write a "-" placeholder.
        alts = [a for a in (up.get("alternative_names") or []) if a and a.strip() != "-"]
        return alts[0] if alts else ""
    return ""
