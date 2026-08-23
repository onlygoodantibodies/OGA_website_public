"""Render a SelectionRecord's validation plan to a PDF.

Uses ReportLab Platypus so the plan wraps and paginates cleanly. Mirrors the
two-stage recommended protocol the learner saw on screen (OGA flow-cytometry
guide, Figure 3): screen to confirm detection, then confirm in the real sample.
"""
import os

from django.conf import settings
from django.http import HttpResponse
from django.utils import timezone

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    HRFlowable, Image, ListFlowable, ListItem, Paragraph, SimpleDocTemplate,
    Spacer, Table, TableStyle,
)

BRAND = colors.HexColor("#1f6f54")
INK = colors.HexColor("#1a1a1a")
MUTED = colors.HexColor("#666666")

APP_LABELS = {
    "WB": "Western blot", "IP": "Immunoprecipitation", "IF": "Immunofluorescence / ICC",
    "FC": "Flow cytometry", "IHC": "IHC", "ELISA": "ELISA", "other": "Other",
}
PILLAR_LABELS = {
    "knockout": "Knockout (KO)", "knockdown": "Knockdown (siRNA / shRNA)",
    "tagged": "Tagged / over-expression", "orthogonal": "Orthogonal (omics correlation)",
    "independent": "Independent antibody", "cell_treatment": "Cell treatment",
}
PILLAR_COST = {
    "knockout": "££–£££", "knockdown": "££", "tagged": "£–££",
    "orthogonal": "£", "independent": "££", "cell_treatment": "£",
}
EVIDENCE_LABELS = {
    "genetic": "Yes — genetic (KO/KD) validation data found",
    "other": "Yes — other supportive data found",
    "none": "No / couldn't find validation data",
    "not_looked": "Hasn't looked yet",
}


def _styles():
    ss = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("t", parent=ss["Title"], textColor=BRAND, fontSize=20,
                                spaceAfter=2, leading=24),
        "sub": ParagraphStyle("s", parent=ss["Normal"], textColor=MUTED, fontSize=9.5, spaceAfter=2),
        "h2": ParagraphStyle("h2", parent=ss["Heading2"], textColor=BRAND, fontSize=12.5,
                             spaceBefore=12, spaceAfter=4),
        "stage": ParagraphStyle("st", parent=ss["Heading3"], textColor=colors.white,
                                fontSize=11, leading=13, backColor=BRAND,
                                borderPadding=(5, 6, 5, 6), spaceBefore=10, spaceAfter=6),
        "body": ParagraphStyle("b", parent=ss["Normal"], textColor=INK, fontSize=10,
                               leading=14, spaceAfter=3),
        "step": ParagraphStyle("stp", parent=ss["Normal"], textColor=INK, fontSize=10,
                               leading=14, spaceAfter=2, leftIndent=4),
        "small": ParagraphStyle("sm", parent=ss["Normal"], textColor=MUTED, fontSize=8.5, leading=11),
        "kv": ParagraphStyle("kv", parent=ss["Normal"], textColor=INK, fontSize=10, leading=14),
    }


def _p(text, style):
    return Paragraph(text if text else "—", style)


def _kv_table(rows, styles):
    data = []
    for k, v in rows:
        if v in (None, "", [], {}):
            v = "—"
        if isinstance(v, list):
            v = ", ".join(str(x) for x in v) or "—"
        data.append([Paragraph(f"<b>{k}</b>", styles["kv"]), Paragraph(str(v), styles["kv"])])
    t = Table(data, colWidths=[4.6 * cm, 11.4 * cm])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, colors.HexColor("#e6e6e6")),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
    ]))
    return t


def _stage(box, steps, styles):
    if not steps:
        box.append(_p("<i>No approach was selected for this stage — see the gaps note.</i>", styles["small"]))
        return
    items = []
    for s in steps:
        title = s.get("title", "")
        detail = s.get("detail", "")
        items.append(ListItem(Paragraph(f"<b>{title}.</b> {detail}", styles["step"]), leftIndent=8))
    box.append(ListFlowable(items, bulletType="1", leftIndent=14))


def render_plan_pdf(rec, request):
    plan = rec.plan or {}
    proto = plan.get("recommended_protocol") or {}
    styles = _styles()

    response = HttpResponse(content_type="application/pdf")
    gene = plan.get("target_gene") or rec.target_gene or "plan"
    safe = "".join(ch for ch in gene if ch.isalnum() or ch in "-_") or "plan"
    response["Content-Disposition"] = f'inline; filename="validation_plan_{safe}.pdf"'

    doc = SimpleDocTemplate(response, pagesize=A4, leftMargin=1.8 * cm, rightMargin=1.8 * cm,
                            topMargin=1.6 * cm, bottomMargin=1.8 * cm,
                            title=f"Antibody validation plan — {gene}")
    story = []

    logo_path = os.path.join(settings.STATIC_ROOT, "core", "logo.png")
    if os.path.exists(logo_path):
        try:
            img = Image(logo_path, width=2.0 * cm, height=2.0 * cm)
            img.hAlign = "CENTER"
            story.append(img)
        except Exception:
            pass
    story.append(_p("Antibody Validation Plan", styles["title"]))
    date_str = timezone.localtime(rec.created_at).strftime("%d %B %Y")
    app = APP_LABELS.get(plan.get("application", ""), plan.get("application", ""))
    story.append(_p(f"Target: <b>{gene}</b> &nbsp;·&nbsp; {app} &nbsp;·&nbsp; {date_str} "
                    f"&nbsp;·&nbsp; Only Good Antibodies", styles["sub"]))
    if getattr(rec, "is_compliance", False):
        who = rec.name or "—"
        inst = rec.institution or "—"
        story.append(_p(f"<b>Compliance record</b> &nbsp;·&nbsp; {who}, {inst} &nbsp;·&nbsp; "
                        f"this plan may be shared with the funder as evidence of good practice.",
                        styles["sub"]))
    else:
        story.append(_p("<b>Personal plan</b> — for your own use; not submitted as a compliance record.",
                        styles["sub"]))
    story.append(HRFlowable(width="100%", thickness=1, color=BRAND, spaceBefore=6, spaceAfter=2))

    # 1. Context
    story.append(_p("Your target &amp; experiment", styles["h2"]))
    rows = [
        ("Target", plan.get("target_gene")),
        ("Protein", plan.get("protein_name")),
        ("Species", plan.get("species")),
        ("Technique", app),
        ("Sample(s)", plan.get("sample_types")),
    ]
    if plan.get("location"):
        rows.append(("Epitope location", plan.get("location")))
    if plan.get("cell_line"):
        rows.append(("Cell model", plan.get("cell_line")))
    if plan.get("ptm"):
        rows.append(("Modification-specific", "Yes — PTM antibody"))
    if rec.name:
        rows.append(("Prepared by", rec.name))
    if getattr(rec, "institution", ""):
        rows.append(("Institution", rec.institution))
    story.append(_kv_table(rows, styles))
    oga = plan.get("oga") or {}
    if oga.get("in_dataset"):
        url = request.build_absolute_uri(oga.get("gene_page_url", ""))
        story.append(Spacer(1, 4))
        story.append(_p(f"✓ <b>{gene} is in the OGA knockout-controlled dataset.</b> Start from the "
                        f"characterisation data and recommended antibodies: "
                        f'<a href="{url}" color="#1f6f54">{url}</a>', styles["body"]))

    # 2. Recommended protocol
    if proto.get("oga_lead"):
        story.append(Spacer(1, 4))
        story.append(_p(f"<b>Start here:</b> {proto['oga_lead']}", styles["body"]))
    story.append(_p("Recommended validation protocol", styles["h2"]))
    story.append(_p("Stage 1 — confirm the antibody detects the target (screen in a tractable system)",
                    styles["stage"]))
    _stage(story, proto.get("stage1") or [], styles)
    story.append(_p("Stage 2 — confirm the signal is your target in your sample of interest",
                    styles["stage"]))
    _stage(story, proto.get("stage2") or [], styles)

    if proto.get("technique_note"):
        story.append(Spacer(1, 4))
        story.append(_p(f"<b>For your technique:</b> {proto['technique_note']}", styles["small"]))
    for gap in (proto.get("gaps") or []):
        story.append(Spacer(1, 3))
        story.append(_p(f"⚠ {gap}", styles["small"]))

    # 3. Feasible approaches (what the learner said they can do)
    feas = plan.get("feasible_pillars") or []
    story.append(_p("Approaches you said you can do", styles["h2"]))
    if feas:
        data = [[Paragraph("<b>Approach</b>", styles["small"]), Paragraph("<b>Cost</b>", styles["small"])]]
        for k in feas:
            data.append([Paragraph(PILLAR_LABELS.get(k, k), styles["small"]),
                         Paragraph(PILLAR_COST.get(k, "—"), styles["small"])])
        t = Table(data, colWidths=[12.0 * cm, 4.0 * cm], repeatRows=1)
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef4f1")),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#dfe7e3")),
            ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(t)
    else:
        story.append(_p("<i>None ticked — the plan above suggests the most accessible options.</i>",
                        styles["small"]))
    ev = plan.get("evidence_status")
    if ev:
        story.append(Spacer(1, 3))
        story.append(_p(f"Existing validation data in {app}: {EVIDENCE_LABELS.get(ev, ev)}", styles["small"]))

    # 4. Find a validated antibody first
    if proto.get("find_first"):
        story.append(_p("First, look for an antibody already validated this way", styles["h2"]))
        story.append(ListFlowable(
            [ListItem(_p(x, styles["body"]), leftIndent=6) for x in proto["find_first"]],
            bulletType="bullet", start="•"))

    # 5. Candidate antibody + present/identify
    ab = plan.get("antibody_identity") or {}
    if any(ab.get(k) for k in ("vendor", "catalogue", "clone", "rrid")):
        story.append(_p("Candidate antibody", styles["h2"]))
        story.append(_kv_table([
            ("Vendor", ab.get("vendor")), ("Catalogue #", ab.get("catalogue")),
            ("Clone", ab.get("clone")), ("RRID", ab.get("rrid")),
        ], styles))
    if plan.get("ptm") and plan.get("ptm_control"):
        story.append(_p("Modification-specificity control", styles["h2"]))
        story.append(_p(plan.get("ptm_control"), styles["body"]))

    if plan.get("budget"):
        story.append(_p("Validation budget", styles["h2"]))
        story.append(_p(plan.get("budget"), styles["body"]))

    story.append(Spacer(1, 6))
    story.append(_p("<b>Present &amp; identify:</b> show these controls in your figures, next to the "
                    "hypothesis-testing data, and identify every reagent (antibody, cell line, plasmid) "
                    "with an RRID so the work is reproducible.", styles["small"]))

    # Guidance appendix
    if rec.guidance:
        story.append(_p("What this plan is based on", styles["h2"]))
        story.append(ListFlowable(
            [ListItem(_p(g, styles["small"]), leftIndent=6) for g in rec.guidance],
            bulletType="bullet", start="–"))

    story.append(Spacer(1, 10))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#cccccc")))
    story.append(_p("This plan is decision support, not a guarantee. Antibody performance is protocol- "
                    "and sample-specific — confirm with your own controls. For a hard target, or access "
                    "to wild-type/knockout material, contact Only Good Antibodies.", styles["small"]))

    verify_url = request.build_absolute_uri(_reverse_safe("selector:record_view", rec.public_code))

    def _footer(canvas, doc_):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(MUTED)
        canvas.drawCentredString(A4[0] / 2, 1.0 * cm,
                                 f"OGA validation record {rec.public_code}  ·  {verify_url}  ·  page {doc_.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return response


def _reverse_safe(name, *args):
    from django.urls import reverse
    try:
        return reverse(name, args=args)
    except Exception:
        return ""
