from django.contrib import admin

from .models import Certificate, PendingClaim


@admin.register(Certificate)
class CertificateAdmin(admin.ModelAdmin):
    list_display = ("recipient_name", "institutional_email", "kind",
                    "module_title", "target_gene", "issued_at")
    list_filter = ("kind", "issued_at")
    search_fields = ("recipient_name", "institutional_email", "target_gene",
                     "rrid", "module_id", "verification_code")
    readonly_fields = ("verification_code", "issued_at")


@admin.register(PendingClaim)
class PendingClaimAdmin(admin.ModelAdmin):
    list_display = ("name", "institutional_email", "modules", "created_at",
                    "verified_at", "num_certificates")
    list_filter = ("verified_at", "created_at")
    search_fields = ("name", "institutional_email", "target_gene", "rrid", "token")
    readonly_fields = ("token", "created_at", "verified_at")

    @admin.display(description="certs issued")
    def num_certificates(self, obj):
        return obj.certificates.count()
