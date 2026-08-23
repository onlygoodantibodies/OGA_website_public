"""
YCharOS Pipeline — Django Admin Configuration

Registers all pipeline models for CRUD access during development.
Organised by schema layer with sensible list/filter/search defaults.

This is primarily a development and testing tool. The real user-facing
interface (Sara's queue, Riham's entry, Carl's dashboard) will be
custom views built in Step 3.
"""

from django.contrib import admin
from pipeline.models import (
    Site, Member, GrantingAgency, Project, Company,
    Target, Report, TargetAssignment, CellLine, CellLineVial, Antibody,
    InventoryLocation,
    ManufacturerContact, ReagentRequestBatch, ReagentRequest, Shipment,
    ProtocolTemplate,
    ExperimentSession, WbResult, IpResult, IfResult, FcResult,
    FileAttachment,
)


# =============================================================================
# Layer 1: Core entities
# =============================================================================

@admin.register(Site)
class SiteAdmin(admin.ModelAdmin):
    list_display = ['name', 'short_code', 'is_active']
    list_filter = ['is_active']
    search_fields = ['name', 'short_code']


@admin.register(Member)
class MemberAdmin(admin.ModelAdmin):
    list_display = ['__str__', 'site', 'role', 'is_active']
    list_filter = ['site', 'role', 'is_active']
    search_fields = ['display_name', 'user__first_name', 'user__last_name']
    raw_id_fields = ['user']


@admin.register(GrantingAgency)
class GrantingAgencyAdmin(admin.ModelAdmin):
    list_display = ['name', 'abbreviation']
    search_fields = ['name', 'abbreviation']


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ['name', 'granting_agency', 'is_active']
    list_filter = ['granting_agency', 'is_active']
    search_fields = ['name']


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ['name', 'website', 'is_active']
    list_filter = ['is_active']
    search_fields = ['name']


# --- Target and related inlines ---

class ReportInline(admin.TabularInline):
    model = Report
    extra = 0
    fields = ['status', 'zenodo_doi', 'f1000_doi', 'zenodo_date', 'f1000_date']


class TargetAssignmentInline(admin.TabularInline):
    model = TargetAssignment
    extra = 0
    fields = ['site', 'task_type', 'status', 'assigned_to', 'priority', 'notes']
    raw_id_fields = ['assigned_to']


@admin.register(Target)
class TargetAdmin(admin.ModelAdmin):
    list_display = [
        'protein_name', 'gene_name', 'uniprot_id', 'status',
        'site', 'project',
    ]
    list_filter = ['status', 'site', 'project', 'ko_validated']
    search_fields = [
        'protein_name', 'gene_name', 'uniprot_id', 'alternative_name',
    ]
    list_editable = ['status']
    inlines = [TargetAssignmentInline, ReportInline]
    fieldsets = (
        (None, {
            'fields': (
                'protein_name', 'gene_name', 'alternative_name',
                'uniprot_id', 'protein_type', 'theoretical_mass_kda',
            )
        }),
        ('Relationships', {
            'fields': ('project', 'granting_agency', 'site', 'created_by')
        }),
        ('Status & KO', {
            'fields': (
                'status', 'commercial_ko_available', 'ko_validated',
                'depmap_expression',
            )
        }),
        ('Access migration', {
            'classes': ('collapse',),
            'fields': ('access_id',)
        }),
    )


@admin.register(Report)
class ReportAdmin(admin.ModelAdmin):
    list_display = ['target', 'status', 'zenodo_doi', 'f1000_doi', 'zenodo_date']
    list_filter = ['status']
    search_fields = ['target__protein_name', 'target__gene_name']
    raw_id_fields = ['target', 'generated_by']


@admin.register(TargetAssignment)
class TargetAssignmentAdmin(admin.ModelAdmin):
    list_display = ['target', 'site', 'task_type', 'status', 'assigned_to', 'priority']
    list_filter = ['site', 'task_type', 'status']
    search_fields = ['target__protein_name', 'target__gene_name', 'notes']
    list_editable = ['status', 'priority']
    raw_id_fields = ['target', 'assigned_by', 'assigned_to']


# --- Cell Lines ---

class InventoryLocationInline(admin.TabularInline):
    """Shows storage locations inline on both Antibody and CellLine."""
    model = InventoryLocation
    extra = 0
    fields = ['storage_type', 'building', 'room', 'freezer', 'shelf', 'rack', 'box', 'position']


class CellLineInventoryInline(InventoryLocationInline):
    """InventoryLocation inline filtered for cell lines."""
    fk_name = 'cell_line'


@admin.register(CellLine)
class CellLineAdmin(admin.ModelAdmin):
    list_display = [
        'name', 'target', 'genotype', 'company',
        'catalogue_number', 'site', 'ko_validated', 'received',
    ]
    list_filter = ['genotype', 'site', 'ko_validated', 'received', 'company']
    search_fields = [
        'name', 'catalogue_number', 'cellosaurus_id',
        'target__protein_name', 'target__gene_name',
    ]
    raw_id_fields = ['target', 'parent_line']
    inlines = [CellLineInventoryInline]


# --- Cell Line Vials ---

class VialInventoryInline(InventoryLocationInline):
    """InventoryLocation inline filtered for vial batches."""
    fk_name = 'vial'


@admin.register(CellLineVial)
class CellLineVialAdmin(admin.ModelAdmin):
    list_display = [
        'cell_line', 'c_number', 'freeze_date', 'passage_number',
        'vial_count', 'site', 'received', 'thawed',
    ]
    list_filter = ['site', 'received', 'thawed', 'acquisition_method']
    search_fields = [
        'cell_line__name', 'c_number',
        'cell_line__target__protein_name', 'cell_line__target__gene_name',
    ]
    raw_id_fields = ['cell_line']
    inlines = [VialInventoryInline]


# --- Antibodies ---

class AntibodyInventoryInline(InventoryLocationInline):
    """InventoryLocation inline filtered for antibodies."""
    fk_name = 'antibody'


@admin.register(Antibody)
class AntibodyAdmin(admin.ModelAdmin):
    list_display = [
        'catalogue_number', 'target', 'company', 'clonality',
        'host_species', 'rrid', 'site',
    ]
    list_filter = ['clonality', 'site', 'company', 'acquisition_method', 'out_of_market']
    search_fields = [
        'catalogue_number', 'rrid', 'clone_id',
        'target__protein_name', 'target__gene_name',
        'company__name',
    ]
    raw_id_fields = ['target']
    inlines = [AntibodyInventoryInline]
    fieldsets = (
        (None, {
            'fields': (
                'target', 'company', 'catalogue_number', 'lot_number',
                'rrid', 'ab_number', 'site',
            )
        }),
        ('Characterisation', {
            'fields': (
                'clonality', 'clone_id', 'host_species', 'isotype',
                'species_reactivity', 'concentration', 'is_recombinant',
            )
        }),
        ('Supplier validation', {
            'classes': ('collapse',),
            'fields': (
                'supplier_validated_wb', 'supplier_validated_ip',
                'supplier_validated_if', 'supplier_validated_ihc',
                'supplier_validated_elisa', 'supplier_validated_fc',
                'supplier_validated_applications',
                'validation_details_wb', 'validation_details_if',
            )
        }),
        ('Procurement', {
            'fields': (
                'acquisition_method', 'received_date',
                'in_kind_value', 'in_kind_currency', 'out_of_market',
            )
        }),
        ('Notes', {
            'fields': ('comments', 'created_by')
        }),
        ('Access migration', {
            'classes': ('collapse',),
            'fields': ('access_id',)
        }),
    )


@admin.register(InventoryLocation)
class InventoryLocationAdmin(admin.ModelAdmin):
    list_display = ['__str__', 'site', 'storage_type', 'antibody', 'cell_line', 'vial']
    list_filter = ['site', 'storage_type']
    search_fields = [
        'antibody__catalogue_number', 'cell_line__name',
        'vial__cell_line__name', 'vial__c_number',
        'building', 'room', 'freezer', 'box',
    ]


# =============================================================================
# Layer 2: Pipeline — Reagent Requests & Shipments
# =============================================================================

@admin.register(ManufacturerContact)
class ManufacturerContactAdmin(admin.ModelAdmin):
    list_display = ['company', 'contact_name', 'email', 'site', 'is_active']
    list_filter = ['company', 'site', 'is_active']
    search_fields = ['contact_name', 'email', 'company__name']


@admin.register(ReagentRequestBatch)
class ReagentRequestBatchAdmin(admin.ModelAdmin):
    list_display = ['title', 'site', 'status', 'item_count', 'received_count', 'created_at']
    list_filter = ['status', 'site']
    search_fields = ['title', 'notes']
    raw_id_fields = ['created_by']


@admin.register(ReagentRequest)
class ReagentRequestAdmin(admin.ModelAdmin):
    list_display = ['target', 'company', 'item_type', 'status', 'quantity', 'requested_date', 'created_at']
    list_filter = ['status', 'item_type', 'site', 'company']
    search_fields = [
        'catalogue_numbers',
        'target__protein_name', 'target__gene_name',
        'company__name',
    ]
    list_editable = ['status']
    raw_id_fields = ['target', 'company', 'batch', 'requested_by']
    fieldsets = (
        (None, {
            'fields': (
                'batch', 'target', 'item_type', 'company',
                'catalogue_numbers', 'quantity', 'requested_volume_ul',
                'site', 'project', 'status',
            )
        }),
        ('Dates & People', {
            'fields': ('requested_date', 'requested_by')
        }),
        ('Notes', {
            'fields': ('comments',)
        }),
    )


@admin.register(Shipment)
class ShipmentAdmin(admin.ModelAdmin):
    list_display = ['company', 'site', 'received_date', 'received_by', 'tracking_number']
    list_filter = ['site', 'company']
    search_fields = ['tracking_number', 'company__name']
    raw_id_fields = ['received_by']
    filter_horizontal = ['requests']
    date_hierarchy = 'received_date'


# =============================================================================
# Layer 2: Pipeline — Protocol Templates
# =============================================================================

@admin.register(ProtocolTemplate)
class ProtocolTemplateAdmin(admin.ModelAdmin):
    list_display = ['name', 'procedure_type', 'site', 'is_default']
    list_filter = ['procedure_type', 'site', 'is_default']
    search_fields = ['name']


# =============================================================================
# Layer 2: Pipeline — Experiment Sessions and Results
# =============================================================================

class WbResultInline(admin.TabularInline):
    model = WbResult
    extra = 0
    fields = ['antibody', 'dilution', 'signal', 'rating', 'comments']
    raw_id_fields = ['antibody']


class IpResultInline(admin.TabularInline):
    model = IpResult
    extra = 0
    fields = ['antibody', 'enrichment', 'bead_type', 'comments']
    raw_id_fields = ['antibody']


class IfResultInline(admin.TabularInline):
    model = IfResult
    extra = 0
    fields = ['antibody', 'specific_signal', 'concentration_1', 'concentration_2', 'comments']
    raw_id_fields = ['antibody']


class FcResultInline(admin.TabularInline):
    model = FcResult
    extra = 0
    fields = ['antibody', 'concentration', 'histogram_shift', 'comments']
    raw_id_fields = ['antibody']


class FileAttachmentInline(admin.TabularInline):
    model = FileAttachment
    extra = 0
    fields = ['category', 'file', 'original_filename', 'description']


@admin.register(ExperimentSession)
class ExperimentSessionAdmin(admin.ModelAdmin):
    list_display = [
        'target', 'procedure_type', 'date', 'experimenter',
        'site', 'status', 'fc_sub_protocol_display',
    ]
    list_filter = ['procedure_type', 'site', 'status', 'fc_sub_protocol']
    search_fields = [
        'target__protein_name', 'target__gene_name',
        'comments',
    ]
    raw_id_fields = ['target', 'experimenter', 'protocol_template', 'cell_line_wt', 'cell_line_ko']
    date_hierarchy = 'date'
    inlines = [WbResultInline, IpResultInline, IfResultInline, FcResultInline, FileAttachmentInline]

    def fc_sub_protocol_display(self, obj):
        """Only show FC sub-protocol for FC sessions."""
        if obj.procedure_type == 'FC':
            return obj.get_fc_sub_protocol_display()
        return '—'
    fc_sub_protocol_display.short_description = 'FC Sub-protocol'


# --- Standalone result admins (for direct access / debugging) ---

@admin.register(WbResult)
class WbResultAdmin(admin.ModelAdmin):
    list_display = ['antibody', 'session', 'signal', 'rating', 'dilution']
    list_filter = ['rating']
    search_fields = [
        'antibody__catalogue_number',
        'session__target__protein_name',
        'signal',
    ]
    raw_id_fields = ['session', 'antibody']


@admin.register(IpResult)
class IpResultAdmin(admin.ModelAdmin):
    list_display = ['antibody', 'session', 'enrichment', 'bead_type']
    search_fields = [
        'antibody__catalogue_number',
        'session__target__protein_name',
        'enrichment',
    ]
    raw_id_fields = ['session', 'antibody']


@admin.register(IfResult)
class IfResultAdmin(admin.ModelAdmin):
    list_display = [
        'antibody', 'session', 'specific_signal',
        'concentration_1', 'concentration_2', 'best_concentration',
    ]
    search_fields = [
        'antibody__catalogue_number',
        'session__target__protein_name',
        'specific_signal',
    ]
    raw_id_fields = ['session', 'antibody']


@admin.register(FcResult)
class FcResultAdmin(admin.ModelAdmin):
    list_display = ['antibody', 'session', 'histogram_shift', 'concentration']
    search_fields = [
        'antibody__catalogue_number',
        'session__target__protein_name',
        'histogram_shift',
    ]
    raw_id_fields = ['session', 'antibody']


@admin.register(FileAttachment)
class FileAttachmentAdmin(admin.ModelAdmin):
    list_display = ['original_filename', 'category', 'session', 'uploaded_at']
    list_filter = ['category']
    search_fields = ['original_filename', 'description']
    raw_id_fields = ['session', 'wb_result', 'ip_result', 'if_result', 'fc_result', 'uploaded_by']
