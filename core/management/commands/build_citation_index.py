"""Turn the OGA x CiteAb join database into the artefact the extension ships.

    python manage.py build_citation_index /path/to/oga_citeab.sqlite

The join database is built elsewhere (``citeab_join/build_join_db.py``), is ~30 MB,
and is **not** in this repo. What is in this repo is the small derived file this
command writes — ``core/data/citeab_papers_<vintage>.json``, a few hundred KB — and
that split is the whole design:

* the 30 MB source never reaches a web service, so the supplier-level analyses that
  live in it cannot leak by accident. They are not excluded by policy; they are
  absent by construction.
* the artefact is a plain file the extension index folds in, so there is no second
  service, no runtime database, and nothing new that can be down.

WHAT IS KEPT, AND WHAT IS DROPPED
---------------------------------
* **Preferred matches only** (``citeab_match.is_preferred``). The join is on
  catalogue number because CiteAb holds no RRID, so one of ours can hit several of
  theirs; the source resolves most of those by comparing the gene and marks which
  hit it chose. An unresolved one is *not guessed at here* — it is dropped and
  counted.
* **RRID required.** The extension index is keyed on RRID, so a reagent without one
  has nothing to attach to. Counted, not silently skipped.
* **Withdrawn attributions excluded.** CiteAb keeps a table of antibody-paper links
  it has taken back. On the 2026-05 snapshot every one of the 3,214 is already
  absent from the live citation table, so this filter removes nothing today — it is
  here because a later snapshot may overlap, and finding that out by shipping a
  claim CiteAb has retracted is the wrong way to find it out.
* **YCharOS's own reports are KEPT**, deliberately. They are the clearest case of a
  paper using a failing antibody on purpose, so an analysis that counts harm has to
  drop them — but this artefact does not count anything, it reports. Keeping them
  also gives the team the easiest possible check that the extension works: open one
  of your own papers and see whether it lights up correctly.

The command prints what it wrote AND what it left out, with counts, because a
number with no list under it invents the noun.
"""

from __future__ import annotations

import json
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core.citations import (APPLICATION_BITS, AMBIGUOUS, DATA_DIR, SCHEMA,
                            UNTESTED_APP, normalise_title, title_key)

# One row per antibody x paper x application token, restricted to the reagents the
# extension can key on. LEFT JOIN on the application table on purpose: a paper that
# cites a reagent with no application recorded is still worth knowing about, and an
# INNER JOIN would silently turn 23% of the links into "this paper does not cite it".
_ROWS = """
SELECT p.citeab_publication_id AS pid,
       p.pubmedid, p.pprid, p.title, p.published_year,
       a.rrid, ca.token, ca.oga_application, ca.confidence
  FROM antibody a
  JOIN citeab_match m ON m.oga_id = a.oga_id AND m.is_preferred = 1
  JOIN citation ct    ON ct.citeabid = m.citeabid
  JOIN publication p  ON p.citeab_publication_id = ct.citeab_publication_id
  LEFT JOIN citation_application ca
         ON ca.citeabid = ct.citeabid
        AND ca.citeab_publication_id = ct.citeab_publication_id
 WHERE TRIM(COALESCE(a.rrid, '')) <> ''
   AND NOT EXISTS (SELECT 1 FROM deleted_citation d
                    WHERE d.citeabid = m.citeabid
                      AND d.pubmedid IS NOT NULL
                      AND d.pubmedid = p.pubmedid)
"""


def _title_hash(title):
    return title_key(title) if normalise_title(title) else None


class Command(BaseCommand):
    help = "Build the extension's paper->reagent->application artefact from the CiteAb join database."

    def add_arguments(self, parser):
        parser.add_argument("source", help="Path to oga_citeab.sqlite")
        parser.add_argument("--out", default=None,
                            help="Output path. Default: core/data/citeab_papers_<vintage>.json")
        parser.add_argument("--dry-run", action="store_true",
                            help="Report the counts and write nothing.")

    def handle(self, *args, **options):
        source = Path(options["source"]).expanduser()
        if not source.exists():
            raise CommandError(f"No such file: {source}")

        connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            meta = {k: v for k, v in connection.execute("SELECT key, value FROM run_meta")}
        except sqlite3.DatabaseError as exc:
            raise CommandError(
                f"{source} does not look like an oga_citeab join database "
                f"(no run_meta table): {exc}")

        vintage = (meta.get("citeab_updated_antibody_dataset") or "")[:7].replace("-", "_")
        if not vintage:
            raise CommandError(
                "run_meta has no citeab_updated_antibody_dataset, so the artefact "
                "cannot be dated. A snapshot that cannot say what it is a snapshot "
                "OF is not shippable.")

        out = Path(options["out"]) if options["out"] else DATA_DIR / f"citeab_papers_{vintage}.json"

        per_paper = defaultdict(lambda: defaultdict(lambda: [0, set()]))
        papers_meta = {}
        tokens = Counter()
        for row in connection.execute(_ROWS):
            pid = row["pid"]
            papers_meta[pid] = row
            entry = per_paper[pid][row["rrid"].strip()]
            confidence, application = row["confidence"], row["oga_application"]
            if confidence == "exact" and application in APPLICATION_BITS:
                entry[0] |= APPLICATION_BITS[application]
            elif confidence == "ambiguous":
                entry[0] |= AMBIGUOUS
            elif confidence == "none":
                entry[0] |= UNTESTED_APP
                if row["token"]:
                    entry[1].add(row["token"])
                    tokens[row["token"]] += 1

        rrids = sorted({r for reagents in per_paper.values() for r in reagents})
        rrid_at = {r: i for i, r in enumerate(rrids)}
        token_list = [t for t, _ in tokens.most_common()]
        token_at = {t: i for i, t in enumerate(token_list)}

        packed, years, by_title, by_pmid = [], [], {}, {}
        no_title, no_id, title_clash = 0, 0, 0
        for pid in sorted(per_paper):
            row = papers_meta[pid]
            slot = len(packed)
            fields = []
            for rrid, (flags, untested) in per_paper[pid].items():
                cell = f"{rrid_at[rrid]:x}:{flags:x}"
                if untested:
                    cell += "." + ".".join(f"{token_at[t]:x}" for t in
                                           sorted(untested, key=lambda t: token_at[t]))
                fields.append(cell)
            packed.append(",".join(fields))
            try:
                years.append(int(row["published_year"]))
            except (TypeError, ValueError):
                years.append(0)          # 0 = unknown; the reader skips the check
            digest = _title_hash(row["title"])
            if digest is None:
                no_title += 1
            elif digest in by_title:
                # Two papers whose titles normalise identically. Real (958 of them
                # in the 2026-05 source) and NOT a hash artefact. Neither can be
                # served safely, so the key is REMOVED rather than pointed at one
                # of them: an arbitrary winner is indistinguishable from a correct
                # answer, which is the one thing this file must never produce.
                by_title.pop(digest, None)
                title_clash += 1
            else:
                by_title[digest] = slot
            identifier = (row["pubmedid"] or "").strip() or (row["pprid"] or "").strip()
            if identifier:
                by_pmid[identifier] = slot
            else:
                no_id += 1

        artefact = {
            "schema": SCHEMA,
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": {
                "citeab_data_through": meta.get("citeab_updated_antibody_dataset", "")[:10],
                "join_built_at": meta.get("built_at", ""),
                "join_tool_version": meta.get("tool_version", ""),
                "population": meta.get("population", ""),
                "matcher_sha256": meta.get("matcher_sha256", ""),
            },
            "flags": {"WB": 1, "IP": 2, "IF": 4, "FC": 8,
                      "ambiguous": AMBIGUOUS, "untested_app": UNTESTED_APP},
            "rrids": rrids,
            "tokens": token_list,
            "papers": packed,
            "years": years,
            "by_title": by_title,
            "by_pmid": by_pmid,
            # CiteAb stores no DOI, so this cannot be filled from the source alone.
            # `resolve_citation_dois` fills it from PubMed. Empty is honest; absent
            # would make the reader guess whether the key is unsupported or unfilled.
            "by_doi": {},
            "counts": {
                "papers": len(packed),
                "reagents": len(rrids),
                "pairs": sum(len(v) for v in per_paper.values()),
                "by_title": len(by_title),
                "by_pmid": len(by_pmid),
                "by_doi": 0,
                "papers_without_a_usable_title": no_title,
                "papers_without_pubmed_or_preprint_id": no_id,
                "titles_shared_by_two_papers_so_unusable": title_clash,
            },
        }

        text = json.dumps(artefact, separators=(",", ":"), sort_keys=False)
        counts = artefact["counts"]
        write = self.stdout.write
        write(f"source      {source}")
        write(f"CiteAb data through {artefact['source']['citeab_data_through']}  "
              f"(join built {artefact['source']['join_built_at']})")
        write("")
        write(f"  papers                 {counts['papers']:>7,}")
        write(f"  reagents               {counts['reagents']:>7,}")
        write(f"  (paper, reagent) pairs {counts['pairs']:>7,}")
        write(f"  findable by title      {counts['by_title']:>7,}")
        write(f"  findable by PubMed id  {counts['by_pmid']:>7,}")
        write(f"  findable by DOI        {counts['by_doi']:>7,}  "
              f"(run resolve_citation_dois to fill this)")
        write("")
        write("  left out:")
        write(f"    {counts['papers_without_a_usable_title']:>5,} papers whose title normalises to nothing")
        write(f"    {counts['titles_shared_by_two_papers_so_unusable']:>5,} titles shared by two papers, so unusable as a key")
        write(f"    {counts['papers_without_pubmed_or_preprint_id']:>5,} papers with no PubMed or preprint id")
        write("")
        write(f"  {len(text)/1024:,.0f} KB uncompressed")

        if options["dry_run"]:
            write(self.style.WARNING("\nDry run: nothing written."))
            return
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        write(self.style.SUCCESS(f"\nWrote {out}"))
