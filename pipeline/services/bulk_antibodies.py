"""
Bulk antibody paste — add/update many antibodies at once from a pasted list
(e.g. catalogue + product-URL rows looked up in a chat), instead of editing them
one at a time.

Reuses the cropper's building blocks so nothing drifts:
  - metadata.parse_table  — the same deterministic table parser (now gene-aware)
  - commit._apply_metadata — fill-only-blank field application (Company.resolve,
    RRID → bare AB_<n> + registry link, clonality enum, lot, concentration,
    supplier-validated apps)
  - db.find_antibody / resolve_company — dedup-safe matching

`plan()` is read-only (drives the preview + dry-run). `apply()` writes.
"""
from __future__ import annotations

from django.db import transaction

from pipeline.models import Target, Antibody
from pipeline.services import concentration as concentration_svc
from pipeline.services import lab_numbers
from pipeline.services import sites as site_svc
from pipeline.services.cropper import db as cdb
from pipeline.services.cropper import metadata as meta
from pipeline.services.cropper.commit import _apply_metadata
from pipeline.services import example_row
from pipeline.services.targets import resolve_or_create_target

DB = "pipeline_db"


def parse(text: str, default_gene: str = "", *, header_led: bool = False):
    """Parse the pasted table into rows; fill any blank gene with the default.

    A template's own example row is dropped first — see
    ``services/example_row.py``. Downloading the blank sheet, filling it in under
    the example and uploading it is the flow the sheet's own instructions
    describe, so the example must not arrive as an antibody.

    ``header_led`` is passed by the **upload** path and by nothing else: a file
    has a header row and is read by it, where a paste may be wrapped PDF text
    with no header at all. `metadata.parse_table` explains what that turns off
    and what it cost to leave on.
    """
    rows = meta.parse_table(example_row.drop(text or ""), header_led=header_led)
    dg = (default_gene or "").strip()
    for r in rows:
        if not (r.get("gene") or "").strip():
            r["gene"] = dg
    return rows


def _resolve_target(gene: str, catalogue: str):
    """Find the Target for a row. Prefer the gene; if there's no gene, fall back
    to matching the catalogue across the DB — but only when it's unambiguous (a
    single target owns that catalogue), so a URL-only paste can still land."""
    gene = (gene or "").strip()
    if gene:
        return Target.objects.using(DB).filter(gene_name__iexact=gene).first()
    cat = (catalogue or "").strip()
    if cat:
        targets = {a.target_id for a in Antibody.objects.using(DB)
                   .filter(catalogue_number__iexact=cat).only("target_id")}
        if len(targets) == 1:
            return Target.objects.using(DB).filter(id=next(iter(targets))).first()
    return None


def _row_site(row, member):
    """`(site_id, site_name, error)` for one row.

    A `site` column is honoured; a blank one falls back to whoever is pasting.
    This is what makes the download → edit → upload round trip safe: without it,
    every uploaded row was stamped with the uploader, so a Leicester user putting
    McGill's SOD1 sheet back created 22 Leicester duplicates of McGill's vials —
    and the preview said "new", correctly, about rows that already existed.
    """
    site_id, err = site_svc.for_row(row.get("site"), member=member)
    if err:
        return None, (row.get("site") or "").strip(), err
    if (row.get("site") or "").strip():
        site = site_svc.resolve(row["site"])
        return site_id, (site.name if site else ""), None
    return site_id, (getattr(getattr(member, "site", None), "name", "") or ""), None


def plan(rows, create_targets: bool = False, member=None):
    """Per-row: what would happen (update existing / create / create-target /
    blocked) plus the company-resolution status — read-only.

    ``member`` gives the site for a row that does not name one, because a row
    resolves to a *vial* and site is half of what makes a vial that vial. It must
    be the same member ``apply`` gets, or the preview and the write disagree about
    which rows already exist — the one thing a preview must never do.
    """
    items = []
    for r in rows:
        gene = (r.get("gene") or "").strip()
        cat = (r.get("catalogue") or "").strip()
        # Whose vial. Per row, not per paste: a downloaded sheet spans sites.
        site_id, site_name, site_err = _row_site(r, member)
        target = _resolve_target(gene, cat)
        existing = (cdb.find_vial(target, r.get("company", ""), cat,
                                  r.get("lot", ""), site_id)
                    if (target and cat and not site_err) else None)
        comp = cdb.resolve_company(r.get("company", ""), cat, create=False) if r.get("company") else None
        note = ""
        if site_err:
            status, note = "blocked", site_err
        elif target:
            status = "update" if existing else "create"
        elif gene and create_targets:
            status = "create-target"
        else:
            status = "no-target"
        lot = (r.get("lot") or "").strip()
        ab_number, ab_note = _ab_number(r.get("ab_number"), site_id, site_name,
                                        existing)
        if ab_note and status != "blocked":
            # A number that cannot be read, or one another vial at this bench
            # already carries, blocks the row. Unlike a concentration this
            # cannot convert — where the row is still worth writing without the
            # value — a wrong lab number is worse than none: it is the thing a
            # person reads off a freezer box to find the tube, so two rows
            # answering to one number is exactly the confusion the column
            # exists to prevent.
            status, note = "blocked", ab_note
        items.append({
            "ab_number": ab_number,
            # A row that will be created, at a known bench, with the number
            # column left blank — the rows the save is about to mint a number
            # for. Not the number itself: see `summarize`.
            "will_be_numbered": (status == "create" and bool(site_id)
                                 and ab_number is None),
            "row": r, "gene": gene, "catalogue": cat,
            "target_id": target.id if target else None,
            "target_gene": target.gene_name if target else "",
            "existing_ab_id": existing.id if existing else None,
            "company_status": ("existing" if comp else ("new" if r.get("company") else "none")),
            # The name the row will be *saved* under, which is not always the one
            # a lookup of what exists today returns — see `resolved_company_name`.
            "company_resolved": cdb.resolved_company_name(r.get("company", ""), cat,
                                                          existing=comp),
            # The five parts that make a row the vial it is, so the preview can
            # name each row by what actually distinguishes it. `company` is the
            # typed name; `company_resolved` above is what Company.resolve made of it.
            "company": (r.get("company") or "").strip(),
            "lot": lot,
            "site": site_name,
            "site_id": site_id,
            "status": status,
            # A gene that is not a target yet is the commonest way an antibody
            # paste stops, and "skipped — no matching gene" said what happened
            # without saying what to do about it. The same pair the cell-line
            # preview carries: the sentence names the tickbox on this page, and
            # the URL is the way to the board where a gene actually belongs.
            "add_target_url": (_add_target_url(gene)
                               if status == "no-target" and gene else ""),
            "note": _with_supplier_apps_note(
                _with_concentration_note(
                    note or _no_target_note(status, gene)
                    or _identity_note(status, existing, lot),
                    r.get("concentration")),
                r.get("apps_raw")),
            # Carried on the item so `summarize` can *count* the rows whose
            # concentration will not be stored. The per-row note has said so
            # since run 6; nothing added it up, so the save summary was silent
            # about a value the preview had just refused — see `summarize`.
            "concentration_dropped": bool(_concentration_error(r.get("concentration"))),
        })
    return items


def _ab_number(raw, site_id, site_name, existing):
    """``(number, refusal)`` for the row's ``ab #`` cell.

    Three cases, and the middle one is the reason this is not just ``parse``:

    * **``A-118``** — the lab's own number, typed on purpose. Used, unless
      another vial at this bench already carries it.
    * **a bare integer** — what every antibodies sheet downloaded before
      numbers were issued again holds in this column, because it printed the
      *record* id there. Ignored: it is not a lab number, nobody typed it, and
      writing it back would relabel the row. See
      ``services/lab_numbers.py::read_reference``.
    * **anything else** — refused by name, the same way a C-number that is not
      a C-number is (``C-RUN11-01`` cost two indistinguishable knockouts).

    ``None`` means "nothing typed", which is what leaves room for the number to
    be issued at save — and on an **update** it means "leave the number this
    vial already has alone", never "clear it".
    """
    kind, value = lab_numbers.read_reference(raw)
    if kind == "record":
        return None, ""
    if kind == "number":
        if existing is not None and existing.ab_number == value:
            return None, ""          # already this row's own number
        msg = lab_numbers.clash(lab_numbers.ANTIBODY, site_id, value,
                                exclude_pk=existing.pk if existing else None,
                                site_name=site_name)
        return (None, msg) if msg else (value, "")
    text = str(raw if raw is not None else "").strip()
    if not text:
        return None, ""
    _n, err = lab_numbers.parse(text, kind=lab_numbers.ANTIBODY)
    return None, err


def _with_supplier_apps_note(note, raw):
    """Say which applications the supplier-recommendations cell will actually
    store, when that is not simply what was typed.

    The cell is free text and only recognised application codes become flags, so
    ``WB, IHC`` stored WB and lost IHC without a word — the same class of silent
    drop as a concentration whose unit could not be read. IHC and ELISA are read
    now (the model has always had the columns), but the cell can still hold
    something nothing recognises, and the honest answer to that is not a list of
    everything this cannot parse — it is *what will be saved*, which the reader
    can compare against what they typed.
    """
    typed = (raw or "").strip()
    if not typed:
        return note
    apps = meta._apps_from(typed)
    # Quiet when the cell is exactly what will be stored, which is the ordinary
    # case — a note on every row is a note nobody reads.
    if ", ".join(apps).lower() == typed.lower().replace(" ", "").replace(",", ", ").strip():
        return note
    said = (f"supplier recommendations will be stored as {', '.join(apps)}"
            if apps else
            f"none of '{typed}' is an application this recognises — supplier "
            f"recommendations will be left empty")
    return f"{note} · {said}" if note else said


def _concentration_error(raw) -> str:
    """The refusal for this concentration cell, or `""`. Blank cells are fine."""
    _value, err = concentration_svc.parse(raw)
    return err


def _with_concentration_note(note, raw):
    """Add a word about the concentration cell when it will not be stored.

    The number goes in without its unit — the column *is* µg/mL — so "1.0 mg/mL"
    used to be filed as 1, a thousandfold out, in silence. Converting it is the
    fix; saying so when the unit is one this cannot convert is the other half,
    because the alternative is the same silence one step along. Not a refusal:
    an odd concentration is no reason to reject an otherwise good antibody row,
    and the rest of the row is still worth writing.
    """
    err = _concentration_error(raw)
    if not err:
        return note
    return f"{note} · {err}" if note else err


def _no_target_note(status, gene):
    """Why a row with a gene was skipped, and the two ways out of it."""
    if status != "no-target":
        return ""
    if not gene:
        return ("no gene on this row — an antibody is against something, so the "
                "gene column decides which target it lands on")
    return (f"gene '{gene}' is not in the pipeline yet — tick “Add the gene as a "
            f"new target if it isn't one yet” above, or add it on the target "
            f"board first")


def _add_target_url(gene: str) -> str:
    """The target board, with this gene filled in and the Add panel open."""
    try:
        from django.urls import reverse
        from urllib.parse import urlencode
        return f"{reverse('pipeline:target_board')}?{urlencode({'gene': gene, 'add': '1'})}"
    except Exception:  # never break a preview over a URL
        return ""


def _identity_note(status, existing, lot):
    """Why this row is an update rather than a creation, in one clause.

    "Already on file — will be updated" is true and unreadable when two rows
    print the same name. Naming the lot that matched says which vial is meant.
    """
    if status != "update" or existing is None:
        return ""
    on_file = (existing.lot_number or "").strip()
    if lot and on_file:
        return f"matches the vial already recorded with lot {on_file}"
    if lot and not on_file:
        return f"fills in lot {lot} on the row that has none recorded"
    return "matches the row already on file, which has no lot recorded"


def summarize(items) -> dict:
    return {
        "rows": len(items),
        "update": sum(1 for i in items if i["status"] == "update"),
        "create": sum(1 for i in items if i["status"] == "create"),
        "create_target": sum(1 for i in items if i["status"] == "create-target"),
        "blocked": sum(1 for i in items if i["status"] == "no-target"),
        # An unrecognised site is its own refusal, counted apart from "no gene" so
        # the summary can say which thing to fix.
        "bad_site": sum(1 for i in items if i["status"] == "blocked"),
        # **Say what you are about to drop, and count it.** A unit this cannot
        # convert is refused per row and the row is still written — deliberately,
        # since an odd concentration is no reason to throw away a good vial — but
        # the count was nowhere, so run 8 read the refusal on `3 mM`, pressed
        # Create them with no further warning, and got a row with no
        # concentration at all. The panel two along, for an unrecognised
        # spreadsheet column, has done this correctly since run 6.
        "no_concentration": sum(1 for i in items if i.get("concentration_dropped")),
        # **A number the app gives a record is a thing the app did.** The first
        # field test on A-numbers left the number column blank, got A-1, and
        # said nothing in the preview or the save message mentioned that a lab
        # reference had been minted. The check says one *will* be given and does
        # not promise which — the value depends on what else lands in the same
        # batch — and `apply` reports what was actually issued.
        "will_be_numbered": sum(1 for i in items if i.get("will_be_numbered")),
        "new_companies": sorted({i["row"].get("company", "") for i in items
                                 if i["company_status"] == "new" and i["row"].get("company")}),
        # Every site the paste would write to. One name is the ordinary case; more
        # than one means the sheet spans benches, which a download legitimately
        # does and a hand-typed paste almost never should.
        "sites": sorted({i["site"] for i in items if i.get("site")}),
    }


def apply(rows, create_targets: bool, member=None, overwrite: bool = False,
          lookup_rrids: bool = False):
    """Create/update antibodies (and, if asked, missing targets) in one
    transaction. Fills only blank fields by default, so re-pasting never clobbers
    data; overwrite=True lets non-empty values replace existing ones (the edited-
    export round-trip). Returns a result dict with per-antibody handles.

    ``lookup_rrids`` (opt-in): when a row leaves ``rrid`` blank, resolve it from
    the Antibody Registry by catalogue + vendor + gene (the same strictly-gated
    match the backfill command uses) and fill it — so new antibodies aren't
    created RRID-less. OFF by default because each lookup is a ~seconds network
    call: safe for the single-add form (one row), but NOT for large bulk pastes
    under the request timeout — those rely on the periodic
    ``backfill_rrid_from_registry`` command instead."""
    # The same member the preview got. It used to be omitted here, so `plan`'s
    # per-row site — and therefore which rows it called new — was computed against
    # no site at all while the write used the member's. Preview and write have to
    # resolve the same vial or the preview is fiction.
    items = plan(rows, create_targets, member=member)
    out = {"created": [], "updated": [], "created_targets": [], "skipped": [],
           "blocked": [], "rrids_filled": 0,
           # Rows written *without* the concentration they named, so the save can
           # repeat the refusal the preview gave. A warning that only appears
           # before the button is a warning the button can outlive.
           "no_concentration": [],
           # The lab numbers this press minted. A number the app gives a record
           # is a thing the app did — and it goes on a freezer box — so the save
           # names each one rather than leaving the reader to find A-1 on the
           # board later and wonder where it came from.
           "numbers_issued": []}
    with transaction.atomic(using=DB):
        for it in items:
            r, gene, cat = it["row"], it["gene"], it["catalogue"]
            if it["status"] == "blocked":
                # An unrecognised site name. Refuse the row rather than quietly
                # filing another bench's vial under the uploader's own.
                out["blocked"].append(f"{cat or gene or '(row)'}: {it['note']}")
                continue
            target = (Target.objects.using(DB).filter(id=it["target_id"]).first()
                      if it["target_id"] else None)
            if target is None:
                if gene and create_targets:
                    # Enrich a newly-created target from UniProt (protein name,
                    # accession, mass, synonyms) so it isn't a hollow stub.
                    target, made = resolve_or_create_target(gene, member=member)
                    if made:
                        out["created_targets"].append(target.gene_name)
                if target is None:
                    out["skipped"].append(cat or gene or "(row)")
                    continue
            if not cat:
                out["skipped"].append(gene or "(no catalogue)")
                continue
            # The vial, not the product: another site's vial of the same
            # catalogue number is a different physical tube, and so a row of its
            # own. Matching on the product alone meant a second site's paste
            # silently edited the first site's record instead of recording theirs.
            #
            # The site comes from the row — its own `site` column if the sheet has
            # one, the member otherwise — and it is the value the preview showed.
            site_id = it["site_id"]
            ab = cdb.find_vial(target, r.get("company", ""), cat,
                               r.get("lot", ""), site_id)
            created = ab is None
            issued_here = False
            if created:
                ab = Antibody(target=target, catalogue_number=cat, site_id=site_id)
                # What the preview showed. A blank cell leaves this None, and
                # the number is issued on save from this site's own run
                # (`pipeline/signals.py`); a typed one is what the freezer box
                # already says, so it wins.
                ab.ab_number = it.get("ab_number")
                issued_here = ab.ab_number is None
            elif it.get("ab_number") is not None and ab.ab_number is None:
                # Fill-only-blank, like every other column: a vial already
                # carrying a number keeps it, and a blank one takes what the
                # sheet says. Never cleared by a blank cell.
                ab.ab_number = it["ab_number"]
            elif ab.site_id is None and site_id:
                # Adopting a row that predates sites: stamp whose it is, or the
                # next site to paste would match the same unsited row and the two
                # would go on sharing one record.
                ab.site_id = site_id
            _apply_metadata(ab, r, overwrite=overwrite)
            # Opt-in: fill a still-blank RRID from the registry (best-effort).
            if lookup_rrids and not (ab.rrid or "").strip():
                if _fill_rrid_from_registry(ab, cat, r.get("company", ""), target.gene_name or ""):
                    out["rrids_filled"] += 1
            ab.save(using=DB)
            if it.get("concentration_dropped"):
                out["no_concentration"].append(
                    {"catalogue": ab.catalogue_number,
                     "typed": str(r.get("concentration") or "").strip()})
            if created and issued_here and ab.ab_number is not None:
                out["numbers_issued"].append(
                    {"number": lab_numbers.label(ab.ab_number,
                                                 kind=lab_numbers.ANTIBODY),
                     "name": ab.catalogue_number})
            rec = {"catalogue": ab.catalogue_number, "id": ab.pk, "gene": target.gene_name,
                   "ab_number": lab_numbers.label(ab.ab_number,
                                                  kind=lab_numbers.ANTIBODY)}
            (out["created"] if created else out["updated"]).append(rec)
    return out


def _fill_rrid_from_registry(ab, catalogue: str, company: str, gene: str) -> bool:
    """Best-effort: set ab.rrid (+ rrid_link, and supplier_url if blank) from a
    HIGH-confidence registry match. Returns True if it filled the RRID. Never
    raises — a registry hiccup just leaves the RRID blank."""
    from pipeline.services import scicrunch
    from pipeline import rrid_utils
    try:
        rrid, url, _note = scicrunch.resolve_rrid(catalogue, company, gene)
    except Exception:
        return False
    if not rrid:
        return False
    ab.rrid = rrid
    ab.rrid_link = rrid_utils.registry_url(rrid)
    if url and not (ab.supplier_url or "").strip():
        ab.supplier_url = url
    return True
