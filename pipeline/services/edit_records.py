"""Shared field-application for editing an antibody or a cell line.

Before this, the save logic lived inline in ``views/search.py::antibody_edit``
and ``cell_line_edit``. Server B (the MCP write path) needs the *same*
normalisation — RRID → bare ``AB_<n>`` + registry link, ``Company.resolve`` so
suppliers never duplicate, enum/decimal/date coercion — so it is extracted here
and both the view and the tool call it. Don't duplicate this logic.

Two modes:
  * ``mode="form"``  — the HTML edit form submits the WHOLE record, so an absent
    text field clears it and an absent checkbox means unchecked (False). This
    reproduces the original view behaviour exactly.
  * ``mode="patch"`` — an MCP ``edit_*`` call is a partial update: only the keys
    actually supplied are touched; everything else is left alone. This is the
    safe default for programmatic edits.

These functions MUTATE and normalise the instance but do NOT call ``.save()`` —
the caller decides when to persist (the view saves directly; Server B wraps the
call in a transaction so it can dry-run then commit). Note ``Company.resolve``
may create a Company row as a side effect; callers that need a true dry-run
should run inside a rolled-back transaction.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.utils.dateparse import parse_date

from pipeline import rrid_utils
from pipeline.models import Antibody, CellLine, Company, Target
from pipeline.services import concentration as concentration_svc

DB = "pipeline_db"

_AB_TEXT_FIELDS = [
    "catalogue_number", "lot_number", "clone_id", "host_species",
    "isotype", "antigen", "species_reactivity", "supplier_url", "comments",
]
_AB_BOOL_FIELDS = [
    "is_recombinant", "out_of_market", "empty_vial", "is_test",
    "supplier_validated_wb", "supplier_validated_ip", "supplier_validated_if",
    "supplier_validated_fc", "supplier_validated_ihc", "supplier_validated_elisa",
    "wb_recommended", "ip_recommended", "if_recommended", "fc_recommended",
]

_CL_TEXT_FIELDS = [
    "name", "catalogue_number", "lot_number", "clone", "medium",
    "growth_properties", "origin", "species", "parental_line_name",
]
_CL_BOOL_FIELDS = ["received", "ko_validated", "thawed"]


def _truthy(value) -> bool:
    """Interpret a checkbox / JSON value as a boolean."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "on", "yes", "y", "t"}


def _wanter(data, mode):
    """Return a predicate: in form mode always True; in patch mode 'key present'."""
    if mode == "form":
        return lambda key: True
    if mode == "patch":
        return lambda key: key in data
    raise ValueError(f"mode must be 'form' or 'patch', got {mode!r}")


def _apply_decimal(instance, field, data, key, want):
    if not want(key):
        return
    raw = (str(data.get(key) or "")).strip()
    if not raw:
        setattr(instance, field, None)
        return
    try:
        setattr(instance, field, Decimal(raw))
    except InvalidOperation:
        pass  # bad number → keep the old value (matches the view)


def apply_antibody_fields(antibody: Antibody, data, *, db: str = DB, mode: str = "patch") -> Antibody:
    """Apply edit fields to ``antibody`` (does not save). See module docstring."""
    want = _wanter(data, mode)

    for f in _AB_TEXT_FIELDS:
        if want(f):
            setattr(antibody, f, (str(data.get(f) or "")).strip())

    for f in _AB_BOOL_FIELDS:
        if mode == "form":
            setattr(antibody, f, f in data)          # present checkbox == checked
        elif f in data:
            setattr(antibody, f, _truthy(data.get(f)))

    if want("clonality"):
        clon = (str(data.get("clonality") or "unknown")).strip().lower()
        if clon in dict(Antibody.Clonality.choices):
            antibody.clonality = clon
            if clon == "recombinant":
                antibody.is_recombinant = True

    if want("acquisition_method"):
        acq = (str(data.get("acquisition_method") or "")).strip().lower()
        if acq in dict(Antibody.AcquisitionMethod.choices):
            antibody.acquisition_method = acq

    # Concentration carries a unit and the column does not, so it goes through
    # the one reader that converts into the stored µg/mL rather than keeping the
    # digits and dropping the unit. See services/concentration.py.
    if want("concentration"):
        raw = (str(data.get("concentration") or "")).strip()
        if not raw:
            antibody.concentration = None
        else:
            value, err = concentration_svc.parse(raw)
            if err:
                raise ValueError(err)
            antibody.concentration = value
    _apply_decimal(antibody, "in_kind_value", data, "in_kind_value", want)

    if want("in_kind_currency"):
        antibody.in_kind_currency = (str(data.get("in_kind_currency") or "GBP")).strip() or "GBP"

    if want("received_date"):
        rd = (str(data.get("received_date") or "")).strip()
        antibody.received_date = parse_date(rd) if rd else None

    if want("rrid"):
        rrid_raw = (str(data.get("rrid") or "")).strip()
        bare = rrid_utils.normalize_rrid(rrid_raw) if rrid_raw else None
        antibody.rrid = bare or ""
        antibody.rrid_link = rrid_utils.registry_url(bare) if bare else ""

    _apply_company(antibody, data, want, db, mode)
    return antibody


def apply_cell_line_fields(cl: CellLine, data, *, db: str = DB, mode: str = "patch") -> CellLine:
    """Apply edit fields to a cell line ``cl`` (does not save)."""
    want = _wanter(data, mode)

    for f in _CL_TEXT_FIELDS:
        if want(f):
            setattr(cl, f, (str(data.get(f) or "")).strip())

    for f in _CL_BOOL_FIELDS:
        if mode == "form":
            setattr(cl, f, f in data)
        elif f in data:
            setattr(cl, f, _truthy(data.get(f)))

    if want("cellosaurus_id"):
        cl.cellosaurus_id = (str(data.get("cellosaurus_id") or "")).strip().upper()

    if want("genotype"):
        g = (str(data.get("genotype") or "")).strip()
        if g in dict(CellLine.Genotype.choices):
            cl.genotype = g

    if want("acquisition_method"):
        acq = (str(data.get("acquisition_method") or "")).strip().lower()
        if acq in dict(CellLine.AcquisitionMethod.choices):
            cl.acquisition_method = acq

    if want("target_id"):
        tid = (str(data.get("target_id") or "")).strip()
        cl.target = Target.objects.using(db).filter(pk=tid).first() if tid else None

    if want("parent_line_id"):
        pid = (str(data.get("parent_line_id") or "")).strip()
        cl.parent_line = (CellLine.objects.using(db).filter(pk=pid).first()
                          if pid and pid != str(cl.pk) else None)

    _apply_company(cl, data, want, db, mode)

    if want("received_date"):
        rd = (str(data.get("received_date") or "")).strip()
        cl.received_date = parse_date(rd) if rd else None

    _apply_decimal(cl, "in_kind_value", data, "in_kind_value", want)
    if want("in_kind_currency"):
        cl.in_kind_currency = (str(data.get("in_kind_currency") or "GBP")).strip() or "GBP"

    if want("origin_comments"):
        cl.origin_comments = (str(data.get("origin_comments") or "")).strip()
    if want("ko_validation_notes"):
        cl.ko_validation_notes = (str(data.get("ko_validation_notes") or "")).strip()

    return cl


def _apply_company(instance, data, want, db, mode):
    """Resolve the supplier the dedup-safe way (Company.resolve), matching the
    edit views. In patch mode we only touch the company when a company key is
    actually supplied."""
    has_new = "new_company" in data
    has_id = "company_id" in data
    if mode == "patch" and not (has_new or has_id):
        return

    new_company = (str(data.get("new_company") or "")).strip()
    if new_company:
        company, _ = Company.resolve(new_company, db=db)
        instance.company = company
    else:
        cid = (str(data.get("company_id") or "")).strip()
        instance.company = (Company.objects.using(db).filter(pk=cid).first()
                            if cid else None)
