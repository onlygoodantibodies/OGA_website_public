"""Self-contained OGA credentials — interactive-learning certificates.

Decoupled from the academy models and from ``auth_user``: everything is stored
**by value** (recipient name, institutional email, gene, RRID, plan, module) with
no ForeignKeys to any other app. Routes to ``academy_db`` (the Render persistent
disk), so its tables sit alongside the academy tables without depending on them.

Three certificate kinds:
  * **module**   — one per learning module (earned by passing the module quiz);
  * **workshop** — the validation plan for the learner's own target;
  * **capstone** — awarded when a learner holds all module passes + the workshop.
"""
import uuid

from django.db import models

# Module id → certificate title. Kept small + local so this app stays
# self-contained; mirrors mcp_servers/content/quizzes.json (3 modules).
MODULE_TITLES = {
    "framework": "Planning antibody validation",
    "controls": "Controls that count",
    "acquiring": "Finding and acquiring controls",
}
# Modules required (together with a workshop plan) to earn the capstone.
CAPSTONE_MODULES = {"framework", "controls", "acquiring"}


class Certificate(models.Model):
    MODULE = "module"
    WORKSHOP = "workshop"
    CAPSTONE = "capstone"
    KIND_CHOICES = [
        (MODULE, "Learning module"),
        (WORKSHOP, "Antibody Validation Workshop"),
        (CAPSTONE, "Full Certification (capstone)"),
    ]

    verification_code = models.UUIDField(default=uuid.uuid4, unique=True,
                                         editable=False)
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, default=WORKSHOP)
    recipient_name = models.CharField(max_length=200)
    institutional_email = models.EmailField()
    # module certs
    module_id = models.CharField(max_length=50, blank=True)
    module_title = models.CharField(max_length=200, blank=True)
    # workshop certs — pipeline linkage BY VALUE (gene/RRID strings), never an FK
    target_gene = models.CharField(max_length=100, blank=True)
    rrid = models.CharField(max_length=50, blank=True)
    plan = models.JSONField(null=True, blank=True)
    pack_version = models.CharField(max_length=30, blank=True)
    score = models.FloatField(default=100.0)
    issued_at = models.DateTimeField(auto_now_add=True)
    pending_claim = models.ForeignKey("PendingClaim", null=True, blank=True,
                                      on_delete=models.SET_NULL,
                                      related_name="certificates")

    class Meta:
        ordering = ["-issued_at"]

    @property
    def display_title(self):
        if self.kind == self.MODULE:
            return f"OGA Antibody Validation — {self.module_title or self.module_id}"
        if self.kind == self.CAPSTONE:
            return "OGA Antibody Validation — Full Certification"
        suffix = f" — {self.target_gene}" if self.target_gene else ""
        return f"Antibody Validation Planning{suffix}"

    @property
    def recipient_display(self):
        return self.recipient_name or "Recipient"

    def get_verify_url(self):
        from django.urls import reverse
        return reverse("credentials:verify", args=[self.verification_code])

    def __str__(self):
        return f"{self.recipient_name} – {self.display_title}"


class PendingClaim(models.Model):
    """A credential claim awaiting institutional-email verification.

    One claim can request several certificates at once: any modules the learner
    passed, plus a workshop certificate if they submitted a validation plan. The
    capstone is added automatically when the learner (by verified email) holds all
    module passes + a workshop.
    """
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    name = models.CharField(max_length=200)
    institutional_email = models.EmailField()
    modules = models.JSONField(default=list, blank=True)   # list of module ids
    target_gene = models.CharField(max_length=100, blank=True)
    rrid = models.CharField(max_length=50, blank=True)
    plan = models.JSONField(null=True, blank=True)
    pack_version = models.CharField(max_length=30, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        state = "verified" if self.verified_at else "pending"
        return f"Claim {self.institutional_email} ({state})"

    def issue_all(self):
        """Idempotently mint every certificate this verified claim has earned.

        Returns the list of Certificates (module certs, a workshop cert if a plan
        was submitted, and a capstone if the learner now holds all modules + a
        workshop). Re-following the link returns the already-issued set.
        """
        from django.utils import timezone

        existing = list(self.certificates.all())
        if existing:
            return existing

        created = []
        for mid in (self.modules or []):
            if mid not in MODULE_TITLES:
                continue
            created.append(Certificate.objects.create(
                kind=Certificate.MODULE, module_id=mid,
                module_title=MODULE_TITLES[mid], recipient_name=self.name,
                institutional_email=self.institutional_email,
                pack_version=self.pack_version, pending_claim=self))

        if self.plan:
            created.append(Certificate.objects.create(
                kind=Certificate.WORKSHOP, recipient_name=self.name,
                institutional_email=self.institutional_email,
                target_gene=self.target_gene, rrid=self.rrid, plan=self.plan,
                pack_version=self.pack_version, pending_claim=self))

        # Capstone: union of this email's module certs (incl. the ones just made)
        # covers the requirement AND the learner holds a workshop cert.
        email = self.institutional_email
        held_modules = set(Certificate.objects.filter(
            institutional_email__iexact=email, kind=Certificate.MODULE
        ).values_list("module_id", flat=True))
        has_workshop = Certificate.objects.filter(
            institutional_email__iexact=email, kind=Certificate.WORKSHOP).exists()
        has_capstone = Certificate.objects.filter(
            institutional_email__iexact=email, kind=Certificate.CAPSTONE).exists()
        if CAPSTONE_MODULES <= held_modules and has_workshop and not has_capstone:
            created.append(Certificate.objects.create(
                kind=Certificate.CAPSTONE, recipient_name=self.name,
                institutional_email=email, pack_version=self.pack_version,
                pending_claim=self))

        self.verified_at = timezone.now()
        self.save(update_fields=["verified_at"])
        return created
