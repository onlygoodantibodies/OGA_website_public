"""
pipeline/services/planning.py

Planning artefact generators for experiment sessions.
Produces .xlsx downloads matching Riham's USP30 working sheet format.
"""

from io import BytesIO
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from pipeline.models import Antibody
# The `Ab#` column: the lab's own A-number, or nothing. It used to print a
# *record* id where there was no A-number, which put `4550` on a printed bench
# sheet under a heading a person reads as this antibody's number — and it is
# not one, the board says the vial is unnumbered, and nothing reads it back.
# A row still finds its way home on the upload: `bench_results._resolve_ab`
# matches the session's own antibodies on supplier and catalogue, both of which
# are on every row of every one of these sheets.
from pipeline.services import clonality as clonality_svc
from pipeline.services import lab_numbers
from pipeline.services import sessions as sess

DB = 'pipeline_db'

# Shared styles
HEADER_FONT = Font(bold=True, size=11, name='Arial')
DATA_FONT = Font(size=11, name='Arial')
HEADER_FILL = PatternFill('solid', fgColor='C0C0C0')
THIN_BORDER = Border(
    left=Side(style='thin'),
    right=Side(style='thin'),
    top=Side(style='thin'),
    bottom=Side(style='thin'),
)


def generate_wb_ip_sheet(session):
    """
    Generate a WB/IP planning sheet for a session's target.

    Matches Riham's Sheet 2 from the USP30 working sheet:
    - Gene, Ab#, Company, CatNumber, Conc, Clonality, Clone, Host,
      WB recommended dilution, WB used dilution (blank), IP volume (=2/conc formula)
    - Bold grey headers, borders, auto-width columns
    - IP volume as Excel formula so it recalculates if conc changes

    Returns an openpyxl Workbook.
    """
    target = session.target
    gene = target.gene_name or target.protein_name or 'Unknown'

    antibodies = (
        Antibody.objects.using(DB)
        .filter(target=target)
        .select_related('company')
        .order_by('company__name', 'catalogue_number')
    )

    wb = Workbook()
    ws = wb.active
    ws.title = 'WB IP'

    # -- Headers --
    headers = [
        ('Gene', 13),
        ('Ab#', 11),
        ('Company', 18),
        ('CatNumber', 20),
        ('Conc. (µg/mL)', 13),
        ('Clonality', 19),
        ('Clone', 12),
        ('Host', 11),
        ('WB recommended dilution', 24),
        ('WB used dilution', 16),
        ('IP (V in µl)', 14),
    ]

    # -- Title rows first (rows 1-2) --
    ws.cell(row=1, column=1, value=f'{gene} — WB/IP Planning Sheet').font = Font(bold=True, size=14, name='Arial')
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(headers))

    info_parts = []
    if session.cell_line_wt:
        info_parts.append(f'WT: {session.cell_line_wt.name}')
    if session.cell_line_ko:
        info_parts.append(f'KO: {session.cell_line_ko.name}')
    if session.date:
        info_parts.append(f'Date: {session.date.strftime("%d/%m/%Y")}')
    if session.experimenter:
        info_parts.append(str(session.experimenter))
    if session.site:
        info_parts.append(session.site.short_code)
    ws.cell(row=2, column=1, value=' · '.join(info_parts)).font = Font(size=10, name='Arial', color='666666')
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=len(headers))

    # -- Headers at row 3 --
    for col_idx, (header, width) in enumerate(headers, 1):
        cell = ws.cell(row=3, column=col_idx, value=header)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.border = THIN_BORDER
        cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    # -- Data rows starting at row 4 --
    conc_col_letter = get_column_letter(5)  # Column E = Conc

    for row_idx, ab in enumerate(antibodies, 4):
        wb_rec = ''
        if ab.supplier_recommended_dilutions:
            wb_rec = ab.supplier_recommended_dilutions.get('WB', '')

        row_data = [
            gene,
            lab_numbers.sheet_number(ab),
            ab.company.name if ab.company else '—',
            ab.catalogue_number,
            float(ab.concentration) if ab.concentration else None,
            clonality_svc.label(ab),
            ab.clone_id or '',
            ab.host_species or '',
            wb_rec or '—',
            '',  # WB used dilution — blank for planning
        ]

        for col_idx, value in enumerate(row_data, 1):
            cell = ws.cell(row=row_idx, column=col_idx, value=value)
            cell.font = DATA_FONT
            cell.border = THIN_BORDER

        # IP volume as Excel formula: =2/E{row}
        ip_cell = ws.cell(row=row_idx, column=len(headers))
        conc = ab.concentration
        if conc and float(conc) > 0:
            ip_cell.value = f'=2000/{conc_col_letter}{row_idx}'
            ip_cell.number_format = '0.00'
        else:
            ip_cell.value = '—'
        ip_cell.font = DATA_FONT
        ip_cell.border = THIN_BORDER

    # Print setup
    ws.page_setup.orientation = 'landscape'
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.print_title_rows = '3:3'

    return wb

def wb_ip_sheet_to_response(session):
    """Generate WB/IP sheet and return as Django HttpResponse for download."""
    from django.http import HttpResponse

    wb = generate_wb_ip_sheet(session)
    gene = session.target.gene_name or session.target.protein_name or 'target'

    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    response = HttpResponse(
        buffer.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    response['Content-Disposition'] = f'attachment; filename="{gene}_WB_IP_Planning.xlsx"'
    return response


# ─────────────────────────────────────────────────────────────
# IF Plate Map
# ─────────────────────────────────────────────────────────────

# 96-well plate: rows A-H, columns 1-12
# Controls in B02-B11
# Triton wells start C02, fill C02..C11 then D02..D11 then E02..E11
# Saponin wells start F02, fill F02..F11 then G02..G11 then H02..H11

CONTROL_WELLS = [
    ('B02', '', '', 'media', '__media'),
    ('B03', '', '', 'WT cells green+dapi', '__WT cells green+dapi'),
    ('B04', '', '', 'KO cells red+dapi', '__KO cells red+dapi'),
    ('B05', '', '', 'Dapi', '__Dapi'),
    ('B06', '', '', 'RbCoralite555+dapi', '__RbCoralite555+dapi'),
    ('B07', '', '', 'MsCoralite555+dapi', '__MsCoralite555+dapi'),
    ('B08', 'S6 Ribosomal', 'rabbit', 'S6 Ribosomal_1in250_T', '__S6 Ribosomal_1in250_T'),
    ('B09', 'ARID1A', 'rabbit', 'ARID1A_1in300_T', '__ARID1A_1in300_T'),
    ('B10', 'S6 Ribosomal', 'rabbit', 'S6 Ribosomal_1in250_S', '__S6 Ribosomal_1in250_S'),
    ('B11', 'ARID1A', 'rabbit', 'ARID1A_1in300_S', '__ARID1A_1in300_S'),
]


def _get_if_dilutions(ab):
    """
    Determine 2 IF dilutions for an antibody.

    Logic:
    - If supplier_recommended_dilutions has IF entry with a range (e.g. "1:200-1:800"),
      use the endpoints.
    - If IF entry is a single dilution (e.g. "1:100"), use it and double it (1:200).
    - If no IF recommendation: polyclonal → 1:250 + 1:500, monoclonal → 1:500 + 1:1000.
    """
    rec = ''
    if ab.supplier_recommended_dilutions:
        rec = ab.supplier_recommended_dilutions.get('IF', '')

    if rec and rec not in ('NA', 'NR', 'N/A', '—', '-'):
        # Try to parse range like "1:200-1:800" or "1/200-1/800"
        rec_clean = rec.replace('/', ':')
        if '-' in rec_clean and ':' in rec_clean:
            parts = rec_clean.split('-')
            try:
                nums = []
                for p in parts:
                    p = p.strip()
                    if ':' in p:
                        nums.append(int(p.split(':')[1]))
                if len(nums) >= 2:
                    return nums[0], nums[1]
            except (ValueError, IndexError):
                pass
        # Single dilution like "1:100"
        if ':' in rec_clean:
            try:
                dil = int(rec_clean.split(':')[1])
                return dil, dil * 2
            except (ValueError, IndexError):
                pass

    # Defaults by clonality
    clonality = (ab.clonality or '').lower()
    if 'poly' in clonality:
        return 250, 500
    else:
        return 500, 1000


def _well_sequence(start_row, start_col=2, end_col=11):
    """
    Generate well identifiers for a 96-well plate section.
    Yields 'C02', 'C03', ..., 'E11', then 'Plate2_C02', 'Plate2_C03', ...
    """
    rows = [chr(ord(start_row) + i) for i in range(3)]
    plate = 1
    while True:
        prefix = '' if plate == 1 else f'Plate{plate}_'
        for row_letter in rows:
            for col in range(start_col, end_col + 1):
                yield f'{prefix}{row_letter}{col:02d}'
        plate += 1


def generate_if_plate_map(session):
    """
    Generate an IF plate map workbook for a session.

    Matches Riham's Sheet 3 from the USP30 working sheet:
    - Flat list with: Plate#, Gene, Ab#, Company, CatNumber, Conc, Clonality,
      Host, Well, Recommended dilution, Used dilution, Histogram name
    - Controls in B02-B11
    - Triton wells from C02
    - Saponin wells from F02
    - 2 dilutions per antibody per permeabilization
    - Histogram naming: {company}_{catnum}_{1inDilution}_{T/S}

    Returns an openpyxl Workbook.
    """
    target = session.target
    gene = target.gene_name or target.protein_name or 'Unknown'

    # The session's own antibodies, not the gene's — the plate map allocates two
    # wells per antibody per permeabilisation, so a gene-wide list books wells for
    # antibodies nobody is testing. Same reason as the WB/IP/FC sheet.
    antibodies = _session_antibodies(session)

    wb = Workbook()
    ws = wb.active
    ws.title = f'{gene} Plate map for IF'

    # -- Title rows --
    ko_info = ''
    if session.cell_line_ko:
        ko_info = f'{gene} KO in {session.cell_line_wt.name if session.cell_line_wt else "?"}'
    ws.cell(row=1, column=1, value=ko_info or f'{gene} IF Plate Map').font = Font(bold=True, size=12, name='Arial')

    vial_info = ''
    if session.cell_line_ko:
        # Try to get c-number
        from pipeline.models import CellLineVial
        vials = CellLineVial.objects.using(DB).filter(cell_line=session.cell_line_ko).values_list('c_number', flat=True)[:1]
        if vials:
            vial_info = f'C-{vials[0]}'
    # Row 2 is `_session_info_line`, exactly as on the WB/IP/FC sheet — which is
    # where the session number lives, and where `bench_results._sheet_session_id`
    # reads it back. This row used to be the KO vial's C-number *alone*, so the IF
    # plate map was the one bench sheet carrying no session number at all: the
    # guard had nothing to check, and a plate map filled in for one session was
    # accepted by another without a word, while the panel above it promised
    # "both files carry this session's number". Two sessions of one gene differ
    # only in the readings written on them, so that is a plausible-looking record
    # made by one wrong click. The C-number keeps its place in the line, and the
    # "these are the gene's vials" caveat comes from `_session_info_line` now
    # rather than being spelled a second time here.
    ws.cell(row=2, column=1,
            value=_session_info_line(session, extra=[vial_info])
            ).font = Font(size=10, name='Arial', color='666666')

    # -- Headers at row 3 --
    headers = [
        ('Plate Number', 14),
        ('Gene', 10),
        ('Ab#', 10),
        ('Company', 22),
        ('CatNumber', 18),
        ('Conc. (µg/mL)', 13),
        ('Clonality', 18),
        ('Host', 10),
        ('Well number', 12),
        ('Recommended dilution', 22),
        ('Used dilution', 20),
        ('Name of histogram', 40),
        ('Specific signal', 22),
    ]

    for col_idx, (header, width) in enumerate(headers, 1):
        cell = ws.cell(row=3, column=col_idx, value=header)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.border = THIN_BORDER
        cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    n_cols = len(headers)
    row_idx = 4
    plate_num = ''  # User fills in

    # Colours for sections
    CONTROL_FILL = PatternFill('solid', fgColor='FFF2CC')  # Light yellow
    TRITON_FILL = PatternFill('solid', fgColor='E2EFDA')   # Light green
    SAPONIN_FILL = PatternFill('solid', fgColor='DEEBF7')  # Light blue

    def write_row(row_num, data, fill=None):
        data = list(data) + [''] * (n_cols - len(data))  # pad to the Specific-signal column
        for col_idx, value in enumerate(data, 1):
            cell = ws.cell(row=row_num, column=col_idx, value=value)
            cell.font = DATA_FONT
            cell.border = THIN_BORDER
            if fill:
                cell.fill = fill

    # -- Controls --
    for well, ctrl_name, host, used_dil, hist_name in CONTROL_WELLS:
        write_row(row_idx, [
            plate_num, '', '', '', '', '', ctrl_name or '', host,
            well, '', used_dil, hist_name,
        ], CONTROL_FILL)
        row_idx += 1

    # -- Triton test wells --
    triton_wells = _well_sequence('C')
    for ab in antibodies:
        dil1, dil2 = _get_if_dilutions(ab)
        company_name = ab.company.name if ab.company else ''
        cat_clean = (ab.catalogue_number or '').rstrip('*')

        rec_display = ''
        if ab.supplier_recommended_dilutions:
            rec_display = ab.supplier_recommended_dilutions.get('IF', '')

        for i, dil in enumerate([dil1, dil2]):
            well = next(triton_wells, '?')
            used = f'1in{dil}_T'
            hist = f'{company_name}_{cat_clean}_{used}'
            write_row(row_idx, [
                plate_num, gene, lab_numbers.sheet_number(ab), company_name,
                cat_clean, float(ab.concentration) if ab.concentration else '',
                clonality_svc.label(ab),
                ab.host_species or '', well,
                rec_display if i == 0 else '',  # Only show rec on first dilution
                used, hist,
            ], TRITON_FILL)
            row_idx += 1

    # -- Saponin test wells --
    saponin_wells = _well_sequence('F')
    for ab in antibodies:
        dil1, dil2 = _get_if_dilutions(ab)
        company_name = ab.company.name if ab.company else ''
        cat_clean = (ab.catalogue_number or '').rstrip('*')

        rec_display = ''
        if ab.supplier_recommended_dilutions:
            rec_display = ab.supplier_recommended_dilutions.get('IF', '')

        for i, dil in enumerate([dil1, dil2]):
            well = next(saponin_wells, '?')
            used = f'1in{dil}_S'
            hist = f'{company_name}_{cat_clean}_{used}'
            write_row(row_idx, [
                plate_num, gene, lab_numbers.sheet_number(ab), company_name,
                cat_clean, float(ab.concentration) if ab.concentration else '',
                clonality_svc.label(ab),
                ab.host_species or '', well,
                rec_display if i == 0 else '',
                used, hist,
            ], SAPONIN_FILL)
            row_idx += 1

    # Print setup
    ws.page_setup.orientation = 'landscape'
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.print_title_rows = '3:3'

    return wb


def if_plate_map_to_response(session):
    """Generate IF plate map and return as Django HttpResponse for download."""
    from django.http import HttpResponse

    wb = generate_if_plate_map(session)
    gene = session.target.gene_name or session.target.protein_name or 'target'

    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    response = HttpResponse(
        buffer.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    response['Content-Disposition'] = f'attachment; filename="{gene}_IF_Plate_Map.xlsx"'
    return response


# ─────────────────────────────────────────────────────────────
# Round-trip bench sheet — one downloadable sheet per session that
# matches its procedure and reads back to record results (see
# services/bench_results.py). One row per antibody for WB / IP / FC;
# IF uses the plate map above (well-based).
# ─────────────────────────────────────────────────────────────

# Per-procedure fillable result columns: (result_field, header label, width).
# The header labels are the contract the upload parser matches on — keep them
# in step with bench_results.RESULT_HEADER_ALIASES.
BENCH_RESULT_COLUMNS = {
    'WB': [('dilution', 'Used dilution', 16), ('signal', 'Signal', 26), ('rating', 'Rating', 14)],
    'IP': [('amount_of_antibody', 'Ab amount (µl)', 14), ('amount_of_lysate', 'Lysate (µg)', 13),
           ('enrichment', 'Enrichment', 26), ('ip_assessment', 'IP assessment', 20)],
    'FC': [('concentration', 'Used concentration', 18), ('histogram_shift', 'Histogram shift', 22),
           ('median_fluorescence_wt', 'MFI WT', 11), ('median_fluorescence_ko', 'MFI KO', 11),
           ('gating_strategy', 'Gating strategy', 22)],
}

# The stable key column that ties a row back to its antibody on upload.
AB_KEY_HEADER = 'Ab#'


def _session_antibodies(session):
    """The antibodies **in this session** — one per result row, in order.

    This used to be every antibody recorded for the gene, which is a different
    set and almost always a larger one: a two-antibody IP session printed a
    four-row sheet, and the third field test filled one in. A row on a bench
    sheet is an invitation to write a reading on it, so an extra row is a result
    for an experiment nobody ran.

    A session with no result rows has nothing of its own to print, so the sheet
    falls back to the gene's vials — this site's, if it has any — as a picking
    list, and ``_session_info_line`` says so on the sheet rather than letting it
    look like the session's own antibodies.
    """
    model = sess.RESULT_MODEL_MAP.get(session.procedure_type)
    if model is not None:
        rows = (model.objects.using(DB).filter(session_id=session.pk)
                .select_related('antibody', 'antibody__company').order_by('pk'))
        seen, out = set(), []
        for r in rows:
            ab = r.antibody
            if ab is not None and ab.pk not in seen:
                seen.add(ab.pk)
                out.append(ab)
        if out:
            return out

    qs = (Antibody.objects.using(DB).filter(target=session.target)
          .select_related('company').order_by('company__name', 'catalogue_number'))
    if session.site_id:
        own = list(qs.filter(site_id=session.site_id))
        if own:
            return own
    return list(qs)


def _session_has_own_antibodies(session) -> bool:
    """True when the sheet's rows are the session's result rows rather than a
    fallback picking list. Drives the caveat in the info line."""
    model = sess.RESULT_MODEL_MAP.get(session.procedure_type)
    if model is None:
        return False
    return model.objects.using(DB).filter(
        session_id=session.pk, antibody__isnull=False).exists()


# How the sheet says which session it belongs to, and how the importer reads it
# back. A bench sheet is printed, carried around and typed up days later, and
# nothing on it named the session — so uploading Monday's WB sheet into
# Wednesday's session was one wrong click with no way to notice. Same job as the
# workbook's `session_ref`, in the one line a bench sheet has room for.
SESSION_STAMP = 'Session #'


def _session_info_line(session, extra=()):
    """The grey line under the title, on every bench sheet this module writes.

    ``extra`` is for what one sheet has and the others do not — the IF plate
    map's KO vial C-number — so a sheet can add to the line without opting out
    of it. Opting out is what the plate map did, and the stamp went with it.
    """
    parts = [f'{SESSION_STAMP}{session.pk}']
    if session.cell_line_wt:
        # The full label, site included: a bare `HAP1` is shared by hundreds of
        # rows, so a sheet that names one is not saying which.
        from pipeline.services import cell_lines as clines
        parts.append(f'WT: {clines.label(session.cell_line_wt)}')
    if session.cell_line_ko:
        parts.append(f'KO: {session.cell_line_ko.name}')
    if session.date:
        parts.append(f'Date: {session.date.strftime("%d/%m/%Y")}')
    if session.experimenter:
        parts.append(str(session.experimenter))
    if session.site:
        parts.append(session.site.short_code)
    parts.extend(p for p in extra if p)
    if not _session_has_own_antibodies(session):
        # Say it on the sheet. Otherwise a fallback list of the gene's vials is
        # indistinguishable from the session's own antibodies, which is the
        # confusion this whole function exists to stop.
        parts.append('no antibodies recorded for this session yet — '
                     'these are the gene\'s vials to choose from')
    return ' · '.join(parts)


def _generate_antibody_bench_sheet(session, proc):
    """WB / IP / FC bench sheet: one row per antibody, identity columns + blank
    fillable result columns. Column 2 is the Ab# key used on upload."""
    gene = session.target.gene_name or session.target.protein_name or 'Unknown'
    antibodies = _session_antibodies(session)

    wb = Workbook()
    ws = wb.active
    ws.title = {'WB': 'WB', 'IP': 'IP', 'FC': 'FC'}.get(proc, proc)

    proc_label = dict(session.ProcedureType.choices).get(proc, proc)
    # µg/mL, because that is what `Antibody.concentration` holds and what this
    # column is filled from. It said mg/mL over values of 1000, 500 and 250 —
    # a thousand-fold mislabel on the one artefact that goes to the bench, where
    # somebody dilutes from it. The paste checker converts units correctly
    # (`services/concentration.py`) and the workbook headed it `(ug/mL)`; only
    # this sheet was wrong.
    id_cols = [('Gene', 12), (AB_KEY_HEADER, 10), ('Company', 20), ('CatNumber', 20),
               ('Conc. (µg/mL)', 12), ('Clonality', 18), ('Clone', 12), ('Host', 10)]
    if proc == 'FC':
        id_cols.append(('Tube', 8))
    result_cols = BENCH_RESULT_COLUMNS[proc]
    headers = id_cols + [(lbl, w) for (_f, lbl, w) in result_cols]

    ws.cell(row=1, column=1, value=f'{gene} — {proc_label} bench sheet').font = Font(bold=True, size=14, name='Arial')
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(headers))
    ws.cell(row=2, column=1, value=_session_info_line(session)).font = Font(size=10, name='Arial', color='666666')
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=len(headers))

    for col_idx, (header, width) in enumerate(headers, 1):
        cell = ws.cell(row=3, column=col_idx, value=header)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.border = THIN_BORDER
        cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    row_idx = 4
    for ab in antibodies:
        base = [
            gene,
            lab_numbers.sheet_number(ab),
            ab.company.name if ab.company else '—',
            ab.catalogue_number,
            float(ab.concentration) if ab.concentration else None,
            clonality_svc.label(ab),
            ab.clone_id or '',
            ab.host_species or '',
        ]
        if proc == 'FC':
            base.append('')  # Tube — filled at bench
        row_data = base + ['' for _ in result_cols]
        for col_idx, value in enumerate(row_data, 1):
            cell = ws.cell(row=row_idx, column=col_idx, value=value)
            cell.font = DATA_FONT
            cell.border = THIN_BORDER
        row_idx += 1

    ws.page_setup.orientation = 'landscape'
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.print_title_rows = '3:3'
    ws.freeze_panes = 'A4'
    return wb


def generate_bench_sheet(session):
    """The right bench sheet for a session's procedure. IF → the plate map;
    WB/IP/FC → one row per antibody with fillable result columns."""
    proc = session.procedure_type
    if proc == 'IF':
        return generate_if_plate_map(session)
    return _generate_antibody_bench_sheet(session, proc)


def bench_sheet_to_response(session):
    from django.http import HttpResponse
    wb = generate_bench_sheet(session)
    gene = session.target.gene_name or session.target.protein_name or 'target'
    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    response = HttpResponse(
        buffer.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    # The session number is in the file (row 2, `_session_info_line`) and the
    # page says "both files carry this session's number" — but the name did not,
    # so two bench sheets for one gene and procedure collide in Downloads as
    # `(1)` and `(2)` and the only way to tell them apart is to open both. The
    # workbook has carried its number since it was written; this is the other
    # half of the same sentence.
    response['Content-Disposition'] = (
        f'attachment; filename="{gene}_{session.procedure_type}_'
        f'session_{session.pk}_bench_sheet.xlsx"')
    return response
