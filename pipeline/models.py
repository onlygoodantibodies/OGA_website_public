"""
YCharOS Internal Data Pipeline — Django Models

Schema version: 1.2
Source documents:
  - YCharOS_Pipeline_Scoping_Document.docx (design spec)
  - YCharOS_Database_Architecture.md (Access field inventory)
  - Antibody_Database.xlsx (Leicester data)
  - YCharOSreagent_request_1.xlsx (reagent request format)
  - Session_Workflow_Planning.md (Session Workflow Redesign additions)
  - session12_model_additions.py (reagent request batch workflow)

Design principles:
  - Session-based experiment entry (scoping doc §3.1–3.2)
  - Multi-site with harmonised schema but flexible usage (§4, §7.2)
  - Protocol templates to eliminate repetitive entry (§11.2)
  - No recommendation tracking (stays in OGA)
  - No staging/append tables (replaced by bulk import)
  - No Tissues table (empty in Access, never used)

Access → PostgreSQL mapping notes are in field-level comments.
"""

import re

from django.db import models
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator

from pipeline.storages import attachment_storage


# =============================================================================
# Layer 1: Core entities
# =============================================================================

class Site(models.Model):
    """
    Multi-site awareness. Currently Montreal, Leicester, Cornell, UBC.
    Scoping doc §3.4: expandable list of consortium sites.
    """
    name = models.CharField(max_length=100, unique=True)
    short_code = models.CharField(
        max_length=10, unique=True,
        help_text="e.g. 'MTL', 'LEI', 'COR', 'UBC'"
    )
    address = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class Member(models.Model):
    """
    Lab personnel. Extends Django auth user.
    Access source: Members table (18 rows, just ID + Name).
    Scoping doc §4: role-based access for Sara, Riham, Michael, etc.
    """
    class Role(models.TextChoices):
        ADMIN = 'admin', 'Administrator'
        PI = 'pi', 'Principal Investigator'
        COORDINATOR = 'coordinator', 'Logistics Coordinator'
        EXPERIMENTER = 'experimenter', 'Experimenter'
        VIEWER = 'viewer', 'View Only'

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='pipeline_member'
    )
    site = models.ForeignKey(
        Site, on_delete=models.PROTECT, related_name='members'
    )
    role = models.CharField(max_length=20, choices=Role.choices)
    # Access: Members.Name → split into Django User first_name/last_name
    display_name = models.CharField(
        max_length=255, blank=True,
        help_text="Optional override for display; otherwise uses User names"
    )
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.display_name or self.user.get_full_name()


class GrantingAgency(models.Model):
    """
    Funding bodies.
    Access source: GrantingAgencies table (22 rows).
    """
    name = models.CharField(max_length=255, unique=True)
    # Access: GrantingAgencies.GrantingAgency → name
    abbreviation = models.CharField(max_length=50, blank=True)

    class Meta:
        verbose_name_plural = 'Granting agencies'
        ordering = ['name']

    def __str__(self):
        return self.name


class Project(models.Model):
    """
    Funding/organisational groupings.
    Access source: Projects table (34 rows).
    Reagent request: 'project' column (e.g. MJFF, ALS-RAP).
    """
    name = models.CharField(max_length=255, unique=True)
    # Access: Projects.ProjectName → name
    granting_agency = models.ForeignKey(
        GrantingAgency, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='projects'
    )
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class Company(models.Model):
    """
    Antibody and cell line suppliers.
    Access source: Companies table (45 rows).
    Leicester Excel: 'Supplier' column.
    Reagent request: company names as column group headers (row 9).
    """
    name = models.CharField(max_length=255, unique=True)
    # Access: Companies.Company → name
    website = models.URLField(blank=True)
    is_active = models.BooleanField(default=True)
    display_name = models.CharField(
        max_length=255, blank=True,
        help_text='OGA canonical supplier name for public display (falls back to name if empty)'
    )

    class Meta:
        verbose_name_plural = 'Companies'
        ordering = ['name']

    def __str__(self):
        return self.name

    # --- duplicate-variant prevention (abcam / Abcam / abcam. all collapse) ---

    @staticmethod
    def canonical_key(name):
        """Normalised identity for a supplier name (case/punctuation-insensitive)."""
        return re.sub(r'[^a-z0-9]', '', (name or '').lower())

    def clean(self):
        """
        Block saving a company whose name is a formatting variant of an existing
        one (runs on admin saves via ModelForm validation). Prevents the
        'abcam' vs 'Abcam' duplicates we had to clean up.
        """
        key = self.canonical_key(self.name)
        if not key:
            return
        for other in Company.objects.exclude(pk=self.pk):
            if self.canonical_key(other.name) == key:
                raise ValidationError({'name':
                    f"'{other.name}' (id {other.id}) is already this supplier. "
                    f"Select it instead of creating a variant."})

    @classmethod
    def resolve(cls, name, db='pipeline_db'):
        """
        Return (company, created) for a supplier name, matching case/punctuation-
        insensitively against existing records so imports never create variants.
        Use this instead of get_or_create(name=...) in data-loading code.
        """
        key = cls.canonical_key(name)
        for c in cls.objects.using(db).all():
            if cls.canonical_key(c.name) == key:
                return c, False
        return cls.objects.using(db).create(name=name), True


class Target(models.Model):
    """
    Protein targets for antibody characterisation.
    Access source: Proteins table (508 rows). Renamed to 'Target' per scoping doc.

    CONSORTIUM-WIDE ENTITY: There is ONE Target record per protein, shared
    across all sites. Montreal and Leicester (and future sites) create
    ExperimentSessions that point to the same Target. The site FK records
    who initiated the target, not who owns it.

    Anti-duplication: unique constraints on uniprot_id and gene_name prevent
    a site from accidentally creating a Target that already exists elsewhere.

    Status workflow (scoping doc §3.4):
      not_started → in_progress → wb_complete → ip_complete → if_complete
      → report_ready → published
    Also: cancelled, on_hold.
    """
    class Status(models.TextChoices):
        NOT_STARTED = 'not_started', 'Not Started'
        IN_PROGRESS = 'in_progress', 'In Progress'
        WB_COMPLETE = 'wb_complete', 'WB Complete'
        IP_COMPLETE = 'ip_complete', 'IP Complete'
        IF_COMPLETE = 'if_complete', 'IF Complete'
        FC_COMPLETE = 'fc_complete', 'FC Complete'
        REPORT_READY = 'report_ready', 'Report Ready'
        PUBLISHED = 'published', 'Published'
        CANCELLED = 'cancelled', 'Cancelled'
        ON_HOLD = 'on_hold', 'On Hold'

    # Access: Proteins.Protein → protein_name
    protein_name = models.CharField(max_length=255)
    # Access: Proteins.Gene → gene_name
    gene_name = models.CharField(
        max_length=255, blank=True, null=True, unique=True,
        help_text="Gene symbol — unique across all sites to prevent duplicate work"
    )
    # Access: Proteins.AlternProteinName
    alternative_name = models.CharField(max_length=255, blank=True)
    # Access: Proteins.Uniprot
    uniprot_id = models.CharField(
        max_length=50, blank=True, null=True, unique=True,
        help_text="UniProt accession, e.g. P37840 — unique across all sites"
    )
    # Access: Proteins.TheoreticalMassINkDa (stored as text in Access)
    theoretical_mass_kda = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True,
        help_text="Predicted molecular weight in kDa"
    )
    # Access: Proteins.Type
    protein_type = models.CharField(max_length=255, blank=True)

    # Relationships
    project = models.ForeignKey(
        Project, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='targets'
    )
    granting_agency = models.ForeignKey(
        GrantingAgency, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='targets'
    )

    # Status workflow
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.NOT_STARTED
    )

    # Access: Proteins.AvailableComercialKO
    commercial_ko_available = models.BooleanField(default=False)
    # Access: Proteins.RequestedComercialKO
    requested_commercial_ko = models.BooleanField(default=False)
    # Access: Proteins.RequestCustomKO
    request_custom_ko = models.BooleanField(default=False)
    # Access: Proteins.CommentsCustomKO
    comments_custom_ko = models.CharField(max_length=255, blank=True)
    # Access: Proteins.KOvalidation
    ko_validated = models.BooleanField(default=False)

    # Access: Proteins.ExpectingAbFrom
    expecting_ab_from = models.CharField(max_length=255, blank=True)
    # Access: Proteins.FundingAvailable
    funding_available = models.BooleanField(default=False)
    # Access: Proteins.SharedInCRAC
    shared_in_crac = models.DateField(null=True, blank=True)
    # Access: Proteins.RequestedAb
    requested_ab_date = models.DateField(null=True, blank=True)
    # Access: Proteins.DateOfNomination
    date_of_nomination = models.DateField(null=True, blank=True)
    # Access: Proteins.ProjectComment
    project_comment = models.CharField(max_length=255, blank=True)

    # Carl's target-list column L. A human call read off DepMap, not something we
    # can derive: DepMapExpression holds expression, not CRISPR gene effect.
    essential_gene = models.CharField(
        max_length=20, blank=True,
        help_text="Is this an essential gene per DepMap? 'YES' / 'NO' / 'slightly'"
    )

    # DepMap expression data (scoping doc §5.1)
    depmap_expression = models.DecimalField(
        max_digits=6, decimal_places=2, null=True, blank=True,
        help_text="log2(TPM+1) from DepMap; threshold ≥2.5 for suitability"
    )

    # Multi-site: which site initiated this target
    site = models.ForeignKey(
        Site, on_delete=models.PROTECT, related_name='targets',
        null=True, blank=True,
        help_text="Site that initiated this target — not exclusive ownership"
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    created_by = models.ForeignKey(
        Member, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='targets_created'
    )

    # Access legacy ID for migration
    access_id = models.IntegerField(
        null=True, blank=True, unique=True,
        help_text="Original Proteins.ID from Access DB for migration reference"
    )

    # OGA display fields
    citation = models.TextField(
        blank=True,
        help_text="Citation text for this target's publication"
    )
    aliases = models.CharField(
        max_length=500, blank=True,
        help_text="Comma-separated alternative gene names/symbols (from HGNC)"
    )
    cell_line_link = models.URLField(
        blank=True, null=True,
        help_text="Link to cell line used for this gene's experiments"
    )

    class Meta:
        ordering = ['protein_name']

    def __str__(self):
        return f"{self.protein_name} ({self.gene_name})" if self.gene_name else self.protein_name


class Report(models.Model):
    """
    Publication tracking, pulled out of Proteins/Targets table.
    Access source: Proteins.ZenodoDOI, F1000DOI, DateAddedZenodo, DateAddedF1000.
    """
    class ReportStatus(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        GENERATED = 'generated', 'Generated'
        SUBMITTED = 'submitted', 'Submitted'
        PUBLISHED = 'published', 'Published'

    target = models.ForeignKey(
        Target, on_delete=models.CASCADE, related_name='reports'
    )
    status = models.CharField(
        max_length=20, choices=ReportStatus.choices, default=ReportStatus.DRAFT
    )
    zenodo_doi = models.URLField(blank=True, help_text="Zenodo DOI link")
    f1000_doi = models.URLField(blank=True, help_text="F1000Research DOI link")
    zenodo_date = models.DateField(null=True, blank=True)
    f1000_date = models.DateField(null=True, blank=True)
    f1000_priority = models.CharField(
        max_length=120, blank=True,
        help_text="Free text: is this queued for an F1000 write-up, and any caveat "
                  "(e.g. 'YES with IF repeat'). Carl's target-list column R."
    )
    f1000_note = models.CharField(
        max_length=120, blank=True,
        help_text="Where the F1000 column says something that isn't a link "
                  "('coming', 'publish elsewhere') — kept rather than discarded"
    )
    introduction_text = models.TextField(
        blank=True, help_text="Manually written disease context paragraph"
    )
    generated_at = models.DateTimeField(null=True, blank=True)
    generated_by = models.ForeignKey(
        Member, on_delete=models.SET_NULL, null=True, blank=True
    )

    def __str__(self):
        return f"Report: {self.target} ({self.get_status_display()})"


class TargetAssignment(models.Model):
    """
    Assigns work on a target to a specific site.
    One row per (target, site, task_type) combination.
    """
    class TaskType(models.TextChoices):
        WB = 'WB', 'Western Blot'
        IP = 'IP', 'Immunoprecipitation'
        IF = 'IF', 'Immunofluorescence'
        FC = 'FC', 'Flow Cytometry'
        KO_GENERATION = 'KO', 'KO Cell Line Generation'

    class AssignmentStatus(models.TextChoices):
        PLANNED = 'planned', 'Planned'
        ASSIGNED = 'assigned', 'Assigned'
        IN_PROGRESS = 'in_progress', 'In Progress'
        COMPLETE = 'complete', 'Complete'
        BLOCKED = 'blocked', 'Blocked'
        NOT_APPLICABLE = 'na', 'Not Applicable'

    target = models.ForeignKey(Target, on_delete=models.CASCADE, related_name='assignments')
    site = models.ForeignKey(Site, on_delete=models.PROTECT, related_name='assignments')
    task_type = models.CharField(max_length=3, choices=TaskType.choices)
    status = models.CharField(max_length=20, choices=AssignmentStatus.choices, default=AssignmentStatus.PLANNED)
    assigned_by = models.ForeignKey(Member, on_delete=models.SET_NULL, null=True, blank=True, related_name='assignments_made')
    assigned_date = models.DateField(null=True, blank=True)
    assigned_to = models.ForeignKey(Member, on_delete=models.SET_NULL, null=True, blank=True, related_name='assignments_received')
    priority = models.PositiveSmallIntegerField(default=0)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['target', 'site', 'task_type']
        constraints = [
            models.UniqueConstraint(fields=['target', 'site', 'task_type'], name='unique_assignment_per_site_task'),
        ]

    def __str__(self):
        return f"{self.target} → {self.site} ({self.get_task_type_display()}: {self.get_status_display()})"


class TargetNomination(models.Model):
    """
    "Somebody put this gene forward, under this grant, to be run at this site."

    Carl's master target list is one row per *nomination*, not per gene: the same
    gene legitimately appears twice when it is funded under one project and also
    sitting on a wish-list under another (SYNGAP1 under AMP-AD and again under
    Autism-related). ``Target`` is one row per gene and enforces that, so the
    repeatable half lives here — which keeps both entries instead of making the
    import pick a winner.

    A nomination with ``funded=False`` *is* the wish-list: a gene of interest to a
    funder that we are not paying for yet, kept so we notice when it becomes
    feasible (e.g. a KO line appears in the Horizon catalogue).

    Carl's free-text workflow columns (Status, Conclusion, Antibodies requested?)
    ride along as notes so his file round-trips losslessly, but nothing keys off
    them — his call was that funded + completed is all the workflow state needed,
    and "completed" is derived from ``Report`` rather than typed.
    """
    target = models.ForeignKey(
        Target, on_delete=models.CASCADE, related_name='nominations'
    )
    site = models.ForeignKey(
        Site, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='nominations',
        help_text="Which YCharOS site is pursuing it — the key to spotting the "
                  "same target funded at two sites"
    )
    granting_agency = models.ForeignKey(
        GrantingAgency, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='nominations'
    )
    project = models.ForeignKey(
        Project, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='nominations'
    )
    funded = models.BooleanField(default=False)
    date_of_nomination = models.DateField(null=True, blank=True)
    date_text = models.CharField(
        max_length=50, blank=True,
        help_text="What the source actually said when it wasn't a real date "
                  "('2020', 'april 2024') — kept so nothing is silently invented"
    )
    comments = models.CharField(max_length=255, blank=True)
    contact = models.CharField(
        max_length=255, blank=True, help_text="Who nominated it / who to ask"
    )

    # Carried through for round-trip fidelity; not workflow state.
    status_note = models.CharField(max_length=120, blank=True)
    conclusion_note = models.CharField(max_length=120, blank=True)
    antibodies_requested = models.CharField(max_length=40, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        Member, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='nominations_created'
    )

    class Meta:
        ordering = ['target__gene_name', 'project__name']
        indexes = [
            models.Index(fields=['funded']),
            models.Index(fields=['site', 'funded']),
        ]

    def __str__(self):
        where = self.site.name if self.site else "unassigned"
        what = self.project.name if self.project else "no project"
        return f"{self.target} — {what} @ {where}"


class TargetClassification(models.Model):
    """
    Protein-class tags on a target, for the grant-writing portfolio view: "we have
    already characterised N GPCRs, N secreted proteins, N nuclear proteins".

    Carl does this from memory today — there is no column for it anywhere in the
    master list. Most labels are derived from UniProt keywords and GO terms (see
    ``services/protein_class.py``); ``manual`` is the escape hatch for anything the
    ontologies don't capture. ``source`` is kept so a derived label can be
    re-derived on the next backfill without trampling a hand-added one.
    """
    class Source(models.TextChoices):
        UNIPROT = 'uniprot', 'UniProt keyword'
        GO = 'go', 'GO term'
        FAMILY = 'family', 'Gene family'
        MANUAL = 'manual', 'Added by hand'

    target = models.ForeignKey(
        Target, on_delete=models.CASCADE, related_name='classifications'
    )
    label = models.CharField(max_length=80, db_index=True)
    source = models.CharField(
        max_length=20, choices=Source.choices, default=Source.MANUAL
    )
    evidence = models.CharField(
        max_length=255, blank=True,
        help_text="The raw keyword / GO term the label came from"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['label']
        constraints = [
            models.UniqueConstraint(
                fields=['target', 'label', 'source'],
                name='unique_target_class_per_source'),
        ]

    def __str__(self):
        return f"{self.target} — {self.label} ({self.get_source_display()})"


class CellLine(models.Model):
    """
    Cell line inventory with WT/KO pairing.
    Access source: CellLines table (744 rows).
    Leicester Excel: 'Cell lines' sheet (46 rows).

    Key design: parent_line FK creates WT→KO relationship.
    """
    class Genotype(models.TextChoices):
        WILD_TYPE = 'WT', 'Wild Type'
        KNOCKOUT = 'KO', 'Knockout'
        OTHER = 'other', 'Other'

    name = models.CharField(max_length=255)
    c_number = models.IntegerField(null=True, blank=True, help_text="Internal lab label / C number")
    target = models.ForeignKey(
        Target, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='cell_lines'
    )
    genotype = models.CharField(
        max_length=10, choices=Genotype.choices, default=Genotype.WILD_TYPE
    )
    parent_line = models.ForeignKey(
        'self', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='ko_derivatives',
        help_text="For KO lines, the WT parent cell line"
    )
    parental_line_name = models.CharField(max_length=255, blank=True)
    arrived_with_ko = models.ForeignKey(
        'self', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='arrived_with_wt',
        help_text="For WT lines, the KO line it was shipped alongside"
    )

    # Supplier info
    company = models.ForeignKey(
        Company, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='cell_lines'
    )
    catalogue_number = models.CharField(max_length=255, blank=True)
    lot_number = models.CharField(max_length=255, blank=True)
    cellosaurus_id = models.CharField(max_length=100, blank=True, help_text="Cellosaurus RRID, e.g. CVCL_0030")

    species = models.CharField(max_length=100, blank=True, default='Human')
    origin = models.CharField(max_length=255, blank=True)
    origin_comments = models.TextField(blank=True)
    clone = models.CharField(max_length=255, blank=True)
    growth_properties = models.CharField(max_length=255, blank=True)
    medium = models.CharField(max_length=255, blank=True)

    # Multi-site
    site = models.ForeignKey(
        Site, on_delete=models.PROTECT, related_name='cell_lines',
        null=True, blank=True
    )

    # Procurement
    class AcquisitionMethod(models.TextChoices):
        PURCHASED = 'purchased', 'Purchased'
        IN_KIND = 'in_kind', 'In Kind'
        UNKNOWN = 'unknown', 'Unknown'

    acquisition_method = models.CharField(
        max_length=20, choices=AcquisitionMethod.choices, default=AcquisitionMethod.UNKNOWN
    )
    received_date = models.DateField(null=True, blank=True)

    in_kind_value = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="In-kind contribution value (currency per site)"
    )
    in_kind_currency = models.CharField(
        max_length=3, blank=True, default='GBP',
        help_text="ISO currency code"
    )

    received = models.BooleanField(default=False)
    thawed = models.BooleanField(default=False, help_text="Has been thawed? (superseded by CellCultureEvent)")
    location_original_vial = models.CharField(max_length=255, blank=True)

    # KO validation
    ko_validated = models.BooleanField(
        default=False,
        help_text="Has this KO been validated by WB (no truncated protein)?"
    )
    ko_validation_notes = models.TextField(blank=True)

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Access legacy
    access_id = models.IntegerField(null=True, blank=True, unique=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.get_genotype_display()})"


class CellLineVial(models.Model):
    """
    Freeze-down batch — one c_number per batch, multiple physical vials.
    Sara's Montreal workflow: freeze down cells → assign c_number → store vials
    in boxes across LN2 and -80°C locations.

    Hierarchy:
      CellLine (scientific identity, one per real cell line per site)
        → CellLineVial (freeze-down batch, one per c_number)
          → InventoryLocation (physical vial storage, many per batch)

    Access source: Each CellLines row maps to one CellLineVial.
    Access CellLines.LabLabel → c_number.
    """
    cell_line = models.ForeignKey(
        CellLine, on_delete=models.CASCADE, related_name='vials'
    )
    c_number = models.IntegerField(
        null=True, blank=True,
        help_text="Internal lab label / C number — one per freeze-down batch"
    )
    freeze_date = models.DateField(
        null=True, blank=True,
        help_text="Date this batch was frozen down"
    )
    passage_number = models.IntegerField(
        null=True, blank=True,
        help_text="Passage number at time of freeze-down"
    )
    vial_count = models.IntegerField(
        null=True, blank=True,
        help_text="Number of vials created in this freeze-down batch"
    )

    # Fields moving from CellLine (per-batch, not per-identity)
    received_date = models.DateField(null=True, blank=True)

    class AcquisitionMethod(models.TextChoices):
        PURCHASED = 'purchased', 'Purchased'
        IN_KIND = 'in_kind', 'In Kind'
        UNKNOWN = 'unknown', 'Unknown'

    acquisition_method = models.CharField(
        max_length=20, choices=AcquisitionMethod.choices,
        default=AcquisitionMethod.UNKNOWN
    )
    received = models.BooleanField(default=False)
    thawed = models.BooleanField(
        default=False,
        help_text="Has any vial from this batch been thawed?"
    )
    location_original_vial = models.CharField(max_length=255, blank=True)

    in_kind_value = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="In-kind contribution value (currency per site)"
    )
    in_kind_currency = models.CharField(
        max_length=3, blank=True, default='GBP',
        help_text="ISO currency code"
    )

    notes = models.TextField(blank=True)

    site = models.ForeignKey(
        Site, on_delete=models.PROTECT, related_name='cell_line_vials',
        null=True, blank=True
    )

    # Access legacy
    access_id = models.IntegerField(
        null=True, blank=True, unique=True,
        help_text="Original CellLines.ID from Access DB"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['cell_line', 'c_number']
        verbose_name = 'Cell line vial batch'
        verbose_name_plural = 'Cell line vial batches'

    def __str__(self):
        c = f"C-{self.c_number}" if self.c_number else "no C#"
        return f"{self.cell_line.name} ({c})"


class Antibody(models.Model):
    """
    Core antibody records.
    Access source: Antibodies table (3,142 rows).
    Leicester Excel: 'Antibodies' sheet (423 rows).
    """
    ab_number = models.IntegerField(
        null=True, blank=True,
        help_text="Internal sequential antibody number (Access legacy)"
    )
    target = models.ForeignKey(
        Target, on_delete=models.CASCADE, related_name='antibodies'
    )
    company = models.ForeignKey(
        Company, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='antibodies'
    )
    catalogue_number = models.CharField(max_length=255, blank=True)
    lot_number = models.CharField(max_length=255, blank=True)
    rrid = models.CharField(
        max_length=255, blank=True,
        help_text="Research Resource Identifier, e.g. AB_10677043"
    )
    rrid_link = models.URLField(blank=True, help_text="Link to RRID registry entry")
    antigen = models.CharField(max_length=500, blank=True, help_text="Immunogen / antigen")

    # Characterisation fields
    class Clonality(models.TextChoices):
        MONOCLONAL = 'monoclonal', 'Monoclonal'
        POLYCLONAL = 'polyclonal', 'Polyclonal'
        RECOMBINANT = 'recombinant', 'Recombinant'
        UNKNOWN = 'unknown', 'Unknown'

    clonality = models.CharField(
        max_length=20, choices=Clonality.choices, default=Clonality.UNKNOWN
    )
    clone_id = models.CharField(max_length=255, blank=True)
    host_species = models.CharField(
        max_length=255, blank=True,
        help_text="Host expression system / species"
    )
    concentration = models.DecimalField(
        max_digits=10, decimal_places=3, null=True, blank=True,
        help_text="Concentration in µg/mL"
    )
    isotype = models.CharField(max_length=100, blank=True)
    species_reactivity = models.CharField(max_length=255, blank=True)

    is_recombinant = models.BooleanField(default=False)

    @property
    def clonality_label(self):
        """The printed clonality — never `get_clonality_display()`.

        The enum and `is_recombinant` are filled under two site conventions and
        neither answers the question alone; `services/clonality.py` is the one
        reader. Imported here rather than at module level so models.py keeps no
        import into services.
        """
        from pipeline.services import clonality as clonality_svc
        return clonality_svc.label(self)

    @property
    def recombinant(self):
        """Whether this is a recombinant, asking both columns."""
        from pipeline.services import clonality as clonality_svc
        return clonality_svc.is_recombinant(self)

    # Supplier validation flags
    supplier_validated_wb = models.BooleanField(default=False)
    supplier_validated_ip = models.BooleanField(default=False)
    supplier_validated_if = models.BooleanField(default=False)
    supplier_validated_ihc = models.BooleanField(default=False)
    supplier_validated_elisa = models.BooleanField(default=False)
    supplier_validated_fc = models.BooleanField(default=False)
    supplier_validated_applications = models.CharField(max_length=500, blank=True)

    validation_details_wb = models.TextField(blank=True)
    validation_details_if = models.TextField(blank=True)

    # NEW (Session Workflow): per-application recommended dilutions from supplier datasheet
    supplier_recommended_dilutions = models.JSONField(
        default=dict, blank=True,
        help_text='Per-application recommended dilutions from supplier datasheet. '
                  'Format: {"WB": "1/1000", "IF": "1/200", "IP": "2 µg", "FC": "1/100"}'
    )

    # Procurement
    class AcquisitionMethod(models.TextChoices):
        PURCHASED = 'purchased', 'Purchased'
        IN_KIND = 'in_kind', 'In Kind'
        UNKNOWN = 'unknown', 'Unknown'

    acquisition_method = models.CharField(
        max_length=20, choices=AcquisitionMethod.choices,
        default=AcquisitionMethod.UNKNOWN
    )
    received_date = models.DateField(null=True, blank=True)

    in_kind_value = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    in_kind_currency = models.CharField(max_length=3, blank=True, default='GBP')

    out_of_market = models.BooleanField(default=False, help_text="No longer commercially available")
    empty_vial = models.BooleanField(default=False, help_text="Vial is empty / used up")
    is_test = models.BooleanField(default=False, help_text="Test/trial antibody flag")
    others_flag = models.BooleanField(default=False, help_text="Miscellaneous flag from Access")
    supplier_url = models.URLField(blank=True, max_length=500, help_text="Supplier product page URL")
    validated_apps_by_supplier = models.CharField(max_length=500, blank=True)

    comments = models.TextField(blank=True)

    # Multi-site
    site = models.ForeignKey(
        Site, on_delete=models.PROTECT, related_name='antibodies',
        null=True, blank=True
    )

    created_by = models.ForeignKey(
        Member, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='antibodies_created'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Access legacy
    access_id = models.IntegerField(null=True, blank=True, unique=True)

    # OGA recommendation booleans
    wb_recommended = models.BooleanField(default=False, help_text="Recommended for Western Blot")
    ip_recommended = models.BooleanField(default=False, help_text="Recommended for Immunoprecipitation")
    if_recommended = models.BooleanField(default=False, help_text="Recommended for Immunofluorescence")
    fc_recommended = models.BooleanField(default=False, help_text="Recommended for Flow Cytometry")

    class Meta:
        verbose_name_plural = 'Antibodies'
        ordering = ['target', 'company', 'catalogue_number']
        constraints = [
            models.UniqueConstraint(
                fields=['catalogue_number', 'company', 'target', 'lot_number', 'site'],
                name='unique_antibody_per_site_lot'
            ),
        ]

    def __str__(self):
        return f"{self.catalogue_number} ({self.company})" if self.company else self.catalogue_number


class InventoryLocation(models.Model):
    """
    Storage location tracking, separate from entity records.
    Flexible hierarchical model for both Montreal's detailed tracking
    and Leicester's simpler needs.
    """
    class StorageType(models.TextChoices):
        FRIDGE_4C = '4c', '4°C Fridge'
        FREEZER_NEG20 = '-20', '-20°C Freezer'
        FREEZER_NEG80 = '-80', '-80°C Freezer'
        LN2 = 'ln2', 'Liquid Nitrogen'
        RT = 'rt', 'Room Temperature'
        OTHER = 'other', 'Other'

    # Polymorphic link: either an antibody or a cell line
    antibody = models.ForeignKey(
        Antibody, on_delete=models.CASCADE,
        null=True, blank=True, related_name='locations'
    )
    cell_line = models.ForeignKey(
        CellLine, on_delete=models.CASCADE,
        null=True, blank=True, related_name='locations'
    )
    vial = models.ForeignKey(
        CellLineVial, on_delete=models.CASCADE,
        null=True, blank=True, related_name='locations',
        help_text="Vial batch this storage location belongs to (replaces direct cell_line FK)"
    )
    site = models.ForeignKey(Site, on_delete=models.PROTECT)
    storage_type = models.CharField(max_length=10, choices=StorageType.choices)

    # Hierarchical location fields
    building = models.CharField(max_length=100, blank=True)
    room = models.CharField(max_length=100, blank=True)
    freezer = models.CharField(max_length=100, blank=True)
    shelf = models.CharField(max_length=100, blank=True)
    rack = models.CharField(max_length=100, blank=True)
    box = models.CharField(max_length=100, blank=True)
    position = models.CharField(max_length=100, blank=True)

    notes = models.TextField(blank=True)

    class Meta:
        verbose_name_plural = 'Inventory locations'

    def __str__(self):
        parts = filter(None, [self.building, self.room, self.freezer, self.box])
        return f"{self.get_storage_type_display()}: {' / '.join(parts)}"


class CellCultureEvent(models.Model):
    """
    Cell culture lifecycle tracking per C number (CellLine record).
    Added v1.1 per Sara meeting feedback (3 April 2026).
    Replaces the simple Access Thawed boolean with full lifecycle tracking.
    """
    class EventType(models.TextChoices):
        THAW = 'thaw', 'Thaw from Storage'
        PASSAGE = 'passage', 'Passage / Split'
        FREEZE_DOWN = 'freeze_down', 'Freeze Down'
        DISCARD = 'discard', 'Discard'

    cell_line = models.ForeignKey(CellLine, on_delete=models.CASCADE, related_name='culture_events')
    event_type = models.CharField(max_length=20, choices=EventType.choices)
    date = models.DateField()
    passage_number = models.IntegerField(null=True, blank=True)
    split_ratio = models.CharField(max_length=20, blank=True)
    vials_frozen = models.IntegerField(null=True, blank=True, help_text="Number of vials frozen (freeze_down events)")
    from_location = models.ForeignKey(
        InventoryLocation, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='thaw_events'
    )
    discard_reason = models.CharField(max_length=255, blank=True)
    performed_by = models.ForeignKey(Member, on_delete=models.SET_NULL, null=True, blank=True, related_name='culture_events')
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date', '-created_at']
        verbose_name = 'Cell culture event'

    def __str__(self):
        return f"{self.get_event_type_display()}: {self.cell_line.name} ({self.date})"


# =============================================================================
# NEW MODEL — Sample (Session Workflow Redesign)
# =============================================================================

class Sample(models.Model):
    """
    A prepared lysate or concentrated supernatant — the physical tube.
    Exists independently of sessions; may be used across multiple sessions.
    Captures data needed for figure legends (protein loading, buffer, method).
    """

    class SampleType(models.TextChoices):
        LYSATE = 'lysate', 'Cell Lysate'
        SUPERNATANT = 'supernatant', 'Concentrated Supernatant'
        MEDIA = 'media', 'Culture Medium (unconcentrated)'

    class SampleStatus(models.TextChoices):
        AVAILABLE = 'available', 'Available'
        LOW = 'low', 'Low Volume'
        DEPLETED = 'depleted', 'Depleted'
        DISCARDED = 'discarded', 'Discarded'

    # Identity
    cell_line = models.ForeignKey(
        'CellLine', on_delete=models.CASCADE,
        related_name='samples',
        help_text='Which cell line this sample was prepared from'
    )
    sample_type = models.CharField(
        max_length=20, choices=SampleType.choices,
        help_text='Lysate (intracellular) or supernatant (secreted)'
    )

    # Preparation details — figure legend fields
    preparation_date = models.DateField(
        help_text='Date sample was prepared'
    )
    prepared_by = models.ForeignKey(
        'Member', on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='prepared_samples',
        help_text='Auto-populated from logged-in user'
    )
    lysis_buffer = models.CharField(
        max_length=200, blank=True,
        help_text='e.g. RIPA, Pierce IP buffer'
    )
    protease_inhibitor = models.CharField(
        max_length=200, blank=True, default='1x protease inhibitor cocktail',
        help_text='e.g. 1x protease inhibitor cocktail'
    )

    # Quantification — from BCA/Bradford assay
    protein_concentration = models.DecimalField(
        max_digits=10, decimal_places=2,
        null=True, blank=True,
        help_text='µg/mL from BCA or Bradford assay'
    )
    quantification_method = models.CharField(
        max_length=100, blank=True,
        help_text='e.g. BCA, Bradford'
    )

    # For supernatants — conditioning details (figure legend)
    conditioning_time_hours = models.DecimalField(
        max_digits=5, decimal_places=1,
        null=True, blank=True,
        help_text='Hours of serum-free conditioning (supernatants only)'
    )
    concentration_method = models.CharField(
        max_length=200, blank=True,
        help_text='e.g. 3 kDa Amicon Ultra, 10 kDa cutoff'
    )
    concentration_fold = models.IntegerField(
        null=True, blank=True,
        help_text='Approximate fold concentration achieved'
    )

    # Storage & tracking
    volume_remaining_ul = models.DecimalField(
        max_digits=8, decimal_places=1,
        null=True, blank=True,
        help_text='Remaining volume in µL'
    )
    storage_location = models.CharField(
        max_length=200, blank=True,
        help_text='e.g. -80°C Freezer 2, Rack A'
    )
    storage_temperature = models.CharField(
        max_length=20, blank=True, default='-80°C',
        help_text='Storage temperature'
    )
    status = models.CharField(
        max_length=20, choices=SampleStatus.choices,
        default='available'
    )

    # Metadata
    site = models.ForeignKey(
        'Site', on_delete=models.CASCADE,
        related_name='samples'
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-preparation_date']

    def __str__(self):
        return (
            f"{self.get_sample_type_display()} — "
            f"{self.cell_line.name} "
            f"({self.preparation_date})"
        )

    @property
    def is_fresh(self):
        """True if prepared today — relevant for IP where fresh lysate is required."""
        from datetime import date
        return self.preparation_date == date.today()


# =============================================================================
# Layer 2: Pipeline — Reagent Requests
# =============================================================================

class ManufacturerContact(models.Model):
    """
    Contact at a partner manufacturer for automated reagent requests.
    Staff turnover means these need easy UI management.
    One company may have multiple contacts (different reps per region/site).
    """
    company = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name='contacts'
    )
    contact_name = models.CharField(max_length=255)
    email = models.EmailField()
    site = models.ForeignKey(
        Site, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='manufacturer_contacts',
        help_text="Which YCharOS site this contact serves (blank = all sites)"
    )
    custom_message = models.TextField(
        blank=True,
        help_text="Personalised note included in request emails to this contact"
    )
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True, help_text="Internal notes (not sent in emails)")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['company__name', 'contact_name']

    def __str__(self):
        site_label = f" [{self.site.short_code}]" if self.site else ""
        return f"{self.contact_name} ({self.company.name}){site_label}"


class ReagentRequestBatch(models.Model):
    """
    A round of reagent requesting. Multi-target, multi-manufacturer.
    Each manufacturer gets one email containing only their items.
    """
    class BatchStatus(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        SENT = 'sent', 'Sent'
        PARTIALLY_RECEIVED = 'partial', 'Partially Received'
        COMPLETE = 'complete', 'Complete'
        CANCELLED = 'cancelled', 'Cancelled'

    title = models.CharField(
        max_length=255,
        help_text="e.g. 'April 2024 GBA1 Canada antibodies'"
    )
    site = models.ForeignKey(
        Site, on_delete=models.PROTECT, related_name='request_batches'
    )
    project = models.ForeignKey(
        Project, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='request_batches'
    )
    status = models.CharField(
        max_length=20, choices=BatchStatus.choices, default=BatchStatus.DRAFT
    )
    created_by = models.ForeignKey(
        Member, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='request_batches_created'
    )
    sent_date = models.DateTimeField(
        null=True, blank=True,
        help_text="When request emails were sent to manufacturers"
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Reagent request batch'
        verbose_name_plural = 'Reagent request batches'

    def __str__(self):
        return f"{self.title} ({self.get_status_display()})"

    @property
    def item_count(self):
        return self.items.count()

    @property
    def received_count(self):
        return self.items.filter(status='received').count()

    @property
    def companies_involved(self):
        return Company.objects.filter(
            pk__in=self.items.values_list('company_id', flat=True).distinct()
        )


class ReagentRequest(models.Model):
    """
    Per (target, company) request within a batch.
 
    Session 13 simplification: requests are communication records, not
    inventory tracking. Receipts are handled independently — Sara logs
    what arrived via the receipt form, which creates Antibody/CellLine
    records directly.
 
    catalogue_numbers is a free-text field (one per line) because most
    requests are open ("send us your antibodies for TREM2") and the
    manufacturer decides what to ship.
    """
    class RequestStatus(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        SENT = 'sent', 'Sent'
        RECEIVED = 'received', 'Received'
        NOT_SENT = 'not_sent', 'Not Sent'
        CANCELLED = 'cancelled', 'Cancelled'
 
    class ItemType(models.TextChoices):
        ANTIBODY = 'antibody', 'Antibody'
        CELL_LINE = 'cell_line', 'Cell Line'
 
    # Grouping — null for ad-hoc receipts logged without a batch
    batch = models.ForeignKey(
        'ReagentRequestBatch', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='items',
        help_text="Null for items logged outside a batch"
    )
 
    # What's being requested
    target = models.ForeignKey(
        Target, on_delete=models.CASCADE, related_name='reagent_requests'
    )
    item_type = models.CharField(max_length=20, choices=ItemType.choices)
    company = models.ForeignKey(
        Company, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='reagent_requests'
    )
    catalogue_numbers = models.TextField(
        blank=True,
        help_text="One catalogue number per line, or blank for open requests"
    )
 
    # Standard request: 100µL at ~100µg/mL, quantity=2 when both sites need it
    quantity = models.PositiveIntegerField(
        default=1,
        help_text="Number of vials (2 when both sites need the same antibody)"
    )
    requested_volume_ul = models.PositiveIntegerField(
        default=100, help_text="Requested volume in µL per vial"
    )
 
    # Ownership
    site = models.ForeignKey(
        Site, on_delete=models.PROTECT, related_name='reagent_requests'
    )
    project = models.ForeignKey(
        Project, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='reagent_requests'
    )
 
    # Status
    status = models.CharField(
        max_length=20, choices=RequestStatus.choices, default=RequestStatus.DRAFT
    )
 
    # Dates
    requested_date = models.DateField(null=True, blank=True)
 
    # Who requested
    requested_by = models.ForeignKey(
        Member, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='reagent_requests'
    )
 
    comments = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
 
    class Meta:
        ordering = ['-created_at']
 
    def __str__(self):
        company_name = self.company.name if self.company else '—'
        return f"{self.get_item_type_display()}: {company_name} → {self.target} ({self.get_status_display()})"
 
    @property
    def catalogue_list(self):
        """Catalogue numbers as a clean list."""
        if not self.catalogue_numbers:
            return []
        return [c.strip() for c in self.catalogue_numbers.strip().split('\n') if c.strip()]

class Shipment(models.Model):
    """
    Groups received reagent requests into physical deliveries.
    """
    company = models.ForeignKey(
        Company, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='shipments'
    )
    site = models.ForeignKey(
        Site, on_delete=models.PROTECT, related_name='shipments'
    )
    received_date = models.DateField()
    received_by = models.ForeignKey(
        Member, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='shipments_received'
    )
    tracking_number = models.CharField(max_length=255, blank=True)
    condition_notes = models.TextField(
        blank=True, help_text="Condition on arrival, temperature, packaging"
    )
    requests = models.ManyToManyField(
        ReagentRequest, blank=True, related_name='shipments'
    )

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Shipment from {self.company} on {self.received_date}"


# =============================================================================
# Layer 2: Pipeline — Protocol Templates
# =============================================================================

class ProtocolTemplate(models.Model):
    """
    Reusable protocol definitions per site and procedure type.
    Scoping doc §3.4: "per-site standard protocols."
    """
    class ProcedureType(models.TextChoices):
        WB = 'WB', 'Western Blot'
        IP = 'IP', 'Immunoprecipitation'
        IF = 'IF', 'Immunofluorescence'
        FC = 'FC', 'Flow Cytometry'

    name = models.CharField(
        max_length=255,
        help_text="e.g. 'Leicester WB standard', 'Montreal IF standard'"
    )
    procedure_type = models.CharField(max_length=2, choices=ProcedureType.choices)
    site = models.ForeignKey(
        Site, on_delete=models.PROTECT, related_name='protocol_templates'
    )
    is_default = models.BooleanField(
        default=False,
        help_text="Default template for this site + procedure combination"
    )

    conditions = models.JSONField(
        default=dict,
        help_text="Structured protocol conditions as JSON"
    )

    # NEW (Session Workflow): collapsible protocol phases for bench display
    protocol_guidance = models.JSONField(
        default=list, blank=True,
        help_text='Collapsible protocol phases for bench display. '
                  'List of {title, summary, detail, data_fields} dicts.'
    )

    description = models.TextField(blank=True)
    created_by = models.ForeignKey(
        Member, on_delete=models.SET_NULL, null=True, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['site', 'procedure_type', 'name']

    def __str__(self):
        return f"{self.name} ({self.get_procedure_type_display()})"


# =============================================================================
# Layer 2: Pipeline — Experiment Sessions and Results
# =============================================================================

class ExperimentSession(models.Model):
    """
    Base session model for all four procedure types.
    Scoping doc §3.1: "The natural unit of work at the bench is 'I ran a
    WB screening session today.'"
    """
    class ProcedureType(models.TextChoices):
        WB = 'WB', 'Western Blot'
        IP = 'IP', 'Immunoprecipitation'
        IF = 'IF', 'Immunofluorescence'
        FC = 'FC', 'Flow Cytometry'

    procedure_type = models.CharField(max_length=2, choices=ProcedureType.choices)
    target = models.ForeignKey(
        Target, on_delete=models.CASCADE, related_name='sessions'
    )
    protocol_template = models.ForeignKey(
        ProtocolTemplate, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='sessions'
    )

    # Who and when
    experimenter = models.ForeignKey(
        Member, on_delete=models.PROTECT, related_name='sessions'
    )
    date = models.DateField(help_text="Date experiment was performed")
    site = models.ForeignKey(
        Site, on_delete=models.PROTECT, related_name='sessions'
    )

    # Session-level conditions (override or supplement template)
    session_conditions = models.JSONField(
        default=dict,
        help_text="Session-level experimental conditions (supplements/overrides template)"
    )

    # Cell lines used in this session
    cell_line_wt = models.ForeignKey(
        CellLine, on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='sessions_as_wt',
        help_text="Wild-type cell line"
    )
    cell_line_ko = models.ForeignKey(
        CellLine, on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='sessions_as_ko',
        help_text="Knockout cell line"
    )

    # FC sub-protocol
    class FcSubProtocol(models.TextChoices):
        INTRACELLULAR_SAPONIN = 'ic_saponin', 'Intracellular PFA/Saponin'
        INTRACELLULAR_TRITON = 'ic_triton', 'Intracellular PFA/Triton'
        CELL_SURFACE = 'surface', 'Cell Surface'
        NA = 'na', 'N/A'

    fc_sub_protocol = models.CharField(
        max_length=20, choices=FcSubProtocol.choices,
        default=FcSubProtocol.NA,
        help_text="Only applicable for FC sessions"
    )

    # Status — PLANNED added per Session Workflow Redesign
    class SessionStatus(models.TextChoices):
        PLANNED = 'planned', 'Planned'
        IN_PROGRESS = 'in_progress', 'In Progress'
        COMPLETE = 'complete', 'Complete'
        FAILED = 'failed', 'Failed'
        REPEAT_NEEDED = 'repeat_needed', 'Repeat Needed'
        CANCELLED = 'cancelled', 'Cancelled'

    status = models.CharField(
        max_length=20, choices=SessionStatus.choices,
        default=SessionStatus.IN_PROGRESS
    )

    # NEW (Session Workflow): planned date vs execution date
    planned_date = models.DateField(
        null=True, blank=True,
        help_text='Date session was planned/created (auto from today)'
    )
    # NOTE: existing 'date' field becomes the actual execution date
    # Set auto when status moves from planned to in_progress, or manually

    # NEW (Session Workflow): sample links (replaces or supplements cell_line_wt/cell_line_ko)
    samples = models.ManyToManyField(
        'Sample', blank=True,
        related_name='sessions',
        help_text='Lysates/supernatants used in this session'
    )

    # NEW (Session Workflow): protein loading for figure legend
    protein_loading_ug = models.DecimalField(
        max_digits=6, decimal_places=1,
        null=True, blank=True,
        help_text='µg of protein loaded per lane (for figure legend)'
    )

    comments = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-date', 'target']

    def save(self, *args, **kwargs):
        for f in self._meta.fields:
            if f.__class__.__name__ == 'DecimalField' and getattr(self, f.name) == '':
                setattr(self, f.name, None)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.get_procedure_type_display()} session: {self.target} ({self.date})"



class WbResult(models.Model):
    """
    Per-antibody result within a WB session.
    Access source: Wb table (1,912 rows).
    """
    session = models.ForeignKey(
        ExperimentSession, on_delete=models.CASCADE, related_name='wb_results'
    )
    antibody = models.ForeignKey(
        Antibody, on_delete=models.CASCADE, related_name='wb_results'
    )

    dilution = models.CharField(max_length=100, blank=True)
    exposure_time = models.CharField(max_length=100, blank=True)
    signal = models.CharField(
        max_length=255, blank=True,
        help_text="Band pattern / signal assessment"
    )
    rating = models.CharField(max_length=100, blank=True)

    extra_lanes = models.JSONField(
        default=list, blank=True,
        help_text="Additional cell line lanes beyond WT/KO, as list of {cell_line_id, label}"
    )

    gel = models.CharField(max_length=255, blank=True)
    membrane = models.CharField(max_length=255, blank=True)

    ecl = models.CharField(max_length=255, blank=True)
    detection_system = models.CharField(max_length=255, blank=True)

    primary_ab_dilution = models.CharField(max_length=255, blank=True)
    secondary_ab = models.CharField(max_length=255, blank=True)
    secondary_ab_dilution = models.CharField(max_length=255, blank=True)

    comments = models.TextField(blank=True)
    access_id = models.IntegerField(null=True, blank=True, unique=True)

    class Meta:
        verbose_name = 'WB result'
        verbose_name_plural = 'WB results'

    def save(self, *args, **kwargs):
        for f in self._meta.fields:
            if f.__class__.__name__ == 'DecimalField' and getattr(self, f.name) == '':
                setattr(self, f.name, None)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"WB: {self.antibody} — {self.signal}"


class IpResult(models.Model):
    """
    Per-antibody result within an IP session.
    Access source: IP table (1,641 rows).
    """
    session = models.ForeignKey(
        ExperimentSession, on_delete=models.CASCADE, related_name='ip_results'
    )
    antibody = models.ForeignKey(
        Antibody, on_delete=models.CASCADE, related_name='ip_results'
    )

    enrichment = models.CharField(max_length=255, blank=True)
    amount_of_antibody = models.CharField(max_length=255, blank=True)
    amount_of_lysate = models.CharField(max_length=255, blank=True)
    protein_concentration = models.CharField(max_length=255, blank=True)
    volume_of_lysate_ml = models.CharField(max_length=255, blank=True)
    bead_type = models.CharField(max_length=255, blank=True)
    lysis_buffer = models.CharField(max_length=255, blank=True)

    # IP-WB detection step
    detection_ab = models.CharField(
        max_length=255, blank=True,
        help_text="KO-validated antibody used for WB detection step"
    )
    detection_ab_dilution = models.CharField(max_length=255, blank=True)
    secondary_ab = models.CharField(max_length=255, blank=True)
    secondary_ab_dilution = models.CharField(max_length=255, blank=True)

    gel = models.CharField(max_length=255, blank=True)
    membrane = models.CharField(max_length=255, blank=True)
    ecl = models.CharField(max_length=255, blank=True)
    detection_system = models.CharField(max_length=255, blank=True)

    # Three-fraction readout
    sm_assessment = models.CharField(max_length=255, blank=True)
    ub_assessment = models.CharField(max_length=255, blank=True)
    ip_assessment = models.CharField(max_length=255, blank=True)

    # NEW (Session Workflow): exposure time and ECL type for Zenodo filename convention
    exposure_time = models.CharField(
        max_length=50, blank=True,
        help_text='e.g. 20 mins — needed for Zenodo filename convention'
    )
    ecl_type = models.CharField(
        max_length=100, blank=True,
        help_text='e.g. Pierce ECL, Clarity — needed for Zenodo filename'
    )

    comments = models.TextField(blank=True)
    access_id = models.IntegerField(null=True, blank=True, unique=True)

    class Meta:
        verbose_name = 'IP result'
        verbose_name_plural = 'IP results'

    def save(self, *args, **kwargs):
        for f in self._meta.fields:
            if f.__class__.__name__ == 'DecimalField' and getattr(self, f.name) == '':
                setattr(self, f.name, None)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"IP: {self.antibody} — {self.enrichment}"


class IfResult(models.Model):
    """
    Per-antibody result within an IF session.
    Access source: IF table (1,639 rows).
    """
    session = models.ForeignKey(
        ExperimentSession, on_delete=models.CASCADE, related_name='if_results'
    )
    antibody = models.ForeignKey(
        Antibody, on_delete=models.CASCADE, related_name='if_results'
    )

    specific_signal = models.CharField(max_length=255, blank=True)
    wt_ko_ratio_1 = models.DecimalField(
        max_digits=10, decimal_places=4, null=True, blank=True
    )
    wt_ko_ratio_2 = models.DecimalField(
        max_digits=10, decimal_places=4, null=True, blank=True
    )

    concentration_1 = models.CharField(max_length=100, blank=True)
    concentration_2 = models.CharField(max_length=100, blank=True)
    best_concentration = models.CharField(max_length=100, blank=True)

    fixative = models.CharField(max_length=255, blank=True)
    blocking = models.CharField(max_length=255, blank=True)
    # NOTE: permeabilisation already existed in v1.0; Session Workflow doc
    # confirms it stays — allows 2 IfResult rows per antibody (one per perm type)
    permeabilisation = models.CharField(
        max_length=255, blank=True,
        help_text='e.g. Triton, Saponin — allows 2 IfResult rows per antibody'
    )

    primary_ab_dilution = models.CharField(max_length=255, blank=True)
    dilution_buffer = models.CharField(max_length=255, blank=True)
    primary_ab_condition = models.CharField(max_length=255, blank=True)

    secondary_ab = models.CharField(max_length=255, blank=True)
    secondary_ab_condition = models.CharField(max_length=255, blank=True)

    plate_number = models.CharField(max_length=50, blank=True)
    well_number = models.CharField(max_length=50, blank=True)

    image_acquired_by = models.CharField(max_length=255, blank=True)
    image_analysed_by = models.CharField(max_length=255, blank=True)
    microscope = models.CharField(max_length=255, blank=True)
    objective = models.CharField(max_length=255, blank=True)

    comments = models.TextField(blank=True)
    access_id = models.IntegerField(null=True, blank=True, unique=True)

    class Meta:
        verbose_name = 'IF result'
        verbose_name_plural = 'IF results'

    def save(self, *args, **kwargs):
        for f in self._meta.fields:
            if f.__class__.__name__ == 'DecimalField' and getattr(self, f.name) == '':
                setattr(self, f.name, None)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"IF: {self.antibody} — {self.specific_signal}"


class FcResult(models.Model):
    """
    Per-antibody result within a Flow Cytometry session.
    Not in Access — Leicester only currently.
    """
    session = models.ForeignKey(
        ExperimentSession, on_delete=models.CASCADE, related_name='fc_results'
    )
    antibody = models.ForeignKey(
        Antibody, on_delete=models.CASCADE, related_name='fc_results'
    )

    concentration = models.CharField(max_length=100, blank=True)

    histogram_shift = models.CharField(
        max_length=255, blank=True,
        help_text="Assessment of WT vs KO separation in histogram"
    )
    median_fluorescence_wt = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    median_fluorescence_ko = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )

    gating_strategy = models.TextField(blank=True)
    comments = models.TextField(blank=True)

    class Meta:
        verbose_name = 'FC result'
        verbose_name_plural = 'FC results'

    def save(self, *args, **kwargs):
        for f in self._meta.fields:
            if f.__class__.__name__ == 'DecimalField' and getattr(self, f.name) == '':
                setattr(self, f.name, None)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"FC: {self.antibody} — {self.histogram_shift}"


# =============================================================================
# Layer 2: Pipeline — File Attachments
# =============================================================================

class FileAttachment(models.Model):
    """
    Raw images, WB scans, FC histograms, Ponceau stains.
    """
    class FileCategory(models.TextChoices):
        WB_SCAN = 'wb_scan', 'WB Scan'
        WB_COMPOSITE = 'wb_composite', 'WB Composite Panel'
        PONCEAU = 'ponceau', 'Ponceau S Stain'
        IF_IMAGE = 'if_image', 'IF Image'
        IF_PLATE = 'if_plate', 'IF Plate Overview'
        FC_HISTOGRAM = 'fc_histogram', 'FC Histogram'
        FC_FCS = 'fc_fcs', 'FC Raw Data (.fcs)'
        IP_SCAN = 'ip_scan', 'IP Scan'
        OTHER = 'other', 'Other'

    session = models.ForeignKey(
        ExperimentSession, on_delete=models.CASCADE,
        related_name='attachments'
    )
    wb_result = models.ForeignKey(
        WbResult, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='attachments'
    )
    ip_result = models.ForeignKey(
        IpResult, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='attachments'
    )
    if_result = models.ForeignKey(
        IfResult, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='attachments'
    )
    fc_result = models.ForeignKey(
        FcResult, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='attachments'
    )

    category = models.CharField(max_length=20, choices=FileCategory.choices)
    # Its own storage, not the public media bucket the antibody pages serve
    # publication images off — an uncropped gel scan is unpublished lab data.
    # See pipeline/storages.py.
    file = models.FileField(upload_to='pipeline/attachments/%Y/%m/',
                            storage=attachment_storage)
    original_filename = models.CharField(max_length=500)
    file_size_bytes = models.BigIntegerField(null=True, blank=True)
    mime_type = models.CharField(max_length=100, blank=True)
    # SHA-256 of the bytes as stored, computed on upload. A data repository
    # records a checksum for every file it holds, and this is the only way to
    # say later that the file in the Zenodo deposit is the file this row is
    # about — the row, the reading it is evidence for and the published record
    # are otherwise joined by a filename. Blank for anything written before it
    # existed, and never recomputed behind somebody's back.
    checksum_sha256 = models.CharField(max_length=64, blank=True)

    description = models.TextField(blank=True)
    uploaded_by = models.ForeignKey(
        Member, on_delete=models.SET_NULL, null=True, blank=True
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.get_category_display()}: {self.original_filename}"


class PublicationImage(models.Model):
    """
    Cropped publication-ready images displayed on the OGA public site.
    """
    class ApplicationType(models.TextChoices):
        WB = 'WB', 'Western Blot'
        IP = 'IP', 'Immunoprecipitation'
        ICC_IF = 'ICC-IF', 'Immunocytochemistry/IF'
        FC = 'FC', 'Flow Cytometry'

    antibody = models.ForeignKey(
        Antibody, on_delete=models.CASCADE, related_name='publication_images'
    )
    application_type = models.CharField(max_length=10, choices=ApplicationType.choices)
    image = models.FileField(
        upload_to='publication_images/%Y/',
        help_text="Cropped image for public display (supports SVG, PNG, JPG)"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Publication image'
        verbose_name_plural = 'Publication images'
        constraints = [
            models.UniqueConstraint(
                fields=['antibody', 'application_type'],
                name='unique_pub_image_per_antibody_app'
            ),
        ]

    def __str__(self):
        return f"{self.antibody.catalogue_number} — {self.get_application_type_display()}"


class PendingPublicationImage(models.Model):
    """A cropped figure that has been made but not yet published.

    **A `PublicationImage` row is what makes a gene public** — `pipeline/
    public.py` derives the whole public site from "a named gene with at least
    one antibody carrying a published figure", and twenty-eight modules read
    that relation. So the figure cropper, which writes those rows, was a
    one-press path from a scientist's laptop to the live website with no review
    in between; the only gate was a checkbox about *overwriting* what was
    already there.

    This is the waiting room. The cropper writes here, the figures are reviewed
    internally (and by the supplier whose reagent it is, through the data
    portal), and **releasing is a separate deliberate act** that writes the
    `PublicationImage` — see `services/review.py`.

    It is a **separate table rather than a `status` column on `PublicationImage`**
    for the reason this repo keeps relearning: a flag is only as good as the
    readers that remember it, and there are twenty-eight of them here — the
    public gene page, the home page counter, the API feeds, the manifest, the
    extension index, the MCP servers, the selector, the snapshot. One that
    forgot to filter would put an unreviewed figure on the public site in
    silence, which is the exact shape of every defect in CLAUDE.md. Nothing
    existing reads this table, so nothing existing can leak from it.

    Three things it holds beyond the bytes:

    * **The recommendation, withheld.** The cropper used to set
      `Antibody.wb_recommended` and friends at commit — and `core/
      recommendations.py::curated_gene_ids` asks whether *any* antibody on a
      target carries one, which is what separates "tested and not recommended"
      from "never assessed" on the public gene page. Writing the flag while
      holding the figure back would have moved that answer for every other
      antibody on the gene. It is applied to the antibody at release.
    * **Private storage.** `attachment_storage` — never the public custom
      domain, signed and short-lived — because an unreleased figure sitting on
      a permanent public URL is released to anybody who has the key, whatever
      the database says. The bytes are served through a gated view
      (`views/review.py` for staff, `core/api_pipeline.py` for the supplier).
    * **One row per antibody per application**, the same constraint the public
      table carries, so re-cropping a figure *revises* the pending row rather
      than queueing a second copy. Re-cropping something already released puts
      the row back to `pending` and leaves the public figure standing until
      somebody releases the revision.

    A released row is kept rather than deleted: it is the record of what went
    public, when and by whom, and it is what the gene page's history and the
    portal's "released" list are drawn from.
    """

    class Status(models.TextChoices):
        PENDING = 'pending', 'Awaiting release'
        RELEASED = 'released', 'Released to the public site'

    antibody = models.ForeignKey(
        Antibody, on_delete=models.CASCADE, related_name='pending_images'
    )
    application_type = models.CharField(
        max_length=10, choices=PublicationImage.ApplicationType.choices)
    image = models.FileField(
        upload_to='pending_figures/%Y/%m/',
        storage=attachment_storage,
        help_text="The crop as it would appear on the public gene page.",
    )
    # The human's verdict for this antibody in this application, held here until
    # release rather than written onto the antibody. See the class docstring.
    recommended = models.BooleanField(default=False)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING,
        db_index=True)
    # Username rather than an FK: pipeline users live in two databases and the
    # cropper's own session rows already identify their owner this way.
    staged_by = models.CharField(max_length=150, blank=True)
    released_by = models.CharField(max_length=150, blank=True)
    released_at = models.DateTimeField(null=True, blank=True)
    # What the review meeting said. Free text on purpose — the outcome that
    # matters is release or discard, and this is the note beside it.
    notes = models.TextField(blank=True)
    source_session = models.ForeignKey(
        'pipeline.CropperSession', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='pending_images',
        help_text="The cropper session this crop came from, if it still exists.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Pending publication image'
        verbose_name_plural = 'Pending publication images'
        ordering = ['antibody__target__gene_name', 'antibody__catalogue_number',
                    'application_type']
        constraints = [
            models.UniqueConstraint(
                fields=['antibody', 'application_type'],
                name='unique_pending_image_per_antibody_app'
            ),
        ]

    def __str__(self):
        return (f"{self.antibody.catalogue_number} — "
                f"{self.get_application_type_display()} ({self.get_status_display()})")


# =============================================================================
# Layer 3: Reference data caches (external datasets)
# =============================================================================

class DepMapExpression(models.Model):
    """
    Cached DepMap RNA expression data for feasibility lookups.
    Source: Broad DepMap OmicsExpressionAllGenesTPMLogp1Profile.csv
    Values are log2(TPM+1). YCharOS threshold: ≥2.5 for adequate expression.
    """
    gene_name = models.CharField(max_length=100, db_index=True)
    entrez_id = models.IntegerField(null=True, blank=True)
    cell_line = models.CharField(max_length=100, db_index=True)
    depmap_id = models.CharField(max_length=30, blank=True)
    tpm_log2 = models.FloatField()
    depmap_release = models.CharField(max_length=20, blank=True)

    class Meta:
        ordering = ['gene_name', 'cell_line']
        indexes = [models.Index(fields=['gene_name', 'cell_line'])]
        constraints = [
            models.UniqueConstraint(fields=['gene_name', 'cell_line'], name='unique_depmap_gene_cellline'),
        ]

    def __str__(self):
        return f"{self.gene_name} in {self.cell_line}: {self.tpm_log2:.2f}"

    @property
    def above_threshold(self):
        return self.tpm_log2 >= 2.5


class ProteomicsExpression(models.Model):
    """
    Cached Cell Model Passports proteomics data for feasibility lookups.
    Source: Sanger Cell Model Passports Protein_matrix_averaged_YYYYMMDD.tsv
    Values are log2 protein intensity from DIA mass spectrometry.
    """
    gene_name = models.CharField(max_length=100, db_index=True)
    uniprot_id = models.CharField(max_length=20, blank=True, db_index=True)
    cell_line = models.CharField(max_length=100, db_index=True)
    protein_intensity = models.FloatField()
    dataset_version = models.CharField(max_length=30, blank=True)

    class Meta:
        ordering = ['gene_name', 'cell_line']
        indexes = [models.Index(fields=['gene_name', 'cell_line'])]
        constraints = [
            models.UniqueConstraint(fields=['gene_name', 'uniprot_id', 'cell_line'], name='unique_proteomics_gene_cellline'),
        ]

    def __str__(self):
        return f"{self.gene_name} ({self.uniprot_id}) in {self.cell_line}: {self.protein_intensity:.2f}"


class HorizonKoLine(models.Model):
    """
    Catalogue of HAP1 knockout cell lines commercially available from Horizon
    Discovery, keyed by gene symbol so Feasibility can tell people "you can just
    order a ready-made KO for this target" instead of making one.

    This is a *reference catalogue* (what you could buy), not lab inventory —
    the lines the lab actually holds live in ``CellLine``. Loaded from Horizon's
    HAP1 KO clone list via ``import_horizon_ko``; re-runnable (wipe + reload).
    """
    gene_name = models.CharField(max_length=100, db_index=True)
    item_number = models.CharField(
        max_length=50, unique=True,
        help_text="Horizon catalogue/item number, e.g. HZGHC000001c010",
    )
    product_name = models.CharField(
        max_length=255, blank=True,
        help_text="Horizon product description, e.g. 'Human ARAF 5bp deletion knockout cell line'",
    )
    supplier = models.CharField(max_length=100, default="Horizon Discovery")
    background = models.CharField(max_length=50, default="HAP1")
    dataset_version = models.CharField(max_length=30, blank=True)

    class Meta:
        ordering = ['gene_name', 'item_number']
        indexes = [models.Index(fields=['gene_name'])]

    def __str__(self):
        return f"{self.gene_name} — {self.item_number} ({self.background} KO)"


# Figure-cropper session persistence (separate module to avoid editing this
# file in parallel; see pipeline/cropper_models.py).
from pipeline.cropper_models import CropperSession, CropperImage  # noqa: E402,F401

# The daily full capture, kept where both the cron job and the web service can
# reach it (see pipeline/snapshot_models.py).
from pipeline.snapshot_models import DatasetSnapshot  # noqa: E402,F401
