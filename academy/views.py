from django.shortcuts                     import render, get_object_or_404, redirect
from django.http                          import HttpResponse
from django.contrib.auth.decorators      import login_required
from django.contrib.auth.forms           import UserCreationForm
from django.contrib.auth                 import authenticate, login, logout as auth_logout
from django.urls                         import reverse_lazy
from allauth.account.views                import LoginView as AllAuthLoginView, SignupView as AllauthSignupView, PasswordResetView as AllauthPasswordResetView
import urllib.request
import urllib.parse
import json
from django.contrib.auth.views            import LogoutView
from django.contrib.auth.mixins           import LoginRequiredMixin
from django.db.models                     import Count
from django.views.generic                 import TemplateView, UpdateView
from django.contrib.auth                 import get_user_model

from reportlab.pdfgen                     import canvas

from .models import (
    Lesson, LessonProgress, LessonSection, SectionProgress,
    Quiz, Question, Answer, Certificate
)

from .forms                               import ProfileUpdateForm

User = get_user_model()
# views.py
import os
from django.conf import settings
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from django.shortcuts import get_object_or_404
from django.http import HttpResponse
from django.contrib.auth.decorators import login_required
from .models import Certificate

@login_required
def home(request):
    """The Academy's front page: the modules, and where this learner is in them.

    It used to open with three cards of equal size — the modules, the Antibody
    Selection Tool and bring-your-own-AI — so the page answered "what am I here
    for?" three different ways at once. Worse, the modules' card was titled
    *Existing e-learning* beside two badged *Prototype*, which reads as the
    leftover option kept around next to the new things. It is not a legacy
    option: it is the Academy, and it is what somebody signs in to do. So the
    modules lead, with this learner's own progress on them and one thing to
    press, and the other two tools keep their place further down the page —
    unchanged, still current, just not competing for the top of it.

    ``ai_configured`` / ``tutor_offline`` / ``offline_message`` used to be sent
    to this template and read by none of it, left over from the retired AI
    tutor. ``assistant_page`` computes its own; nothing else wanted these.
    """
    lessons = list(
        Lesson.objects
        .annotate(section_total=Count('sections', distinct=True))
        .order_by('order')
    )
    lesson_ids = [lesson.id for lesson in lessons]

    # Three queries for the whole list rather than two per module: the loop this
    # replaces asked the database twice for every module it drew.
    viewed = dict(
        SectionProgress.objects
        .filter(user=request.user, section__lesson_id__in=lesson_ids)
        .values_list('section__lesson')
        .annotate(seen=Count('section', distinct=True))
    )
    passed = set(
        LessonProgress.objects
        .filter(user=request.user, completed=True, lesson_id__in=lesson_ids)
        .values_list('lesson_id', flat=True)
    )
    # A passed module has a certificate behind it, and until now the only way to
    # one was the page shown immediately after the quiz — leave that page and the
    # PDF was unreachable from anywhere on the site. Ordered so a retake's
    # certificate is the one the module offers.
    certificate_of = {
        cert.lesson_id: cert
        for cert in Certificate.objects
        .filter(user=request.user, lesson_id__in=lesson_ids)
        .order_by('issued_at')
    }

    progress_data = []
    for lesson in lessons:
        seen = viewed.get(lesson.id, 0)
        completed = lesson.id in passed
        if completed:
            percent = 100
        elif lesson.section_total:
            percent = round(seen / lesson.section_total * 100)
        else:
            percent = 0
        progress_data.append({
            'lesson': lesson,
            'progress': percent,
            'completed': completed,
            'started': completed or seen > 0,
            'certificate': certificate_of.get(lesson.id),
        })

    return render(request, 'academy/home.html', {
        'progress_data': progress_data,
        'module_total': len(progress_data),
        'completed_total': sum(1 for item in progress_data if item['completed']),
        # The one thing to press: the first module not yet passed. None once
        # every module is done, which the page says rather than inventing a step.
        'next_up': next((item for item in progress_data if not item['completed']), None),
    })
from django.shortcuts               import render, get_object_or_404
from django.contrib.auth.decorators import login_required
from .models                        import Lesson, LessonProgress, LessonSection, SectionProgress

@login_required
def lesson_detail(request, lesson_id):
    # 1) Fetch lesson & its sections
    lesson   = get_object_or_404(Lesson, pk=lesson_id)
    sections = lesson.sections.all()

    # 2) Ensure we have a LessonProgress record (visited but not necessarily completed)
    LessonProgress.objects.get_or_create(
        user=request.user,
        lesson=lesson,
        defaults={'completed': False},
    )

    # 3) Count how many sections the user has viewed
    total_sections  = sections.count()
    viewed_sections = SectionProgress.objects.filter(
        user=request.user,
        section__lesson=lesson
    ).count()

    # 4) Compute a percentage (round to nearest whole number)
    progress = round((viewed_sections / total_sections) * 100) if total_sections else 0

    return render(request, 'academy/lesson_detail.html', {
        'lesson':   lesson,
        'sections': sections,
        'progress': progress,
    })

@login_required
def quiz_view(request, quiz_id):
    quiz      = get_object_or_404(Quiz, id=quiz_id)
    lesson    = quiz.lesson
    questions = quiz.question_set.all()

    if request.method == 'POST':
        # 1) Grade and collect per-question results
        score, results = calculate_score_with_results(request.POST, quiz)
        passed = score >= quiz.pass_mark

        # 2) Mark lesson progress
        LessonProgress.objects.update_or_create(
            user=request.user,
            lesson=lesson,
            defaults={'completed': passed},
        )

        # 3) Create certificate if passed
        cert = None
        if passed:
            cert = Certificate.objects.create(
                user=request.user,
                lesson=lesson,
                score=round(score, 1),
            )

        # 4) Render unified results page (pass or fail)
        return render(request, 'academy/quiz_result.html', {
            'quiz':      quiz,
            'score':     round(score, 1),
            'passed':    passed,
            'pass_mark': quiz.pass_mark,
            'results':   results,
            'cert':      cert,
        })

    # GET → show the quiz form
    return render(request, 'academy/quiz.html', {
        'quiz':      quiz,
        'questions': questions,
    })


def calculate_score_with_results(post_data, quiz):
    """
    Returns (score_float, results_list).
    results_list contains one dict per question:
        {
            'question':       Question instance,
            'selected':       Answer instance the user chose (or None),
            'correct_answer': Answer instance that is correct,
            'is_correct':     bool,
        }
    """
    correct_count = 0
    results       = []
    questions     = quiz.question_set.all()

    for question in questions:
        selected_id    = post_data.get(f'question_{question.id}')
        selected       = None
        correct_answer = question.answer_set.filter(is_correct=True).first()
        is_correct     = False

        if selected_id:
            try:
                selected   = Answer.objects.get(id=selected_id)
                is_correct = selected.is_correct
                if is_correct:
                    correct_count += 1
            except Answer.DoesNotExist:
                pass

        results.append({
            'question':       question,
            'selected':       selected,
            'correct_answer': correct_answer,
            'is_correct':     is_correct,
        })

    total = questions.count()
    score = (correct_count / total * 100) if total > 0 else 0
    return score, results


@login_required
def certificate_view(request, cert_id):
    cert = get_object_or_404(Certificate, id=cert_id, user=request.user)
    return render(request, 'academy/certificate.html', {'cert': cert})

@login_required
def generate_pdf(request, cert_id):
    cert = get_object_or_404(Certificate, id=cert_id, user=request.user)

    # Prepare HTTP response
    response = HttpResponse(content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="certificate_{cert.user.username}.pdf"'

    # Create canvas
    c = canvas.Canvas(response, pagesize=letter)
    width, height = letter

    # Draw border
    c.setLineWidth(4)
    margin = 0.5 * inch
    c.rect(margin, margin, width - 2*margin, height - 2*margin)

    # Draw logo at top center (optional)
    logo_path = os.path.join(settings.STATIC_ROOT, 'core', 'logo.png')
    if os.path.exists(logo_path):
        logo_width = 1.5 * inch
        c.drawImage(
            logo_path,
            (width - logo_width) / 2,
            height - margin - logo_width,
            logo_width,
            logo_width,
            preserveAspectRatio=True
        )

    # Title
    c.setFont("Helvetica-Bold", 24)
    c.drawCentredString(width/2, height - margin - (1.6*inch), "Certificate of Completion")

    # Recipient name
    full_name = cert.user.get_full_name() or cert.user.username
    c.setFont("Helvetica-Bold", 20)
    c.drawCentredString(width/2, height - margin - (2.4*inch), full_name)

    # Static text
    c.setFont("Helvetica", 12)
    c.drawCentredString(width/2, height - margin - (3.2*inch), "has successfully completed the module")
    c.setFont("Helvetica-BoldOblique", 16)
    c.drawCentredString(width/2, height - margin - (3.8*inch), cert.lesson.title)

    # Score and date
    c.setFont("Helvetica", 12)
    c.drawCentredString(width/2, height - margin - (4.6*inch), f"Score: {cert.score}%")
    date_str = cert.issued_at.strftime('%B %d, %Y')
    c.drawCentredString(width/2, height - margin - (5.2*inch), f"Issued on {date_str}")

    # Verification code + QR (added 2026-07). Wrapped so a QR failure never breaks
    # the PDF, and skipped gracefully if a legacy cert has no code yet.
    if cert.verification_code:
        verify_url = request.build_absolute_uri(cert.get_verify_url())
        c.setFont("Helvetica", 9)
        c.drawCentredString(width/2, margin + 0.95*inch,
                            f"Verification code: {cert.verification_code}")
        c.setFont("Helvetica-Oblique", 8)
        c.drawCentredString(width/2, margin + 0.75*inch, "Verify this certificate at:")
        c.drawCentredString(width/2, margin + 0.60*inch, verify_url)
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
            renderPDF.draw(d, c, (width - qr_size) / 2, margin + 1.15*inch)
        except Exception:
            pass  # QR is a nice-to-have; never let it break PDF generation.

    c.showPage()
    c.save()

    return response


# ---------------------------------------------------------------------------
# Public certificate verification (no login — a funder/institution checks it)
# ---------------------------------------------------------------------------

def certificate_verify(request, code):
    """Public verify page. Shows only non-sensitive fields — never the learner's
    email — so an institution or funder can confirm a certificate is genuine."""
    cert = (Certificate.objects
            .select_related("user", "lesson")
            .filter(verification_code=code)
            .first())
    return render(request, "academy/certificate_verify.html",
                  {"cert": cert, "code": code})


# ---------------------------------------------------------------------------
# In-app AI assistant (behind the academy login) — the hosted chat surfaces
# ---------------------------------------------------------------------------
from django.utils import timezone
from django.http import Http404, JsonResponse
from django.views.decorators.http import require_POST
from . import assistant
from .models import AIChatUsage

_ASSISTANT_META = {
    "elearning": {
        "title": "OGA AI e-learning tutor",
        "blurb": "A conversational tutor that walks you through OGA's antibody-"
                 "validation training and, when you're ready, points you to claim "
                 "your certificate.",
    },
    "database": {
        "title": "OGA AI database search",
        "blurb": "Ask factual questions about OGA's independently tested, "
                 "knockout-controlled antibody data.",
    },
}


def _daily_cap():
    return int(getattr(settings, "ACADEMY_AI_DAILY_MESSAGE_CAP", 40))


def _check_and_increment_quota(user):
    """Atomically bump today's message count. Returns (allowed, remaining)."""
    from django.db.models import F
    cap = _daily_cap()
    today = timezone.now().date()
    usage, _ = AIChatUsage.objects.get_or_create(user=user, date=today)
    if usage.message_count >= cap:
        return False, 0
    AIChatUsage.objects.filter(pk=usage.pk).update(message_count=F("message_count") + 1)
    usage.refresh_from_db()
    return True, max(cap - usage.message_count, 0)


@login_required
def assistant_page(request, mode):
    if mode not in assistant.MODES:
        raise Http404("Unknown assistant mode")
    meta = _ASSISTANT_META[mode]
    tutor_offline = mode == "elearning" and assistant.tutor_offline()
    return render(request, "academy/assistant_chat.html", {
        "mode": mode,
        "title": meta["title"],
        "blurb": meta["blurb"],
        "configured": assistant.is_configured(),
        "daily_cap": _daily_cap(),
        "tutor_offline": tutor_offline,
        "offline_message": assistant.offline_message(),
    })


@login_required
@require_POST
def assistant_api(request):
    try:
        payload = json.loads(request.body or b"{}")
    except (ValueError, TypeError):
        return JsonResponse({"ok": False, "error": "bad_json",
                             "reply": "Sorry — I couldn't read that."}, status=400)

    mode = payload.get("mode")
    messages = payload.get("messages") or []
    if mode not in assistant.MODES:
        return JsonResponse({"ok": False, "error": "bad_mode",
                             "reply": "Unknown assistant mode."}, status=400)

    if mode == "elearning" and assistant.tutor_offline():
        return JsonResponse({"ok": False, "error": "tutor_offline",
                             "reply": assistant.offline_message()}, status=200)

    if not assistant.is_configured():
        return JsonResponse({"ok": False, "error": "not_configured",
                             "reply": "The AI assistant isn't switched on for this "
                                      "site yet. Please check back soon."}, status=200)

    allowed, remaining = _check_and_increment_quota(request.user)
    if not allowed:
        return JsonResponse({
            "ok": False, "error": "limit", "remaining": 0,
            "reply": ("You've reached today's message limit for the in-app "
                      "assistant (a cost safeguard). You can keep going right now by "
                      "connecting your own AI — Claude, ChatGPT, or similar — to the "
                      "OGA database; see the “connect your AI to our database” option "
                      "on the academy page. Your in-app limit resets tomorrow."),
        }, status=200)

    result = assistant.run_turn(mode, messages)
    result["remaining"] = remaining
    return JsonResponse(result, status=200)


class CustomSignupView(AllauthSignupView):
    template_name = 'academy/signup.html'

    def form_valid(self, form):
        token = self.request.POST.get('g-recaptcha-response', '')
        if not self._verify_recaptcha(token):
            form.add_error(None, 'Security check failed. Please try again.')
            return self.form_invalid(form)
        return super().form_valid(form)

    def _verify_recaptcha(self, token):
        if not token:
            return False
        try:
            secret = getattr(settings, 'RECAPTCHA_SECRET_KEY', '')
            if not secret:
                return True  # Fail open if key not configured
            data = urllib.parse.urlencode({
                'secret': secret,
                'response': token,
                'remoteip': self.request.META.get('REMOTE_ADDR', ''),
            }).encode()
            req = urllib.request.Request(
                'https://www.google.com/recaptcha/api/siteverify',
                data=data,
                method='POST'
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                result = json.loads(response.read().decode())
            return result.get('success') is True and result.get('score', 0) >= 0.5
        except Exception:
            return True  # Fail open on network error


class CustomPasswordResetView(AllauthPasswordResetView):
    template_name = 'account/password_reset.html'

    def post(self, request, *args, **kwargs):
        token = request.POST.get('g-recaptcha-response', '')
        if not self._verify_recaptcha(token):
            form = self.get_form()
            form.add_error(None, 'Security check failed. Please try again.')
            return self.form_invalid(form)
        return super().post(request, *args, **kwargs)

    def _verify_recaptcha(self, token):
        if not token:
            return False
        try:
            secret = getattr(settings, 'RECAPTCHA_SECRET_KEY', '')
            if not secret:
                return True
            data = urllib.parse.urlencode({
                'secret': secret,
                'response': token,
            }).encode()
            req = urllib.request.Request(
                'https://www.google.com/recaptcha/api/siteverify',
                data=data,
                method='POST',
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                result = json.loads(resp.read().decode())
            return result.get('success', False) and result.get('score', 0) >= 0.5
        except Exception:
            return True  # Fail open on network error


@login_required
def account_view(request):
    return render(request, 'academy/account.html', {'user': request.user})


class AcademyLogoutView(LogoutView):
    """Signing out is a POST. A GET asks first.

    This used to log out in ``get()`` — it re-opened ``http_method_names`` to do
    it, Django's own ``LogoutView`` having been POST-only since 4.1 precisely to
    stop this. So anything that fetched the URL without a person deciding to
    ended the session: a link prefetcher, a chat client building an unfurl
    preview, a middle-click. The twelfth field test lost its run that way on the
    pipeline's copy of the same bug.

    The public site links here with a plain ``<a>`` from nine templates, so GET
    stays a valid request rather than becoming a 405 — it renders the same
    template in its ``confirm`` state, and the POST from that form is what
    actually signs out. One extra click on a deliberate action; none of the
    site's navigation had to be restyled to get it.
    """

    template_name   = "academy/logout.html"
    http_method_names = ['get', 'post', 'head', 'options']

    def get(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return render(request, self.template_name, {"confirm": False})
        return render(request, self.template_name, {"confirm": True})

    def post(self, request, *args, **kwargs):
        auth_logout(request)
        return render(request, self.template_name, {"confirm": False})


class ProfileUpdateView(LoginRequiredMixin, UpdateView):
    model         = User
    form_class    = ProfileUpdateForm
    template_name = "academy/edit_account.html"
    success_url   = reverse_lazy("academy:account")
    def get_object(self):
        return self.request.user


from django.contrib.auth.decorators import login_required
from django.contrib.auth import logout
from django.shortcuts import redirect

@login_required
def delete_account(request):
    if request.method == "POST":
        user = request.user

        # Log the user out BEFORE deleting
        logout(request)

        # Now safely delete the user
        user.delete()

        return redirect("academy:account_deleted_success")
    
    return redirect("academy:account")


def privacy_policy(request):
    return render(request, 'academy/privacy_policy.html')

from django.http import JsonResponse
from django.views.decorators.http import require_POST

@login_required
@require_POST
def mark_section_viewed(request):
    section_id = request.POST.get('section_id')
    section = get_object_or_404(LessonSection, id=section_id)
    SectionProgress.objects.get_or_create(
        user=request.user,
        section=section
    )
    # recalc progress
    lesson = section.lesson
    total = lesson.sections.count()
    viewed = SectionProgress.objects.filter(user=request.user, section__lesson=lesson).count()
    return JsonResponse({
        'viewed': viewed,
        'total': total,
        'progress': round((viewed/total)*100,1),
    })