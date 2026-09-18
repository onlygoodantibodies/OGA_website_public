"""Record an experiment session (WB/IP/IF/FC) + its per-antibody results.

This is the write-spine behind "explain my day at the bench in natural language
and have it land in the pipeline": an LLM turns the narrative into a structured
payload, and this service resolves the references (gene → Target, username →
Member, cell-line/antibody names → rows), validates, and creates the
``ExperimentSession`` + child result rows in one transaction.

Before this, the only path was ``views/session_entry.py::_handle_session_post``
(bound to the multi-step form). That logic now lives here so the view and MCP
Server B share one implementation. ``plan()`` is read-only (dry-run); ``apply()``
writes.

Payload shape (all name fields are resolved case-insensitively)::

    {
      "procedure_type": "WB",              # WB | IP | IF | FC   (required)
      "gene": "SNCA",                      # or "target_id": 12  (required)
      "date": "2026-01-15",                # required
      "experimenter": "sara",              # username; or "experimenter_id"; or the actor
      "site": "MTL",                       # short_code/name; or "site_id"; or actor's site
      "status": "complete",                # ExperimentSession.SessionStatus (default in_progress)
      "comments": "...",
      "conditions": {"gel": "4-12%"},      # session_conditions
      "cell_line_wt": "HAP1 WT",           # name; or "cell_line_wt_id"
      "cell_line_ko": "HAP1 SNCA-KO",
      "results": [
        {"antibody": "ab212184",           # catalogue / RRID / id / {catalogue,company}
         "rating": "Recommended", "signal": "clean single band", "dilution": "1/1000",
         "selective": true}                # optional: also set <app>_recommended on the antibody
      ]
    }
"""
from __future__ import annotations

from datetime import date as _date

from django.db import transaction
from django.utils.dateparse import parse_date

from pipeline.models import (
    Antibody, CellLine, ExperimentSession, Member, Site, Target,
    WbResult, IpResult, IfResult, FcResult,
)
from pipeline.services import lab_numbers
from pipeline.services.targets import resolve_target

DB = "pipeline_db"

RESULT_MODEL_MAP = {"WB": WbResult, "IP": IpResult, "IF": IfResult, "FC": FcResult}
APP_REC_FIELD = {"WB": "wb_recommended", "IP": "ip_recommended",
                 "IF": "if_recommended", "FC": "fc_recommended"}

# Result fields we never accept from the payload (set by the system / relations).
_RESULT_RESERVED = {"id", "session", "session_id", "antibody", "antibody_id",
                    "access_id", "created_at", "updated_at"}


def _result_field_names(procedure_type):
    """Concrete result-model fields the payload may set (names + FK attnames),
    minus the system/relation fields."""
    model = RESULT_MODEL_MAP[procedure_type]
    names = set()
    for f in model._meta.fields:
        names.add(f.name)
        names.add(f.attname)
    return names - _RESULT_RESERVED


def _resolve_site(payload, member):
    if payload.get("site_id"):
        site = Site.objects.using(DB).filter(pk=payload["site_id"]).first()
        return site, (None if site else f"site_id {payload['site_id']} not found")
    val = (payload.get("site") or "").strip()
    if val:
        site = (Site.objects.using(DB).filter(short_code__iexact=val).first()
                or Site.objects.using(DB).filter(name__iexact=val).first())
        return site, (None if site else f"site '{val}' not found")
    if member is not None and getattr(member, "site_id", None):
        return Site.objects.using(DB).filter(pk=member.site_id).first(), None
    return None, "site is required (no site given and the actor has none)"


def _resolve_experimenter(payload, member):
    if payload.get("experimenter_id"):
        m = Member.objects.using(DB).filter(pk=payload["experimenter_id"]).first()
        return m, (None if m else f"experimenter_id {payload['experimenter_id']} not found")
    username = (payload.get("experimenter") or "").strip()
    if username:
        from django.contrib.auth.models import User
        # id-only select: never pull the full User row (password hash included)
        # just to resolve a username. Originally required by the retired mcp_tools
        # role's column-level grant; kept because it is the right default anyway.
        user_id = (User.objects.using(DB).filter(username__iexact=username)
                   .values_list("pk", flat=True).first())
        m = (Member.objects.using(DB).filter(user_id=user_id).first() if user_id else None)
        return m, (None if m else f"experimenter '{username}' has no pipeline member")
    if member is not None:
        return member, None
    return None, "experimenter is required (none given and no actor)"


def _resolve_target(payload):
    if payload.get("target_id"):
        t = Target.objects.using(DB).filter(pk=payload["target_id"]).first()
        return t, (None if t else f"target_id {payload['target_id']} not found")
    gene = (payload.get("gene") or "").strip()
    if not gene:
        return None, "gene (or target_id) is required"
    t = resolve_target(gene)
    return t, (None if t else
               f"gene '{gene}' is not in the pipeline — add it first (resolve_or_create_target)")


def _resolve_cell_line(value, *, genotype=None, site_id=None):
    """`(line, error)` for a cell line named on a session save.

    Through `services/cell_lines.py`, for two reasons this used to get wrong in
    opposite directions at once. It matched the **bare name only**, so the
    board's own rendering — `SH-SY5Y — Leicester`, the string on screen and
    therefore the string a person retypes into the cell — matched nothing and
    came back *"cell line 'SH-SY5Y — Leicester' not found"*, about a line that
    plainly exists. And retyping the bare `SH-SY5Y` instead re-resolved to the
    same arbitrary row of five, so a mis-set wild type could not be corrected
    from the surface displaying it: one spelling was refused, the other was a
    silent no-op.

    `genotype` is passed by the callers that know which slot is being filled, so
    a wild-type cell cannot be filled with a knockout.
    """
    from pipeline.services import cell_lines as clines
    return clines.resolve(value, genotype=genotype, site_id=site_id)


def _resolve_antibody(target, spec):
    """Resolve a result's antibody spec to a row within ``target``.

    A bare string is treated as a catalogue number (or an RRID if it starts
    ``AB_``) — never as a primary key, because catalogue numbers are frequently
    all-digits (e.g. CST "2642"). To reference by id, pass an int or {"id": n}.
    """
    if isinstance(spec, int):
        ab = Antibody.objects.using(DB).filter(pk=spec).first()
        return ab, (None if ab else f"antibody id {spec} not found")
    catalogue = company = rrid = ""
    if isinstance(spec, dict):
        if spec.get("id"):
            ab = Antibody.objects.using(DB).filter(pk=spec["id"]).first()
            return ab, (None if ab else f"antibody id {spec['id']} not found")
        catalogue = (spec.get("catalogue") or spec.get("catalogue_number") or "").strip()
        company = (spec.get("company") or "").strip()
        rrid = (spec.get("rrid") or "").strip()
    else:
        s = (spec or "").strip()
        if s.upper().startswith("AB_"):
            rrid = s
        else:
            catalogue = s
    qs = Antibody.objects.using(DB).filter(target=target)
    if rrid:
        qs = qs.filter(rrid__iexact=rrid)
    if catalogue:
        qs = qs.filter(catalogue_number__iexact=catalogue)
    if company:
        qs = qs.filter(company__name__icontains=company)
    matches = list(qs[:3])
    if not matches:
        return None, f"no antibody matching {spec!r} on target {target.gene_name}"
    if len(matches) > 1:
        return None, (f"{spec!r} matches {len(matches)} antibodies on "
                      f"{target.gene_name} — add a company to disambiguate")
    return matches[0], None


def _resolve(payload, member):
    """Resolve every reference in the payload. Returns (resolved, errors)."""
    errors = []
    proc = (payload.get("procedure_type") or "").strip().upper()
    if proc not in RESULT_MODEL_MAP:
        errors.append(f"procedure_type must be one of {sorted(RESULT_MODEL_MAP)}; got {proc!r}")

    target, e = _resolve_target(payload)
    if e:
        errors.append(e)
    site, e = _resolve_site(payload, member)
    if e:
        errors.append(e)
    experimenter, e = _resolve_experimenter(payload, member)
    if e:
        errors.append(e)

    raw_date = (payload.get("date") or "").strip()
    parsed_date = parse_date(raw_date) if raw_date else None
    if not parsed_date:
        errors.append(f"date is required and must be YYYY-MM-DD; got {raw_date!r}")

    # Each slot says which kind it is, so a wild-type cell cannot be filled with
    # a knockout — the mistake the quick New-session panel made silently. The
    # site is a preference, not a filter: your own bench's line wins a shared
    # name, another site's is still reachable by naming it after the dash.
    site_pref = getattr(member, "site_id", None) or payload.get("site_id")
    wt, e = _resolve_cell_line(payload.get("cell_line_wt"),
                               genotype="WT", site_id=site_pref)
    if e:
        errors.append(e)
    ko, e = _resolve_cell_line(payload.get("cell_line_ko"),
                               genotype="KO", site_id=site_pref)
    if e:
        errors.append(e)

    status = (payload.get("status") or "in_progress").strip()
    if status not in dict(ExperimentSession.SessionStatus.choices):
        errors.append(f"status {status!r} is not a valid session status")

    fc_sub = (payload.get("fc_sub_protocol") or "na").strip()
    if fc_sub not in dict(ExperimentSession.FcSubProtocol.choices):
        fc_sub = "na"
    protocol_template_id = payload.get("protocol_template_id") or None

    # Results
    allowed = _result_field_names(proc) if proc in RESULT_MODEL_MAP else set()
    results = []
    for i, r in enumerate(payload.get("results") or []):
        ab, e = _resolve_antibody(target, r.get("antibody")) if target else (None, "no target")
        if e:
            errors.append(f"result[{i}]: {e}")
            continue
        fields, unknown = {}, []
        for k, v in r.items():
            if k in ("antibody", "selective"):
                continue
            if k in allowed:
                fields[k] = v
            else:
                unknown.append(k)
        if unknown:
            errors.append(f"result[{i}] ({ab}): unknown {proc} fields {unknown}")
        selective = r.get("selective")
        results.append({"antibody": ab, "fields": fields, "selective": selective})

    resolved = {
        "procedure_type": proc, "target": target, "site": site,
        "experimenter": experimenter, "date": parsed_date, "status": status,
        "fc_sub_protocol": fc_sub, "protocol_template_id": protocol_template_id,
        "cell_line_wt": wt, "cell_line_ko": ko,
        "conditions": payload.get("conditions") or {},
        "comments": (payload.get("comments") or "").strip(),
        "results": results,
    }
    return resolved, errors


def plan(payload, member=None) -> dict:
    """Dry-run: resolve everything and describe what would be written."""
    resolved, errors = _resolve(payload, member)
    rec_changes = []
    for r in resolved.get("results", []):
        if isinstance(r.get("selective"), bool):
            field = APP_REC_FIELD[resolved["procedure_type"]]
            ab = r["antibody"]
            if getattr(ab, field) != r["selective"]:
                rec_changes.append({"antibody": str(ab), "field": field,
                                    "from": getattr(ab, field), "to": r["selective"]})
    return {
        "mode": "plan",
        "ok": not errors,
        "errors": errors,
        "would_create": {
            "procedure_type": resolved["procedure_type"],
            "target": resolved["target"].gene_name if resolved["target"] else None,
            "experimenter": str(resolved["experimenter"]) if resolved["experimenter"] else None,
            "site": resolved["site"].short_code if resolved["site"] else None,
            "date": str(resolved["date"]) if resolved["date"] else None,
            "status": resolved["status"],
            "results": [{"antibody": str(r["antibody"]), "fields": r["fields"]}
                        for r in resolved["results"]],
        },
        "recommendation_changes": rec_changes,
    }


def apply(payload, member=None) -> dict:
    """Create the session + result rows (+ optional recommendation-flag updates)
    in one transaction. Returns an error dict (no write) if resolution fails."""
    resolved, errors = _resolve(payload, member)
    if errors:
        return {"mode": "apply", "ok": False, "errors": errors}

    proc = resolved["procedure_type"]
    ResultModel = RESULT_MODEL_MAP[proc]
    rec_field = APP_REC_FIELD[proc]
    created_results, rec_changes = [], []

    with transaction.atomic(using=DB):
        session = ExperimentSession(
            procedure_type=proc,
            target_id=resolved["target"].pk,
            experimenter_id=resolved["experimenter"].pk,
            site_id=resolved["site"].pk,
            date=resolved["date"],
            status=resolved["status"],
            fc_sub_protocol=resolved["fc_sub_protocol"],
            protocol_template_id=resolved["protocol_template_id"],
            session_conditions=resolved["conditions"],
            comments=resolved["comments"],
            cell_line_wt_id=resolved["cell_line_wt"].pk if resolved["cell_line_wt"] else None,
            cell_line_ko_id=resolved["cell_line_ko"].pk if resolved["cell_line_ko"] else None,
        )
        session.planned_date = _date.today()
        session.save(using=DB)

        # **A number exists before the experiment.** An antibody logged on the
        # board is created without one, so a bench can deal them out in one go
        # and keep a protein's vials in one box (`services/renumber.py`).
        # Planning is where that stops: this session's bench sheet prints
        # `lab_numbers.sheet_number`, and a blank cell there is a tube nobody
        # can identify. Issued in the order the session lists them, and named
        # in the reply — a number the app gives a record is a thing the app did.
        numbers_issued = lab_numbers.ensure_numbered(
            [r["antibody"] for r in resolved["results"]], db=DB)

        for r in resolved["results"]:
            ab = r["antibody"]
            row = ResultModel(session=session, antibody_id=ab.pk, **r["fields"])
            row.save(using=DB)
            created_results.append({"antibody": str(ab), "id": row.pk})
            if isinstance(r.get("selective"), bool) and getattr(ab, rec_field) != r["selective"]:
                setattr(ab, rec_field, r["selective"])
                ab.save(using=DB, update_fields=[rec_field])
                rec_changes.append({"antibody": str(ab), "field": rec_field, "to": r["selective"]})

    return {
        "mode": "apply", "ok": True, "session_id": session.pk,
        "procedure_type": proc, "created_results": created_results,
        "recommendation_changes": rec_changes,
        "numbers_issued": numbers_issued,
    }
