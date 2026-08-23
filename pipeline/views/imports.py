"""
Excel/CSV templates + upload for the paste tools.

A downloadable .xlsx whose columns match the paste parsers, and an upload
endpoint that turns an .xlsx/.csv back into the same preview the paste box
produces — so a file round-trips through the existing plan→commit flow with no
new commit path.
"""
import csv
import io

from django.http import HttpResponse, JsonResponse
from django.views.decorators.http import require_GET, require_POST

from pipeline.decorators import pipeline_member_required
from pipeline.models import Member
from pipeline.services import board_columns
from pipeline.services import bulk_antibodies as bulk
from pipeline.services import bulk_cell_lines as bulkcl
from pipeline.services import bulk_sessions as bulksess
from pipeline.services import example_row
from pipeline.services import workbook as wbk
from pipeline.services.cropper import metadata as meta

DB = "pipeline_db"

# Columns match the parser aliases; order is just a sensible sheet layout.
#
# `site` is here — and blank in both examples — because the downloads carry it and
# the parsers now honour it. Blank means "my site", which is what someone entering
# their own bench's reagents wants; a name means that bench, which is what makes a
# downloaded sheet safe to edit and put back.
# `concentration` names its unit, because the number is stored without one and a
# bare column heading meant a supplier's own "1.0 mg/mL" went in as 1 µg/mL — a
# thousand-fold error nothing on screen could catch. A unit typed in the cell is
# converted (services/concentration.py), so the example shows one. Spelled "ug"
# rather than "µg" on purpose: the paste header alias map already knows
# "concentration ugml", and its normaliser drops the µ.
#
# **The example's unit is the header's unit.** It read `1.0 mg/mL` under a
# heading saying `(ug/mL)`, which is the sheet contradicting itself on the one
# column where a misread costs a factor of a thousand — and the reader who
# resolves it in the header's favour writes a bare `1.0` for a milligram stock,
# which is the original bug typed by hand. Showing an explicit `1.0 ug/mL`
# teaches the thing that actually matters: the unit belongs *in the cell*. Any
# other unit is still converted, and a cell this cannot read is refused by name
# with the units it takes.
# `supplier recommendations` is the one name for that column across every sheet
# — see `cropper/metadata.py::HEADER_ALIASES`. It said `applications` here and
# `supplier claims` in the board's own download, for the same field.
# `ab #` is last, not first, and that is on purpose twice over. The `e.g.`
# marker lives in the sheet's **first** cell (`services/example_row.py`), and a
# gene page's upload panel tells you to clear the gene column — so the marker
# has to stay on a column that page does not ask you to empty. And a number is
# the one column nobody needs to fill in: leave it blank and the record is given
# this bench's next one when it is created (`services/lab_numbers.py`). The
# example leaves it blank for exactly that reason — an example number would
# teach people to type one, and a typed number that is already in use is
# refused.
ANTIBODY_COLUMNS = ["gene", "catalogue", "company", "rrid", "host", "clonality",
                    "clone", "lot", "site", "concentration (ug/mL)", "product url",
                    "supplier recommendations", "comments", "ab #"]
ANTIBODY_EXAMPLE = ["SNCA", "ab138501", "Abcam", "AB_2537855", "Rabbit", "recombinant",
                    "EPR1234", "L123", "", "1.0 ug/mL", "https://www.abcam.com/...",
                    "WB, IF", "from the Feb order", ""]
# **The example is the lab's own convention, checked against the database.**
#
# It used to read `HAP1 SNCA KO` in the name column. Not one of the 564 cell lines
# on file is named that way: `name` is the bare parental line — `HAP1` 151 times,
# `HeLa` 54, `HEK293T` 32 — with the gene in its own column and `genotype` saying
# which it is. 395 of the 398 knockouts do not mention their gene in the name. So
# the example was teaching a second convention alongside the real one, on the
# surface where new rows are typed, and `services/cell_lines.py` exists precisely
# because hundreds of rows share a name.
#
# `parent` is a C-number here for the same reason: of the 169 parents recorded,
# 145 are written as C-numbers and 24 as names. A name works too — see
# `bulk_cell_lines.resolve_parent`, which now reads both.
#
# `paired ko` is blank because this example is a knockout and only a wild type
# has one, which is what the column's own name now says.
CELL_LINE_COLUMNS = ["name", "gene", "genotype", "parent", "c number",
                     "paired ko (WT rows)",
                     "cellosaurus", "supplier", "catalogue", "lot", "site", "medium",
                     "clone", "storage", "freezer", "box", "position", "comments"]
CELL_LINE_EXAMPLE = ["HAP1", "SNCA", "KO", "C-48", "632", "", "CVCL_0030",
                     "Horizon", "HZGHC001", "", "", "IMDM", "", "-80", "F2", "B1", "A1",
                     "from the Feb order"]

# Targets are a one-column import — the gene symbol, and everything else comes
# from UniProt. It lived as a literal in `views/target_board.py` while the other
# three read this module, so the one board whose columns were hard-coded was
# also the only one with no downloadable template. Same source now, four kinds.
TARGET_COLUMNS = ["gene"]
TARGET_EXAMPLE = ["SOD1"]

_KINDS = {
    "antibodies": {"columns": ANTIBODY_COLUMNS, "example": ANTIBODY_EXAMPLE, "title": "Antibodies"},
    "cell-lines": {"columns": CELL_LINE_COLUMNS, "example": CELL_LINE_EXAMPLE, "title": "Cell lines"},
    "sessions": {"columns": bulksess.SESSION_COLUMNS, "example": bulksess.SESSION_EXAMPLE,
                 "title": "Session antibodies"},
    "targets": {"columns": TARGET_COLUMNS, "example": TARGET_EXAMPLE, "title": "Targets"},
}


def columns_and_example(kind):
    """The column list and example row for one import kind.

    One source read by three surfaces — the downloadable Excel template, the
    paste parser's expected order, and the new-entry table on each board — so a
    column added here appears in all three rather than in two of them.
    """
    spec = _KINDS.get(kind)
    if not spec:
        return [], []
    return list(spec["columns"]), list(spec["example"])


def _member(request):
    from django.contrib.auth.models import User
    try:
        pu = User.objects.using(DB).get(username=request.user.username)
        return Member.objects.using(DB).get(user_id=pu.pk, is_active=True)
    except Exception:
        return None


@pipeline_member_required
@require_GET
def import_template(request, kind):
    cfg = _KINDS.get(kind)
    if not cfg:
        return HttpResponse("Unknown template", status=404)
    import openpyxl
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = cfg["title"][:31]
    ws.append(cfg["columns"])
    # **Row 2 says it is an example.** Grey italics asked the reader to infer that
    # from the formatting, and the personal review said plainly that it is not
    # clear you are meant to type over it. The first cell is labelled `e.g.`, so
    # it is a fact a person reads and a fact `services/example_row.py` acts on —
    # every parser drops the row, and your own first record goes on row 3.
    example = list(cfg["example"])
    if example:
        example[0] = example_row.mark(example[0])
    ws.append(example)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="064C83")
    for c in ws[2]:
        c.font = Font(italic=True, color="9AA7B4")
        c.fill = PatternFill("solid", fgColor="F4F6FB")
    for i, col in enumerate(cfg["columns"], 1):
        ws.column_dimensions[get_column_letter(i)].width = max(12, len(col) + 4)
    # Below the example, so both the headings and the row that explains them stay
    # on screen while you fill the sheet in.
    ws.freeze_panes = "A3"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    resp = HttpResponse(
        buf.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp["Content-Disposition"] = f'attachment; filename="{kind.replace("-", "_")}_template.xlsx"'
    return resp


def _known_headers(kind):
    """Every column name this kind's parser answers to, for sheet-picking.

    The template's own columns plus the parser's aliases, so a sheet somebody
    renamed a column on is still recognised as the data sheet.
    """
    names = set(_KINDS.get(kind, {}).get("columns", ()))
    if kind == "cell-lines":
        names |= set(bulkcl.HEADER_ALIASES)
    elif kind == "sessions":
        names |= set(bulksess._ALIASES)
    elif kind == "antibodies":
        names |= set(meta.HEADER_ALIASES) if hasattr(meta, "HEADER_ALIASES") else set()
    return names


def _file_to_text(f, kind=""):
    """An uploaded .xlsx / .csv / .tsv as the tab-separated text the paste
    parsers already understand (header row + data rows).

    ``(text, note)`` — the note names the sheet that was read when the workbook
    had more than one, because this reader used to take ``wb.active`` and a
    ``Notes`` tab left selected in Excel therefore replaced the data, silently.
    See ``services/workbook.py``.
    """
    sheet = wbk.read(f, recognise=wbk.header_scorer(_known_headers(kind)),
                     prefer=[_KINDS.get(kind, {}).get("title", "")])
    lines = []
    for r in sheet.rows:
        cells = ["" if c is None else str(c).strip() for c in r]
        if any(cells):
            lines.append("\t".join(cells))
    return "\n".join(lines), sheet.note


def _column_letter(i: int) -> str:
    """0 → A, 25 → Z, 26 → AA. Spreadsheet coordinates, because that is what the
    person fixing the sheet is looking at."""
    name = ""
    while True:
        name = chr(ord("A") + i % 26) + name
        i = i // 26 - 1
        if i < 0:
            return name


def _blank_headings(header: list[str]) -> list[str]:
    """Column letters in the header row with no heading text.

    **A merged cell in row 1 is the ordinary way to get one.** Merging `A1:B1`
    keeps A1's text and blanks B1, so a heading disappears from a sheet that
    still looks complete on screen — which is why this is worth naming rather
    than describing as "a column I did not recognise".
    """
    return [_column_letter(i) for i, cell in enumerate(header) if not cell.strip()]


def _header_line(header: list[str]) -> str:
    return ", ".join(c.strip() or "(blank)" for c in header)


def _heading_note(text: str) -> str:
    """A sentence about the header row worth showing even when the rows parsed.

    Merging two columns nobody matches on still loses whatever was under the
    second one, silently — the same failure as the key column, one column
    smaller. `sheet_note` is already drawn in amber above every preview
    (`board.js::sheetLine`), so it is where this belongs.
    """
    lines = (text or "").splitlines()
    if not lines:
        return ""
    blanks = _blank_headings(lines[0].split("\t"))
    if not blanks:
        return ""
    where = ", ".join(blanks)
    return (f"Column {where} of the header row has no heading, so nothing in "
            f"{'those columns' if len(blanks) > 1 else 'that column'} was read. "
            f"Merging cells in row 1 does this — it keeps the first cell's text "
            f"and blanks the rest.")


def _nothing_read_refusal(kind: str, text: str) -> str:
    """Why a sheet with rows in it produced no records — never a bare zero.

    **A preview reading "0 rows read. 0 new, 0 already known" over a file
    holding four antibodies is the worst answer this endpoint can give**, and it
    is what run 17 got from a sheet whose only fault was a merged pair of header
    cells: the `catalogue` heading vanished, `metadata.parse_table` drops every
    row that has no catalogue, and nothing between the two said so. A scientist
    who tidied a sheet, uploaded it, read "0 rows" and pressed Save would
    conclude their edits had already been applied.

    Deliberately not a special case for merged cells. The tester's own reading
    was that the defect is wider than the merge — *any* missing or renamed
    heading does the same — and it is right, so the refusal names the headings it
    actually read and which of the matching columns are not among them. That
    answers a deleted column, a renamed one and a merge with one message.
    """
    # **The example row is not a row somebody wrote**, so a blank template
    # uploaded untouched has nothing in it and is not a failure — it previews as
    # nothing to do, which is the truth. Counted after `example_row.drop` for
    # that reason: refusing it would turn "download the sheet and look at it"
    # into an error message.
    lines = (text or "").splitlines()
    body = example_row.drop(text or "").splitlines()
    if len(lines) < 2 or len(body) < 2:
        return ""
    header = lines[0].split("\t")
    thing = {"antibodies": "antibody", "cell-lines": "cell line",
             "targets": "target", "sessions": "session"}.get(kind, "record")
    # Verbs agree with counts, and counts are often 1 — the rule `OGABoard.plural`
    # holds on the other side of the wire.
    n = len(lines) - 1
    parts = [f"Read {n} {'row' if n == 1 else 'rows'} from that sheet and could "
             f"not make {'an' if thing[0] in 'aeiou' else 'a'} {thing} out of "
             f"{'it' if n == 1 else 'any of them'}."]
    note = _heading_note(text)
    if note:
        parts.append(note)
    matched_on = [c.sheet for c in board_columns.registry(kind)
                  if c.upload == board_columns.MATCH]
    present = {c.strip().lower() for c in header}
    missing = [m for m in matched_on if m.lower() not in present]
    if missing:
        parts.append(f"A row is matched on {', '.join(matched_on)}; "
                     f"this sheet has no {', '.join(missing)} column.")
    parts.append(f"The headings read were: {_header_line(header)}.")
    return " ".join(parts)


@pipeline_member_required
@require_POST
def import_upload(request, kind):
    """Parse an uploaded file into the same preview the paste box returns."""
    if kind not in _KINDS:
        return JsonResponse({"error": "unknown import type"}, status=404)
    f = request.FILES.get("file")
    if not f:
        return JsonResponse({"error": "no file uploaded"}, status=400)
    try:
        text, sheet_note = _file_to_text(f, kind)
    except Exception as e:
        return JsonResponse({"error": f"could not read the file ({e})"}, status=400)
    if not text.strip():
        # "Nothing was read" is a refusal, not an empty preview: the panel showed
        # nothing at all for it, which is how a whole upload could fail in
        # silence. Name the sheets so the reader can see which one it landed on.
        return JsonResponse(
            {"error": "No rows were read from that file. "
                      + (sheet_note or "The sheet is empty.")},
            status=400)

    create_targets = request.POST.get("create_targets") in ("true", "1", "on")
    default_gene = request.POST.get("default_gene", "")

    # `text` goes back with the preview so the caller can commit exactly what it
    # was shown, through the same bulk_*_commit endpoint the paste box uses.
    # Without it a board would need its own commit endpoint for uploads, which
    # is a second write path for the same job — and the two would drift.
    # A heading that went missing is worth saying whether or not the rows
    # survived it, so it rides on the note the panel already draws.
    sheet_note = " ".join(p for p in (sheet_note, _heading_note(text)) if p)

    if kind == "antibodies":
        # `header_led`: this is a file, so it has a header row and is read by
        # it. Without that the RRID-anchored PDF walk runs on spreadsheets and
        # invents a catalogue number — see `metadata.parse_table`.
        rows = bulk.parse(text, default_gene, header_led=True)
        # Refused only when the sheet *had* rows in it and none of them became a
        # record — an empty sheet, or a blank template, previews as nothing to do
        # because that is what it is.
        if not rows:
            refusal = _nothing_read_refusal(kind, text)
            if refusal:
                return JsonResponse({"error": refusal}, status=400)
        items = bulk.plan(rows, create_targets=create_targets,
                          member=_member(request))
        return JsonResponse({"items": items, "summary": bulk.summarize(items),
                             "text": text, "sheet_note": sheet_note})

    if kind == "cell-lines":
        rows = bulkcl.parse(text, default_gene, request.POST.get("default_genotype", ""))
        if not rows:
            refusal = _nothing_read_refusal(kind, text)
            if refusal:
                return JsonResponse({"error": refusal}, status=400)
        items = bulkcl.plan(rows, create_targets=create_targets, member=_member(request))
        return JsonResponse({"items": items, "summary": bulkcl.summarize(items),
                             "text": text, "sheet_note": sheet_note})

    # Other kinds (e.g. "sessions") have their own upload endpoints.
    return JsonResponse({"error": f"upload not supported for '{kind}' here"}, status=400)
