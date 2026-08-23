from django.urls import path

from . import views

app_name = "selector"

urlpatterns = [
    path("", views.tool, name="tool"),
    path("gene-lookup/", views.gene_lookup, name="gene_lookup"),
    path("record/", views.record_plan, name="record_plan"),
    path("record/<uuid:code>/identity/", views.attach_identity, name="attach_identity"),
    path("record/<uuid:code>/", views.record_view, name="record_view"),
    path("plan/<uuid:code>.pdf", views.plan_pdf, name="plan_pdf"),
]
