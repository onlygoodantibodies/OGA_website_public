"""Board guides — one Markdown file per board, read in the app or downloaded.

The Markdown file is the single source: it is what gets emailed to a new site
lead and what is read here, so the two cannot say different things. If the
renderer is unavailable the raw text is shown rather than an error page — a guide
is never worth a 500, and a missing file says so in a sentence.

All four boards share this one implementation. ``target_board_guide`` used to
live in ``views/target_board.py``; it moved here when the second guide arrived,
rather than being copied.
"""
from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from pipeline.decorators import pipeline_member_required

# board url name → (markdown file, download name, page title, guide url name)
_GUIDES = {
    "target": ("TARGET_BOARD_GUIDE.md", "OGA_target_board_guide.md",
               "the target board", "pipeline:target_board_guide",
               "pipeline:target_board"),
    "session": ("SESSION_BOARD_GUIDE.md", "OGA_sessions_board_guide.md",
                "the sessions board", "pipeline:session_board_guide",
                "pipeline:session_board"),
    "antibody": ("ANTIBODY_BOARD_GUIDE.md", "OGA_antibodies_board_guide.md",
                 "the antibodies board", "pipeline:antibody_board_guide",
                 "pipeline:antibody_board"),
    "cell_line": ("CELL_LINE_BOARD_GUIDE.md", "OGA_cell_lines_board_guide.md",
                  "the cell lines board", "pipeline:cell_line_board_guide",
                  "pipeline:cell_line_board"),
}


def _guide(request, which):
    filename, download_name, title, guide_url, back_url = _GUIDES[which]

    path = Path(settings.BASE_DIR) / filename
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        text = (f"# Guide — {title}\n\nThe guide file (`{filename}`) is missing "
                "from this deploy. Ask Harvinder for a copy.")

    if request.GET.get("download"):
        resp = HttpResponse(text, content_type="text/markdown; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="{download_name}"'
        return resp

    try:
        import markdown
        body = markdown.markdown(text, extensions=["tables", "sane_lists"])
        rendered = True
    except Exception:
        body, rendered = text, False

    return render(request, "pipeline/board_guide.html", {
        "body": body, "rendered": rendered, "title": title,
        "guide_url": guide_url, "back_url": back_url,
    })


@pipeline_member_required
@require_GET
def target_board_guide(request):
    return _guide(request, "target")


@pipeline_member_required
@require_GET
def session_board_guide(request):
    return _guide(request, "session")


@pipeline_member_required
@require_GET
def antibody_board_guide(request):
    return _guide(request, "antibody")


@pipeline_member_required
@require_GET
def cell_line_board_guide(request):
    return _guide(request, "cell_line")
