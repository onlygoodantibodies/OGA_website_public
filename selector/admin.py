from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html

from .models import SelectionRecord


@admin.register(SelectionRecord)
class SelectionRecordAdmin(admin.ModelAdmin):
    list_display = ("created_at", "target_gene", "is_compliance", "display_name",
                    "email", "institution", "application", "species",
                    "pillars_summary", "completed", "pdf_link")
    list_filter = ("is_compliance", "completed", "application", "species",
                   "pack_version", "created_at")
    search_fields = ("target_gene", "name", "email", "institution", "public_code")
    readonly_fields = ("public_code", "created_at", "updated_at", "academy_user_id",
                       "plan", "guidance", "pdf_link")
    date_hierarchy = "created_at"

    @admin.display(description="PDF")
    def pdf_link(self, obj):
        try:
            url = reverse("selector:plan_pdf", args=[obj.public_code])
            return format_html('<a href="{}" target="_blank">plan.pdf</a>', url)
        except Exception:
            return "—"
