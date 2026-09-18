from django.contrib import admin, messages
from django.db.models import F

from .models import APIConsumer, ApiUsageDay, McpUsageDay


@admin.register(APIConsumer)
class APIConsumerAdmin(admin.ModelAdmin):
    list_display = ('name', 'consumer_type', 'tier', 'supplier_filter', 'gene_filter', 'api_key', 'last_queried_at', 'is_active', 'is_internal')
    list_filter = ('consumer_type', 'tier', 'is_active', 'is_internal')
    readonly_fields = ('api_key', 'last_queried_at', 'created_at')
    search_fields = ('name', 'supplier_filter')

    fieldsets = (
        (None, {
            'fields': ('name', 'consumer_type', 'tier', 'supplier_filter', 'gene_filter', 'is_active', 'is_internal')
        }),
        # A field a write path fills must be drawn on some screen. The portal
        # has written this since it was added and no admin screen showed it, so
        # the one question worth asking about a consumer's downloads — what
        # image format are they set to? — could not be answered from here at
        # all. It matters because the value is the *consumer's*, shared by
        # anybody holding the key: testing with somebody else's key and pressing
        # Save writes your form onto their account.
        ('Portal settings', {
            'fields': ('portal_config',),
            'description': (
                'Written by the customer portal. Empty is the default and is '
                'what every setting falls back to — clear this box to put a '
                'consumer back to defaults. Keys: filename_pattern, '
                'url_pattern, image_format ("png" = original, "jpg" = '
                'converted in the browser), contact_email.'),
        }),
        ('Auto-generated', {
            'fields': ('api_key', 'last_queried_at', 'created_at'),
            'classes': ('collapse',),
        }),
    )

@admin.register(ApiUsageDay)
class ApiUsageDayAdmin(admin.ModelAdmin):
    """Read-only, because these are counted, not typed.

    A number a write path fills and no screen draws is a number nobody can act
    on — the same rule as `Target.aliases` on the pipeline side. This is the
    screen. It is deliberately the plainest one that answers the question the
    counter exists for: who is calling, which endpoints, how often, and how the
    keyless share of that moves.
    """
    list_display = ('date', 'who', 'endpoint', 'count')
    list_filter = ('date', 'endpoint', 'consumer')
    search_fields = ('endpoint', 'consumer__name')
    date_hierarchy = 'date'

    @admin.display(description='Consumer', ordering='consumer__name')
    def who(self, obj):
        # "no key" rather than an empty cell: a blank reads as a missing value,
        # and this one is a fact — the request carried no usable key, and the
        # caller is deliberately not recorded.
        return obj.consumer.name if obj.consumer else '— no key —'

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(McpUsageDay)
class McpUsageDayAdmin(admin.ModelAdmin):
    """Counted, not typed — with one editable column, and a reason for it.

    `ApiUsageDay` above is read-only because every fact on it is derived from a
    request. This one is the same except for `internal`, which is a *judgement*
    the connector makes at call time and can only make once it has been told
    whose sign-ins are ours. Rows written before that — or by a caller it could
    not attribute — are external by default, and there has to be somewhere to
    correct them, or the first weeks of counting are permanently wrong with no
    way to say so.

    Correcting a row **moves** a call between the two buckets and never deletes
    it: the impact page prints what it excluded, so a call marked ours is still
    visible there, and deleting instead would quietly shrink a total somebody
    may already have quoted.

    **Ticking a row whose bucket is already occupied merges it.** `internal` is
    part of the unique key, so a day/tool/client that has calls in both buckets
    has two rows, and moving one onto the other collides. Left to the database
    that is an `IntegrityError` and a 500 — which is what happened the first
    time anybody used this screen, on 1 Sep 2026, ticking all four rows at once.
    The right answer is not to refuse: the person means "count these as ours",
    and the two rows are the same tool on the same day, so the counts add. The
    row is absorbed into the one that survives and `message_user` says what
    moved. No count is lost, and nothing is decided silently.
    """

    list_display = ('date', 'tool', 'named_client', 'internal', 'count')
    list_filter = ('date', 'internal', 'tool', 'client')
    search_fields = ('tool', 'client')
    date_hierarchy = 'date'
    list_editable = ('internal',)
    readonly_fields = ('date', 'tool', 'client', 'count')

    @admin.display(description='Client', ordering='client')
    def named_client(self, obj):
        # Same rule as `who` above: a blank cell reads as a missing value, and
        # this one is a fact — the client did not name itself.
        return obj.client or '— did not say —'

    def save_model(self, request, obj, form, change):
        """Move the row, or merge it into the one already in that bucket.

        The merge is the whole point of this override, and it is deliberately
        an addition rather than a replacement: both rows count real calls, and
        the survivor has to end up holding all of them. Done with an `F`
        expression so two people ticking at once cannot lose one.
        """
        twin = (McpUsageDay.objects
                .filter(date=obj.date, tool=obj.tool, client=obj.client,
                        internal=obj.internal)
                .exclude(pk=obj.pk)
                .first())
        if twin is None:
            super().save_model(request, obj, form, change)
            return

        McpUsageDay.objects.filter(pk=twin.pk).update(count=F('count') + obj.count)
        moved = obj.count
        McpUsageDay.objects.filter(pk=obj.pk).delete()
        twin.refresh_from_db()
        # Never open with the tool name: Django's admin template renders
        # `{{ message|capfirst }}`, so `list_targets` would print as
        # `List_targets`, which is not a tool anybody can search for. Same rule
        # as the `µg/mL` heading CSS uppercased into `MG/ML` — an identifier is
        # not prose and presentation must not rewrite it.
        self.message_user(
            request,
            f"Two rows for {obj.tool} on {obj.date:%-d %b %Y} were merged — "
            f"there was already one for "
            f"{'our own' if obj.internal else 'external'} calls. "
            f"{moved} call{'' if moved == 1 else 's'} moved across and that row "
            f"now holds {twin.count}. Nothing was lost.",
            messages.WARNING)
