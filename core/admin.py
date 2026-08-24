from django.contrib import admin

from .models import APIConsumer, ApiUsageDay


@admin.register(APIConsumer)
class APIConsumerAdmin(admin.ModelAdmin):
    list_display = ('name', 'consumer_type', 'tier', 'supplier_filter', 'gene_filter', 'api_key', 'last_queried_at', 'is_active')
    list_filter = ('consumer_type', 'tier', 'is_active')
    readonly_fields = ('api_key', 'last_queried_at', 'created_at')
    search_fields = ('name', 'supplier_filter')

    fieldsets = (
        (None, {
            'fields': ('name', 'consumer_type', 'tier', 'supplier_filter', 'gene_filter', 'is_active')
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
