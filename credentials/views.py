"""Public workshop-certificate claim + verification (no login).

The connector points a learner here after they complete the interactive workshop.
They submit name + institutional email (+ the plan); we email a one-click link;
following it issues a Certificate on `credentials_db`. Everything here is public
and self-contained — no academy login, no `auth_user`.
"""
import json
import os

from django.conf import settings
from django.core.mail import send_mail
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

from .institutional import is_free_provider, is_institutional_email
from .models import MODULE_TITLES, Certificate, PendingClaim


def _render_certificate_pdf(cert, request):
    """Render a Certificate to a PDF HttpResponse (with verification code + QR)."""
    who = cert.recipient_display
    response = HttpResponse(content_type="application/pdf")
    safe = "".join(ch for ch in who if ch.isalnum() or ch in "-_") or "certificate"
    response["Content-Disposition"] = f'attachment; filename="certificate_{safe}.pdf"'

    c = canvas.Canvas(response, pagesize=letter)
    width, height = letter

    c.setLineWidth(4)
    margin = 0.5 * inch
    c.rect(margin, margin, width - 2 * margin, height - 2 * margin)

    logo_path = os.path.join(settings.STATIC_ROOT, "core", "logo.png")
    if os.path.exists(logo_path):
        logo_width = 1.5 * inch
        c.drawImage(logo_path, (width - logo_width) / 2,
                    height - margin - logo_width, logo_width, logo_width,
                    preserveAspectRatio=True)

    c.setFont("Helvetica-Bold", 24)
    c.drawCentredString(width / 2, height - margin - (1.6 * inch),
                        "Certificate of Completion")
    c.setFont("Helvetica-Bold", 20)
    c.drawCentredString(width / 2, height - margin - (2.4 * inch), who)
    c.setFont("Helvetica", 12)
    c.drawCentredString(width / 2, height - margin - (3.2 * inch),
                        "has successfully completed")
    c.setFont("Helvetica-BoldOblique", 16)
    c.drawCentredString(width / 2, height - margin - (3.8 * inch), cert.display_title)
    c.setFont("Helvetica", 12)
    date_str = cert.issued_at.strftime("%B %d, %Y")
    c.drawCentredString(width / 2, height - margin - (4.8 * inch),
                        f"Issued on {date_str}")

    verify_url = request.build_absolute_uri(cert.get_verify_url())
    c.setFont("Helvetica", 9)
    c.drawCentredString(width / 2, margin + 0.95 * inch,
                        f"Verification code: {cert.verification_code}")
    c.setFont("Helvetica-Oblique", 8)
    c.drawCentredString(width / 2, margin + 0.75 * inch, "Verify this certificate at:")
    c.drawCentredString(width / 2, margin + 0.60 * inch, verify_url)
    try:
        from reportlab.graphics import renderPDF
        from reportlab.graphics.barcode import qr
        from reportlab.graphics.shapes import Drawing
        qr_size = 0.9 * inch
        widget = qr.QrCodeWidget(verify_url)
        b = widget.getBounds()
        d = Drawing(qr_size, qr_size,
                    transform=[qr_size / (b[2] - b[0]), 0, 0,
                               qr_size / (b[3] - b[1]), 0, 0])
        d.add(widget)
        renderPDF.draw(d, c, (width - qr_size) / 2, margin + 1.15 * inch)
    except Exception:
        pass  # QR is a nice-to-have; never let it break PDF generation.

    c.showPage()
    c.save()
    return response


def _module_choices(selected):
    """The module checkboxes shown on the claim form (ordered, with checked state)."""
    order = ["framework", "controls", "acquiring"]
    sel = set(selected or [])
    return [{"id": mid, "title": MODULE_TITLES[mid], "checked": mid in sel}
            for mid in order if mid in MODULE_TITLES]


def claim(request):
    """Claim form — request module certificates and/or the workshop certificate."""
    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()
        email = (request.POST.get("email") or "").strip()
        gene = (request.POST.get("gene") or "").strip()
        rrid = (request.POST.get("rrid") or "").strip()
        plan_raw = (request.POST.get("plan") or "").strip()
        modules = [m for m in request.POST.getlist("modules") if m in MODULE_TITLES]

        errors = []
        if not name:
            errors.append("Please enter your name.")
        if not email:
            errors.append("Please enter your email.")
        elif is_free_provider(email):
            errors.append(
                "That looks like a personal email. The certificate is tied to an "
                "institutional email — please use your university/organisation "
                "address.")
        elif not is_institutional_email(email):
            errors.append("Please enter a valid institutional email address.")

        plan = None
        if plan_raw:
            try:
                plan = json.loads(plan_raw)
            except (ValueError, TypeError):
                errors.append("The plan didn't look like valid JSON — you can "
                              "leave it blank if you don't have it to hand.")

        if not modules and not plan:
            errors.append("Select at least one module you completed, or paste your "
                          "validation plan, so there's a certificate to issue.")

        if errors:
            return render(request, "credentials/claim.html", {
                "errors": errors,
                "module_choices": _module_choices(modules),
                "form": {"name": name, "email": email, "gene": gene, "rrid": rrid,
                         "plan": plan_raw}})

        pack_version = plan.get("pack_version", "") if isinstance(plan, dict) else ""
        claim_obj = PendingClaim.objects.create(
            name=name, institutional_email=email, modules=modules,
            target_gene=gene, rrid=rrid, plan=plan, pack_version=pack_version)

        verify_url = request.build_absolute_uri(
            reverse("credentials:claim_verify", args=[claim_obj.token]))
        _send_claim_email(claim_obj, verify_url)

        show_link = not bool(getattr(settings, "EMAIL_HOST_PASSWORD", ""))
        return render(request, "credentials/claim_sent.html", {
            "claim": claim_obj, "verify_url": verify_url if show_link else None})

    # GET — the connector deep-links with ?modules=framework,controls&gene=SNCA
    pre_modules = [m.strip() for m in (request.GET.get("modules") or "").split(",")
                   if m.strip() in MODULE_TITLES]
    return render(request, "credentials/claim.html", {
        "errors": [],
        "module_choices": _module_choices(pre_modules),
        "form": {"gene": request.GET.get("gene", ""),
                 "rrid": request.GET.get("rrid", "")}})


def _send_claim_email(claim_obj, verify_url):
    """Email the verification link. Never raises — a mail outage must not lose the
    claim (the owner can also re-send / issue manually). A recognisable From name
    and a fuller body help the message clear spam filters."""
    addr = getattr(settings, "EMAIL_HOST_USER", None)
    from_email = f"Only Good Antibodies <{addr}>" if addr else None
    try:
        send_mail(
            "Confirm your Only Good Antibodies certificate",
            (f"Hi {claim_obj.name},\n\n"
             "Thank you for working through the Only Good Antibodies antibody-"
             "validation training. You're one step away from your certificate.\n\n"
             "Please confirm this is your institutional email address by opening "
             "the link below — this lets us issue and record your certificate:\n\n"
             f"{verify_url}\n\n"
             "If you didn't request this, you can safely ignore this message and "
             "no certificate will be issued.\n\n"
             "Best wishes,\n"
             "The Only Good Antibodies team\n"
             "https://onlygoodantibodies.co.uk"),
            from_email,
            [claim_obj.institutional_email],
            fail_silently=True,
        )
    except Exception:
        pass


def claim_verify(request, token):
    """Follow the emailed link → issue (or re-show) every earned certificate."""
    claim_obj = PendingClaim.objects.filter(token=token).first()
    if not claim_obj:
        return render(request, "credentials/claim_result.html", {"claim": None})
    certs = claim_obj.issue_all()
    return render(request, "credentials/claim_result.html",
                  {"claim": claim_obj, "certs": certs})


def cert_pdf(request, code):
    """Download a certificate's PDF by its verification code (public — the code is
    already the public handle printed on the certificate)."""
    cert = get_object_or_404(Certificate, verification_code=code)
    return _render_certificate_pdf(cert, request)


def verify(request, code):
    """Public certificate-verification route. Shows only non-sensitive fields —
    never the institutional email or the plan."""
    cert = Certificate.objects.filter(verification_code=code).first()
    return render(request, "credentials/verify.html", {"cert": cert, "code": code})
