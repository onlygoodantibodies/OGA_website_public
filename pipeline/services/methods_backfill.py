"""Loading the methods read out of the published reports (30 Sep 2026).

Two writes from one file (`pipeline/data/methods_record_2026_09_30.json`),
``plan`` then ``apply`` like every bulk path here:

1. **The methods records** — one `MethodsRecord` per gene and application with
   a published figure, and an `AntibodyMethod` per published antibody. These
   are what the *Copy methods* button prints (`services/methods_text.py`).
2. **The lab's own records, backfilled** — the session conditions nobody typed
   in (7 of 485 sessions had any), blank dilutions, and the corrections the
   extraction's hypotheses support. The owner accepted those hypotheses on
   30 Sep 2026, so each difference is settled by its group:

   ============================  ==========================================
   ``DB error``                  the lab record is corrected to the report
   ``Not a real conflict``       blanks filled; the stored value stays
   ``Report error``              the lab value stands, and is what is printed
   ``Different run or panel``    both stand; the report's value is printed
   ``Unresolved``                both stand; the report's value is printed
   ============================  ==========================================

Rules the write keeps, from CLAUDE.md:

* **Fill-only-blank.** A value already in a lab record is never replaced —
  except a ``DB error``, and then only where the cell *still* holds the value
  the extraction saw ("a diff is applied only where it still matches what it
  claims to correct"). Anything else is named and left alone.
* **No structure is changed here.** Splitting a session that holds two runs,
  creating a session and adding result rows is `methods_sessions`, which the
  owner asked for on 1 Oct 2026 and which decides from the live rows rather
  than from the extraction's labels.
* **The report value, not a guess.** A per-antibody value comes from the
  report that the gene's published figures come from; where the report gives
  none, from the lab record only when that record holds exactly one value.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.db import transaction

from pipeline.dedup_utils import normcat
from pipeline.services import methods_text

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "methods_record_2026_09_30.json"

# The extraction speaks the bench's `IF`; a figure says `ICC-IF`.
FIGURE_APP = {"WB": "WB", "IP": "IP", "IF": "ICC-IF", "FC": "FC", "IHC": "IHC"}

BASIS = {
    "Antibody named with value": "report_named",
    "IP protocol amount (same for all antibodies)": "report_protocol",
    "Protocol amount (same for all antibodies)": "report_protocol",
    "General statement (antibody not named with value)": "report_general",
    "VALUE NOT FOUND NEXT TO ANTIBODY — check": "report_general",
}

# Which result columns a per-antibody value lands in.
ROW_FIELDS = {
    "dilution + primary_ab_dilution": ("dilution", "primary_ab_dilution"),
    "amount_of_antibody": ("amount_of_antibody",),
    "best_concentration (row already holds tested concentrations)": ("best_concentration",),
    "primary_ab_dilution": ("primary_ab_dilution",),
    "concentration": ("concentration",),
    "concentration_1 / concentration_2 / best_concentration": ("best_concentration",),
}

_ROW_REF = re.compile(r"pipeline_(wb|ip|if|fc|ihc)result #(\d+)")
_DRAFT = re.compile(r"_v\d|draft| v\d", re.I)


def _models():
    from pipeline.models import FcResult, IfResult, IhcResult, IpResult, WbResult
    return {"wb": WbResult, "ip": IpResult, "if": IfResult, "fc": FcResult, "ihc": IhcResult}


def _lab_amount(value: str) -> str:
    """The lab's spelling, made to read like the reports': ``1in700`` → ``1/700``."""
    return re.sub(r"^\s*1\s*in\s*", "1/", (value or "").strip(), flags=re.I)


@dataclass
class Plan:
    records: list = field(default_factory=list)          # dicts
    antibody_methods: list = field(default_factory=list)  # dicts
    session_fills: list = field(default_factory=list)     # (session, key, value)
    session_loading: list = field(default_factory=list)   # (session, Decimal)
    row_fills: list = field(default_factory=list)         # (row, field, old, new, why)
    left: dict = field(default_factory=dict)              # reason -> [sentence]

    def leave(self, reason: str, sentence: str):
        self.left.setdefault(reason, []).append(sentence)


def load(path=DATA_FILE) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _target(gene: str):
    from pipeline.models import Target
    exact = list(Target.objects.filter(gene_name=gene))
    if len(exact) == 1:
        return exact[0]
    loose = list(Target.objects.filter(gene_name__iexact=gene))
    return loose[0] if len(loose) == 1 else None


def plan(data: dict, gene: str | None = None) -> Plan:
    from pipeline.models import (AntibodyMethod, ExperimentSession, MethodsRecord,
                                 PublicationImage)
    p = Plan()
    runs = [r for r in data["run_conditions"]
            if not gene or r["gene"].upper() == gene.upper()]
    values = [a for a in data["antibody_values"]
              if not gene or a["gene"].upper() == gene.upper()]
    diffs = data["dilution_differences"]
    reports = data["reports"]

    # ── 1. Methods records ──────────────────────────────────────────────
    by_gene_app: dict = {}
    for r in runs:
        by_gene_app.setdefault((r["gene"].upper(), r["app"]), []).append(r)
    values_by: dict = {}
    for a in values:
        values_by.setdefault((a["gene"].upper(), a["app"]), []).append(a)

    targets: dict = {}
    for (g, app), candidates in sorted(by_gene_app.items()):
        target = targets.setdefault(g, _target(candidates[0]["gene"]))
        fig_app = FIGURE_APP[app]
        if target is None:
            p.leave("gene not on file", f"{g}: no target of that name")
            continue
        published = list(PublicationImage.objects
                         .filter(antibody__target=target, application_type=fig_app)
                         .select_related("antibody"))
        if not published:
            p.leave("no published figure", f"{g} {fig_app}")
            continue
        pub_cats = {normcat(img.antibody.catalogue_number) for img in published}
        mine = values_by.get((g, app), [])

        def score(run):
            hits = sum(1 for a in mine if a["report_id"] == run["report_id"]
                       and normcat(a["catalogue"]) in pub_cats)
            name = run["report_file"]
            return (hits, not _DRAFT.search(name), run["report_year"] or "",
                    name.lower().endswith(".pdf"))
        chosen = max(candidates, key=score)
        conditions = methods_text.clean_conditions(fig_app, chosen["conditions"])
        existing = MethodsRecord.objects.filter(target=target, application=fig_app).first()
        status = ("new" if existing is None else
                  "same" if existing.conditions == conditions else "changed")
        p.records.append(dict(target=target, application=fig_app, conditions=conditions,
                              report_file=chosen["report_file"],
                              source_ref=chosen["record_id"], status=status,
                              others=len(candidates) - 1))

        # Per-antibody values, for every antibody with a published figure.
        for img in published:
            ab = img.antibody
            cat = normcat(ab.catalogue_number)
            options = [a for a in mine if normcat(a["catalogue"]) == cat and a["report_value"]
                       and a["evidence_type"] in BASIS]
            options.sort(key=lambda a: (a["report_id"] == chosen["report_id"],
                                        reports.get(a["report_id"], {}).get("year", "")),
                         reverse=True)
            amount = basis = where = ref = ""
            if options:
                a = options[0]
                d = diffs.get(a["record_id"])
                if d and d["group"] == "Report error" and d["db_value"]:
                    amount, basis = _lab_amount(d["db_value"]), "lab_record"
                else:
                    amount, basis = a["report_value"].strip(), BASIS[a["evidence_type"]]
                where, ref = a["where"], a["record_id"]
            else:
                amount = _single_lab_value(ab, fig_app)
                basis = "lab_record" if amount else ""
            if not amount:
                continue
            current = (AntibodyMethod.objects.filter(antibody=ab, record__target=target,
                                                     record__application=fig_app)
                       .order_by("-updated_at", "-id").first())
            ab_status = ("new" if current is None else
                         "same" if (current.amount, current.basis) == (amount, basis)
                         else "changed")
            p.antibody_methods.append(dict(target=target, application=fig_app, antibody=ab,
                                           amount=amount, basis=basis, where=where[:255],
                                           source_ref=ref, status=ab_status))

    # ── 2. Lab records ──────────────────────────────────────────────────
    models = _models()
    targets_map = data["run_column_targets"]
    report_rows: dict = {}
    for a in values:
        for kind, pk in _ROW_REF.findall(a.get("db_target") or ""):
            report_rows.setdefault((a["report_id"], a["app"]), set()).add((kind, int(pk)))

    for r in runs:
        if r["action"] != "FILL":
            continue  # sessions to create or split: `methods_sessions`
        try:
            session = ExperimentSession.objects.select_related("target").get(pk=int(r["session_ids"]))
        except (ValueError, ExperimentSession.DoesNotExist):
            p.leave("session not found", f"{r['gene']} {r['app']}: session {r['session_ids']}")
            continue
        if (session.target.gene_name or "").upper() != r["gene"].upper() or \
                session.procedure_type != r["app"]:
            p.leave("session is another gene or procedure",
                    f"session #{session.pk} is {session.target} {session.procedure_type}, "
                    f"the report says {r['gene']} {r['app']}")
            continue
        conds = dict(session.session_conditions or {})
        cleaned = {k: methods_text.clean(v) for k, v in r["conditions"].items()}
        for src_key, where_to in targets_map[r["app"]].items():
            value = cleaned.get(src_key)
            if not value or not where_to:
                continue
            for dest in (d.strip() for d in where_to.replace("+", ";").split(";")):
                if dest.startswith("session_conditions."):
                    key = dest.split(".", 1)[1]
                    if not str(conds.get(key) or "").strip():
                        p.session_fills.append((session, key, value))
                elif dest == "protein_loading_ug":
                    number = _number(value)
                    if number is not None and session.protein_loading_ug is None:
                        p.session_loading.append((session, number))
                elif "result." in dest:
                    kind, col = dest.split("result.", 1)
                    rows = [pk for k, pk in report_rows.get((r["report_id"], r["app"]), ())
                            if k == kind]
                    for row in models[kind].objects.filter(pk__in=rows, session=session):
                        if not str(getattr(row, col) or "").strip():
                            p.row_fills.append((row, col, "", value, "run condition"))

    for a in values:
        refs = _ROW_REF.findall(a.get("db_target") or "")
        cols = ROW_FIELDS.get(a.get("db_field") or "")
        value = (a.get("report_value") or "").strip()
        if a["action"] == "ANTIBODY NOT IN DB":
            p.leave("antibody not in db", f"{a['gene']} {a['app']} {a['catalogue']}")
            continue
        if a["action"] == "CREATE ROW":
            continue  # a row the run lacks: `methods_sessions` adds it
        if not (refs and cols and value) or a["evidence_type"] not in BASIS:
            continue
        detail = a.get("action_detail", "")
        diff = diffs.get(a["record_id"])
        overwrite = bool(diff and diff["group"] == "DB error")
        if a["action"] == "UPDATE ROW" and "agrees" in detail:
            continue
        if a["action"].startswith("CHECK") and not diff:
            continue
        if diff and diff["group"] not in ("DB error", "Not a real conflict"):
            continue
        for kind, pk in refs:
            row = models[kind].objects.filter(pk=int(pk)).select_related("antibody").first()
            if row is None:
                p.leave("result row not found", f"{a['gene']} {a['catalogue']}: {kind} #{pk}")
                continue
            if normcat(row.antibody.catalogue_number) != normcat(a["catalogue"]):
                p.leave("row is another antibody",
                        f"{kind} #{pk} is {row.antibody.catalogue_number}, the report says {a['catalogue']}")
                continue
            for col in cols:
                old = str(getattr(row, col) or "").strip()
                if not old:
                    p.row_fills.append((row, col, "", value, "blank filled"))
                elif overwrite and old != value:
                    if old == (diff["db_value"] or "").strip():
                        p.row_fills.append((row, col, old, value, "corrected (DB error)"))
                    else:
                        p.leave("changed since the extraction",
                                f"{a['gene']} {a['catalogue']} {kind} #{pk} {col}: now "
                                f"{old!r}, the extraction saw {diff['db_value']!r}")
    # One fill per cell — a row named by two report values keeps the first.
    seen, unique = set(), []
    for fill in p.row_fills:
        key = (type(fill[0]).__name__, fill[0].pk, fill[1])
        if key not in seen:
            seen.add(key)
            unique.append(fill)
    p.row_fills = unique
    return p


def _number(value: str):
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(?:µg|ug)?\s*", value or "")
    if not m:
        return None
    try:
        return Decimal(m.group(1))
    except InvalidOperation:
        return None


def _single_lab_value(antibody, fig_app: str) -> str:
    """The lab record's value when it holds exactly one — never a choice."""
    if fig_app == "WB":
        vals = {v for row in antibody.wb_results.all()
                for v in (row.dilution, row.primary_ab_dilution) if v and v.strip()}
    elif fig_app == "IP":
        vals = {row.amount_of_antibody for row in antibody.ip_results.all()
                if row.amount_of_antibody.strip()}
    elif fig_app == "ICC-IF":
        vals = {row.best_concentration for row in antibody.if_results.all()
                if row.best_concentration.strip()}
    elif fig_app == "FC":
        vals = {row.concentration for row in antibody.fc_results.all()
                if row.concentration.strip()}
    else:
        vals = set()
    vals = {_lab_amount(v) for v in vals}
    return vals.pop() if len(vals) == 1 else ""


@transaction.atomic(using="pipeline_db")
def apply(p: Plan) -> dict:
    from pipeline.models import AntibodyMethod, ExperimentSession, MethodsRecord
    counts = dict(records=0, antibody_methods=0, session_conditions=0,
                  protein_loading=0, result_cells=0)
    records = {}
    for rec in p.records:
        obj, _ = MethodsRecord.objects.update_or_create(
            target=rec["target"], application=rec["application"],
            defaults=dict(conditions=rec["conditions"], report_file=rec["report_file"],
                          source_ref=rec["source_ref"]))
        records[(rec["target"].pk, rec["application"])] = obj
        counts["records"] += rec["status"] != "same"
    for m in p.antibody_methods:
        record = records[(m["target"].pk, m["application"])]
        AntibodyMethod.objects.filter(record=record, antibody=m["antibody"]).delete()
        AntibodyMethod.objects.create(record=record, antibody=m["antibody"],
                                      amount=m["amount"][:120], basis=m["basis"],
                                      where=m["where"], source_ref=m["source_ref"])
        counts["antibody_methods"] += m["status"] != "same"
    # Fresh copies: the plan's promise is about what was blank when it was
    # read, and a session edited on the board since keeps its edit.
    ids = {s.pk for s, *_ in p.session_fills} | {s.pk for s, _ in p.session_loading}
    sessions = {s.pk: s for s in ExperimentSession.objects.filter(pk__in=ids)}
    for session, key, value in p.session_fills:
        s = sessions[session.pk]
        conds = dict(s.session_conditions or {})
        if not str(conds.get(key) or "").strip():
            conds[key] = value
            s.session_conditions = conds
            counts["session_conditions"] += 1
    for session, number in p.session_loading:
        s = sessions[session.pk]
        if s.protein_loading_ug is None:
            s.protein_loading_ug = number
            counts["protein_loading"] += 1
    for s in sessions.values():
        s.save(update_fields=["session_conditions", "protein_loading_ug"])
    for row, col, old, new, _why in p.row_fills:
        fresh = type(row).objects.get(pk=row.pk)
        if str(getattr(fresh, col) or "").strip() != old:
            continue  # moved under us; the plan was a promise about `old`
        setattr(fresh, col, new[:getattr(type(fresh), col).field.max_length or 255])
        fresh.save(update_fields=[col])
        counts["result_cells"] += 1
    return counts
