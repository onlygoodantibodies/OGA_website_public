"""A gene somebody outside the consortium asked us to characterise.

**This is not a ``TargetNomination``, and the two must never be merged.** A
nomination is an internal fact — *this site, under this grant, is pursuing this
gene* — with a funder, a project and a bench behind it, and the Portfolio counts
it as work the consortium has committed to. A ``GeneRequest`` is a fact about
*demand*: a stranger on the public website could not find their gene and said
they need it. Writing one into the other would put unfunded strangers' wishes on
a site's list and into every "who is doing what" total on the Portfolio, which
is the one thing those screens exist to answer honestly.

They also have different lifetimes. A request stays exactly as it was submitted
— it is a record of who asked and when, evidence for a grant application — while
a nomination is edited on the target board as the work moves. A request that a
site decides to act on becomes a nomination *as well*; ``target`` is filled in
when the gene exists here, and nothing about that changes the request.

Until 23 Aug 2026 there was no such row at all: the public "Nominate a Target"
button went to the contact form, so every request that has ever been made lives
in a Gmail inbox as prose, unaggregated and unqueryable. Twelve people asking
for one gene is the single most useful prioritisation signal this project can
collect, and it was being stored in a way that cannot count.

Nothing keys off these rows automatically, deliberately. What a request buys is
a number beside a gene when somebody is deciding what to run next; the deciding
is still a human act, taken at a meeting, the same as publishing is.
"""
from __future__ import annotations

from django.db import models


class GeneRequest(models.Model):
    """One press of "Nominate this gene" on the public site.

    One row per press, never deduplicated: two people asking for MAPT is the
    signal, and folding them into one row with a counter throws away who asked
    and when. ``pipeline/services/gene_requests.py`` is the one reader that
    groups them for a screen.
    """

    #: Exactly what was typed into the search box or the form, untouched. Kept
    #: apart from ``gene_symbol`` because a request for "phospho-tau S396" that
    #: we resolved to nothing is still a thing somebody asked for, and the
    #: string they used is the evidence of what they meant.
    typed_text = models.CharField(max_length=120)

    #: The symbol UniProt confirmed, uppercase. Blank only when UniProt could
    #: not be reached at the moment of writing (``checked='unchecked'``) — a
    #: request is never lost to an outage in a service we do not run.
    gene_symbol = models.CharField(max_length=40, blank=True, db_index=True)

    uniprot_id = models.CharField(max_length=20, blank=True)
    protein_name = models.CharField(max_length=255, blank=True)

    #: Comma-joined subset of ``gene_requests.APPLICATION_CHOICES`` — the four
    #: codes ``PublicationImage.application_type`` uses, plus IHC, OTHER and
    #: UNSURE. A CharField rather than a boolean each, so adding an application
    #: is a data change and not a migration against live PostgreSQL. (IHC was
    #: added that way; ``applications_other`` below is the one that cost a
    #: column, because free text has nowhere else to go.)
    applications = models.CharField(max_length=60, blank=True)

    #: What was typed beside "Something else". Its own column rather than a
    #: sentence folded into ``note``: this is the answer to a question we asked,
    #: and a tally that cannot name what "other" meant counts requests while
    #: telling nobody what they were for.
    applications_other = models.CharField(max_length=120, blank=True)

    email = models.EmailField(max_length=254)
    requester_name = models.CharField(max_length=120, blank=True)
    organisation = models.CharField(max_length=160, blank=True)

    #: "I may have a way to fund this." The public page asks because a funded
    #: request is a different conversation from a wish, and the only thing that
    #: turns a request into work is money — so the box that says so has to exist
    #: at the moment somebody is already telling us what they need.
    has_funding = models.BooleanField(default=False)
    funding_note = models.TextField(blank=True)

    #: Why they need it — free text, the part a prioritisation meeting reads.
    note = models.TextField(blank=True)

    #: Set when the gene is already a ``Target`` here (in the pipeline but not
    #: published). Nullable and ``SET_NULL``: deleting a target must not delete
    #: the evidence that somebody asked for it.
    target = models.ForeignKey(
        'pipeline.Target', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='public_requests',
    )

    CHECKED_HUMAN = 'human'
    CHECKED_UNCHECKED = 'unchecked'
    CHECKED_CHOICES = [
        (CHECKED_HUMAN, 'Confirmed human protein in UniProt'),
        (CHECKED_UNCHECKED, 'UniProt could not be reached'),
    ]
    #: **Which of those two it is decides how much the symbol is worth.** A row
    #: written during a UniProt outage carries whatever was typed and has not
    #: been confirmed to be a gene at all; the screen that lists requests says
    #: so rather than drawing it beside confirmed ones as if they were alike.
    checked = models.CharField(
        max_length=12, choices=CHECKED_CHOICES, default=CHECKED_HUMAN)

    #: Where the press happened, so we can tell whether the search box is doing
    #: its job: 'search' (arrived from a failed search) or 'direct'.
    source = models.CharField(max_length=20, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['gene_symbol'])]

    def __str__(self):
        return f"{self.gene_symbol or self.typed_text} requested by {self.email}"
