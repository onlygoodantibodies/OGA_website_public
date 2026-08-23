"""Antibody & Controls Selection Support — prototype (self-contained).

A deterministic decision-support tool that walks a scientist through choosing an
antibody and its controls for a target — the interactive, one-to-one version of
the Champions antibody-selection workshop. The end product is a documented
validation *plan* (downloadable PDF) plus a *record* that the person used the
tool, for OGA.

Design notes
------------
* **Isolation.** This app is a prototype, reachable only by its (unlinked) URL.
  It is deliberately decoupled from the rest of the site: no nav entry, no
  sitemap, no ForeignKeys into other apps. Everything is stored *by value*.
* **Routing.** Like ``credentials``, this app is not ``pipeline`` and not a
  ``core`` data model, so ``OGARouter`` sends its tables to ``academy_db`` (the
  Render persistent disk) — the natural home for a usage record that must
  survive deploys.
* **Login later, not now.** The tool is intended to live *behind the academy
  login* eventually (``academy_user_id`` is captured when a request is
  authenticated). For the prototype it is open, and identity is captured
  optionally at the end (name + email) so there is still "a record that the
  person used it".
* **Plan shape.** ``plan`` follows ``mcp_servers/content/plan_schema.json`` (the
  7-item validation-framework checklist) so the artifact stays analysable and
  interoperable with the workshop-certificate flow in ``credentials``.
"""
import uuid

from django.db import models

PACK_VERSION = "selector-0.1.0"


class SelectionRecord(models.Model):
    """One completed (or in-progress) run of the selection tool.

    Created when a learner finishes the walkthrough. Identity fields stay blank
    for anonymous runs; they are filled in if the learner chooses to save the
    plan to their OGA record, or automatically from the logged-in academy user
    once the tool moves behind the login.
    """

    #: Public handle, printed on the PDF and used for the shareable plan URL.
    public_code = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Identity — all optional for the open prototype.
    academy_user_id = models.IntegerField(
        null=True, blank=True,
        help_text="auth_user PK when the run was authenticated (future: behind login).",
    )
    name = models.CharField(max_length=200, blank=True)
    email = models.EmailField(blank=True)
    institution = models.CharField(max_length=200, blank=True)

    #: True when the run was saved as a *compliance record* — an identifiable,
    #: institutionally-affiliated plan that can be reported to a funder (Cancer
    #: Research UK / Horizons, NC3Rs, ...). False = a "just for me" personal plan,
    #: which needs no name/affiliation and is not offered to funders as evidence.
    is_compliance = models.BooleanField(default=False)

    # Denormalised headline fields (also present inside `plan`) — cheap to list
    # and filter in the admin without cracking open the JSON.
    target_gene = models.CharField(max_length=120, blank=True)
    species = models.CharField(max_length=60, blank=True)
    application = models.CharField(max_length=40, blank=True)
    question_type = models.CharField(max_length=40, blank=True)
    decision = models.CharField(max_length=40, blank=True)

    #: The structured validation plan (plan_schema.json shape).
    plan = models.JSONField(null=True, blank=True)
    #: The tailored guidance the learner actually saw, in order (for the PDF and
    #: so we can see *what advice* the tool gave for this run).
    guidance = models.JSONField(null=True, blank=True)

    completed = models.BooleanField(default=False)
    pack_version = models.CharField(max_length=40, default=PACK_VERSION)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        who = self.name or self.email or "anonymous"
        return f"{self.target_gene or '—'} · {who} · {self.created_at:%Y-%m-%d}"

    @property
    def display_name(self):
        return self.name or "Anonymous researcher"

    @property
    def record_type_label(self):
        return "Compliance record" if self.is_compliance else "Personal plan"

    def get_pdf_url(self):
        from django.urls import reverse
        return reverse("selector:plan_pdf", args=[self.public_code])

    @property
    def pillars_summary(self):
        """Comma-joined list of the validation approaches the learner said they
        can do — a cheap at-a-glance column for the admin."""
        keys = (self.plan or {}).get("feasible_pillars") or []
        return ", ".join(keys) if keys else "—"
