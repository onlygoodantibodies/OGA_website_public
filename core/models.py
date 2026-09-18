import uuid

from django.db import models
from django.utils import timezone


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
    #: Ours, not a customer's — a key we hold for testing or for running a demo.
    #: The impact page's totals are what somebody quotes in a grant, and our own
    #: exercising of the API is not reach. It is drawn there rather than hidden:
    #: excluded from the headline and counted underneath, because a number that
    #: silently swallowed our traffic cannot tell a quiet month from a broken
    #: counter, which is the confusion these counters exist to end.
    #:
    #: Deliberately separate from `is_active`: a live internal key is normal, and
    #: a switched-off customer key is still a customer.
    is_internal = models.BooleanField(
        default=False,
        verbose_name="Internal / test key",
        help_text="Ours — testing or demos. Kept out of the impact page's "
                  "headline totals and counted separately beneath them.",
    )

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
    Routes to academy_db automatically — every core model does now.
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


class McpUsageDay(models.Model):
    """How many tool calls the hosted MCP server served, per tool, per day.

    **Nothing recorded this before.** The MCP server is its own Render service
    (`OGA_MCP`), so the only evidence it was being used at all was that
    service's log — which Render keeps for seven days and then discards. On
    31 Aug 2026 the question "is anybody using the MCP server?" could be
    answered for the past week and for no week before it, and the WorkOS user
    list that looked like the answer is a list of *accounts*, which counts a
    sign-in that never called a tool the same as a partner using it daily.

    Three things about the shape, each borrowed deliberately from
    ``ApiUsageDay`` above, whose docstring carries the longer reasoning.

    **A row per (day, tool, client), incremented in place**, so the table's
    size is bounded by tools × clients × days and not by how hard anybody
    pulls.

    **``client`` is the MCP client's own name** — ``claude-ai``,
    ``claude-code``, ``chatgpt`` — as it reports itself at connection. It is
    the software, never the person: the server knows an OAuth subject and this
    table deliberately does not, so the impact page can quote reach without
    holding identities.

    **Blank means the client did not say**, and is a value like any other
    rather than ``NULL``, which keeps a plain unique constraint honest — the
    lesson ``ApiUsageDay`` paid for with two partial ones.

    Written by `core/mcp_usage.py` when the MCP service reports a call. A call
    served with no report — the server running locally over stdio, or the
    reporting token unset — is absent rather than zero, and the impact page
    says so.
    """

    date = models.DateField(db_index=True)
    tool = models.CharField(
        max_length=64,
        help_text="MCP tool name, e.g. 'search_antibodies'.",
    )
    client = models.CharField(
        max_length=64, blank=True,
        help_text="The MCP client's own name. Blank means it did not say.",
    )
    #: A call by us rather than by a reader — decided by the connector, which
    #: compares the caller's OAuth subject against ``MCP_INTERNAL_SUBJECTS`` and
    #: sends this boolean **instead of** the identity. So the table still holds
    #: no identities, and the impact page can still tell our own testing from
    #: somebody else's use. A call the connector could not attribute — no token,
    #: or a subject not on the list — is external, which is the direction that
    #: over-reports us rather than inventing reach.
    internal = models.BooleanField(default=False)
    count = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = "MCP usage (day)"
        verbose_name_plural = "MCP usage (days)"
        ordering = ['-date', 'tool']
        constraints = [
            # `internal` is part of the key, not a property of the row. Without
            # it, our call and a reader's call to one tool on one day collide on
            # the same row and the flag becomes whichever arrived first — the
            # count would be right and the attribution silently wrong, which is
            # worse than not splitting them at all.
            models.UniqueConstraint(
                fields=['date', 'tool', 'client', 'internal'],
                name='one_mcp_usage_row_per_day_tool_client_origin',
            ),
        ]

    def __str__(self):
        return f"{self.date} {self.tool} — {self.client or 'unnamed client'}: {self.count}"
