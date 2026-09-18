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

import logging

from django.db import transaction

from pipeline.models import Target, Antibody
from pipeline.services import concentration as concentration_svc
from pipeline.services import lab_numbers
from pipeline.services import received as received_svc
from pipeline.services import sites as site_svc
from pipeline.services import storage as storage_svc
from pipeline.services.cropper import db as cdb
from pipeline.services.cropper import metadata as meta
from pipeline.services.cropper.commit import _apply_metadata
from pipeline.services import example_row

DB = "pipeline_db"

logger = logging.getLogger(__name__)


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


def plan(rows, member=None, *, number_now: bool = False):
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
        else:
            # **A target is created on the targets doors and nowhere else**
            # (owner, 5 Sep 2026). There was a `create-target` status here,
            # reached when the panel's tick was on, and three things were wrong
            # with it at once: no preview confirmed the symbol, so a typo became
            # a permanent gene; the write called `resolve_or_create_target`
            # *inside* the commit's transaction, holding PostgreSQL open for a
            # 10 s UniProt call per new gene on a four-thread site; and neither
            # of those is true of the door built for the job, which confirms the
            # symbol and creates the row bare. So an unknown gene stops here and
            # `add_target_url` carries the reader to the board that does it
            # properly.
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
            # column left blank. Whether the save gives it a number is the
            # bench's choice (`number_now`), so the check has to answer the
            # question the tick actually asks — a preview that says "no
            # A-number" over a ticked box is worse than saying nothing.
            #
            # It used to be `will_be_numbered`, and the save minted an A-number
            # there and then. uOttawa stopped logging into the portal because of
            # exactly that: the number decides which freezer box a vial goes in,
            # and reagents arriving over weeks were being numbered in order of
            # arrival, scattering each protein across five boxes. Measured 2 Sep
            # 2026, McGill's 277 multi-antibody genes sit in 6.77 separate runs
            # of numbers each. So numbering waits until somebody deals the set
            # out (`services/renumber.py`), or until a session is planned
            # (`lab_numbers.ensure_numbered`), whichever comes first. Cell lines
            # are unchanged and still numbered on creation.
            "will_be_unnumbered": (status == "create" and bool(site_id)
                                   and ab_number is None and not number_now),
            "will_be_numbered": (status == "create" and bool(site_id)
                                 and ab_number is None and number_now),
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
            "note": _with_received_note(
                _with_supplier_apps_note(
                    _with_concentration_note(
                        note or _no_target_note(status, gene)
                        or _identity_note(status, existing, lot),
                        r.get("concentration"), r.get("concentration_unit_from")),
                    r.get("apps_raw")),
                r.get("received")),
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


def _with_concentration_note(note, raw, unit_from=""):
    """Add a word about the concentration cell when it will not be stored — or
    when what will be stored is not the number in the cell.

    The number goes in without its unit — the column *is* µg/mL — so "1.0 mg/mL"
    used to be filed as 1, a thousandfold out, in silence. Converting it is the
    fix; saying so when the unit is one this cannot convert is the other half,
    because the alternative is the same silence one step along. Not a refusal:
    an odd concentration is no reason to reject an otherwise good antibody row,
    and the rest of the row is still worth writing.

    ``unit_from="heading"`` is the third case and the one nobody could see. A
    sheet headed ``Concentration (mg/mL)`` means its cells are milligrams, so
    ``0.498`` is stored as **498** — and the cell a person is looking at says
    `0.498`, with the unit two rows up in the header. A conversion the reader
    cannot see in the cell is exactly the kind a preview owes them, so this
    names it whenever the arithmetic actually changes the number.
    """
    err = _concentration_error(raw)
    if err:
        return f"{note} · {err}" if note else err
    said = _heading_unit_note(raw, unit_from)
    if said:
        return f"{note} · {said}" if note else said
    return note


def _heading_unit_note(raw, unit_from) -> str:
    """"the heading says mg/mL, so 0.498 is stored as 498 µg/mL" — or ``""``.

    Quiet when the unit came from the cell (the person typed it, so they know),
    and quiet when the conversion leaves the number alone, which is every sheet
    this app exports.
    """
    if unit_from != "heading":
        return ""
    text = str(raw or "").strip()
    value, err = concentration_svc.parse(text)
    if err or value is None:
        return ""
    digits, _err = concentration_svc.parse(
        text.split(" ")[0] if " " in text else text)
    if digits is None or digits == value:
        return ""
    unit = text.rsplit(" ", 1)[-1]
    return (f"the heading says {unit}, so {_plain(digits)} is stored as "
            f"{_plain(value)} {concentration_svc.STORED_UNIT}")


def _plain(value) -> str:
    """A Decimal as a scientist writes it — never in scientific notation.

    `Decimal.normalize()` is the obvious way to drop trailing zeros and it
    reaches for an exponent the moment there are any: `Decimal("500.0")`
    normalises to `5E+2`, so the note read *"1 is stored as 1E+3 µg/mL"* — on
    the one number the message exists to make checkable, and on the commonest
    value in the sheet that prompted all of this (12 of uOttawa's 42
    concentrations are a bare `1`). `format(d, "f")` is the fixed-point spelling
    of the same value.
    """
    return format(value.normalize(), "f")


def _with_received_note(note, raw):
    """Say when the arrival date cell cannot be read, or is less precise than
    it looks.

    Same shape as the concentration note and for the same reason: the row is
    still worth writing without the date, so this is a note rather than a
    refusal — but a cell nobody could read must not go by in silence, or the
    person finds out months later that the column is empty.

    A month is **not** a defect and gets no note. ``Aug 2026`` is a real answer
    and is stored as one (`services/received.py`); saying "this is only a
    month" on every uOttawa row would be noise on the ordinary case.
    """
    text = str(raw or "").strip()
    if not text:
        return note
    _when, _precision, err = received_svc.parse(text)
    if not err:
        return note
    return f"{note} · {err}" if note else err


def _no_target_note(status, gene):
    """Why a row with a gene was skipped, and the way out of it.

    **The way out is the target board, and it is the only one** (owner, 5 Sep
    2026): a target is added on the targets doors, so this no longer offers a
    tick that creates one from an antibody row. It named one for a while, which
    was true on the Add panel and false on the Upload panel beside it — run 20
    followed the sentence and found no such control. Both panels have lost the
    box, so there is one sentence again and it points at the board; the row also
    carries ``add_target_url`` as a link to it.
    """
    if status != "no-target":
        return ""
    if not gene:
        return ("no gene on this row — an antibody is against something, so the "
                "gene column decides which target it lands on")
    return (f"gene '{gene}' is not in the pipeline yet — add it on the target "
            f"board first, then bring this row back")


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
        # **A number the app does *not* give a record is also a thing the app
        # did**, and the same rule applies: say so. The check named a minted
        # A-number from run 8's finding onwards; now it names the absence, and
        # says where the numbers come from instead. Deliberately a different key
        # from the cell lines' `will_be_numbered` — `board.js::labNumberNote` is
        # one writer for both panels, and one key meaning opposite things on two
        # boards is how a message ends up true of neither.
        "will_be_unnumbered": sum(1 for i in items
                                  if i.get("will_be_unnumbered")),
        # The same count for a bench that ticked "give them numbers now" —
        # McGill has always numbered on receipt and asked to keep doing it.
        # `board.js::labNumberNote` reads both keys; only one is ever non-zero.
        "will_be_numbered": sum(1 for i in items if i.get("will_be_numbered")),
        "new_companies": sorted({i["row"].get("company", "") for i in items
                                 if i["company_status"] == "new" and i["row"].get("company")}),
        # Every site the paste would write to. One name is the ordinary case; more
        # than one means the sheet spans benches, which a download legitimately
        # does and a hand-typed paste almost never should.
        "sites": sorted({i["site"] for i in items if i.get("site")}),
    }


def apply(rows, member=None, overwrite: bool = False,
          lookup_rrids: bool = False, number_now: bool = False):
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
    items = plan(rows, member=member)
    out = {"created": [], "updated": [], "skipped": [],
           "blocked": [], "rrids_filled": 0,
           # Rows written *without* the concentration they named, so the save can
           # repeat the refusal the preview gave. A warning that only appears
           # before the button is a warning the button can outlive.
           "no_concentration": [],
           # The lab numbers this press minted. A number the app gives a record
           # is a thing the app did — and it goes on a freezer box — so the save
           # names each one rather than leaving the reader to find A-1 on the
           # board later and wonder where it came from.
           "numbers_issued": [], "left_unnumbered": 0}
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
                # Never created here — see `plan`. The row was previewed as
                # `no-target` and is skipped, which is what the preview said.
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
            left_unnumbered = issued_here = False
            if created:
                ab = Antibody(target=target, catalogue_number=cat, site_id=site_id)
                # What the preview showed.
                ab.ab_number = it.get("ab_number")
                # **Logging does not allocate by default, and the bench can
                # say otherwise.** A typed number is what the freezer box
                # already says and always wins. Otherwise the row stays blank
                # unless `number_now` — the tick on the Add panel — in which
                # case the `pre_save` signal mints this site's next, exactly as
                # it did before 2 Sep 2026.
                #
                # Off by default because the two mistakes are not symmetrical:
                # numbering when you did not want it puts arrival order onto the
                # freezer and takes a renumbering to undo, while not numbering
                # when you did is one press of Assign numbers — and happens
                # anyway the moment a session is planned.
                if ab.ab_number is None and not number_now:
                    lab_numbers.withhold(ab)
                    left_unnumbered = True
                    issued_here = False
                else:
                    left_unnumbered = False
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
            # Where the vial lives. After the save because a location is its own
            # row and needs this one's pk — the same order `bulk_cell_lines`
            # writes a vial's location in. Fill-only-blank and deduped, so
            # re-uploading a sheet does not stack a second freezer, and silent
            # when the row said nothing about storage, which is most rows.
            storage_svc.write_row(ab, r, db=DB)
            if it.get("concentration_dropped"):
                out["no_concentration"].append(
                    {"catalogue": ab.catalogue_number,
                     "typed": str(r.get("concentration") or "").strip()})
            if created and left_unnumbered:
                out["left_unnumbered"] += 1
            # **A number the app gives a record is a thing the app did**, so the
            # save names it — the finding that put this message here in the
            # first place. Only for a number the *app* chose: one the reader
            # typed is not news, they wrote it.
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
    from pipeline.services import scicrunch, targets as targets_service
    from pipeline import rrid_utils
    try:
        # Same alias list the backfill command passes, for the same reason: the
        # registry names a gene by a synonym often enough that our primary
        # symbol alone reads a match as a mismatch. Both doors, one behaviour.
        aliases = (targets_service.registry_names(ab.target)
                   if getattr(ab, "target_id", None) else [])
        rrid, url, _note = scicrunch.resolve_rrid(catalogue, company, gene,
                                                  aliases=aliases)
    except Exception:
        # Deliberately broad: a registry hiccup must leave the RRID blank rather
        # than fail an import. But it is LOGGED, because this same handler will
        # swallow a programming error -- a signature that drifted, say -- and
        # report it as "no RRID found", which no screen contradicts.
        logger.exception("RRID lookup failed for %r (%s)", catalogue, company)
        return False
    if not rrid:
        return False
    ab.rrid = rrid
    ab.rrid_link = rrid_utils.registry_url(rrid)
    if url and not (ab.supplier_url or "").strip():
        ab.supplier_url = url
    return True
