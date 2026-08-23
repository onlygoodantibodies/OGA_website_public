"""Set a target's publication links (F1000 / Zenodo DOIs).

Small write helper behind "the LLM found and verified the missing report DOIs —
add them" without hand-writing a shell command. Fill-only-blank by default so a
re-run never clobbers a link already recorded; ``overwrite=True`` replaces.
``plan()`` is read-only; ``apply()`` writes.
"""
from __future__ import annotations

from django.db import transaction
from django.utils.dateparse import parse_date

from pipeline.models import Report
from pipeline.services.targets import resolve_target

DB = "pipeline_db"


def _resolve(gene, f1000_doi, zenodo_doi, status, f1000_date, zenodo_date):
    errors = []
    target = resolve_target(gene)
    if not target:
        errors.append(f"gene '{gene}' is not in the pipeline")
        return None, None, errors, {}
    report = Report.objects.using(DB).filter(target=target).order_by("id").first()
    proposed = {
        "f1000_doi": (f1000_doi or "").strip(),
        "zenodo_doi": (zenodo_doi or "").strip(),
        "f1000_date": (f1000_date or "").strip(),
        "zenodo_date": (zenodo_date or "").strip(),
        "status": (status or "").strip(),
    }
    if proposed["status"] and proposed["status"] not in dict(Report.ReportStatus.choices):
        errors.append(f"status {proposed['status']!r} is not a valid report status")
    for k in ("f1000_date", "zenodo_date"):
        if proposed[k] and parse_date(proposed[k]) is None:
            errors.append(f"{k} must be YYYY-MM-DD; got {proposed[k]!r}")
    return target, report, errors, proposed


def _changes(report, proposed, overwrite):
    """Which fields would change under fill-only-blank / overwrite rules."""
    changes = {}
    for field, val in proposed.items():
        if not val:
            continue
        current = getattr(report, field) if report else ""
        current = "" if current is None else str(current)
        if overwrite or not current:
            if str(val) != current:
                changes[field] = {"from": current or None, "to": val}
    return changes


def plan(gene, *, f1000_doi="", zenodo_doi="", status="", f1000_date="",
         zenodo_date="", overwrite=False) -> dict:
    target, report, errors, proposed = _resolve(
        gene, f1000_doi, zenodo_doi, status, f1000_date, zenodo_date)
    if errors:
        return {"mode": "plan", "ok": False, "errors": errors}
    return {
        "mode": "plan", "ok": True, "gene": target.gene_name,
        "report_exists": report is not None,
        "changes": _changes(report, proposed, overwrite),
    }


def apply(gene, *, f1000_doi="", zenodo_doi="", status="", f1000_date="",
          zenodo_date="", overwrite=False) -> dict:
    target, report, errors, proposed = _resolve(
        gene, f1000_doi, zenodo_doi, status, f1000_date, zenodo_date)
    if errors:
        return {"mode": "apply", "ok": False, "errors": errors}
    changes = _changes(report, proposed, overwrite)
    with transaction.atomic(using=DB):
        if report is None:
            report = Report(target=target)
        for field, ch in changes.items():
            value = ch["to"]
            if field in ("f1000_date", "zenodo_date"):
                value = parse_date(value)
            setattr(report, field, value)
        report.save(using=DB)
    return {"mode": "apply", "ok": True, "gene": target.gene_name,
            "report_id": report.pk, "changes": changes}
