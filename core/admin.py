from django.contrib import admin
from django import forms
from django.urls import path
from django.http import JsonResponse
from .models import (Gene, Antibody, Experiment, Description, CellLine,
                     APIConsumer, ApiUsageDay)


# --- Cell Line Admin ---
@admin.register(CellLine)
class CellLineAdmin(admin.ModelAdmin):
    list_display = ("name", "purchase_link")
    search_fields = ("name",)
    ordering = ("name",)


@admin.register(Gene)
class GeneAdmin(admin.ModelAdmin):
    fields = (
        'name',
        'cell_line_link',
        'wb_image',
        'ip_image',
        'icc_if_image',
        'fc_image',
        'f1000_report_link',
        'citation',
    )
    list_display = ('name',)
    search_fields = ('name',)


# Inline class for Description
class DescriptionInline(admin.StackedInline):
    model = Description
    extra = 1

    fieldsets = (
        ("Antibody Details", {
            "fields": (
                'rrid',
                'supplier',
                'host',
                'clonality',
                'clone_ID',
                'recombinant',
                'product_link',
                'discontinued',
            )
        }),

        ("Recommended Applications", {
            "fields": (
                'wb_app',
                'icc_if_app',
                'ip_app',
                'fc_app',
            ),
        }),
    )


# Antibody Admin
class AntibodyAdmin(admin.ModelAdmin):
    inlines = [DescriptionInline]
    list_display = ('name', 'gene')
    search_fields = ('name', 'gene__name')


class ExperimentAdminForm(forms.ModelForm):
    gene = forms.ModelChoiceField(
        queryset=Gene.objects.all(),
        required=False,
        label="Gene",
        help_text="Select a gene to filter antibodies."
    )

    class Meta:
        model = Experiment
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Default: empty queryset for antibodies
        self.fields['antibody'].queryset = Antibody.objects.none()

        if self.instance and self.instance.pk:
            # Editing an existing experiment
            gene = self.instance.antibody.gene
            self.fields['gene'].initial = gene
            self.fields['antibody'].queryset = Antibody.objects.filter(gene=gene)
        elif 'gene' in self.data:
            # Adding a new experiment and filtering antibodies based on selected gene
            try:
                gene_id = int(self.data.get('gene'))
                self.fields['antibody'].queryset = Antibody.objects.filter(gene_id=gene_id)
            except (ValueError, TypeError):
                self.fields['antibody'].queryset = Antibody.objects.none()


class ExperimentAdmin(admin.ModelAdmin):
    form = ExperimentAdminForm

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                'antibodies_by_gene/',
                self.admin_site.admin_view(self.antibodies_by_gene),
                name='antibodies_by_gene',
            ),
        ]
        return custom_urls + urls

    def antibodies_by_gene(self, request):
        """
        View to handle AJAX requests for antibodies filtered by gene.
        """
        gene_id = request.GET.get('gene_id')
        if gene_id:
            antibodies = Antibody.objects.filter(gene_id=gene_id).values('id', 'name')
            return JsonResponse({'antibodies': list(antibodies)})
        return JsonResponse({'antibodies': []})

    class Media:
        js = ('core/experiment_admin.js',)


# Register models in admin
admin.site.register(Antibody, AntibodyAdmin)
admin.site.register(Experiment, ExperimentAdmin)


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
