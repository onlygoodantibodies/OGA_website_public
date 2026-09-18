"""
Excel/CSV templates + upload for the paste tools.

A downloadable .xlsx whose columns match the paste parsers, and an upload
endpoint that turns an .xlsx/.csv back into the same preview the paste box
produces — so a file round-trips through the existing plan→commit flow with no
new commit path.
"""
import csv
import io
import logging

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

logger = logging.getLogger(__name__)

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
# `storage`, `box` and `received` are the columns uOttawa wrote into their own
# spreadsheet because the portal appeared not to have them. It did:
# `InventoryLocation` holds a freezer and a box for 3,058 antibodies and
# `Antibody.received_date` is filled on 2,841 — both invisible, in no sheet, and
# so silently dropped from any file that carried them. `services/storage.py` and
# `services/received.py` read them now.
#
# The received example is a full date because that is the habit worth teaching,
# but a **month is a real answer** and stays one: `Aug 2026` is stored as August
# and printed as August, never promoted to the 1st (`services/received.py`). A
# slashed date whose two numbers could each be the month is refused rather than
# guessed — `01/02/2026` is two different days and the cell does not say which.
ANTIBODY_COLUMNS = ["gene", "catalogue", "company", "rrid", "host", "clonality",
                    "clone", "lot", "site", "concentration (ug/mL)", "product url",
                    "supplier recommendations", "isotype", "reactivity",
                    "storage", "box", "received", "acquisition", "comments",
                    "ab #"]
ANTIBODY_EXAMPLE = ["SNCA", "ab138501", "Abcam", "AB_2537855", "Rabbit", "recombinant",
                    "EPR1234", "L123", "", "1.0 ug/mL", "https://www.abcam.com/...",
                    "WB, IF", "IgG", "H, M, R", "-20", "42", "2026-08-14",
                    "in_kind", "from the Feb order", ""]
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
# `paired ko (WT rows)` was here and was removed on 14 Sep 2026 — see
# `bulk_cell_lines.HEADER_ALIASES` for the two live rows it had produced and why
# the write path went with it rather than being repaired.
#
# `growth properties` is next to `medium` because they are one question — how to
# culture this line. uOttawa asked for it: a student looking a line up needs to
# know whether it is adherent or in suspension before they thaw it (14 Sep
# 2026). The column was not missing from the app, only from every screen and
# every sheet — `CellLine.growth_properties` carries 411 rows from the Access
# import — which is the same shape as the freezer box and the received date.
CELL_LINE_COLUMNS = ["name", "gene", "genotype", "parent", "c number",
                     "cellosaurus", "supplier", "catalogue", "lot", "site", "medium",
                     "growth properties", "species",
                     "clone", "storage", "freezer", "box", "position", "comments"]
CELL_LINE_EXAMPLE = ["HAP1", "SNCA", "KO", "C-48", "632", "CVCL_0030",
                     "Horizon", "HZGHC001", "", "", "IMDM", "adherent", "Human",
                     "", "-80", "F2", "B1", "A1",
                     "from the Feb order"]

# **A column's own sentence, on the column, in the file people actually read.**
#
# uOttawa downloaded this template five times and then emailed to ask what
# column F means (14 Sep 2026). Everything about that question was reasonable.
# The header said `paired ko (WT rows)` and the sheet said nothing else: the
# `e.g.` row left that cell blank, the board's grid had no such column,
# `board_columns.CELL_LINES` had no such column, and the one sentence explaining
# it lived inside the Add cell lines pop-out, which is not open when you are in
# Excel. **That column is gone now** — asking what it was for turned out to be
# the right instinct, and the answer on live was two rows and both of them wrong
# (`bulk_cell_lines.HEADER_ALIASES`). The rule it taught outlives it: a heading
# nobody can interpret is a column nobody can fill in correctly.
#
# So the sentence rides on the header cell, as a comment. It is the only place
# in a spreadsheet a sentence can go without adding a row that every parser
# would then have to drop — an extra instruction row is exactly what
# `services/example_row.py` exists to handle, and one is enough. Comments are
# inert to every reader here: `workbook.read` scores a sheet on its header
# *values*, and nothing walks `ws.comments`.
#
# Keyed per kind on the template's own column names, so a renamed column leaves
# an orphan note rather than a silently unexplained one —
# `tests_conventions.py` pins every key against the column list. Cell lines
# only, deliberately: these are conventions already written down in
# `bulk_cell_lines.HEADER_ALIASES`, the board's Add panel and
# CELL_LINE_BOARD_GUIDE.md, restated here where the question was asked. A note
# invented for a column nobody has asked about would be a fifth statement of a
# convention with nothing behind it.
TEMPLATE_NOTES = {
    "cell-lines": {
        "name": (
            "The line's own name — HAP1, HeLa, U2OS. Not the knockout: the gene "
            "goes in the gene column and genotype says which it is. 395 of the "
            "398 knockouts on file do not mention their gene in the name."),
        "gene": (
            "For a KO row, the gene it is a knockout of.\n\n"
            "A wild type has none. Leave it blank or write NA — both mean the "
            "same and neither creates a gene. Filling in a real gene here "
            "creates a second, wrong line."),
        "genotype": "WT for a parental line, KO for a knockout.",
        "parent": (
            "For a KO row, the wild type it was made from — by name (HAP1) or by "
            "C-number (C-48). Most rows on file use the C-number.\n\n"
            "It must be a wild type of the same background: HeLa KO with a HAP1 "
            "parent is refused rather than written."),
        "c number": (
            "This line's own number, as written on the tube. 631, C-631 and C631 "
            "all read the same.\n\n"
            "Leave it blank and the app issues the next one for your site, which "
            "is the usual case. A number another line at your bench already "
            "carries is refused by name."),
        "cellosaurus": "The Cellosaurus RRID for the parental line, e.g. CVCL_0030.",
        "supplier": "Who the line came from, e.g. Horizon.",
        "catalogue": "The supplier's catalogue number for the line.",
        "lot": "The supplier's lot number, if the vial carries one.",
        "site": (
            "Which bench holds this line. Leave it blank and it is filed under "
            "yours.\n\n"
            "A site not on file is refused by name rather than swapped for "
            "yours, so a downloaded sheet can go back without every row being "
            "re-homed to whoever uploads it."),
        "medium": "The growth medium, in your own words, e.g. IMDM + 10% FBS.",
        "species": (
            "Which species the line came from — Human, Mouse, and so on.\n\n"
            "Left blank it is recorded as Human, which is what 604 of the 616 "
            "lines on file are."),
        "growth properties": (
            "How the line grows — adherent or suspension.\n\n"
            "Written in your own words; the board offers the spellings already "
            "in use rather than narrowing it to a list."),
        "clone": (
            "Which single-cell clone this knockout is — a separate origin, often "
            "a separate guide. A gene and a background define the knockout; the "
            "clone says which one.\n\n"
            "Leave it blank if it was not written down. Blank means 'not "
            "recorded', so it never narrows a match and never picks one clone "
            "out of several."),
        "storage": (
            "Where the vial is kept, in your own words. The shelf is read out of "
            "it, so -80, -20 or LN2 in here sets the storage type."),
        "freezer": "Which freezer, e.g. F2.",
        "box": "Which box in that freezer, e.g. B1.",
        "position": "Where in the box, e.g. A1.",
        "comments": (
            "Anything about where this line came from, in your own words. It is "
            "kept on the row and shown on the board."),
    },
}

# **The gene is the only column UniProt can fill; the nomination is not.**
#
# This was one column, on the reasoning that everything else about a target is
# sourced rather than typed — protein name, accession and mass all come from
# UniProt, and offering them here would teach people to type values the app can
# get right by itself. That half still holds and those columns are still absent.
#
# What it missed is that a *nomination* is not sourced from anywhere. Site,
# funder, project, funded and the nomination date are per-row facts about whose
# list a gene is going on, and they were reachable only as the four dropdowns on
# the Add panel — **one site, one funder, one project for the whole press**. So a
# spreadsheet of sixty genes spanning two benches and three grants, which is the
# case the spreadsheet route exists for, could not be expressed at all: you
# uploaded them under one site and then corrected the rest on the board.
#
# All six are columns `target_list_io.COLUMNS` already parses, previews and
# writes, so this adds no parser and no write path — it stops the blank sheet
# being narrower than the door it is posted to. The headers are that module's
# own aliases, which is what makes the file round-trip: download the board's
# export, or fill this in, and the same reader takes both.
#
# Deliberately still absent, and each for its own reason: `protein name`,
# `Uniprot ID`, `alternative protein name` and mass (UniProt sources them);
# `Essential gene (depmap)?` (DepMap does); and Zenodo/F1000/Status/Conclusion,
# which are facts about work that has not started on a gene you are only now
# adding. The board's six computed columns are not here either — a template is
# for typing into, and `Sites pursuing` is not a thing anybody types.
TARGET_COLUMNS = ["gene"]
TARGET_EXAMPLE = ["SOD1"]

# **The blank sheet and the on-board grid are two surfaces, and here they differ.**
#
# `columns` above is read by three things — the Excel template, the new-entry
# table on the board, and the order the paste parser expects — and for the other
# three kinds one list is right for all three. Targets are the exception, in the
# one direction that bites: `bulk_targets.parse` splits a paste on **tabs** and
# treats every token as a gene symbol, because that panel's site, funder and
# project are dropdowns for the whole press rather than columns. Widening the
# shared list therefore did not add columns to that grid so much as teach it to
# create targets called `CIHR`, `yes` and `2026` — one row, seven junk genes,
# with a preview that would have confirmed none of them and an Add button that
# never asked.
#
# So the template carries the nomination columns and the grid does not. They are
# posted to different endpoints and read by different parsers, which is the whole
# reason they can differ: the sheet goes to `target_list_io`, which has read all
# six of these since Carl's workbook was first supported.
#
# `funded` is the word the board's own filter uses; `target_list_io.parse_funding`
# reads "Funding available", "yes", "no" and a blank. The date is a year on
# purpose — the real sheet records nominations as `2020` about as often as a full
# date, and `parse_date` keeps whatever precision it was given.
TARGET_TEMPLATE_COLUMNS = ["gene", "site", "funder", "project", "funded",
                           "date of nomination", "comments"]
TARGET_TEMPLATE_EXAMPLE = ["SOD1", "Leicester", "CIHR", "ALS-RAP", "yes", "2026",
                           "from the March call"]

_KINDS = {
    "antibodies": {"columns": ANTIBODY_COLUMNS, "example": ANTIBODY_EXAMPLE, "title": "Antibodies"},
    "cell-lines": {"columns": CELL_LINE_COLUMNS, "example": CELL_LINE_EXAMPLE, "title": "Cell lines"},
    "sessions": {"columns": bulksess.SESSION_COLUMNS, "example": bulksess.SESSION_EXAMPLE,
                 "title": "Session antibodies"},
    "targets": {"columns": TARGET_COLUMNS, "example": TARGET_EXAMPLE, "title": "Targets",
                "template_columns": TARGET_TEMPLATE_COLUMNS,
                "template_example": TARGET_TEMPLATE_EXAMPLE},
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


def template_columns_and_example(kind):
    """What the **blank sheet** carries, which is not always the board's grid.

    Defaults to `columns_and_example`, so three of the four kinds are unchanged
    and a new kind gets the shared list unless it says otherwise. Targets
    override it — see `TARGET_TEMPLATE_COLUMNS` for why the two must differ.
    """
    spec = _KINDS.get(kind)
    if not spec:
        return [], []
    return (list(spec.get("template_columns") or spec["columns"]),
            list(spec.get("template_example") or spec["example"]))


@pipeline_member_required
@require_GET
def import_template(request, kind):
    cfg = _KINDS.get(kind)
    if not cfg:
        return HttpResponse("Unknown template", status=404)
    columns, example_values = template_columns_and_example(kind)
    import openpyxl
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = cfg["title"][:31]
    ws.append(columns)
    # **Row 2 says it is an example.** Grey italics asked the reader to infer that
    # from the formatting, and the personal review said plainly that it is not
    # clear you are meant to type over it. The first cell is labelled `e.g.`, so
    # it is a fact a person reads and a fact `services/example_row.py` acts on —
    # every parser drops the row, and your own first record goes on row 3.
    example = list(example_values)
    if example:
        example[0] = example_row.mark(example[0])
    ws.append(example)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="064C83")
    # The convention on the column, where somebody reading the file will meet it
    # — see TEMPLATE_NOTES. Sized, because openpyxl's default comment box is
    # about two lines tall and clips the rest without saying so.
    notes = TEMPLATE_NOTES.get(kind, {})
    if notes:
        from openpyxl.comments import Comment
        for cell, col in zip(ws[1], columns):
            note = notes.get(col)
            if not note:
                continue
            cell.comment = Comment(note, "OGA pipeline")
            cell.comment.width, cell.comment.height = 340, 30 + 14 * len(note) // 42
    for c in ws[2]:
        c.font = Font(italic=True, color="9AA7B4")
        c.fill = PatternFill("solid", fgColor="F4F6FB")
    for i, col in enumerate(columns, 1):
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


def _ignored_note(kind: str, text: str) -> str:
    """Which of this sheet's columns the upload will not act on.

    ``board_columns.read_only()`` was written to be the answer to this and had
    **no callers at all** — so its own docstring's promise ("a preview says it
    is ignored rather than leaving somebody to find out") was true of the
    header text and of nothing else. A column nobody matched was dropped in
    silence, which is the failure `session_import._unrecognised_cols` exists to
    prevent one importer along.

    It matters most on a sheet somebody wrote themselves. uOttawa's carried
    ``Reactivity (Supplier)``, ``Isotype``, ``Number of vials`` and ``Ab request
    reference``: four columns of real work, none of them read, and nothing
    between the file and the save that said so.

    Two lists, because they are two different facts. A **read-only** column was
    exported on purpose and is deliberately not written back (an OGA
    recommendation is a verdict a stale spreadsheet must not re-assert); an
    **unrecognised** one is a column this app has nowhere to put.
    """
    lines = (text or "").splitlines()
    if not lines:
        return ""
    header = [c.strip() for c in lines[0].split("\t") if c.strip()]
    known = {h.lower() for h in board_columns.read_only(kind)}
    read_only = [h for h in header if h.lower() in known]
    # Each parser answers "which field is this heading" for its own sheet, and
    # the view asks rather than deriving it — one reader per sheet, no alias
    # table in here.
    #
    # Cell lines said nothing until `paired ko (WT rows)` was removed. Every
    # sheet already downloaded still carries that heading, and a column that
    # quietly goes nowhere is exactly the silent omission this app refuses
    # everywhere else — so the sheet losing a column is the sheet that most
    # needs to say so.
    #
    # `sessions` and `targets` do not reach this function: they have their own
    # upload endpoints, which is why there is no third branch rather than an
    # oversight.
    if kind == "antibodies":
        unread = [h for h in header if not meta.header_field(h)
                  and h.lower() not in known]
    elif kind == "cell-lines":
        unread = [h for h in header if not bulkcl.header_field(h)
                  and h.lower() not in known]
    else:
        unread = []
    parts = []
    if read_only:
        parts.append(f"{', '.join(read_only)} "
                     f"{'is' if len(read_only) == 1 else 'are'} in the sheet "
                     f"for reference and will not be written back.")
    if unread:
        parts.append(f"Nothing was read from {', '.join(unread)} — "
                     f"{'that column has' if len(unread) == 1 else 'those columns have'} "
                     f"no field in the pipeline. Anything worth keeping from "
                     f"{'it' if len(unread) == 1 else 'them'} can go in comments.")
    return " ".join(parts)


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
    if kind == "antibodies":
        # Ask the parser, not the exact spelling. `Gene Name` **is** read, and a
        # refusal that listed `gene` among the missing columns sent somebody to
        # rename a heading that was working — a wrong answer inside the message
        # written to stop a wrong answer.
        supplied = {meta.header_field(c) for c in header}
        missing = [c.sheet for c in board_columns.registry(kind)
                   if c.upload == board_columns.MATCH and c.key not in supplied]
    else:
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
    except Exception:
        # **Exception detail goes to the log; the page gets a sentence.** This
        # interpolated `str(e)`, so a truncated or half-written upload answered
        # `could not read the file (Error -3 while decompressing data: invalid
        # code lengths set)` — zlib's words, to a bench scientist, on the one
        # importer whose every other refusal names the column and the row. Run
        # 18 F1. The name is included because a person uploading several files
        # needs to know which one, and the causes named are the ones that
        # actually produce this: a part-finished download, a `.xls` renamed to
        # `.xlsx`, or a file saved by something that is not a spreadsheet.
        logger.exception("upload could not be read (kind=%s, name=%s)",
                         kind, getattr(f, "name", "?"))
        return JsonResponse(
            {"error": f"“{getattr(f, 'name', 'That file')}” could not be opened "
                      f"as a spreadsheet. It may have been damaged in transit, "
                      f"or saved in an older Excel format — open it and re-save "
                      f"it as .xlsx, then try again."},
            status=400)
    if not text.strip():
        # "Nothing was read" is a refusal, not an empty preview: the panel showed
        # nothing at all for it, which is how a whole upload could fail in
        # silence. Name the sheets so the reader can see which one it landed on.
        return JsonResponse(
            {"error": "No rows were read from that file. "
                      + (sheet_note or "The sheet is empty.")},
            status=400)

    default_gene = request.POST.get("default_gene", "")

    # `text` goes back with the preview so the caller can commit exactly what it
    # was shown, through the same bulk_*_commit endpoint the paste box uses.
    # Without it a board would need its own commit endpoint for uploads, which
    # is a second write path for the same job — and the two would drift.
    # A heading that went missing is worth saying whether or not the rows
    # survived it, so it rides on the note the panel already draws.
    sheet_note = " ".join(p for p in (sheet_note, _heading_note(text),
                                      _ignored_note(kind, text)) if p)

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
        items = bulk.plan(rows, member=_member(request),
                          number_now=request.POST.get("number_now")
                          in ("true", "1", "on"))
        return JsonResponse({"items": items, "summary": bulk.summarize(items),
                             "text": text, "sheet_note": sheet_note})

    if kind == "cell-lines":
        rows = bulkcl.parse(text, default_gene, request.POST.get("default_genotype", ""))
        if not rows:
            refusal = _nothing_read_refusal(kind, text)
            if refusal:
                return JsonResponse({"error": refusal}, status=400)
        items = bulkcl.plan(rows, member=_member(request))
        return JsonResponse({"items": items, "summary": bulkcl.summarize(items),
                             "text": text, "sheet_note": sheet_note})

    # Other kinds (e.g. "sessions") have their own upload endpoints.
    return JsonResponse({"error": f"upload not supported for '{kind}' here"}, status=400)
