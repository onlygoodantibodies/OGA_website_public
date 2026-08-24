"""
Task hub — the pipeline's task-first front door.

Instead of landing on an entity browser, people land here and pick the thing
they came to do.

**The cards are in the order the work happens, and everyone sees the same order.**

They used to be sorted by the signed-in member's role, so the three boards led
and the way *in* — adding a gene — sat fifth, in the middle of the second row.
The sixth field test's annotated walkthrough had to draw a box round it
captioned "1 · Everything starts here": the front door did not say where the
front door was. Frequency ordering also serves only the person who already knows
the app, and the one thing a hub is for is the person who does not.

The role-based "your usual" row went with it. It was a second, competing answer
to "what should I look at", and two orders on one page is no order. Nothing is
lost: every card is a link, Browse in the header goes straight to any board, and
the search box opens a gene's own page — which is where most of the work happens
once a gene exists.

The first card is titled for what it *does*: "Check feasibility" named the
assessment rather than the act.

**There are two doors to a gene, and this page used to deny it.** The card said
adding a gene was "the only way a target gets on your site's list", which is
false — the target board's own Add panel posts to the same
``bulk_targets.plan``/``apply`` pair and writes the same target and the same
nomination. Saying otherwise sends somebody looking up a gene when they already
know they want it, and it is exactly the kind of claim a reader checks once and
then stops trusting the page. So both doors are in step 1, named for what each is
better at: look it up first, or put it straight on the list.

A phase is not a wizard. Nothing enforces the order, and many genes never need
all four applications — the same reason ``services/gene_progress.py`` refuses to
show a percentage. Work done out of order is caught on the gene's own page: its
progress strip is derived from records, and every unfinished step there is a
link to the board that finishes it.
"""
from __future__ import annotations

from django.conf import settings
from django.urls import reverse
from django.utils import timezone
from django.utils.safestring import mark_safe
from django.shortcuts import render

from pipeline.decorators import pipeline_member_required
from pipeline.models import Member

DB = "pipeline_db"

# id, title, one-line (says what it does), url name, icon key
#
# Adding antibodies, cell lines, sessions and targets used to be four cards of
# their own. They are pop-outs on their boards now — you look at what is there
# and add to it in the same place — so the cards are gone. The old pages still
# answer their URLs, so a bookmark lands somewhere that works.
#
# Order here is the order on the page: PHASES below slices this list by id, and
# every task must appear in exactly one phase (there is a test).
TASK_DEFS = [
    ("board", "Target board",
     "Every gene across every site — and the other way in: Add puts genes "
     "straight on your list, where you can then see them.",
     "pipeline:target_board", "board"),
    ("ab_board", "Antibodies board", "Every antibody — find one and edit it, or mark what it is good for.", "pipeline:antibody_board", "add"),
    ("cl_board", "Cell lines board", "Every WT and KO line — find one and edit it.", "pipeline:cell_line_board", "cells"),
    ("feasibility", "Look a gene up, then add it",
     "Is it worth doing? Expression, knockout availability, how many antibodies "
     "exist — then add it to your site's list from the same page.",
     "pipeline:feasibility", "feas"),
    ("record", "Sessions board", "Every session across every site — find one and edit it, results included.", "pipeline:session_board", "record"),
    ("publish", "Publish figures", "Crop a validation figure and send it to the review queue.", "pipeline:cropper", "cropper"),
    ("review", "Review queue",
     "Figures cropped and not yet on the website — check them, then release.",
     "pipeline:review_queue", "review"),
    ("recommend", "Set recommendations", "Review a gene's figures and mark which antibodies are recommended.", "pipeline:recommendations", "recommend"),
    ("data_io", "Downloads & uploads", "Export the whole dataset, edit it, and upload the changes.", "pipeline:data_io", "io"),
    # Added 23 Aug 2026 with the public nomination form. It sits under "Across
    # every gene" rather than in step 1: the two cards there are the two ways a
    # gene gets *onto* a list, and this one puts nothing anywhere — it is a
    # read-only list of what people outside are waiting for, spanning every
    # gene. Acting on a row means going to one of those two doors, which is
    # where each row links.
    ("requests", "Gene requests",
     "What the public has asked us to characterise, most-asked first — demand, "
     "not anybody's list yet.",
     "pipeline:gene_requests", "requests"),
]

# The phases of a gene's life, in the order the ten-step walkthrough walks them.
#
# `note` is the thing a newcomer would otherwise have to be told out of band —
# each one is a place the sixth field test stopped and guessed. They are short on
# purpose: this is a signpost, not a guide, and the guides are one click further.
PHASES = [
    ("start", "Start a gene", "Is it worth doing, and put it on your site's list.",
     ["feasibility", "board"],
     "Either card puts a gene on your site's list — look it up first if you are "
     "deciding, or go straight to the board if you already know. Both record it "
     "as not funded, so set a funder on the board or it stays off the Overview's "
     "active list."),
    ("setup", "Set up what you will test", "The knockout, its parent, and the antibodies.",
     ["cl_board", "ab_board"],
     "A wild-type parent is recorded once with no gene, so it never shows under a "
     "gene — check the cell lines board before adding one, and name it in the "
     "knockout's parent column."),
    ("run", "Run and record experiments", "Plan a session, take a sheet to the bench, put the readings back.",
     ["record"],
     "A gene's own page carries the same sessions with its bench sheets — search "
     "for the gene above to get there."),
    ("publish", "Publish what you found", "Crop the figures, review them, then release.",
     ["publish", "review", "recommend"],
     "Cropping no longer publishes: the figures wait in the review queue until "
     "somebody releases them, which is the press that puts them on the public "
     "website. The gene's page generates a draft Data Note; a Zenodo deposit or "
     "an F1000 paper is what marks it completed."),
]

# Not a phase — these span every gene, so they sit apart rather than pretending
# to be a step. The target board moved out of here and into step 1: it is the
# second way a gene gets on a list, and burying it under "across every gene" is
# what let the first card claim to be the only one.
ACROSS = ("Across every gene", ["data_io", "requests"])

_ICON = {
    "feas": '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg>',
    "add": '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"><path d="M4 7h16M4 12h16M4 17h10"/><circle cx="18" cy="17" r="3"/><path d="M18 15.5v3M16.5 17h3"/></svg>',
    "cells": '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"><circle cx="8" cy="9" r="3.5"/><circle cx="16" cy="15" r="3.5"/><path d="M8 12.5v0M16 11.5v0"/></svg>',
    "plan": '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"><rect x="3" y="4" width="18" height="17" rx="2"/><path d="M3 9h18M8 2v4M16 2v4M8 14h4"/></svg>',
    "record": '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"><path d="M9 11l3 3 8-8"/><path d="M20 12v7a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1h11"/></svg>',
    "cropper": '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"><path d="M6 2v14a2 2 0 0 0 2 2h14"/><path d="M18 22V8a2 2 0 0 0-2-2H2"/></svg>',
    "review": '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"><rect x="3" y="4" width="18" height="14" rx="2"/><path d="M3 15l5-4 3 2.5L15.5 9 21 14"/><circle cx="8.5" cy="8.5" r="1.4"/></svg>',
    "recommend": '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"><path d="M12 3l2.5 5.2 5.5.8-4 3.9.9 5.6L12 16.9 7.1 18.5l.9-5.6-4-3.9 5.5-.8z"/></svg>',
    "requests": '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"><path d="M21 15a2 2 0 0 1-2 2H8l-4 4V5a2 2 0 0 1 2-2h13a2 2 0 0 1 2 2z"/><path d="M9 9h6M9 12h4"/></svg>',
    "io": '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"><path d="M12 3v10M8 9l4 4 4-4"/><path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/></svg>',
    "board": '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18M9 9v11M15 9v11"/></svg>',
}


def _member(request):
    from django.contrib.auth.models import User
    try:
        pu = User.objects.using(DB).get(username=request.user.username)
        return Member.objects.using(DB).get(user_id=pu.pk, is_active=True)
    except Exception:
        return None


@pipeline_member_required
def walkthrough(request):
    """The end-to-end guide: one gene through the whole pipeline, in screenshots.

    The hub is a signpost and the four Markdown guides at the repo root are
    per-board; between them there was nothing that answered *"what is the job,
    start to finish"* — which is the question somebody has on their first day
    and the one the field tests kept having to answer for themselves.

    It lives here rather than in a docs app because it is chrome-first: the
    point of it is that you read a step and then go and do it, so it needs the
    nav, the search box and Browse. Static content, one template, no context.
    """
    return render(request, "pipeline/walkthrough.html")


@pipeline_member_required
def hub(request):
    member = _member(request)

    def _task(d):
        tid, title, desc, url_name, icon = d
        return {"id": tid, "title": title, "desc": desc, "url": reverse(url_name),
                "icon": mark_safe(_ICON.get(icon, ""))}

    tasks = {d[0]: _task(d) for d in TASK_DEFS}

    phases = [{"id": pid, "title": title, "blurb": blurb, "note": note,
               "tasks": [tasks[i] for i in ids if i in tasks]}
              for pid, title, blurb, ids, note in PHASES]
    across_title, across_ids = ACROSS
    across = {"title": across_title,
              "tasks": [tasks[i] for i in across_ids if i in tasks]}

    name = (member.display_name if member and member.display_name
            else (request.user.get_full_name() or request.user.username))
    hour = timezone.localtime().hour
    greet = "Good morning" if hour < 12 else ("Good afternoon" if hour < 18 else "Good evening")
    greetsub = ("These are in the order the work happens. Nothing makes you follow it: "
                "every card is a link, Browse above goes straight to any board, and the "
                "search box opens a gene's own page.")

    return render(request, "pipeline/hub.html", {
        "greeting": f"{greet}, {name}.",
        "greetsub": greetsub,
        "phases": phases,
        "across": across,
        # Superuser-only, and deliberately NOT a task card: the hub's cards are
        # the work a scientist does, and every one of them must be reachable by
        # everyone (there is a test that each appears in exactly one phase).
        # Managing who has access is administration, so it rides as its own row
        # the way the extension preview does.
        "user_admin": request.user.is_superuser,
        # The browser extension install page is team-only until it is announced,
        # so surface it here while that is the case. Once it is public it lives
        # on the public Tools hub and this row disappears.
        "extension_preview": not getattr(settings, "EXTENSION_PAGE_PUBLIC", False),
    })
