"""Entering a gene's published methods by hand — the web door to `MethodsRecord`.

Until 2 Oct 2026 the only writer was ``manage.py load_methods_record``, which
reads a data file in the repo: a gene's methods reached the *Copy methods*
button only through a code change, a deploy and a Render Shell command. This is
the page a member signed in to the pipeline uses instead (`/pipeline/methods/`),
one gene and one application at a time, typed from the report.

``plan`` then ``apply``, like every write path here:

* **The preview is the paragraph the button will copy.** ``plan`` cleans the
  typed conditions with `methods_text.clean_conditions` — what is stored is
  what is printed — and composes each antibody's paragraph with
  `methods_text.text`, the one writer for those words. A value the cleaning
  drops (an extractor's note, a figure reference) is named, never lost quietly.
* **A blank removes.** The form opens holding what is stored, so emptying a box
  is a person correcting a value, the same reasoning as a DOI cell
  (CLAUDE.md, *A URLField validates nothing*). Every removal is listed in the
  preview and counted on the receipt.
* **A preview is not a permission slip.** ``plan`` stamps what is stored now;
  ``apply`` refuses when the record moved underneath (another person saved),
  so nobody overwrites an edit they never saw.

What it does not decide: whether the button shows. That stays
`methods_text.for_antibodies` — a released figure under a **supportive**
result — so entering methods for a gene in the review queue is safe and
reaches no page until the meeting releases it.
"""
from __future__ import annotations

import hashlib
import json

from django.db import transaction

from pipeline.services import methods_text as M

DB = "pipeline_db"

# The figure vocabulary, in the order the gene page draws it.
APPLICATIONS = M.APPLICATIONS

# What each condition key means to the person typing it, and an example of the
# shape the paragraph expects. The keys are `methods_text.CONDITION_KEYS` — a
# key the composer does not know is never offered, since it would be stored
# and never printed.
LABELS = {
    "cell_lines": ("Cells", "e.g. HAP1 WT and DHX58 KO, or THP-1 ctrl and DHX58 KD (siRNA …)"),
    "lysis_buffer": ("Lysis buffer", "e.g. RIPA buffer (Thermo Fisher 89901) + protease inhibitor"),
    "protein_loading_ug": ("Protein loaded (µg)", "e.g. 60"),
    "gel_chemistry": ("Gel", "e.g. precast midi 4-20% Tris-Glycine (Thermo Fisher WXP42012BOX)"),
    "membrane": ("Membrane", "e.g. nitrocellulose"),
    "transfer_method": ("Transfer", "e.g. iBlot 2"),
    "blocking": ("Blocking", "e.g. 5% milk, 1 hr"),
    "primary_incubation": ("Primary incubation", "e.g. O/N at 4°C in 5% milk in TBST"),
    "secondary_antibody": ("Secondary antibody",
                           "e.g. HRP-goat anti-rabbit (Proteintech RGAR001) and anti-mouse (RGAM001)"),
    "secondary_dilution": ("Secondary dilution or concentration",
                           "e.g. 0.05 µg/ml (anti-rabbit), 0.5 µg/ml (anti-mouse)"),
    "ecl_type": ("ECL", "e.g. Pierce ECL (Thermo Fisher 32106)"),
    "imaging_system": ("Imaging system", "e.g. iBright CL1500 (Thermo Fisher A44240)"),
    "exposure_time": ("Exposure time", ""),
    "protein_amount_mg": ("Lysate per IP (mg)", "e.g. 0.5"),
    "protein_concentration": ("Lysate concentration", "e.g. 1 mg/ml"),
    "lysate_volume_ml": ("Lysate volume (ml)", "e.g. 0.5"),
    "bead_type": ("Beads", "e.g. 30 µl Dynabeads protein A (rabbit) or protein G (mouse)"),
    "incubation": ("IP incubation and washes", "e.g. 1 hr at 8°C; beads washed 3x in 1.0 ml IP buffer"),
    "fractions_loaded": ("Fractions loaded", "e.g. SM=6% starting material; UB=6% unbound fraction; IP=immunoprecipitate"),
    "gel": ("Gel", "e.g. precast midi 4-20% Tris-Glycine"),
    "detection_antibody": ("Antibody the IP was probed with", "e.g. MA5-31717"),
    "detection_antibody_dilution": ("Its dilution", "e.g. 1/500"),
    "ecl": ("ECL", "e.g. Pierce ECL (Thermo Fisher 32106)"),
    "detection_system": ("Imaging system", "e.g. iBright CL1500"),
    "mosaic_labelling": ("Mosaic labelling", "e.g. WT labelled with CellTracker green, KO with deep red"),
    "fixation": ("Fixation", "e.g. 4% PFA, 15 min"),
    "permeabilisation": ("Permeabilisation", "e.g. 0.1% Triton X-100"),
    "dilution_buffer": ("Antibody dilution buffer", "e.g. 1% BSA in PBS"),
    "primary_condition": ("Primary incubation", "e.g. O/N at 4°C"),
    "secondary_ab": ("Secondary antibody", "e.g. Alexa Fluor 555 goat anti-rabbit (Thermo Fisher A-21429)"),
    "secondary_condition": ("Secondary incubation", "e.g. 1 hr at room temperature"),
    "nuclear_stain": ("Nuclear stain", "e.g. DAPI"),
    "microscope": ("Microscope", "e.g. ImageXpress Micro Confocal"),
    "objective": ("Objective", "e.g. 20x"),
    "analysis": ("Analysis", ""),
    "tracker_dyes": ("Tracker dyes", "e.g. CellTracker green (WT) and violet (KO)"),
    "live_or_fixed": ("Live or fixed", "e.g. fixed"),
    "flow_cytometer": ("Flow cytometer", "e.g. Attune NxT"),
    "analysis_software": ("Analysis software", "e.g. FlowJo"),
    "tissue_or_cells": ("Tissue or cells", "e.g. HAP1 WT and KO cell pellets"),
    "antigen_retrieval": ("Antigen retrieval", "e.g. CC1, 64 min"),
    "counterstain": ("Counterstain", "e.g. haematoxylin"),
    "scanner": ("Scanner", "e.g. Aperio"),
}



def bases() -> list:
    """``[(value, label)]`` for the basis picker, blank first."""
    from pipeline.models import AntibodyMethod
    return [("", "—")] + [(b.value, b.label) for b in AntibodyMethod.Basis]

# What the per-antibody value is, per application — the word over the column.
AMOUNT_LABEL = {"WB": "Dilution", "IP": "Antibody per IP", "ICC-IF": "Dilution",
                "FC": "Concentration", "IHC": "Dilution"}


class MethodsRefusal(Exception):
    """A save that cannot happen, in a sentence for the page."""


def target_for(gene: str):
    from pipeline.services.methods_backfill import _target
    return _target(gene.strip()) if gene and gene.strip() else None


def fields(app: str) -> list[dict]:
    return [{"key": k, "label": LABELS.get(k, (k, ""))[0], "hint": LABELS.get(k, ("", ""))[1]}
            for k in M.CONDITION_KEYS.get(app, ())]


def _figures(target, app):
    """``{antibody_id: "published" | "in review"}`` for this application."""
    from pipeline.models import PendingPublicationImage, PublicationImage
    out = {}
    for ab_id in (PendingPublicationImage.objects.using(DB)
                  .filter(antibody__target=target, application_type=app,
                          released_at__isnull=True)
                  .values_list("antibody_id", flat=True)):
        out[ab_id] = "in review"
    for ab_id in (PublicationImage.objects.using(DB)
                  .filter(antibody__target=target, application_type=app)
                  .values_list("antibody_id", flat=True)):
        out[ab_id] = "published"
    return out


def _antibodies(target, app):
    """The gene's antibodies, those with a figure in this application first."""
    figures = _figures(target, app)
    abs_ = list(target.antibodies.using(DB).select_related("company", "site")
                .order_by("catalogue_number", "pk"))
    abs_.sort(key=lambda a: (a.pk not in figures, a.catalogue_number.lower(), a.pk))
    return abs_, figures


def _stored_values(record):
    """``{antibody_id: AntibodyMethod}`` — the newest row per antibody, as the
    reader takes it (`methods_text.for_antibodies`)."""
    from pipeline.models import AntibodyMethod
    if record is None:
        return {}
    out = {}
    for m in (AntibodyMethod.objects.using(DB).filter(record=record)
              .order_by("updated_at", "id")):
        out[m.antibody_id] = m
    return out


def _record(target, app):
    from pipeline.models import MethodsRecord
    return MethodsRecord.objects.using(DB).filter(target=target, application=app).first()


def stamp(target, app) -> str:
    """What is stored now, in a few characters: the record and its antibody
    rows. A save compares it with the stamp its preview was made from."""
    record = _record(target, app)
    if record is None:
        return "none"
    rows = sorted((m.antibody_id, m.amount, m.basis, m.where)
                  for m in _stored_values(record).values())
    raw = json.dumps([record.conditions, record.report_file, rows], sort_keys=True,
                     default=str)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _source(target):
    from pipeline.models import Report
    return M.source_for(list(Report.objects.using(DB).filter(target=target)))


def state(target, app) -> dict:
    """What the page opens with: what is stored, and the gene's antibodies."""
    record = _record(target, app)
    values = _stored_values(record)
    abs_, figures = _antibodies(target, app)
    return {
        "gene": target.gene_name,
        "application": app,
        "amount_label": AMOUNT_LABEL[app],
        "fields": fields(app),
        "conditions": dict(record.conditions) if record else {},
        "report_file": record.report_file if record else "",
        "saved_by": record.source_ref if record else "",
        "saved_at": record.updated_at.isoformat() if record else "",
        "antibodies": [{
            "id": ab.pk,
            "label": M.antibody_label(ab),
            "catalogue": ab.catalogue_number,
            "site": ab.site.name if ab.site_id else "",
            "host": ab.host_species or "",
            "figure": figures.get(ab.pk, ""),
            "amount": values[ab.pk].amount if ab.pk in values else "",
            "basis": values[ab.pk].basis if ab.pk in values else "",
            "where": values[ab.pk].where if ab.pk in values else "",
        } for ab in abs_],
        "source": _source(target),
        "stamp": stamp(target, app),
        "bases": bases(),
    }


def plan(target, app, payload: dict) -> dict:
    """What a save of ``payload`` would store, and the paragraph it prints.

    ``payload``: ``{"conditions": {key: text}, "report_file": text,
    "antibodies": [{"id", "amount", "basis", "where"}]}``.
    """
    if app not in APPLICATIONS:
        raise MethodsRefusal(f"“{app}” is not an application — use one of "
                             f"{', '.join(APPLICATIONS)}.")
    typed = {k: str(v or "").strip() for k, v in (payload.get("conditions") or {}).items()}
    unknown = sorted(k for k, v in typed.items() if v and k not in M.CONDITION_KEYS[app])
    conditions = M.clean_conditions(app, typed)
    dropped = [{"key": k, "label": LABELS.get(k, (k,))[0], "typed": typed[k]}
               for k in M.CONDITION_KEYS[app]
               if typed.get(k) and not conditions.get(k)]
    changed = [{"key": k, "label": LABELS.get(k, (k,))[0], "typed": typed[k],
                "stored": conditions[k]}
               for k in conditions if conditions[k] != typed.get(k)]

    record = _record(target, app)
    before = dict(record.conditions) if record else {}
    removed = [{"key": k, "label": LABELS.get(k, (k,))[0], "was": before[k]}
               for k in M.CONDITION_KEYS[app] if before.get(k) and not conditions.get(k)]

    stored_values = _stored_values(record)
    abs_, figures = _antibodies(target, app)
    by_id = {ab.pk: ab for ab in abs_}
    basis_ok = {b for b, _ in bases()}
    rows, refusals = [], []
    for item in payload.get("antibodies") or []:
        try:
            ab = by_id[int(item.get("id"))]
        except (TypeError, ValueError, KeyError):
            refusals.append(f"Antibody id {item.get('id')!r} is not one of "
                            f"{target.gene_name}'s antibodies.")
            continue
        amount = str(item.get("amount") or "").strip()[:120]
        basis = str(item.get("basis") or "").strip()
        if basis not in basis_ok:
            refusals.append(f"{ab.catalogue_number}: “{basis}” is not a basis — use one of "
                            + ", ".join(b for b, _ in bases() if b) + ".")
            continue
        if amount and not basis:
            basis = "report_named"
        rows.append({"antibody": ab, "amount": amount, "basis": basis if amount else "",
                     "where": str(item.get("where") or "").strip()[:255]})
    # Only an antibody the form sent can lose its value: one it left out is
    # not a blank somebody typed, and is kept.
    sent = {r["antibody"].pk: r["amount"] for r in rows}
    ab_removed = [{"catalogue": by_id[i].catalogue_number, "was": m.amount}
                  for i, m in stored_values.items()
                  if m.amount and i in sent and not sent[i]]

    source = _source(target)
    previews = []
    for r in rows:
        ab = r["antibody"]
        if not r["amount"] and not figures.get(ab.pk):
            continue
        previews.append({
            "id": ab.pk, "catalogue": ab.catalogue_number,
            "figure": figures.get(ab.pk, ""),
            "text": M.text(app, conditions, M.antibody_label(ab), r["amount"], source,
                           ab.host_species),
        })
    return {
        "gene": target.gene_name, "application": app,
        "conditions": conditions, "report_file": str(payload.get("report_file") or "").strip()[:255],
        "dropped": dropped, "changed": changed, "removed": removed, "unknown": unknown,
        "antibodies": [{"id": r["antibody"].pk, "catalogue": r["antibody"].catalogue_number,
                        "amount": r["amount"], "basis": r["basis"], "where": r["where"]}
                       for r in rows],
        "antibodies_removed": ab_removed,
        "refusals": refusals,
        "previews": previews,
        "source": source,
        "stamp": stamp(target, app),
        "is_new": record is None,
    }


def apply(target, app, payload: dict, *, based_on: str, by: str) -> dict:
    """Store what ``plan`` previewed. Refuses when the record changed since the
    preview's stamp, or when the plan carried a refusal."""
    from pipeline.models import AntibodyMethod, Antibody, MethodsRecord
    p = plan(target, app, payload)
    if p["refusals"]:
        raise MethodsRefusal(" ".join(p["refusals"]))
    if not p["conditions"] and not any(r["amount"] for r in p["antibodies"]):
        raise MethodsRefusal("Nothing to save: every box is empty. Type the report's "
                             "methods into the boxes above, then Preview.")
    with transaction.atomic(using=DB):
        if stamp(target, app) != (based_on or ""):
            raise MethodsRefusal(
                f"Not saved: {target.gene_name}'s {app} methods were changed by somebody "
                "else after your preview. Press Reload to see what is stored now, then "
                "make your change again.")
        record, _ = MethodsRecord.objects.using(DB).update_or_create(
            target=target, application=app,
            defaults=dict(conditions=p["conditions"], report_file=p["report_file"],
                          source_ref=f"web:{by}"[:32]))
        written = 0
        ids = [r["id"] for r in p["antibodies"]]
        AntibodyMethod.objects.using(DB).filter(record=record, antibody_id__in=ids).delete()
        abs_ = {a.pk: a for a in Antibody.objects.using(DB).filter(pk__in=ids)}
        for r in p["antibodies"]:
            if not r["amount"]:
                continue
            AntibodyMethod.objects.using(DB).create(
                record=record, antibody=abs_[r["id"]], amount=r["amount"],
                basis=r["basis"], where=r["where"], source_ref=f"web:{by}"[:32])
            written += 1
    return {**p, "saved": True, "antibody_values": written,
            "stamp": stamp(target, app)}
