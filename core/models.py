import uuid

from django.db import models
from django.utils import timezone


class CellLine(models.Model):
    name = models.CharField(
        max_length=150,
        unique=True,
        help_text="Name of the cell line (e.g., HeLa, HEK293, A549)"
    )
    purchase_link = models.URLField(
        blank=True,
        null=True,
        help_text="Optional link to purchase or learn more about this cell line"
    )

    def __str__(self):
        return self.name


class Gene(models.Model):
    name = models.CharField(max_length=100, unique=True)
    citation = models.TextField(blank=True, null=True)
    aliases = models.CharField(
        max_length=500,
        blank=True,
        null=True,
        help_text="Comma-separated list of alternative gene names/symbols (auto-populated from HGNC)"
    )

    wb_image = models.ImageField(upload_to="gene_experiments/", null=True, blank=True)
    ip_image = models.ImageField(upload_to="gene_experiments/", null=True, blank=True)
    icc_if_image = models.ImageField(upload_to="gene_experiments/", null=True, blank=True)
    fc_image = models.ImageField(upload_to="gene_experiments/", null=True, blank=True)

    # --- Single link fields ---
    f1000_report_link = models.URLField(
        blank=True,
        null=True,
        help_text="Link to the external F1000 report for this gene"
    )
    cell_line_link = models.URLField(
        blank=True,
        null=True,
        help_text="Link to the cell line used for this gene"
    )

    def __str__(self):
        return self.name


class Antibody(models.Model):
    name = models.CharField(max_length=255)
    gene = models.ForeignKey(Gene, on_delete=models.CASCADE, related_name="antibodies")
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"{self.name} ({self.gene.name})"


class Experiment(models.Model):
    antibody = models.ForeignKey(Antibody, on_delete=models.CASCADE, related_name="experiments")
    experiment_type = models.CharField(
        max_length=50,
        choices=[
            ("WB", "Western Blot"),
            ("IP", "Immunoprecipitation"),
            ("ICC-IF", "Immunocytochemistry/IF"),
            ("FC", "Flow Cytometry"),
        ],
    )
    file_path = models.FileField(upload_to="experiments/", null=True, blank=True)  # Use FileField for SVG support
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"{self.antibody.name} - {self.experiment_type}"


class Description(models.Model):
    antibody = models.OneToOneField(Antibody, on_delete=models.CASCADE, related_name="description")
    rrid = models.CharField(max_length=100, blank=True, null=True)
    supplier = models.CharField(max_length=255, blank=True, null=True)
    host = models.CharField(max_length=100, blank=True, null=True)
    clonality = models.CharField(max_length=100, blank=True, null=True)
    clone_ID = models.CharField(max_length=100, blank=True, null=True)
    recombinant = models.CharField(max_length=100, blank=True, null=True)

    product_link = models.CharField(max_length=255, blank=True, null=True)
    discontinued = models.BooleanField(default=False)

    # ✔ Tick-box fields
    wb_app = models.BooleanField("Western Blot", default=False)
    icc_if_app = models.BooleanField("ICC / IF", default=False)
    ip_app = models.BooleanField("Immunoprecipitation (IP)", default=False)
    fc_app = models.BooleanField("Flow Cytometry (FC)", default=False)

    def __str__(self):
        return f"Description for {self.antibody.name}"


class APIConsumer(models.Model):
    """
    Tracks organisations that consume the OGA API.

    Two consumer types:
    - 'manufacturer': queries filtered by their supplier name (e.g. Abcam)
    - 'rrid': queries all antibodies regardless of vendor

    The API automatically tracks when each consumer last queried,
    so subsequent calls return only what's new.
    """

    CONSUMER_TYPES = [
        ('manufacturer', 'Manufacturer'),
        ('rrid', 'RRID / Registry'),
    ]

    TIER_CHOICES = [
        ('free', 'Free — embed cards and iframe strings only'),
        ('data', 'Data — bulk downloads, custom cards, SEO packages'),
        ('intel', 'Intel — data tier plus competitor benchmarking'),
        ('full', 'Full — all features including issue reporting'),
    ]

    name = models.CharField(
        max_length=255,
        help_text="Organisation name (e.g. 'Abcam', 'Antibody Registry')"
    )
    api_key = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        editable=False,
        help_text="Auto-generated API key — share with the consumer"
    )
    consumer_type = models.CharField(
        max_length=20,
        choices=CONSUMER_TYPES
    )
    tier = models.CharField(
        max_length=20,
        choices=TIER_CHOICES,
        default='free',
        help_text="Controls which portal features this consumer can access"
    )
    supplier_filter = models.CharField(
        max_length=500,
        blank=True,
        null=True,
        help_text="For manufacturers: comma-separated list of supplier names to match "
                  "(e.g. 'Bio-Techne,Bio-Techne (Novus Biologicals),Bio-Techne (R&D Systems)'). "
                  "Must exactly match Description.supplier values. "
                  "Ignored for RRID consumers."
    )
    gene_filter = models.CharField(
        max_length=500,
        blank=True,
        null=True,
        help_text="Comma-separated gene names to restrict access to "
                  "(e.g. 'GBA1 (GCase),GPNMB,CD44'). "
                  "Leave blank for unrestricted access to all genes. "
                  "Used for demo/trial accounts — clear when converting to paid."
    )
    last_queried_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Auto-updated each time this consumer calls the API"
    )
    portal_config = models.JSONField(
        blank=True,
        null=True,
        help_text="Customer portal preferences: URL pattern, filename convention, "
                  "image format, contact info, etc."
    )
    created_at = models.DateTimeField(default=timezone.now)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "API Consumer"
        verbose_name_plural = "API Consumers"

    def __str__(self):
        return f"{self.name} ({self.get_consumer_type_display()})"

    def get_gene_filter_list(self):
        """Return list of allowed gene names, or empty list if unrestricted."""
        if not self.gene_filter:
            return []
        return [g.strip() for g in self.gene_filter.split(',') if g.strip()]


class ReviewedAntibody(models.Model):
    """
    Tracks which antibodies a portal consumer has individually reviewed.
    Works alongside last_queried_at (coarse timestamp) to determine 'new' status.
    An antibody is considered reviewed if EITHER:
      - it was created before consumer.last_queried_at, OR
      - it exists in this table for that consumer.
    Routes to academy_db automatically (not in CORE_DATA_MODELS).
    """
    consumer = models.ForeignKey(
        APIConsumer,
        on_delete=models.CASCADE,
        related_name='reviewed_antibodies'
    )
    antibody_catalogue = models.CharField(
        max_length=100,
        help_text="Antibody catalogue number (e.g. ab12345)"
    )
    reviewed_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('consumer', 'antibody_catalogue')
        verbose_name = "Reviewed Antibody"
        verbose_name_plural = "Reviewed Antibodies"

    def __str__(self):
        return f"{self.consumer.name} reviewed {self.antibody_catalogue}"

class ApiUsageDay(models.Model):
    """How many requests each API consumer made, per endpoint, per day.

    **Nothing recorded API usage before this.** `APIConsumer` holds one
    timestamp, `last_queried_at`, and that is the delta feed's review cursor
    rather than a "last seen" — it moves only on `/antibodies/` without
    `preview=true` or `advance_cursor=false`, and on `/mark-reviewed/`, so a
    consumer syncing off the manifest nightly for a year never touches it. The
    throttle counters live in a cache and evaporate. So the question a key was
    supposed to answer — *who is using this, and how much* — had no answer
    anywhere, and opening the API up would have removed a gate whose value
    nobody could measure, against a baseline that did not exist.

    Three things about the shape, each a decision.

    **A row per (consumer, day, endpoint), incremented in place.** The row count
    is bounded by consumers × endpoints × days no matter what the traffic does,
    so a crawler costs one `UPDATE` per request and no growth. The alternative —
    a row per request — is a log whose size is set by whoever is hammering it,
    on a starter box, in the SQLite database that also holds the site's logins.

    **A keyless request is counted with `consumer=None`**, aggregated, and its
    caller is never identified. The baseline has to include the unauthenticated
    traffic or "did opening it up increase usage?" cannot be answered — but
    storing an address per request would turn a counter into a visitor log, and
    the anonymous throttle already has the address it needs without keeping it.

    **A nullable FK cannot carry a plain `unique_together`**, because `NULL` is
    not equal to `NULL` in SQL: two workers racing on the keyless row would each
    insert one, the constraint would allow both, and the count would silently
    split across rows nobody would think to sum. Two partial constraints instead
    — one for the identified rows, one for the keyless — so a race fails loudly
    and `core/api_usage.py` retries the increment.
    """

    consumer = models.ForeignKey(
        APIConsumer,
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name='usage_days',
        help_text="Blank means the request carried no usable API key.",
    )
    date = models.DateField(db_index=True)
    endpoint = models.CharField(
        max_length=64,
        help_text="URL name, e.g. 'antibodies_feed'.",
    )
    count = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = "API usage (day)"
        verbose_name_plural = "API usage (days)"
        ordering = ['-date', 'endpoint']
        constraints = [
            models.UniqueConstraint(
                fields=['consumer', 'date', 'endpoint'],
                condition=models.Q(consumer__isnull=False),
                name='one_usage_row_per_consumer_day_endpoint',
            ),
            models.UniqueConstraint(
                fields=['date', 'endpoint'],
                condition=models.Q(consumer__isnull=True),
                name='one_usage_row_per_keyless_day_endpoint',
            ),
        ]

    def __str__(self):
        who = self.consumer.name if self.consumer else 'no key'
        return f"{self.date} {self.endpoint} — {who}: {self.count}"
