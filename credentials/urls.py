from django.urls import path

from . import views

app_name = "credentials"

urlpatterns = [
    path("claim/", views.claim, name="claim"),
    path("claim/verify/<uuid:token>/", views.claim_verify, name="claim_verify"),
    path("cert/<uuid:code>/pdf/", views.cert_pdf, name="cert_pdf"),
    path("verify/<uuid:code>/", views.verify, name="verify"),
]
