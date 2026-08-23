"""Fill the citation snapshot's ``by_doi`` index from Europe PMC.

Runnable two ways, and the second is the point:

    python manage.py resolve_citation_dois --check --email you@example.org
    python3 core/citations_doi.py         --check --email you@example.org

**The second needs nothing but Python 3.** No virtualenv, no `pip install`, no
Django, no database — this reads one JSON file, calls one public API, and writes
the JSON file back. Requiring the whole application stack to do that would put a
setup afternoon in front of a job that takes three minutes, and the person who
has to run it is a scientist with a laptop.

What it DOES need is a checkout, because the artefact it rewrites lives in the
repository and the result has to be committed.

WHY EUROPE PMC
--------------
CiteAb's schema has no DOI. It stores a PubMed id (26,201 of our papers) or a
Europe PMC preprint id (3,006) and nothing else, while a publisher's article page
declares a DOI and very often nothing else machine readable. Something has to
join those two.

Europe PMC indexes BOTH: a PubMed record under ``SRC:MED`` by its PMID, a preprint
under ``SRC:PPR`` by its ``PPR`` id. NCBI eutils would resolve the first group and
leave the second needing a second, differently-shaped integration.

RUN ``--check`` FIRST
---------------------
It resolves ONE id and prints Europe PMC's raw answer. This was written without
being able to reach the API — outbound access to ebi.ac.uk was blocked from the
environment it was built in — so the response shape it parses comes from Europe
PMC's documented schema and has not been seen in the wild. One request tells you
whether the parsing is right before you spend ~600 of them.

If the shape has moved, ``dois_from`` is the only function that needs changing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# Importable as `core.citations_doi` and runnable as `core/citations_doi.py`,
# where sys.path[0] is `core/` and the package import would fail.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.citations import ARTEFACT, normalise_doi  # noqa: E402

ENDPOINT = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

# 50 ids is ~1 KB of query string, comfortably inside any URL limit, and one page
# of 100 results always holds a batch's worth even when a PubMed id resolves to
# both a MED record and a PMC one.
BATCH = 50
PAGE_SIZE = 100

# Europe PMC publishes no hard rate limit and asks for considerate use. Three
# requests a second puts a full run at roughly three minutes, which is nothing,
# and leaves their service alone.
DELAY_SECONDS = 0.34


def dois_from(payload):
    """``{external id: doi}`` out of one Europe PMC search response.

    Keyed on the id we ASKED with, not on whatever Europe PMC considers primary:
    a MED record carries ``pmid`` and a preprint carries its ``PPR`` id in ``id``,
    and the caller has to be able to match an answer back to its question.

    THIS IS THE FUNCTION TO CHANGE if ``--check`` shows a different shape.
    """
    out = {}
    for row in (payload.get("resultList") or {}).get("result") or []:
        doi = normalise_doi(row.get("doi"))
        if not doi:
            continue
        pmid = (row.get("pmid") or "").strip()
        if pmid:
            out[pmid] = doi
        identifier = (row.get("id") or "").strip()
        if identifier.startswith("PPR"):
            out[identifier] = doi
    return out


def fetch(query, email):
    url = ENDPOINT + "?" + urlencode({"query": query, "format": "json",
                                      "resultType": "lite", "pageSize": PAGE_SIZE})
    agent = "OGA-citation-index/1.0"
    if email:
        agent += f" (mailto:{email})"
    request = Request(url, headers={"User-Agent": agent, "Accept": "application/json"})
    with urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def _batches(items, size):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _say(*args, **kwargs):
    """`print`, flushed.

    Python block-buffers stdout when it is not a terminal, so on a CI runner the
    progress lines sit in the buffer and the step shows NOTHING for its whole
    run. A ten-minute job with no output is indistinguishable from a hung one,
    and the first thing anybody does about a hung job is cancel it -- which here
    throws away every request it had already made.
    """
    kwargs.setdefault("flush", True)
    print(*args, **kwargs)


def check(artefact, email, say=_say):
    """One request, printed raw, so the parsing can be judged before a full run."""
    ids = list(artefact.get("by_pmid") or {})
    probe = next((i for i in ids if i.isdigit()), None) or ids[0]
    source = "MED" if probe.isdigit() else "PPR"
    say(f"Asking Europe PMC for one id ({probe}, SRC:{source})...\n")
    payload = fetch(f"EXT_ID:{probe} AND SRC:{source}", email)
    say(json.dumps(payload, indent=2)[:4000])
    parsed = dois_from(payload)
    say("\n\nParsed out of that: " + json.dumps(parsed))
    if parsed.get(probe):
        say("\nThe shape is what this expects. Safe to run for real.")
        return True
    say(f"\nNo DOI came back keyed on {probe}. Compare the JSON above against "
        f"dois_from() in core/citations_doi.py — that is the only function that "
        f"needs to change, and nothing has been written.")
    return False


def resolve(artefact, email, cache_path, limit=0, delay=DELAY_SECONDS, say=_say):
    """Ask Europe PMC about everything not already cached. Writes only the cache."""
    ids = list(artefact.get("by_pmid") or {})
    pmids = [i for i in ids if i.isdigit()]
    pprids = [i for i in ids if i.startswith("PPR")]

    resolved = {}
    if cache_path.exists():
        resolved = json.loads(cache_path.read_text(encoding="utf-8"))
        say(f"Resuming: {len(resolved):,} already resolved in {cache_path.name}")

    outstanding = [i for i in ids if i not in resolved]
    if limit:
        outstanding = outstanding[:limit]
    say(f"{len(ids):,} ids in the snapshot ({len(pmids):,} PubMed, "
        f"{len(pprids):,} preprint); {len(outstanding):,} still to ask about.\n")

    failures = 0
    for source, group in (("MED", [i for i in outstanding if i.isdigit()]),
                          ("PPR", [i for i in outstanding if i.startswith("PPR")])):
        total, done = (len(group) + BATCH - 1) // BATCH, 0
        for batch in _batches(group, BATCH):
            done += 1
            query = "(" + " OR ".join(f"EXT_ID:{i}" for i in batch) + f") AND SRC:{source}"
            try:
                found = dois_from(fetch(query, email))
            except (HTTPError, URLError, ValueError) as exc:
                # One bad batch is 50 ids, not the run. Nothing is cached for them,
                # so a rerun picks them up.
                failures += 1
                say(f"  batch failed ({exc}); continuing")
                continue
            resolved.update({k: v for k, v in found.items() if k in artefact["by_pmid"]})
            cache_path.write_text(json.dumps(resolved), encoding="utf-8")
            say(f"  {source} batch {done}/{total}: {len(resolved):,} of "
                f"{len(ids):,} resolved")
            time.sleep(delay)
    return resolved, failures


def index_from(artefact, resolved):
    """``{doi: slot}``, dropping any DOI two of our papers both claim.

    Neither can be served safely, so the key goes — the same rule the builder
    applies to a title two papers share, and for the same reason: an arbitrary
    winner is indistinguishable from a correct answer.
    """
    by_doi, clashes = {}, 0
    for identifier, doi in resolved.items():
        slot = artefact["by_pmid"].get(identifier)
        if slot is None:
            continue
        if doi in by_doi and by_doi[doi] != slot:
            by_doi.pop(doi, None)
            clashes += 1
            continue
        by_doi[doi] = slot
    return by_doi, clashes


def write(path, artefact, by_doi):
    """Through a temporary file: this artefact is committed to the repository, and
    a truncated one is worse than an unresolved one."""
    artefact["by_doi"] = by_doi
    artefact.setdefault("counts", {})["by_doi"] = len(by_doi)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(artefact, separators=(",", ":")), encoding="utf-8")
    temporary.replace(path)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Fill the citation snapshot's by_doi index from Europe PMC.")
    parser.add_argument("--artefact", default=None, help=f"Default: {ARTEFACT}")
    parser.add_argument("--email", default=None, help=(
        "A contact address, sent in the User-Agent. Europe PMC asks for one so "
        "they can reach whoever is making the requests; supply it."))
    parser.add_argument("--check", action="store_true",
                        help="Resolve ONE id, print the raw response, and stop.")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--cache", default=None)
    parser.add_argument("--delay", type=float, default=DELAY_SECONDS)
    parser.add_argument("--apply", action="store_true",
                        help="Write by_doi into the artefact. Without it, nothing is written.")
    args = parser.parse_args(argv)

    path = Path(args.artefact) if args.artefact else ARTEFACT
    if not path.exists():
        print(f"No citation snapshot at {path}. Run build_citation_index first.",
              file=sys.stderr)
        return 2
    artefact = json.loads(path.read_text(encoding="utf-8"))

    if not args.email:
        _say("No --email given. Europe PMC asks for a contact address so they "
              "can reach whoever is making the requests; please supply one.\n")

    if args.check:
        try:
            return 0 if check(artefact, args.email) else 1
        except (HTTPError, URLError) as exc:
            print(f"Could not reach Europe PMC: {exc}", file=sys.stderr)
            return 2

    cache_path = Path(args.cache) if args.cache else path.with_suffix(".doi-cache.json")
    resolved, failures = resolve(artefact, args.email, cache_path,
                                 limit=args.limit, delay=args.delay)
    by_doi, clashes = index_from(artefact, resolved)

    _say()
    _say(f"  resolved            {len(resolved):>7,}")
    _say(f"  usable DOI keys     {len(by_doi):>7,}")
    _say(f"  dropped as clashes  {clashes:>7,}  (two papers claiming one DOI)")
    _say(f"  unresolved          {len(artefact.get('by_pmid') or {}) - len(resolved):>7,}"
          f"  (still reachable by PubMed id and by title)")
    if failures:
        print(f"  {failures} batch(es) failed; rerun to retry them")

    if not args.apply:
        _say(f"\nDry run: {path.name} not written. Re-run with --apply.\n"
              f"Answers are cached in {cache_path.name}, so that costs no more requests.")
        return 0
    write(path, artefact, by_doi)
    _say(f"\nWrote {len(by_doi):,} DOI keys into {path}")
    _say("Commit the artefact — the extension serves it from the repository.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
