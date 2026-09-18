"""
pipeline/views/session_entry.py

Session entry views for the YCharOS pipeline.
Chat D deliverable — experiment session list, creation, and detail/edit.

URLs (pre-configured in urls.py):
    /pipeline/sessions/              → session_list
    /pipeline/session/new/           → session_create
    /pipeline/session/<pk>/          → session_detail

Design principles:
    - Mobile-first: large touch targets, step-by-step flow, minimal scrolling
    - Session-based entry matches bench reality (scoping doc §3.1)
    - Protocol templates pre-fill shared conditions (§7.5)
    - Per-antibody results added via dynamic formset
    - All queries use .using('pipeline_db')
"""

import json
from datetime import date

from django.core.paginator import Paginator
from django.db.models import Count
from django.http import HttpResponse, JsonResponse
from django.urls import reverse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST

from pipeline.decorators import pipeline_member_required
from pipeline.models import (
    Antibody,
    ExperimentSession,
    FcResult,
    IfResult,
    IpResult,
    Member,
    ProtocolTemplate,
    Site,
    Target,
    WbResult,
)
from pipeline.services import cell_lines as cell_line_svc
from pipeline.services import members as member_svc

DB = 'pipeline_db'


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _get_member_or_none(user):
    """Return the Member profile for the logged-in user, or None."""
    try:
        return Member.objects.using(DB).select_related('site').get(user=user)
    except Member.DoesNotExist:
        return None

def _result_count(session):
    """Return total per-antibody result count across all procedure-specific tables."""
    pt = session.procedure_type
    if pt == 'WB':
        return session.wb_results.using(DB).count()
    elif pt == 'IP':
        return session.ip_results.using(DB).count()
    elif pt == 'IF':
        return session.if_results.using(DB).count()
    elif pt == 'FC':
        return session.fc_results.using(DB).count()
    return 0

def _merged_conditions(request, session):
    """Fold the posted condition inputs into the session's existing conditions.

    Only the keys the form actually renders (``PROCEDURE_CONDITION_FIELDS`` for
    this procedure) are touched: a filled input sets its key, a cleared one
    removes it. **Every other key is preserved.** Sessions created from the
    per-gene Excel template carry arbitrary spreadsheet columns in
    ``session_conditions`` (``services/session_import.py::_condition_cols``),
    and the form knows nothing about them — rebuilding the dict from scratch
    silently deleted them on every save.
    """
    conditions = dict(session.session_conditions or {})
    for field_key, _label, _input_type, _ph in PROCEDURE_CONDITION_FIELDS.get(
            session.procedure_type, []):
        val = request.POST.get(f'cond_{field_key}', '').strip()
        if val:
            conditions[field_key] = val
        elif field_key in conditions:
            del conditions[field_key]
    return conditions


# Procedure-specific condition field definitions for template/session forms.
# Each entry: (field_key, label, input_type, placeholder)


# Procedure-specific condition field definitions for template/session forms.
# Each entry: (field_key, label, input_type, placeholder)
# Which physical vial was thawed for this session — optional, on every procedure.
#
# A session records the cell *line* (`cell_line_wt` / `cell_line_ko` are FKs to
# `CellLine`); the freeze-down batch lives one level down, on `CellLineVial`,
# keyed by `c_number`. Nothing joined a session to a batch, so "which vial was
# this run on?" had no home — and a gene's page does not need one, but planning
# and recording a session does.
#
# It goes in `session_conditions` rather than on the model: that dict is exactly
# the designed home for a per-session fact whose applicability varies, it is
# already editable on the sessions board, already round-trips through the
# workbook as a `cond:` column, and it needs no migration against live
# PostgreSQL. Optional everywhere — Leicester has not adopted C-numbers, and a
# blank means "not written down", never "no vial".
VIAL_CONDITION_FIELDS = [
    ('wt_vial_c_number', 'WT vial (C-number)', 'text', 'optional — e.g. C-631'),
    ('ko_vial_c_number', 'KO vial (C-number)', 'text', 'optional — e.g. C-632'),
]

PROCEDURE_CONDITION_FIELDS = {
    'WB': [
        ('lysis_buffer', 'Lysis Buffer', 'text', 'e.g. RIPA'),
        ('protein_loading_ug', 'Protein Loading (µg)', 'number', 'e.g. 20'),
        ('gel_chemistry', 'Gel Chemistry', 'text', 'e.g. 4-20% TG'),
        ('transfer_method', 'Transfer Method', 'text', 'e.g. wet'),
        ('blocking', 'Blocking', 'text', 'e.g. 5% milk in TBST'),
        ('secondary_antibody', 'Secondary Antibody', 'text', 'e.g. HRP anti-rabbit'),
        ('secondary_dilution', 'Secondary Dilution', 'text', 'e.g. 1:10000'),
        ('ecl_type', 'ECL Type', 'text', 'e.g. Pierce-normal'),
        ('imaging_system', 'Imaging System', 'text', 'e.g. iBright'),
        ('num_gels', 'Number of Gels', 'number', 'e.g. 3'),
    ],
    'IP': [
        ('lysis_buffer', 'Lysis Buffer', 'text', 'e.g. IP buffer'),
        ('bead_type', 'Bead Type', 'text', 'e.g. Protein A/G'),
        ('protein_amount_mg', 'Protein Amount (mg)', 'number', 'e.g. 1.0'),
        ('detection_antibody', 'Detection Antibody', 'text', 'KO-controlled Ab for WB step'),
        ('detection_antibody_dilution', 'Detection Ab Dilution', 'text', 'e.g. 1:1000'),
        ('secondary_antibody', 'Secondary Antibody', 'text', 'e.g. anti-rabbit HRP'),
        ('secondary_dilution', 'Secondary Dilution', 'text', 'e.g. 1:10000'),
        ('gel', 'Gel', 'text', 'e.g. 4-20% TG'),
        ('membrane', 'Membrane', 'text', 'e.g. PVDF'),
        ('ecl', 'ECL', 'text', 'e.g. Pierce-normal'),
        ('detection_system', 'Detection System', 'text', 'e.g. iBright'),
    ],
    'IF': [
        ('fixation', 'Fixation', 'text', 'e.g. 4% PFA'),
        ('permeabilisation', 'Permeabilisation', 'text', 'e.g. 0.1% Triton X-100'),
        ('blocking', 'Blocking', 'text', 'e.g. 5% BSA + 5% goat serum'),
        ('secondary_ab', 'Secondary Antibody', 'text', 'e.g. Alexa Fluor 555 anti-rabbit'),
        ('secondary_condition', 'Secondary Condition', 'text', 'e.g. 1h RT'),
        ('dilution_buffer', 'Dilution Buffer', 'text', 'e.g. 1% BSA in PBS'),
        ('microscope', 'Microscope', 'text', 'e.g. ImageXpress Micro'),
        ('objective', 'Objective', 'text', 'e.g. 20x'),
    ],
    'FC': [
        ('tracker_dyes', 'Tracker Dyes', 'text', 'e.g. CellTracker green/violet'),
        ('fixation', 'Fixation', 'text', 'e.g. 4% PFA'),
        ('permeabilisation', 'Permeabilisation', 'text', 'e.g. 0.1% saponin'),
        ('flow_cytometer', 'Flow Cytometer', 'text', 'e.g. Attune NxT'),
        ('analysis_software', 'Analysis Software', 'text', 'e.g. FlowJo'),
        ('secondary_ab', 'Secondary Antibody', 'text', 'e.g. Alexa Fluor 647 anti-rabbit'),
        ('secondary_dilution', 'Secondary Dilution', 'text', 'e.g. 1:500'),
    ],
}

# Appended rather than repeated four times: the vial applies to every procedure,
# and a list written out four times is four lists that drift.
for _proc in PROCEDURE_CONDITION_FIELDS:
    PROCEDURE_CONDITION_FIELDS[_proc] = (
        PROCEDURE_CONDITION_FIELDS[_proc] + VIAL_CONDITION_FIELDS)
del _proc

# Per-antibody result fields by procedure type.
# Each entry: (model_field, label, input_type, placeholder)
# Only the most-used fields — less common fields available on detail page.
RESULT_FIELDS = {
    'WB': [
        ('dilution', 'Dilution', 'text', 'e.g. 1:1000'),
        ('signal', 'Signal', 'text', 'Band pattern / outcome'),
        ('rating', 'Rating', 'text', 'e.g. Good / Fair / Poor'),
        ('exposure_time', 'Exposure Time', 'text', 'e.g. 30s'),
    ],
    'IP': [
        ('enrichment', 'Enrichment', 'text', 'Core outcome'),
        ('amount_of_antibody', 'Amount of Ab', 'text', 'e.g. 2 µg'),
        ('amount_of_lysate', 'Amount of Lysate', 'text', 'e.g. 500 µg'),
        ('sm_assessment', 'SM Assessment', 'text', 'Starting material'),
        ('ub_assessment', 'UB Assessment', 'text', 'Unbound'),
        ('ip_assessment', 'IP Assessment', 'text', 'e.g. Immunoprecipitate'),
    ],
    'IF': [
        ('concentration_1', 'Concentration 1', 'text', 'e.g. 1.0 µg/mL'),
        ('concentration_2', 'Concentration 2', 'text', 'e.g. 2.0 µg/mL'),
        ('best_concentration', 'Best Concentration', 'text', 'Chosen conc.'),
        ('specific_signal', 'Specific Signal', 'text', 'Core outcome'),
        ('primary_ab_dilution', 'Primary Ab Dilution', 'text', 'e.g. 1:200'),
        ('primary_ab_condition', 'Ab Condition', 'text', 'e.g. ON 4°C'),
        ('plate_number', 'Plate #', 'text', ''),
        ('well_number', 'Well #', 'text', ''),
        ('permeabilisation', 'Permeabilisation', 'text', 'Triton / Saponin'),
    ],
    'FC': [
        ('concentration', 'Concentration', 'text', 'e.g. 1 µg/mL'),
        ('histogram_shift', 'Histogram Shift', 'text', 'WT vs KO separation'),
        ('median_fluorescence_wt', 'MFI WT', 'number', ''),
        ('median_fluorescence_ko', 'MFI KO', 'number', ''),
        ('gating_strategy', 'Gating Strategy', 'text', ''),
    ],
}

# Map procedure type to result model class
RESULT_MODEL_MAP = {
    'WB': WbResult,
    'IP': IpResult,
    'IF': IfResult,
    'FC': FcResult,
}


# ─────────────────────────────────────────────────────────────
# Session List
# ─────────────────────────────────────────────────────────────

@pipeline_member_required
def session_template_export(request):
    """Download a per-gene session template: one Excel workbook with a tab per
    application (WB/IP/IF/FC), pre-filled from the DB + the member's site default
    protocol. ``?gene=SYMBOL``. Download-only; nothing is written."""
    from pipeline.services import session_template

    gene = (request.GET.get('gene') or '').strip()
    if not gene:
        return HttpResponse("Add ?gene=SYMBOL to download a session template.",
                            status=400, content_type="text/plain")
    member = _get_member_or_none(request.user)
    site_id = member.site_id if member else None
    try:
        data = session_template.build_gene_template(gene, site_id=site_id)
    except ValueError as e:
        return HttpResponse(str(e), status=404, content_type="text/plain")

    resp = HttpResponse(
        data, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp["Content-Disposition"] = f'attachment; filename="session_template_{gene}.xlsx"'
    return resp

@pipeline_member_required
@require_POST
def session_template_upload_preview(request):
    """Read-only preview of the sessions + results a filled template would create."""
    from pipeline.services import session_import
    f = request.FILES.get("file")
    if not f:
        return JsonResponse({"ok": False, "error": "no file uploaded"}, status=400)
    try:
        parsed = session_import.parse_template(f)
    except ValueError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=400)
    member = _get_member_or_none(request.user)
    return JsonResponse(session_import.plan_import(parsed, uploader=member))


@pipeline_member_required
@require_POST
def session_template_upload_commit(request):
    """Create the sessions + result rows from a filled template (one transaction)."""
    from pipeline.services import session_import
    f = request.FILES.get("file")
    if not f:
        return JsonResponse({"ok": False, "error": "no file uploaded"}, status=400)
    try:
        parsed = session_import.parse_template(f)
    except ValueError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=400)
    member = _get_member_or_none(request.user)
    if member is None:
        return JsonResponse({"ok": False, "error": "no pipeline member profile for your account."}, status=400)

    # When a session's own panel posts here it says which session it is, and the
    # file has to agree. The preview already checks it; a preview is not a
    # permission slip, and the two requests are minutes apart with a file input
    # in between. The gene page sends nothing and is unaffected — a workbook
    # uploaded there may legitimately name any session, or none.
    want = (request.POST.get("session_id") or "").strip()
    if want.isdigit():
        from pipeline.views.session_bulk import _workbook_is_this_session
        from pipeline.models import ExperimentSession as _ES
        session = _ES.objects.using(DB).filter(pk=int(want)).first()
        if session is None:
            return JsonResponse({"ok": False, "error": f"session #{want} is not in the database."})
        refusal = _workbook_is_this_session(parsed, session)
        if refusal:
            return JsonResponse({"ok": False, "error": refusal})

    return JsonResponse(session_import.apply_import(parsed, uploader=member))

@pipeline_member_required
def session_create(request):
    """
    Multi-step session creation form.

    GET: renders the form.  Also handles AJAX sub-requests via ?ajax= param:
        ?ajax=templates&procedure=WB&site=1   → JSON list of protocol templates
        ?ajax=template_detail&template_id=1   → JSON template conditions
        ?ajax=antibodies&target=1             → JSON list of antibodies for target
        ?ajax=cell_lines&target=1             → the WT and KO boxes' own lists

    POST: creates the ExperimentSession and lands on the sessions board with it
    open. **The template posts no antibodies**, so the session has no result
    rows; they arrive through its bench sheet (`bench_results`), which lists
    the gene's vials as a picking list for a session that has none. The page
    says so — run 19 planned one here and then looked for the picker.
    """
    # ── AJAX sub-requests ──
    ajax_type = request.GET.get('ajax', '')
    if ajax_type:
        return _handle_session_ajax(request, ajax_type)

    # ── POST: create session + results ──
    if request.method == 'POST':
        return _handle_session_post(request)

    # ── GET: render form ──
    member = _get_member_or_none(request.user)
    targets = Target.objects.using(DB).order_by('gene_name').values('id', 'gene_name', 'protein_name')
    sites = Site.objects.using(DB).filter(is_active=True)
    procedure_choices = ExperimentSession.ProcedureType.choices
    fc_sub_choices = ExperimentSession.FcSubProtocol.choices
    status_choices = ExperimentSession.SessionStatus.choices

    # Pre-select member's site and experimenter if available
    default_site_id = member.site_id if member else None
    default_experimenter_id = member.pk if member else None

    # Experimenter dropdown — all active members
    # One list, shared with the sessions board's quick panel — see
    # services/members.py. This had its own queryset with an inner join to
    # auth_user, so the two doors to a session offered two different lists of
    # who could have run it.
    experimenters = member_svc.experimenters()

    context = {
        'targets_json': json.dumps(list(targets)),
        'sites': sites,
        'procedure_choices': procedure_choices,
        'fc_sub_choices': fc_sub_choices,
        'status_choices': status_choices,
        'experimenters': experimenters,
        'default_site_id': default_site_id,
        'default_experimenter_id': default_experimenter_id,
        'member': member,
        'today': date.today().isoformat(),
        'condition_fields_json': json.dumps(PROCEDURE_CONDITION_FIELDS),
        'result_fields_json': json.dumps(RESULT_FIELDS),
    }
    return render(request, 'pipeline/session_form.html', context)


def _handle_session_ajax(request, ajax_type):
    """Handle AJAX sub-requests from the session form."""
    if ajax_type == 'templates':
        proc = request.GET.get('procedure', '')
        site_id = request.GET.get('site', '')
        qs = (ProtocolTemplate.objects.using(DB).filter(procedure_type=proc)
              .select_related('site'))
        if site_id:
            qs = qs.filter(site_id=site_id)
        # `is_default` means "default for this site and procedure", and this list
        # spans every site — so two templates on one screen both wore a badge
        # reading `Default`, one of which was not the one you were going to get.
        # Send the site so the badge can say whose default it is, and the pick
        # can prefer your own bench's rather than whichever sorted first.
        templates = [{
            'id': t.pk, 'name': t.name, 'is_default': t.is_default,
            'conditions': t.conditions, 'description': t.description,
            'site': t.site.name if t.site_id else '',
            'site_id': t.site_id,
        } for t in qs]
        member = _get_member_or_none(request.user)
        return JsonResponse({'templates': templates,
                             'my_site_id': getattr(member, 'site_id', None)})

    elif ajax_type == 'template_detail':
        tmpl_id = request.GET.get('template_id', '')
        try:
            tmpl = ProtocolTemplate.objects.using(DB).get(pk=tmpl_id)
            return JsonResponse({
                'id': tmpl.id,
                'name': tmpl.name,
                'conditions': tmpl.conditions,
                'description': tmpl.description,
            })
        except ProtocolTemplate.DoesNotExist:
            return JsonResponse({'error': 'Template not found'}, status=404)

    elif ajax_type == 'antibodies':
        target_id = request.GET.get('target', '')
        if not target_id:
            return JsonResponse({'antibodies': []})
        abs_qs = (
            Antibody.objects.using(DB)
            .filter(target_id=target_id)
            .select_related('company')
            .order_by('company__name', 'catalogue_number')
        )
        antibodies = [
            {
                'id': ab.id,
                'catalogue_number': ab.catalogue_number,
                'company': ab.company.name if ab.company else '—',
                'clonality': ab.get_clonality_display(),
                'host_species': ab.host_species,
                'rrid': ab.rrid,
                'concentration': str(ab.concentration) if ab.concentration else '',
                'label': f"{ab.catalogue_number} ({ab.company.name})" if ab.company else ab.catalogue_number,
            }
            for ab in abs_qs
        ]
        return JsonResponse({'antibodies': antibodies})

    elif ajax_type == 'cell_lines':
        target_id = request.GET.get('target', '')
        if not target_id:
            return JsonResponse({'wt': [], 'ko': [], 'wt_note': '', 'ko_note': ''})

        # Two boxes, two lists — `services/cell_lines.py::session_options`. The
        # page used to split one list by genotype in the browser and put
        # anything of genotype `other` in *both*, over a query that let a line
        # with no gene at all through: a fresh gene's KO box offered exactly one
        # option, a line that is not a knockout of anything. Which lines a slot
        # may offer is the resolver's business, not a template's — every other
        # door already asks it with an explicit `genotype=`.
        try:
            target = Target.objects.using(DB).get(pk=target_id)
        except Target.DoesNotExist:
            return JsonResponse({'error': 'That gene is not on file.'}, status=404)

        return JsonResponse(cell_line_svc.session_options(target))

    return JsonResponse({'error': 'Unknown ajax type'}, status=400)


def _handle_session_post(request):
    """Process the multi-step form submission: create session + results.

    The create logic lives in ``pipeline.services.sessions`` so the form and MCP
    Server B's ``record_session`` tool share ONE implementation. This handler just
    translates the indexed form fields into that service's payload.
    """
    from pipeline.services import sessions as sessions_service

    data = request.POST
    procedure_type = (data.get('procedure_type') or '').strip()

    # ── session_conditions from cond_* fields ──
    session_conditions = {}
    for field_key, _label, _input_type, _ph in PROCEDURE_CONDITION_FIELDS.get(procedure_type, []):
        val = data.get(f'cond_{field_key}', '').strip()
        if val:
            session_conditions[field_key] = val

    # ── per-antibody results from result_*_{idx} ──
    results = []
    result_fields = RESULT_FIELDS.get(procedure_type, [])
    idx = 0
    while True:
        ab_id = data.get(f'result_antibody_{idx}')
        if not ab_id:
            break
        r = {'antibody': {'id': ab_id}}
        for field_key, _label, _input_type, _ph in result_fields:
            val = data.get(f'result_{field_key}_{idx}', '').strip()
            if val:
                r[field_key] = val
        result_comments = data.get(f'result_comments_{idx}', '').strip()
        if result_comments:
            r['comments'] = result_comments
        results.append(r)
        idx += 1

    payload = {
        'procedure_type': procedure_type,
        'target_id': data.get('target') or None,
        'experimenter_id': data.get('experimenter') or None,
        'site_id': data.get('site') or None,
        'date': data.get('date'),
        'status': data.get('status', 'planned'),
        'fc_sub_protocol': data.get('fc_sub_protocol', 'na'),
        'protocol_template_id': data.get('protocol_template') or None,
        'cell_line_wt': data.get('cell_line_wt') or None,
        'cell_line_ko': data.get('cell_line_ko') or None,
        'conditions': session_conditions,
        'comments': (data.get('comments') or '').strip(),
        'results': results,
    }

    out = sessions_service.apply(payload)
    if not out.get('ok'):
        return _rerender_form_with_errors(request, out.get('errors') or ['Could not save session.'])
    # The sessions board, with this session's results already open. The session
    # page it used to land on is retired; landing somewhere you can immediately
    # record results is the point of the whole exercise.
    return redirect(
        f"{reverse('pipeline:session_board')}?open={out['session_id']}")


def _rerender_form_with_errors(request, errors):
    """Re-render the session form with validation errors."""
    member = _get_member_or_none(request.user)
    targets = Target.objects.using(DB).order_by('gene_name').values('id', 'gene_name', 'protein_name')
    sites = Site.objects.using(DB).filter(is_active=True)
    # One list, shared with the sessions board's quick panel — see
    # services/members.py. This had its own queryset with an inner join to
    # auth_user, so the two doors to a session offered two different lists of
    # who could have run it.
    experimenters = member_svc.experimenters()
    context = {
        'targets_json': json.dumps(list(targets)),
        'sites': sites,
        'procedure_choices': ExperimentSession.ProcedureType.choices,
        'fc_sub_choices': ExperimentSession.FcSubProtocol.choices,
        'status_choices': ExperimentSession.SessionStatus.choices,
        'experimenters': experimenters,
        'default_site_id': member.site_id if member else None,
        'default_experimenter_id': member.pk if member else None,
        'member': member,
        'today': date.today().isoformat(),
        'condition_fields_json': json.dumps(PROCEDURE_CONDITION_FIELDS),
        'result_fields_json': json.dumps(RESULT_FIELDS),
        'errors': errors,
        'post_data': request.POST,
    }
    return render(request, 'pipeline/session_form.html', context)


# ─────────────────────────────────────────────────────────────
# Session Detail + Inline Edit
# ─────────────────────────────────────────────────────────────

@pipeline_member_required
def session_download(request, pk, artifact):
    """Download planning artefacts for a session."""
    session = get_object_or_404(
        ExperimentSession.objects.using(DB).select_related(
            'target', 'experimenter__user', 'site', 'cell_line_wt', 'cell_line_ko',
        ),
        pk=pk,
    )
    if artifact == 'workbook':
        # The per-gene workbook, built for **this session**: one tab, its own
        # results pre-filled, its number in every `session_ref` cell. Uploading
        # it fills this session in rather than creating a second one beside it,
        # which is the half the tool was missing — the workbook could only
        # create and the bench sheet could only update, so a session planned
        # through the step-by-step form could not be filled in from a
        # spreadsheet at all.
        from pipeline.services.session_template import build_session_template
        gene = session.target.gene_name or session.target.protein_name or 'target'
        resp = HttpResponse(
            build_session_template(session),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        resp['Content-Disposition'] = (
            f'attachment; filename="{gene}_{session.procedure_type}'
            f'_session_{session.pk}_workbook.xlsx"')
        return resp
    if artifact == 'bench-sheet':
        from pipeline.services.planning import bench_sheet_to_response
        return bench_sheet_to_response(session)
    elif artifact == 'wb-ip':
        from pipeline.services.planning import wb_ip_sheet_to_response
        return wb_ip_sheet_to_response(session)
    elif artifact == 'if-plate-map':
        from pipeline.services.planning import if_plate_map_to_response
        return if_plate_map_to_response(session)

    from django.http import Http404
    raise Http404