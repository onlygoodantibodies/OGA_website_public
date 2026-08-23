"""Adding a list of genes as targets: parse → plan → apply, with a deadline.

Bulk Add Targets was the one paste box with **no preview**. You typed a hundred
gene symbols and pressed *Add All to Pipeline*, and the first thing you learned
about a typo was a permanent junk target with a nomination hanging off it. Every
other paste surface in the app shows you what it will do first; this one wrote
straight through.

The reason it had no preview is real, and it shapes this module: a gene the
pipeline has never seen has to be looked up in **UniProt** before anything
useful can be said about it, and that is a network call per gene. A hundred of
them, three at a time, against a 10-second timeout, is worst-case several
minutes — well past any gateway's patience, and the old endpoint did exactly
that *while writing rows*, so a timeout could leave a half-created batch with no
report of what landed.

Three things follow, and they are the whole design:

* **A deadline, not a batch size.** ``plan`` stops looking things up when its
  budget is spent and returns the rest as ``unchecked``. Guessing a safe chunk
  size is guessing how slow UniProt is today; a deadline is true whatever the
  answer, and the caller simply asks again for what is left. Nothing is ever
  silently dropped — an unchecked gene is a *verdict*, and it refuses to create.
* **The commit makes no network call at all.** ``apply`` writes from what the
  preview already resolved, so the slow half and the writing half are different
  requests. A timeout during lookups now costs you a retry rather than a
  half-finished batch, and the "no external call inside an open transaction"
  rule holds by construction rather than by care.
* **Answer from the database first.** A gene already on the list, or recorded as
  another target's synonym, needs no call at all. That is a cache the pipeline
  already has and never used — pasting ``PARK8`` when ``LRRK2`` is on file cost
  a lookup and then deduplicated afterwards. When the UniProt backfill lands,
  ``_from_db`` is the one place that has to learn about it, and every gene it
  answers is a gene the deadline never has to pay for.

A gene UniProt could not confirm is **not created**, and neither is one the
deadline did not reach. That is deliberately unlike
``services/targets.py::resolve_or_create_target``, which falls back to a bare
target when UniProt is unreachable so that a paste of *antibodies* still lands:
there the gene is incidental and the vials are the payload. Here the gene **is**
the payload, so a bare target created from an unconfirmed symbol is exactly the
junk row this preview exists to prevent.
"""
from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from django.db import transaction

from pipeline.models import Site, Target, TargetNomination
from pipeline.services import example_row
from pipeline.services import gene_symbol
from pipeline.services import sites as site_svc
from pipeline.services import targets as target_svc
from pipeline.services import uniprot

logger = logging.getLogger(__name__)

DB = "pipeline_db"

# The most gene symbols one paste may carry. Unchanged from the old endpoint —
# it is a sanity bound on the textarea, not the timeout defence. The deadline is
# the timeout defence.
MAX_GENES = 100

# How long one `plan` call will spend on the network before it stops and reports
# what is left. Chosen to sit inside a default gunicorn worker timeout with room
# for the database work either side; the caller asks again for the remainder, so
# the only cost of it being conservative is another round trip.
LOOKUP_BUDGET_SECONDS = 18.0

# Concurrent UniProt lookups. Kept low deliberately: this is somebody else's free
# API and a bulk paste is the one place we could hammer it.
LOOKUP_WORKERS = 6

# What a row's `status` can be. `unchecked` is not an error — it means the
# deadline ran out before this gene's turn, and asking again is all it needs.
CREATES = "new"
ON_FILE = "on_file"
SYNONYM = "synonym"
NOT_FOUND = "not_found"
UNCHECKED = "unchecked"


# ---------------------------------------------------------------------------
# Why a gene cannot be added — written once, for both doors.
# ---------------------------------------------------------------------------
#
# The rule at the top of this file ("a gene UniProt could not confirm is not
# created") was enforced here and stated on the page, and the *single-gene* Add
# button four inches up the same page ignored it entirely: a search for the
# deliberately fake `ZZZZZZ` drew a verdict card reading "No human protein found
# for gene 'ZZZZZZ'" with a fully live **Add to Pipeline** beside it, and the
# endpoint behind it created whatever was posted. One screen, two doors, and the
# one that would put a typo on the consortium's master list was the one with no
# check — while the panel below it printed the rule in as many words.
#
# So the sentences live here, next to the verdicts they describe, and both doors
# use them: the feasibility page shows one *on the button* before it is pressed
# (`views/feasibility.py::feasibility_lookup` returns it as `add_refusal`), and
# the same endpoint refuses in the same words at the press. Two copies of a
# refusal is how the two doors drifted apart in the first place.

def _refusal(*, unavailable: bool, gene: str = "") -> str:
    named = f"'{(gene or '').strip().upper()}'" if (gene or "").strip() else "this gene"
    if unavailable:
        # **`found=False` is two different answers.** An unreachable API is
        # nobody's fault and transient; saying "check the spelling" about a real
        # gene sends somebody to correct something that was already right.
        return (f"UniProt could not be reached, so {named} cannot be confirmed. "
                f"Nothing is added until it answers — try the search again in a "
                f"moment.")
    return (f"UniProt has no human protein for {named}, so it is not added. "
            f"Check the spelling.")


def why_not_added(lookup_result, gene: str = "") -> str:
    """Why this gene cannot be added, or `""` when UniProt confirmed it.

    Takes a `uniprot.lookup` / `lookup_gene` answer — what the feasibility page
    already has in hand from the search, so the button can be greyed with the
    reason beside it rather than looking ready over a verdict card that says the
    gene does not exist.
    """
    data = lookup_result or {}
    if data.get("found"):
        return ""
    return _refusal(unavailable=bool(data.get("unavailable")), gene=gene)


def row_refusal(row) -> str:
    """The same answer from a `plan` row — what the server has at the press.

    `UNCHECKED` covers both halves of "ask again": a deadline that ran out and an
    API that could not be reached. Neither learned anything about the gene, and
    the advice is the same.
    """
    row = row or {}
    status = row.get("status")
    if status not in (NOT_FOUND, UNCHECKED):
        return ""
    return _refusal(unavailable=status == UNCHECKED, gene=row.get("gene", ""))


def parse(raw: str) -> list:
    """A pasted blob → an ordered, de-duplicated list of upper-case symbols.

    Splits on the separators a person actually produces — commas, semicolons,
    newlines, tabs — and keeps first-seen order so the preview reads in the order
    it was typed.
    """
    out = []
    for token in re.split(r"[,;\n\r\t]+", example_row.drop(raw or "")):
        gene = token.strip().upper()
        # `NA` is a gene column saying "there isn't one" — true of a cell line,
        # meaningless as a target. It reached this box through the one-column
        # sheet and would have been created as a gene.
        if gene and gene not in out and not target_svc.is_not_applicable(gene):
            out.append(gene)
    return out


def _existing_by_name(genes, db=DB) -> dict:
    """`{UPPER_GENE: Target}` for the ones already on the list."""
    if not genes:
        return {}
    rows = Target.objects.using(db).filter(gene_name__in=genes)
    found = {(t.gene_name or "").upper(): t for t in rows}
    missing = [g for g in genes if g not in found]
    # A second pass only for what the indexed `__in` did not answer: gene names
    # are stored upper-case by every write path here, but the Access import was
    # not so consistent.
    for gene in missing:
        t = Target.objects.using(db).filter(gene_name__iexact=gene).first()
        if t:
            found[gene] = t
    return found


def _existing_by_synonym(genes, db=DB) -> dict:
    """`{UPPER_GENE: Target}` for genes recorded under another target's other names.

    A target's synonyms are a cache of exactly the question a lookup would ask,
    and nothing consulted them: pasting `PARK8` fetched UniProt, learned it
    means `LRRK2`, and only then noticed `LRRK2` was already on the list. Same
    answer, one network call cheaper, and the preview can say which name it is
    on file under rather than silently doing nothing.

    It read **`alternative_name` alone**, which is half the data: 448 of the 583
    targets on file carry symbols in `aliases` instead, where
    `enrich_targets_from_uniprot` writes them. `targets.alias_owners` is the one
    reader for both columns now, so this door and the cropper's cannot answer
    differently about the same gene — the failure this repo keeps meeting when
    two surfaces grow their own copy of one question.

    **An ambiguous synonym is left for UniProt to settle.** Thirteen other-names
    on the live data name two targets each (`CALM` is CALM1 *and* PICALM), and
    this returned whichever it walked into first — a silent coin toss over which
    gene a pasted symbol joins. Dropping it here costs one lookup and is safe by
    construction: UniProt answers with the canonical symbol, and `apply`
    re-checks that name under the lock before creating anything.
    """
    if not genes:
        return {}
    wanted = set(genes)
    owners = target_svc.alias_owners(db=db)
    found = {}
    for key in wanted:
        matches = {t.pk: t for t in (owners.get(key) or [])}
        if len(matches) == 1:
            found[key] = next(iter(matches.values()))
    return found


def _consortium_context(genes, targets_by_gene, db=DB):
    """What the rest of the consortium already knows about these genes.

    The same questions `target_board.nomination_check` answers — is anyone
    already doing it, has it been published, can the knockout simply be bought,
    who has run the rest of its family — but asked **once for the batch** rather
    than once per gene.

    That distinction is the whole reason this exists rather than a loop.
    `nomination_check` calls `family_expertise()`, which walks every nomination
    in the database; a hundred pasted genes would have walked it a hundred times.
    Everything here is set-based, so the query count does not grow with the
    number of genes — which is how these pages die, and it dies on the real
    dataset while looking fine on a handful of dev rows.
    """
    from pipeline.models import HorizonKoLine, Report
    from pipeline.services.target_board import (completed_report_q, family_expertise,
                                                gene_family)

    target_ids = [t.pk for t in targets_by_gene.values()]

    sites_by_target = {}
    if target_ids:
        for nom in (TargetNomination.objects.using(db)
                    .filter(target_id__in=target_ids).select_related("site")):
            if nom.site_id:
                sites_by_target.setdefault(nom.target_id, set()).add(nom.site.name)

    published = set()
    if target_ids:
        published = set(Report.objects.using(db)
                        .filter(completed_report_q(), target_id__in=target_ids)
                        .values_list("target_id", flat=True))

    ko_by_gene = {}
    for line in (HorizonKoLine.objects.using(db)
                 .filter(gene_name__in=list(genes))
                 .values("gene_name", "item_number", "product_name", "background")):
        ko_by_gene.setdefault((line["gene_name"] or "").upper(), []).append(line)

    expertise = family_expertise()      # once, not once per gene

    context = {}
    for gene in genes:
        target = targets_by_gene.get(gene)
        sites = sorted(sites_by_target.get(getattr(target, "pk", None), set()))
        family = gene_family(gene)
        best_site = ""
        stats = {}
        if family:
            ranked = sorted(expertise.get(family, {}).items(),
                            key=lambda kv: (-kv[1]["completed"], -kv[1]["total"]))
            if ranked and ranked[0][1]["total"] > 1:
                best_site, stats = ranked[0]
        context[gene] = {
            "pursued_at": sites,
            "published": bool(target is not None and target.pk in published),
            "horizon_ko": ko_by_gene.get(gene, [])[:3],
            "family": family or "",
            "family_site": best_site,
            "family_stats": stats,
        }
    return context


def _context_notes(gene, ctx):
    """The context as sentences, so a preview can print it without re-deriving."""
    notes = []
    if ctx["pursued_at"]:
        notes.append(f"already on the list at {', '.join(ctx['pursued_at'])}")
    if ctx["published"]:
        notes.append("already has a published report")
    if ctx["horizon_ko"]:
        ko = ctx["horizon_ko"][0]
        notes.append(f"an off-the-shelf {ko['background']} KO line is catalogued "
                     f"({ko['item_number']})")
    if ctx["family_site"] and ctx["family_stats"]:
        s = ctx["family_stats"]
        notes.append(f"{ctx['family_site']} has {s['total']} {ctx['family']} targets "
                     f"({s['completed']} published) — they may be the right site")
    return notes


def _row(gene, status, **extra):
    row = {"gene": gene, "status": status, "target_id": None, "gene_name": "",
           "protein_name": "", "uniprot_id": "", "mass_kda": None,
           "synonyms": "", "note": "", "context": []}
    row.update(extra)
    return row


def _from_db(genes, db=DB):
    """`(rows_by_gene, still_to_look_up)` — everything answerable without a call.

    The seam the UniProt backfill plugs into: teach this to consult a local copy
    of the human proteome and the list handed to the network shrinks to the genes
    nobody has ever recorded, which for a real paste is usually none of them.
    """
    by_name = _existing_by_name(genes, db=db)
    remaining = [g for g in genes if g not in by_name]
    by_synonym = _existing_by_synonym(remaining, db=db)

    rows, to_look_up = {}, []
    for gene in genes:
        target = by_name.get(gene)
        if target is not None:
            rows[gene] = _row(gene, ON_FILE, target_id=target.pk,
                              gene_name=target.gene_name or gene,
                              protein_name=target.protein_name or "",
                              uniprot_id=target.uniprot_id or "",
                              note="already on the list")
            continue
        target = by_synonym.get(gene)
        if target is not None:
            rows[gene] = _row(gene, SYNONYM, target_id=target.pk,
                              gene_name=target.gene_name or "",
                              protein_name=target.protein_name or "",
                              uniprot_id=target.uniprot_id or "",
                              note=f"already on the list as {target.gene_name}")
            continue
        to_look_up.append(gene)
    return rows, to_look_up


def _lookup(genes, *, budget_seconds=LOOKUP_BUDGET_SECONDS, workers=LOOKUP_WORKERS,
            now=time.monotonic):
    """`{gene: uniprot_dict}` for as many as the budget allowed.

    Futures still running when the budget expires are abandoned rather than
    waited on — their genes come back `unchecked` and the caller asks again.
    `now` is injectable so a test can prove the deadline fires without sleeping.
    """
    if not genes:
        return {}
    results = {}
    deadline = now() + budget_seconds

    # **Not** `with ThreadPoolExecutor(...)`. The context manager's exit calls
    # `shutdown(wait=True)`, which blocks until every submitted lookup finishes —
    # so the deadline would decide what got *recorded* while the request still
    # sat there for the slowest call. That is the entire failure this module
    # exists to prevent, reintroduced one indentation level down.
    #
    # `cancel_futures=True` drops the ones that have not started; the handful
    # already in flight cannot be killed (no thread can), but they are bounded by
    # `uniprot.TIMEOUT_SECONDS` and nothing waits on them. Their answers are
    # simply discarded, and their genes come back `unchecked`.
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {pool.submit(uniprot.lookup_gene, g): g for g in genes}
        try:
            for future in as_completed(futures, timeout=max(0.0, deadline - now())):
                gene = futures[future]
                try:
                    results[gene] = future.result()
                except Exception as e:
                    # A dead API degrades rather than raises: this gene simply
                    # has no verdict, which reads as `unchecked` and creates
                    # nothing.
                    logger.warning("UniProt lookup failed for %r: %s", gene, e)
        except TimeoutError:
            logger.info("UniProt budget of %.1fs spent; %d of %d genes checked",
                        budget_seconds, len(results), len(genes))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return results


def site_for(site=None, member=None, db=DB):
    """`(site_id, site_name)` — whose list this batch of genes goes on.

    A batch is one site's worth of genes, the same way it is one grant's worth,
    so this is chosen once for the press rather than per row. It was not chosen
    at all: every nomination was written at the *adder's own* site, which is the
    complaint the single-gene add already fixed — *"McGill is included by default
    when adding a new target entry (ideally there would be a drop down menu with
    McGill, Leicester, uOttawa, UBC, Cornell, etc)"* — and the two bulk doors
    kept. A consortium coordinator could not put a gene on another bench's list
    at all, and somebody whose account is on the wrong site could not stop the
    app filing their work under it.

    Blank means the member's own site, so nothing changes for a scientist adding
    their own genes. An unknown one is **refused by name** rather than falling
    back to the member's: silently re-homing a batch is the whole failure being
    fixed, and `plan`/`apply` are asked before anything is written.
    """
    site_id = site_svc.chosen_id(site, member=member, db=db)
    if not site_id:
        return None, ""
    found = Site.objects.using(db).filter(pk=site_id).first()
    return site_id, getattr(found, "name", "") or ""


def plan(genes, *, member=None, site=None, budget_seconds=LOOKUP_BUDGET_SECONDS,
         db=DB) -> dict:
    """Read-only preview. Never writes, and never spends longer than its budget.

    Every gene comes back with a verdict. `unchecked` means the deadline ran out
    before its turn — ask again with just those and they will be answered.

    ``site`` is whose list the press would add to; blank is the member's own.
    """
    genes = list(genes or [])
    if len(genes) > MAX_GENES:
        return {"ok": False,
                "error": f"{len(genes)} genes pasted; {MAX_GENES} is the most in one go."}
    try:
        site_id, site_name = site_for(site, member=member, db=db)
    except site_svc.UnknownSite as e:
        return {"ok": False, "error": str(e)}

    rows, to_look_up = _from_db(genes, db=db)
    looked_up = _lookup(to_look_up, budget_seconds=budget_seconds)

    # Names claimed within this batch, so two synonyms of one gene do not both
    # ask to be created. The old endpoint caught this only at write time, so the
    # screen said it had added two targets and the database had one.
    claimed = {}
    for gene in to_look_up:
        data = looked_up.get(gene)
        if data is None:
            rows[gene] = _row(gene, UNCHECKED,
                              note="not checked yet — UniProt did not answer in time")
            continue
        # **`found=False` is two different answers.** `lookup_gene` returns it
        # for "no such gene" *and* for "the request failed", and reading only
        # that flag reported a blocked proxy as "TRPA1 not in UniProt — not
        # added, check the spelling" about a real gene, with the raw
        # `ProxyError` printed underneath it. An unreachable API is exactly what
        # `unchecked` is for: nothing was learned, nothing is created, and
        # asking again is all it needs.
        if data.get("unavailable"):
            rows[gene] = _row(gene, UNCHECKED,
                              note="not checked — UniProt could not be reached")
            continue
        if not data.get("found"):
            rows[gene] = _row(gene, NOT_FOUND,
                              note=data.get("error") or "no such gene symbol in UniProt")
            continue
        # `gene_symbol.canonical`, not `.upper()`: uppercase is the rule and
        # `orf` is the exception, so a bare upper turns UniProt's own `C9orf72`
        # into `C9ORF72` on the way in. This value is both the dedup key below
        # and the `gene_name` the commit stores, and it stays consistent because
        # every spelling in the batch comes through the one function.
        canonical = gene_symbol.canonical(data.get("gene_name") or gene)
        synonyms = ", ".join(data.get("gene_synonyms") or [])
        if canonical in claimed:
            rows[gene] = _row(gene, SYNONYM, gene_name=canonical,
                              note=f"same gene as {claimed[canonical]} above")
            continue
        claimed[canonical] = gene
        rows[gene] = _row(gene, CREATES, gene_name=canonical,
                          protein_name=data.get("protein_name") or "",
                          uniprot_id=data.get("uniprot_id") or "",
                          mass_kda=data.get("mass_kda"),
                          synonyms=synonyms,
                          note=f"new — will be added as {canonical}")

    ordered = [rows[g] for g in genes]

    # The consortium's own answer, on the same rows as UniProt's. These were two
    # separate previews on two separate pages — the target board's Add panel knew
    # who was already doing a gene and whether its knockout could be bought but
    # not whether the symbol was real, and this one knew the reverse. Neither had
    # what the other had, and both were called "check".
    targets_by_gene = {g: t for g, t in _existing_by_name(genes, db=db).items()}
    for row in ordered:
        if row["target_id"] and row["gene"] not in targets_by_gene:
            # A synonym hit resolved to a target under a different name.
            targets_by_gene[row["gene"]] = Target.objects.using(db).get(pk=row["target_id"])
    context = _consortium_context(genes, targets_by_gene, db=db)

    # Which of these the chosen site has already nominated — one query, not one
    # per row. Same rule as the context above: nothing here may scale with the
    # number of genes pasted.
    mine = set()
    if site_id:
        ids = [r["target_id"] for r in ordered if r["target_id"]]
        if ids:
            mine = set(TargetNomination.objects.using(db)
                       .filter(target_id__in=ids, site_id=site_id)
                       .values_list("target_id", flat=True))
    for row in ordered:
        row["context"] = _context_notes(row["gene"], context[row["gene"]])
        row["will_nominate"] = _needs_nomination(row, site_id, mine)
        # **The site is part of what was previewed**, so the row carries it and
        # `apply` can tell that the dropdown moved after the check. A row saying
        # "will go on Ontario's list" saved against Leicester is a write nobody
        # previewed — the failure this pair's two-step shape exists to prevent,
        # arriving through a control rather than through the table.
        row["site_id"] = site_id

    return {
        "ok": True,
        "rows": ordered,
        "site": site_name,
        "site_id": site_id,
        "summary": _summarise(ordered),
    }


def _needs_nomination(row, site_id, already_mine) -> bool:
    """Would applying this row put the gene on the chosen site's list?

    A target already on the consortium's list but not nominated by you is the
    case the old endpoint did nothing at all for — it counted the gene as
    "skipped" and moved on, so a site could paste a list, see most of it skipped,
    and still not have any of it on their own board. A nomination is the
    repeatable half of a target; adding yours is what the button means.

    Takes the pre-fetched set rather than querying, so a hundred genes is one
    query and not a hundred.
    """
    if not site_id or row["status"] in (NOT_FOUND, UNCHECKED):
        return False
    if row["status"] == CREATES:
        return True
    if not row["target_id"]:
        return False
    return row["target_id"] not in already_mine


def _summarise(rows) -> dict:
    counts = {"total": len(rows), "create": 0, "on_file": 0, "not_found": 0,
              "unchecked": 0, "nominate": 0}
    for row in rows:
        if row["status"] == CREATES:
            counts["create"] += 1
        elif row["status"] in (ON_FILE, SYNONYM):
            counts["on_file"] += 1
        elif row["status"] == NOT_FOUND:
            counts["not_found"] += 1
        elif row["status"] == UNCHECKED:
            counts["unchecked"] += 1
        if row.get("will_nominate"):
            counts["nominate"] += 1
    return counts


def funding_for(agency=None, project=None, db=DB) -> dict:
    """Resolve one funder and one project for a whole batch, or refuse by name.

    The owner asked to be able to set these when adding targets in bulk — *"just
    1 each"* — because a batch is normally one grant's worth of genes and typing
    the pair into every row afterwards is the work the paste box exists to avoid.

    Both are optional and both are ids from a list the page already renders, so
    the ordinary path cannot mistype. What this is really for is the third case:
    **a project belongs to a funder**, so a project chosen on its own supplies
    the funder, and a pair that contradicts each other is refused with both
    names rather than one silently winning. Guessing which half somebody meant
    is how a gene ends up filed under a grant that did not pay for it.
    """
    from pipeline.models import GrantingAgency, Project

    out = {"granting_agency_id": None, "project_id": None}
    proj = None
    if project:
        proj = Project.objects.using(db).filter(pk=project).first()
        if proj is None:
            raise ValueError(
                f"No project with id {project}. Pick one from the list, or "
                f"leave it blank.")
        out["project_id"] = proj.pk

    if agency:
        found = GrantingAgency.objects.using(db).filter(pk=agency).first()
        if found is None:
            raise ValueError(
                f"No funder with id {agency}. Pick one from the list, or leave "
                f"it blank.")
        if proj is not None and proj.granting_agency_id \
                and proj.granting_agency_id != found.pk:
            raise ValueError(
                f"{proj.name} is funded by {proj.granting_agency}, not "
                f"{found}. Pick the matching funder, or leave the funder blank "
                f"and it will be taken from the project.")
        out["granting_agency_id"] = found.pk
    elif proj is not None and proj.granting_agency_id:
        # A project names its funder, so choosing one is choosing both.
        out["granting_agency_id"] = proj.granting_agency_id
    return out


def apply(rows, *, member=None, site=None, agency=None, project=None,
          funded=None, db=DB) -> dict:
    """Write what the preview showed. **Makes no network call.**

    Only `new` rows create a target, and only rows the preview resolved. A row
    the preview could not confirm is skipped and named, never turned into a bare
    target: the gene is the whole payload here, so an unconfirmed symbol is a
    typo far more often than it is an outage.

    ``site`` is whose list the nominations go on — see `site_for`. ``agency``/
    ``project``/``funded`` are one set of funding details for the whole batch.
    They are written onto the nominations this call creates, and **fill only
    blanks** on a nomination that already exists — the rule every other write
    path here follows, and the one that keeps a second paste of an overlapping
    list from quietly re-filing somebody else's genes.

    ``landed`` is what the press put somewhere you can go and read — see the
    comment where it is built. It is the one key the screens navigate on.
    """
    site_id, site_name = site_for(site, member=member, db=db)
    _refuse_a_moved_site(rows, site_id, site_name, db=db)
    funding = funding_for(agency, project, db=db)
    created, nominated, skipped, refunded = [], [], [], []
    # **Where the press just put something, with the ids to go and look.**
    # `created` carries a target_id and `nominated` carries bare gene names, so
    # between them there was no single answer to "which genes did this press put
    # on a list, and where do I read them?" — which is the question the screen
    # has to answer once it stops leaving you on the panel you pressed. The gene
    # here is the target's own `gene_name`, never what was typed: pasting the
    # synonym `PARK8` files a nomination against **LRRK2**, and a board filtered
    # to `PARK8` would show nothing.
    landed = []
    seen = set()

    with transaction.atomic(using=db):
        for row in rows or []:
            gene = (row.get("gene") or "").strip().upper()
            status = row.get("status")
            if not gene:
                continue

            if status in (NOT_FOUND, UNCHECKED):
                skipped.append({"gene": gene, "reason": row.get("note") or status})
                continue

            target_id = row.get("target_id")
            # The gene as it is (or will be) stored — the synonym `PARK8`
            # resolves to a row whose `gene_name` is LRRK2, and that is the
            # spelling a board filter has to be given.
            landed_name = gene_symbol.canonical(row.get("gene_name") or gene)
            made_target = False
            if status == CREATES:
                canonical = gene_symbol.canonical(row.get("gene_name") or gene)
                if canonical in seen:
                    skipped.append({"gene": gene, "reason": f"same gene as {canonical}"})
                    continue
                # Re-check under the lock: the preview may be minutes old and
                # somebody else may have added the gene in between.
                existing = Target.objects.using(db).filter(gene_name__iexact=canonical).first()
                if existing is not None:
                    target_id = existing.pk
                    skipped.append({"gene": gene,
                                    "reason": f"{canonical} was added by someone else first"})
                else:
                    target = Target(
                        gene_name=canonical,
                        protein_name=row.get("protein_name") or "",
                        uniprot_id=row.get("uniprot_id") or "",
                        theoretical_mass_kda=row.get("mass_kda"),
                        alternative_name=row.get("synonyms") or "",
                        status=Target.Status.NOT_STARTED,
                    )
                    target.save(using=db)
                    target_id = target.pk
                    created.append({"gene": gene, "gene_name": canonical,
                                    "protein_name": target.protein_name,
                                    "uniprot_id": target.uniprot_id,
                                    "mass_kda": row.get("mass_kda"),
                                    "target_id": target.pk})
                    made_target = True
                seen.add(canonical)
                landed_name = canonical

            if site_id and target_id:
                nom, made = TargetNomination.objects.using(db).get_or_create(
                    target_id=target_id, site_id=site_id,
                    defaults={"created_by_id": getattr(member, "pk", None),
                              "funded": bool(funded),
                              **funding})
                if made:
                    nominated.append(gene)
                elif funding["granting_agency_id"] or funding["project_id"] \
                        or funded:
                    # **Fill only blanks.** A gene already on your list keeps
                    # the funder and project somebody recorded against it; a
                    # second paste of an overlapping list must not re-file it.
                    fields = []
                    for key, value in funding.items():
                        if value and not getattr(nom, key):
                            setattr(nom, key, value)
                            fields.append(key)
                    if funded and not nom.funded:
                        nom.funded = True
                        fields.append("funded")
                    if fields:
                        nom.save(using=db, update_fields=fields)
                        refunded.append(gene)
            else:
                made = False

            # A gene this press put somewhere you can go and read it: a target
            # that did not exist, or one that did and was not on the chosen
            # site's list until now. A gene already on that list is not here —
            # there is nothing new to look at — and neither is one whose blank
            # funder got filled in, which is a change to a row rather than a
            # new row.
            if made_target or made:
                landed.append({"gene": landed_name, "target_id": target_id})

    return {"ok": True, "created": created, "nominated": nominated,
            "skipped": skipped, "funding_filled": refunded, "landed": landed,
            "site": site_name, "site_id": site_id,
            "summary": {"created": len(created), "nominated": len(nominated),
                        "skipped": len(skipped),
                        "funding_filled": len(refunded),
                        "landed": len(landed)}}


def _refuse_a_moved_site(rows, site_id, site_name, db=DB):
    """Refuse a commit whose site is not the one the preview was computed for.

    Both doors disarm their save when the site dropdown changes, which is the
    right place to say so — but a preview is not a permission slip, and the same
    two-step shape has already let a bench sheet reach the wrong session. Every
    row `plan` returned carries the site it was checked against, so the mismatch
    is free to spot and names both ends.
    """
    checked = {r.get("site_id") for r in (rows or []) if r.get("site_id")}
    if not checked or checked == {site_id}:
        return
    was = ", ".join(sorted(
        Site.objects.using(db).filter(pk__in=[c for c in checked if c])
        .values_list("name", flat=True))) or "another site"
    raise ValueError(
        f"These genes were checked against {was}, and this would add them to "
        f"{site_name or 'no site'}. Press Check these again so the preview says "
        f"what will be written.")
