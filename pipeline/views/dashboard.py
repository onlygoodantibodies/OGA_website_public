"""
Pipeline views — Target Dashboard + Target Detail

Chat A deliverable for Session 3 parallel build.
Provides Carl's coordination dashboard and per-target detail page.

All queries use .using('pipeline_db') as required by the multi-database setup.
"""

import io
import os

from django.shortcuts import render, get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from pipeline.decorators import pipeline_member_required
from pipeline.services import attachments as attachments_svc
from pipeline.services import cell_lines as cell_line_svc
from pipeline.services import gene_progress
from pipeline.services import ko_validation
from pipeline.services import review as review_svc
from pipeline.services import session_board
from pipeline.services import target_board as board_svc
from pipeline.services import targets as targets_svc
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.db.models import Count, Q, Prefetch
from django.views.decorators.http import require_http_methods

from pipeline.models import (
    Target, Site, Project, GrantingAgency, ExperimentSession,
    Antibody, CellLine, Report, TargetNomination,
)

# TargetAssignment was added in migration 0002 (Session 2).
# Import gracefully in case the model file in project knowledge is stale.
try:
    from pipeline.models import TargetAssignment
    HAS_TARGET_ASSIGNMENT = True
except ImportError:
    HAS_TARGET_ASSIGNMENT = False

DB = 'pipeline_db'
TARGETS_PER_PAGE = 25


# =============================================================================
# Dashboard — Carl's daily coordination view
# =============================================================================

@pipeline_member_required
def target_detail(request, pk):
    """
    Full detail view for a single target.

    Shows:
      - Target metadata (gene, protein, UniProt, mass, status, site, project)
      - Antibodies grouped by site
      - Cell lines as WT/KO pairs
      - Experiment sessions grouped by procedure type with result counts
      - Reports with DOI links
      - Target assignments (if TargetAssignment model available)
    """
    from pipeline.services import report_generator

    target = get_object_or_404(
        Target.objects.using(DB).select_related(
            'site', 'project', 'granting_agency',
        ),
        pk=pk,
    )

    # --- Who is pursuing this target: the nominations, not Target.site ---
    #
    # ``Target.site``/``project``/``granting_agency`` are the denormalised FKs
    # from the Access import, and nothing writes them any more — a target's site
    # lives on its TargetNomination rows, which is what the target board reads
    # and what feasibility writes. So this page showed "Site —" for a target the
    # board showed as Leicester: the same fact, two fields, one of them dead. It
    # reads the nominations now, from the same place as the board.
    nominations = list(
        TargetNomination.objects.using(DB)
        .filter(target_id=target.pk)
        .select_related('site', 'project', 'granting_agency')
        .order_by('site__name', 'project__name')
    )
    # The Access-era targets predate TargetNomination entirely, so for them
    # Target.site is the only record of whose target this is. Reported as what it
    # is — from the import, not from a nomination — rather than shown as a site
    # like any other or thrown away as "nobody's".
    #
    # `services/targets.py::sites_of` is that rule, and this page is where it was
    # first written down. It is shared now because the target board, the
    # Portfolio and Overview's list all needed the same answer and three of them
    # had a different one.
    whose = targets_svc.sites_of(target, nominations)
    nominated_sites = [] if whose["from_import"] else [s.name for s in whose["sites"]]
    legacy_site = whose["sites"][0].name if whose["from_import"] else ""
    nominated_projects = sorted({n.project.name for n in nominations if n.project_id})
    nominated_agencies = sorted({n.granting_agency.name for n in nominations
                                 if n.granting_agency_id})

    # --- Antibodies grouped by site ---
    antibodies = (
        Antibody.objects.using(DB)
        .filter(target=target)
        .select_related('company', 'site')
        .order_by('site__name', 'company__name', 'catalogue_number')
    )

    # Group antibodies by site for side-by-side display
    antibodies_by_site = {}
    for ab in antibodies:
        site_name = ab.site.name if ab.site else 'Unassigned'
        antibodies_by_site.setdefault(site_name, []).append(ab)

    # --- Cell lines with WT/KO pairing ---
    cell_lines = (
        CellLine.objects.using(DB)
        .filter(target=target)
        .select_related('company', 'site', 'parent_line')
        .order_by('site__name', 'genotype', 'name')
    )

    # Build WT/KO pairs: group KO lines under their WT parent.
    #
    # A wild type is recorded once, with no gene, so `filter(target=…)` above
    # can never return one — the parent of a KO here is *always* outside this
    # queryset. Pairing off `wt_lines` therefore matched nothing the moment a KO
    # was linked to its parent properly: the KO fell out of both loops and the
    # panel printed "No cell lines recorded for this target." under a header
    # reading "CELL LINES (1)", because the count and the list read different
    # sets. Doing the right thing made the row disappear. Parents are fetched by
    # their FK instead, and the invariant is that every counted line is shown.
    lines = list(cell_lines)
    wt_lines = [cl for cl in lines if cl.genotype == 'WT']
    ko_lines = [cl for cl in lines if cl.genotype == 'KO']
    other_lines = [cl for cl in lines if cl.genotype not in ('WT', 'KO')]

    wt_by_id = {wt.pk: wt for wt in wt_lines}
    absent_parents = {ko.parent_line_id for ko in ko_lines
                      if ko.parent_line_id and ko.parent_line_id not in wt_by_id}
    if absent_parents:
        for wt in (CellLine.objects.using(DB)
                   .filter(pk__in=absent_parents)
                   .select_related('company', 'site')):
            wt_by_id[wt.pk] = wt

    # Every pair carries all three keys, even when they are None: the template
    # reaches them through `|default:`, and a *missing* key raises there rather
    # than falling back — the same trap CLAUDE.md records for
    # `|default:request.GET.gene`.
    cell_line_pairs = []
    matched_wt_ids = set()
    for ko in ko_lines:
        wt = wt_by_id.get(ko.parent_line_id)
        if wt:
            matched_wt_ids.add(wt.pk)
        cell_line_pairs.append({'wt': wt, 'ko': ko, 'other': None})

    # A WT on this target's own list with nothing derived from it, and anything
    # whose genotype is neither — dropped silently before, which was the second
    # way this panel could count a line it did not show.
    for wt in wt_lines:
        if wt.pk not in matched_wt_ids:
            cell_line_pairs.append({'wt': wt, 'ko': None, 'other': None})
    for cl in other_lines:
        cell_line_pairs.append({'wt': None, 'ko': None, 'other': cl})

    # --- Experiment sessions grouped by procedure type ---
    sessions = (
        ExperimentSession.objects.using(DB)
        .filter(target=target)
        .select_related('site', 'experimenter__user', 'protocol_template')
        .annotate(
            wb_result_count=Count('wb_results'),
            ip_result_count=Count('ip_results'),
            if_result_count=Count('if_results'),
            fc_result_count=Count('fc_results'),
        )
        .order_by('-date')
    )

    sessions_by_type = {
        'WB': [],
        'IP': [],
        'IF': [],
        'FC': [],
    }
    # A **result row is not a reading**, and this page is where that costs most.
    # Planning a session writes one blank row per antibody it will test, so a
    # planned WB session with twenty-two untouched rows counted as twenty-two
    # results — and `procedure_summary` below marks a procedure complete on
    # `results > 0`, so PROCEDURE STATUS read **Complete** for a western blot
    # nobody had run. The comment under that card already said a procedure is
    # done when a reading has been written down; the count it read did not.
    # `session_board.reading_counts` is the one reader (`is_reading` beside it).
    readings = session_board.reading_counts([s.pk for s in sessions])
    # How many raw files each session has, so the panel below can be opened
    # knowing whether there is anything in it — and so the number the Zenodo
    # deposit quotes ("N raw file(s)") can be traced to the sessions it came
    # from, on the page the deposit button is on.
    #
    # Its own query, deliberately: a fifth `Count` on the annotate above joins a
    # second multi-valued relation alongside the four result counts and
    # multiplies both, so a session with 3 results and 2 files would report 6 of
    # each. Same shape as `reading_counts`, one query for the page.
    file_counts = attachments_svc.counts_for([s.pk for s in sessions])
    for session in sessions:
        session.attachment_count = file_counts.get(session.pk, 0)
        proc = session.procedure_type
        # Attach the relevant result count as a convenience attribute
        if proc == 'WB':
            session.result_count = session.wb_result_count
        elif proc == 'IP':
            session.result_count = session.ip_result_count
        elif proc == 'IF':
            session.result_count = session.if_result_count
        elif proc == 'FC':
            session.result_count = session.fc_result_count
        else:
            session.result_count = 0
        session.reading_count = readings.get(session.pk, 0)
        sessions_by_type.setdefault(proc, []).append(session)

    # --- Reports ---
    # The panel's own copy says this list is the published record — a Zenodo
    # deposit or an F1000 paper — "so a draft never appears here", and it then
    # drew every Report row on the target, drafts included. `completed_report_q`
    # is the app's single definition of that same sentence (the board, the
    # Portfolio and Overview's headline all ask it), so the list and the
    # paragraph above it now state one thing.
    #
    # A row that does not qualify is *counted* rather than silently dropped: the
    # Access import wrote Carl's F1000 column onto rows carrying no DOI, and a
    # record that vanishes from the one page about its gene is how a value
    # nobody can see becomes a value nobody can fix.
    all_reports = list(
        Report.objects.using(DB)
        .filter(target=target)
        .order_by('-zenodo_date', '-f1000_date')
    )
    # `board_svc.is_completed_report` rather than a second query asking
    # `completed_report_q` — same sentence, one reader, and the panel below is
    # now drawn from `row["reports"]`, which splits the list the same way.
    reports = [r for r in all_reports if board_svc.is_completed_report(r)]
    unpublished_reports = len(all_reports) - len(reports)

    # --- Target assignments (if model exists) ---
    assignments = []
    if HAS_TARGET_ASSIGNMENT:
        try:
            assignments = list(
                TargetAssignment.objects.using(DB)
                .filter(target=target)
                .select_related('site')
                .order_by('site__name', 'task_type')
            )
        except Exception:
            # Table might not exist yet if migration hasn't run
            assignments = []

    # --- Procedure completion summary ---
    procedure_summary = {
        proc: {'sessions': len(sessions_by_type.get(proc, [])),
               # Rows somebody has written on, not rows that exist — see the
               # note above. One filled row out of twenty-two still counts:
               # gaps in a results section are normal and are not this card's
               # business, but a set of rows with nothing on any of them is not
               # a procedure that has been run.
               'results': sum(getattr(s, 'reading_count', 0) or 0
                              for s in sessions_by_type.get(proc, [])),
               'rows': sum(getattr(s, 'result_count', 0) or 0
                           for s in sessions_by_type.get(proc, [])),
               'complete': False}
        for proc in ('WB', 'IP', 'IF', 'FC')
    }
    # Completion used to be read off `target.status` — "ip_complete" ticked WB and
    # IP, and so on. Nothing advances that field (it is written once, as
    # not-started), so the ticks were either absent or, if somebody typed a status
    # on the board, four ticks for procedures nobody had run.
    #
    # Deriving it from the *existence* of a session was the next version of the
    # same mistake, one step less wrong: a session planned through the
    # step-by-step form has no results yet, and this card read **Complete** over
    # it. Planning an experiment is not doing it. A procedure is done when a
    # reading has been written down, which is what the progress strip and the
    # report both count.
    for proc, summary in procedure_summary.items():
        summary['complete'] = summary['results'] > 0

    # `Target.ko_validated` is dead in the same way `Target.status` is: an Access
    # column no write path sets. Knockout confirmation is recorded per cell line,
    # which is what the progress strip reads — so TARGET INFORMATION said "No"
    # a few centimetres under a strip reading "KO confirmed — 1 of 1 confirmed".
    # Same page, same question, two answers.
    #
    # Read through `ko_validation`, not off the tick: on the live data six rows
    # carry the tick over a reason recording that the check **failed**, so
    # counting the bit alone published those as confirmed knockouts. A row whose
    # two halves disagree is not counted here — the board draws it as a question
    # and one click settles it.
    ko_confirmed_count = sum(1 for cl in ko_lines if ko_validation.confirmed(cl))
    ko_disputed_count = sum(1 for cl in ko_lines if ko_validation.disagrees(cl))
    for cl in ko_lines:
        cl.ko_badge = ko_validation.badge(cl)

    # Where this gene has got to, derived from records rather than a status
    # field — see services/gene_progress.py.
    progress = gene_progress.steps_for(target)
    progress_headline = gene_progress.headline(progress)

    # The wild-type parentals this site already has.
    #
    # A wild type is recorded once, with no gene, so it can never be listed under
    # a gene — and this page told you to "add the KO line and its wild-type
    # parent" while the grid warned that adding a second one is a mistake, with
    # nothing on screen saying which situation you were in. Leicester had the
    # exact HAP1 the sixth field test needed, and the only way to find that out
    # was to attempt the error and read the preview. A site has single figures of
    # these, so listing them is one small query and it is the answer to the
    # question the panel asks.
    # …and they are a **picker** now, not only a sentence. Reading a list and
    # then retyping one of it into a cell is two halves of a job the page can do,
    # on the column where a slip parents a knockout onto another institution's
    # line. `services/cell_lines.py::wild_type_options` decides what goes in the
    # cell, because what the cell may contain is the parser's business and not a
    # template's — the value is a bare name or a C-number, never `label()`'s
    # "HAP1 — Leicester", which `resolve_parent` does not take.
    #
    # One call behind both, so the sentence and the dropdown cannot disagree
    # about what this site has.
    member = _dash_member(request)
    wt_options = cell_line_svc.wild_type_options(
        site_id=getattr(member, "site_id", None))
    wt_parentals = [o["name"] for o in wt_options]

    # The Add pop-outs on this page are the same OGABoard.newEntry the boards
    # use, so their headings come from the same constant as the Excel templates
    # and the paste parsers — a column added there appears here too.
    from pipeline.views.imports import columns_and_example
    ab_columns, ab_example = columns_and_example("antibodies")
    cl_columns, cl_example = columns_and_example("cell-lines")

    # This gene as the **target board** sees it, so the funding and publication
    # cells below can be the board's own — same payload, same patch endpoint,
    # same refusals. The owner recorded a published F1000 for PKN2 and had to
    # leave this page to do it; a second editor written here would be a second
    # set of rules about a Zenodo DOI, and this repo has thirteen field tests'
    # worth of evidence about what two doors that disagree cost.
    #
    # `board_queryset` rather than the `target` above, because `row_for` reads
    # the `is_funded` / `is_completed` annotations it adds — and it is one row,
    # so the prefetches it carries are cheap.
    board_target = board_svc.board_queryset().filter(pk=target.pk).first()
    board_row = board_svc.row_for(board_target) if board_target else None

    # Figures cropped for this gene and not yet published.
    #
    # The cropper writes to the review queue rather than to the public site
    # (`services/review.py`), so without this the gene page would show nothing
    # at all between "somebody spent an afternoon cropping" and "the figures
    # appeared on the website" — a whole stage of the work invisible on the one
    # page about the gene. Drawn, counted, and released from here as well as
    # from the queue itself: the review meeting happens on a gene, and the gene
    # is where you are standing when you ask whether its figures are ready.
    pending_items = list(review_svc.for_target(target.pk))
    pending_rows = review_svc.rows_for(
        pending_items,
        url_of=lambda i: reverse("pipeline:review_image", args=[i.pk]))

    context = {
        'target': target,
        # The nav's Browse menu carries the gene you are looking at. A board knows
        # it from ?gene=; this page knows it from the URL path, so it has to say
        # so — otherwise the one page most obviously about a single gene is the
        # one that drops it, which is what the third field test found.
        'nav_gene': target.gene_name or '',
        # Both alias columns, as one list — `Target.aliases` was written by the
        # spreadsheet round trip and drawn on no pipeline screen at all.
        'other_names': targets_svc.other_names(target),
        'ab_columns': ab_columns,
        'ab_example': ab_example,
        'cl_columns': cl_columns,
        'cl_example': cl_example,
        'progress_steps': progress,
        'progress_headline': progress_headline,
        'next_step': gene_progress.next_step(progress),
        'ko_confirmed_count': ko_confirmed_count,
        'ko_disputed_count': ko_disputed_count,
        'ko_line_count': len(ko_lines),
        'wt_parentals': wt_parentals,
        # The same wild types, as the Add grid's `parent` cell takes them.
        'wt_options': wt_options,
        # Whose bench that list is about. "No wild types on file at your site"
        # is the wrong sentence for somebody whose account has no site — there
        # is nothing to have none of, and the fix is on the people board.
        'my_site': getattr(getattr(member, "site", None), "name", "") or "",
        'nominations': nominations,
        'nominated_sites': nominated_sites,
        'legacy_site': legacy_site,
        'nominated_projects': nominated_projects,
        'nominated_agencies': nominated_agencies,
        'is_funded': any(n.funded for n in nominations),
        'antibodies_by_site': antibodies_by_site,
        'antibody_count': antibodies.count(),
        'cell_line_pairs': cell_line_pairs,
        'cell_line_count': cell_lines.count(),
        'sessions_by_type': sessions_by_type,
        'session_count': sessions.count(),
        'reports': reports,
        'unpublished_reports': unpublished_reports,
        # The board's row for this gene, and the vocabularies its cells offer.
        # A site is validated against the ones on file (`services/sites.py`
        # refuses anything else by name), so that cell gets a closed list;
        # funders and projects are created on demand by the same write path the
        # board uses, so theirs are reminders rather than rules — the trade
        # `newEntry`'s `suggestions` already makes.
        'board_row': board_row,
        'board_sites': [s.name for s in
                        Site.objects.using(DB).filter(is_active=True).order_by('name')],
        'board_agencies': list(GrantingAgency.objects.using(DB)
                               .order_by('name').values_list('name', flat=True)),
        'board_projects': list(Project.objects.using(DB)
                               .order_by('name').values_list('name', flat=True)),
        'assignments': assignments,
        'procedure_summary': procedure_summary,
        # What Generate Report would actually put in the file. Asked of the
        # generator itself rather than recomputed from `procedure_summary`
        # above, which counts result *rows* — a session carrying one blank row
        # counts there and is dropped from the document, so a second count here
        # would be a number the panel prints and the file contradicts. Imported
        # where it is used: report_generator pulls in python-docx at module
        # level, and every gene page would pay for it.
        'report_draft': report_generator.draft_contents(target),
        # Cropped and not yet published — see above. The manifest is the
        # server's answer to what releasing would do, because "this replaces a
        # live figure" and "this gives the gene its first public page" are facts
        # about the database and not about the checkboxes.
        'pending_rows': pending_rows,
        'pending_count': len(pending_rows),
        'pending_manifest': review_svc.manifest(pending_items),
    }
    return render(request, 'pipeline/target_detail.html', context)

@pipeline_member_required
def generate_report_view(request, pk):
    """Generate and download a draft .docx Data Note for a target.

    The document is a draft for a human to finish — it is not filed against the
    target, and the REPORTS panel on the gene page tracks the *published* record
    (a Zenodo deposit, an F1000 paper), so nothing appears there. The page says
    both, because the field test read the silence as failure.

    The workspace is per-request. `generate_report` defaults to a filename in
    /tmp built from the gene alone, so two people asking for one gene's report at
    the same moment wrote to the same path and each could be handed the other's
    half-written file — and a failure anywhere after `doc.save` left it behind.
    """
    import tempfile
    from django.http import FileResponse
    from pipeline.models import Target
    from pipeline.services.report_generator import generate_report

    target = Target.objects.using('pipeline_db').get(pk=pk)
    gene = target.gene_name or target.protein_name
    safe_name = gene.replace('/', '_').replace(' ', '_')
    filename = f"{safe_name}_antibody_characterization_report.docx"

    with tempfile.TemporaryDirectory() as workspace:
        path = generate_report(pk, output_path=os.path.join(workspace, filename))
        with open(path, 'rb') as f:
            payload = f.read()

    response = FileResponse(
        io.BytesIO(payload), as_attachment=True, filename=filename,
        content_type='application/vnd.openxmlformats-officedocument'
                     '.wordprocessingml.document')
    return response


# ============================================================================
# Master Coordination Dashboard
# ============================================================================


# The three states a person types on purpose. Everything else about where a
# target has got to is derived from records — see services/gene_progress.py for
# the same rule on a gene's own page.
STOPPED_STATUSES = ['published', 'cancelled', 'on_hold']

# What the stage chart sets aside, which is **not** the same list.
#
# "Cancelled" and "on hold" are decisions somebody took and no record implies, so
# they are read from the typed field and always will be. "Published" is not: it
# is the Access-era status, and reading it made this page answer "how many have
# we finished" with a different number from the board and the Portfolio. The
# chart's finished bucket is derived now — see `_stage_counts`.
PAUSED_STATUSES = ['cancelled', 'on_hold']


def _annotate_progress(qs):
    """Counts of the records that say how far a target has got.

    ``is_completed`` is the *board's* subquery, not a fourth definition written
    here — see ``target_board.completed_subquery``. This page used to answer "how
    many have we finished" twice on its own (a headline off ``Target.status`` and
    a stage chart counting both that and any Report row) and disagree with the
    board and the Portfolio, which is three numbers for one question.
    """
    from pipeline.services import target_board as board_svc

    return qs.annotate(
        ab_count=Count('antibodies', distinct=True),
        wb_count=Count('sessions', filter=Q(sessions__procedure_type='WB'), distinct=True),
        ip_count=Count('sessions', filter=Q(sessions__procedure_type='IP'), distinct=True),
        if_count=Count('sessions', filter=Q(sessions__procedure_type='IF'), distinct=True),
        fc_count=Count('sessions', filter=Q(sessions__procedure_type='FC'), distinct=True),
        report_count=Count('reports', distinct=True),
        is_completed=board_svc.completed_subquery(),
    )


# The statuses the Access import wrote. Dead for new work — nothing advances
# them — but for the imported targets they are the only record that anybody was
# ever working on them, and McGill has 152 counted through this and nothing else.
TYPED_ACTIVE_STATUSES = ['in_progress', 'wb_complete', 'ip_complete',
                         'if_complete', 'fc_complete', 'report_ready']


def _active(annotated_qs):
    """Work has started on this target and has not finished.

    Two sources, because neither alone is honest:

    * **Derived** — a reagent or a session on file and no report yet. This is
      exactly the funnel's "Reagents Only" plus "Experimenting", so the headline
      figure and the bar underneath it agree by construction. It is what was
      missing: `Target.status` is written once and advanced by nothing, so a gene
      with two targets added, seven sessions run and twenty-two results recorded
      never appeared here, and the sixth field test watched Leicester stay on 6
      all afternoon.
    * **Typed** — the Access-era statuses. Those targets predate nominations and
      often predate any record this app holds; dropping them would have emptied
      McGill's column overnight and read as data loss.

    Union, minus the three stopped states, which are typed on purpose.
    """
    return annotated_qs.filter(
        Q(status__in=TYPED_ACTIVE_STATUSES)
        | (Q(report_count=0)
           & (Q(ab_count__gt=0) | Q(wb_count__gt=0) | Q(ip_count__gt=0)
              | Q(if_count__gt=0) | Q(fc_count__gt=0)))
    ).exclude(status__in=STOPPED_STATUSES)


def _stage_counts(annotated):
    """The funnel's bars, plus the two side tallies — exclusive, and exhaustive.

    Every target lands in exactly one of these seven buckets, which is what makes
    the row of numbers add up to the total. It did add up before, by luck rather
    than construction, and two of the buckets were answering the wrong question:

    * **"Complete" and "Published" were two different facts** — any ``Report`` row
      versus ``Target.status == 'published'`` — and neither was the definition the
      board and the Portfolio use. So the page reported 34 and 134 where they
      reported 164, and "how many have we finished" had three answers across three
      screens. Both are derived now, from ``target_board.completed_subquery``:
      **Reported** is a Zenodo DOI or a published F1000 date, and the bar beside
      it is a draft that has not got one yet. The tile above the chart is the same
      number, and so is every site's column below it.
    * **Feasibility only checked ``wb_count``**, so a target with an IP, IF or FC
      session and no antibodies counted as Feasibility *and* as Experimenting. No
      such target exists on the live data, which is why the total still came out
      right; the bucket was overlapping regardless, and a chart read at a glance
      is the wrong place to leave that.

    ``on_hold``/``cancelled`` stay typed — they are decisions nobody can infer
    from a record — but they exclude anything already counted as reported, or a
    cancelled target that had been written up would appear twice.
    """
    live = annotated.exclude(status__in=PAUSED_STATUSES)
    no_session = Q(wb_count=0, ip_count=0, if_count=0, fc_count=0)
    any_session = (Q(wb_count__gt=0) | Q(ip_count__gt=0)
                   | Q(if_count__gt=0) | Q(fc_count__gt=0))
    # Not started, so nothing to report on. `report_count=0` already implies not
    # reported — a qualifying report needs a Report row — so the first three
    # buckets need no completion test of their own.
    unstarted = live.filter(report_count=0)
    stages = [
        {'key': 'feasibility', 'label': 'Feasibility', 'colour': 'gray',
         'count': unstarted.filter(no_session, ab_count=0).count()},
        {'key': 'reagents_only', 'label': 'Reagents Only', 'colour': 'yellow',
         'count': unstarted.filter(no_session, ab_count__gt=0).count()},
        {'key': 'experimenting', 'label': 'Experimenting', 'colour': 'blue',
         'count': unstarted.filter(any_session).count()},
        {'key': 'drafted', 'label': 'Report drafted', 'colour': 'purple',
         'count': live.filter(report_count__gt=0, is_completed=False).count()},
        # No status exclusion, deliberately: this is the board's own filter and
        # the Portfolio's own total, so the three cannot differ.
        {'key': 'reported', 'label': 'Reported', 'colour': 'green',
         'count': annotated.filter(is_completed=True).count()},
    ]
    on_hold = annotated.filter(status='on_hold', is_completed=False).count()
    cancelled = annotated.filter(status='cancelled', is_completed=False).count()
    return stages, on_hold, cancelled


@pipeline_member_required
def master_dashboard(request):
    from datetime import timedelta
    from pipeline.models import Report, ReagentRequest

    all_targets = Target.objects.using(DB)

    # Pipeline funnel — derived from actual session data, not status field
    total_count = all_targets.count()

    annotated = _annotate_progress(all_targets)

    stages, on_hold_count, cancelled_count = _stage_counts(annotated)
    # The tallest bar, so the template can draw the rest in proportion to it.
    # It used to set each bar's height to the raw count in pixels under a
    # `max-height: 200px`, so every stage over 200 drew identically — 262 and 201
    # were the same bar. A template cannot find a maximum; `widthratio` is all the
    # arithmetic it has. `or 1` because dividing by zero on an empty pipeline
    # would take the page down over a chart.
    stage_max = max((s['count'] for s in stages), default=0) or 1

    # Active targets — derived, like the funnel directly above it.
    #
    # This read `Target.status`, which CLAUDE.md already records as dead: written
    # once as "not started" and advanced by nothing. So a gene with two targets
    # added, seven sessions run and twenty-two results recorded never appeared
    # here and never moved the site's "Active" figure — the sixth field test did
    # exactly that and watched Leicester stay on 6. The page's own funnel, three
    # lines up, had the right answer all along: work has started (a reagent or a
    # session) and has not finished (no report). Active is that, so the tile, the
    # list and the per-site figure now agree with the funnel by construction.
    # The three stopped states are still read from `status` because those are
    # typed on purpose and mean something no record implies.
    active_targets = (
        _active(annotated)
        .select_related('site', 'project', 'granting_agency')
        # Which sites are pursuing a target lives on its nominations. Reading
        # Target.site — the denormalised leftover of the Access import, which
        # nothing writes — printed "—" in the SITE column for every target added
        # since nominations became the record.
        .prefetch_related(Prefetch(
            'nominations',
            queryset=TargetNomination.objects.using(DB).select_related('site')))
        .annotate(
            antibody_count=Count('antibodies', distinct=True),
            wb_sessions=Count('sessions', filter=Q(sessions__procedure_type='WB'), distinct=True),
            ip_sessions=Count('sessions', filter=Q(sessions__procedure_type='IP'), distinct=True),
            if_sessions=Count('sessions', filter=Q(sessions__procedure_type='IF'), distinct=True),
            fc_sessions=Count('sessions', filter=Q(sessions__procedure_type='FC'), distinct=True),
        )
        .order_by('gene_name')
    )
    # A page, not the dataset — the rule `services/board_page.py` exists for, and
    # this page never got it. 358 active targets rendered as one 16,000px table,
    # which is the same shape as the antibodies board handing a browser 3,225 rows
    # and getting a "page unresponsive" dialog.
    #
    # The count above the table stays the *whole* filtered set. Paginating the
    # number as well would turn "358 targets" into "50" and read as work having
    # disappeared, which is the failure the boards' own pager was written around.
    active_total = active_targets.count()
    active_page = Paginator(active_targets, TARGETS_PER_PAGE)
    try:
        page_number = int(request.GET.get('page', 1))
    except (TypeError, ValueError):
        page_number = 1
    try:
        active_targets = active_page.page(page_number)
    except (EmptyPage, PageNotAnInteger):
        active_targets = active_page.page(1)

    # Whose target this is — `services/targets.py::sites_of`, the one reader.
    #
    # This page has fallen back to the Access-era `Target.site` since the sixth
    # field test, and was right to: for the 508 imported targets it is the only
    # record of whose they are. The target board did not, so the same gene read
    # "MCG" here and offered "set site" there, on 192 of the 348 rows in this
    # very list. The fallback is not the bug — having it in one screen and not
    # the other was.
    for t in active_targets:
        whose = targets_svc.sites_of(t)
        t.site_codes = ", ".join(sorted(s.short_code or s.name
                                        for s in whose["sites"]))
        # Said on the row, because the import stamped the same site on every one
        # of those targets: it is where they came from, not a per-gene decision.
        t.site_from_import = whose["from_import"]

    # Recent activity
    two_weeks_ago = timezone.now() - timedelta(days=14)
    recent_sessions = (
        ExperimentSession.objects.using(DB)
        .filter(created_at__gte=two_weeks_ago)
        .select_related('target', 'site')
        .order_by('-created_at')[:15]
    )

    # Cross-site summary
    site_summaries = []
    for site_obj in Site.objects.using(DB).filter(is_active=True):
        # A target counts for a site three ways, and all three are needed.
        #
        # Nominations are the live record, and matching only on Target.site meant
        # a site's newly added targets counted for nobody until somebody happened
        # to enter an antibody — so a site that had started work but not bought
        # reagents showed as idle.
        #
        # But Target.site cannot simply be dropped, and this is where dropping it
        # would have hurt. It is the denormalised leftover of the Access import,
        # nothing writes it any more — and for the Access-era targets it is the
        # *only* record of whose they are, because TargetNomination did not exist
        # when they were imported. One site on live shows 152 active targets and
        # zero antibodies: every one of those 152 is counted through this clause
        # alone, so removing it would have emptied that column overnight and read
        # as data loss.
        #
        # So: nominated here, OR holds an antibody, OR the import said so.
        #
        # Note this cannot tell two Site rows for one real site apart, and there
        # is a known pair on live — see the "one lab, two Site rows" item in
        # PLATFORM_ROADMAP.md. Until those are merged this page reports that lab
        # twice, and the merge is a live-data change, not something to infer here.
        site_targets = all_targets.filter(
            Q(nominations__site=site_obj) | Q(antibodies__site=site_obj)
            | Q(site=site_obj)).distinct()
        annotated_site = _annotate_progress(site_targets)
        sd = {
            'site': site_obj,
            'active_targets': _active(annotated_site).count(),
            # Derived, like the tile and the chart above — this column read
            # `status='published'` and so under-reported every site by whatever
            # it has finished since the Access import.
            'completed': annotated_site.filter(is_completed=True).count(),
            'antibodies': Antibody.objects.using(DB).filter(site=site_obj).count(),
        }
        if sd['active_targets'] > 0 or sd['antibodies'] > 0:
            site_summaries.append(sd)

    # Attention needed
    needs_reagents = list(
        all_targets.filter(status='in_progress')
        .annotate(ab_count=Count('antibodies'))
        .filter(ab_count=0)
        .values_list('gene_name', flat=True)[:10]
    )
    has_antibodies_no_sessions = list(
        all_targets.filter(status='in_progress')
        .annotate(ab_count=Count('antibodies'), session_count=Count('sessions'))
        .filter(ab_count__gt=0, session_count=0)
        .values_list('gene_name', flat=True)[:10]
    )
    report_ready_no_report = list(
        all_targets.filter(status='report_ready')
        .annotate(rep_count=Count('reports'))
        .filter(rep_count=0)
        .values_list('gene_name', flat=True)[:10]
    )

    overall_stats = {
        'total_targets': all_targets.count(),
        'active': _active(annotated).count(),
        # The board's number and the Portfolio's number, asked the board's way.
        # This tile read `status='published'` — 134 where they both said 164 —
        # which is the headline figure on the page a coordinator opens first.
        'completed': annotated.filter(is_completed=True).count(),
        'total_antibodies': Antibody.objects.using(DB).count(),
        'total_sessions': ExperimentSession.objects.using(DB).count(),
        'total_reports': Report.objects.using(DB).count(),
    }

    return render(request, 'pipeline/master_dashboard.html', {
        'stages': stages,
        'stage_max': stage_max,
        'on_hold_count': on_hold_count,
        'cancelled_count': cancelled_count,
        'active_targets': active_targets,
        'active_total': active_total,
        'recent_sessions': recent_sessions,
        'site_summaries': site_summaries,
        'needs_reagents': needs_reagents,
        'has_antibodies_no_sessions': has_antibodies_no_sessions,
        'report_ready_no_report': report_ready_no_report,
        'overall_stats': overall_stats,
    })


def pipeline_login(request):
    """Pipeline-specific login with redirect to overview."""
    from django.contrib.auth import authenticate, login
    from pipeline.models import Member
    from django.contrib.auth.models import User

    if request.user.is_authenticated:
        try:
            pipe_user = User.objects.using('pipeline_db').get(username=request.user.username)
            Member.objects.using('pipeline_db').get(user_id=pipe_user.pk, is_active=True)
            return redirect('pipeline:hub')
        except (User.DoesNotExist, Member.DoesNotExist):
            pass

    error = None
    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            return redirect('pipeline:hub')
        else:
            error = 'Invalid username or password.'

    return render(request, 'pipeline/login.html', {'error': error})
@require_http_methods(["GET", "POST"])
def pipeline_logout(request):
    """Signing out is a POST. A GET asks the question instead of answering it.

    This ended a session on a plain ``GET``, from a view with no decorator at
    all — so anything that follows a link without a person deciding to signed
    the reader out: a browser's link prefetcher, a chat client building an
    unfurl preview, a stray middle-click. The twelfth field test lost its run
    that way, having fetched the URL in the background purely to read it, and
    the only thing on screen afterwards was the login page. A real user loses
    whatever they had half-typed, and nothing anywhere says why.

    The nav posts a form, so the ordinary path is still one click. A GET that
    arrives anyway — a bookmark, a prefetch, a link somebody pasted — renders
    the confirmation rather than acting, which is what makes the fetch safe.
    Django's own ``LogoutView`` has been POST-only since 4.1 for this reason;
    ``academy.views.AcademyLogoutView`` had explicitly opted back out of it.
    """
    from django.contrib.auth import logout

    if request.method == 'POST':
        logout(request)
        return redirect('pipeline:pipeline_login')
    return render(request, 'pipeline/logout_confirm.html')


def _dash_member(request):
    from django.contrib.auth.models import User
    from pipeline.models import Member
    try:
        pu = User.objects.using(DB).get(username=request.user.username)
        return Member.objects.using(DB).get(user_id=pu.pk, is_active=True)
    except Exception:
        return None
