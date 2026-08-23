# academy/models.py

import uuid

from django.db import models
from django.contrib.auth.models import User
from django.urls import reverse
from ckeditor_uploader.fields import RichTextUploadingField

class Lesson(models.Model):
    title   = models.CharField(max_length=200)
    slug    = models.SlugField(blank=True, unique=True)
    order   = models.PositiveIntegerField(default=0)
    content = RichTextUploadingField()
    has_quiz = models.BooleanField(default=True)   # ✅ new field

    def __str__(self):
        return f"{self.order}. {self.title}"

class LessonSection(models.Model):
    lesson = models.ForeignKey(
        Lesson,
        related_name="sections",
        on_delete=models.CASCADE
    )
    title = models.CharField(max_length=200, blank=True)
    body  = RichTextUploadingField(blank=True)
    image = models.ImageField(
        upload_to="lesson_images/",
        blank=True,
        null=True
    )
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order"]

    def __str__(self):
        return f"{self.lesson.title} – Section {self.order}"

class Quiz(models.Model):
    lesson    = models.OneToOneField(Lesson, on_delete=models.CASCADE)
    pass_mark = models.IntegerField(default=80)

    def __str__(self):
        return f"Quiz for {self.lesson.title}"

class Question(models.Model):
    quiz = models.ForeignKey(Quiz, on_delete=models.CASCADE)
    text = models.TextField()

    def __str__(self):
        return self.text[:50]

class Answer(models.Model):
    question   = models.ForeignKey(Question, on_delete=models.CASCADE)
    text       = models.CharField(max_length=200)
    is_correct = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.text} ({'✓' if self.is_correct else '✗'})"

class Certificate(models.Model):
    user      = models.ForeignKey(User, on_delete=models.CASCADE)
    lesson    = models.ForeignKey(Lesson, on_delete=models.CASCADE)
    issued_at = models.DateTimeField(auto_now_add=True)
    score     = models.FloatField()
    # Public, unguessable handle printed on the PDF (with a QR) so a certificate
    # can be verified at /academy/certificate/verify/<code>/. Added 2026-07;
    # existing rows are back-filled by the data migration, so this is additive
    # and never rewrites the score/user/lesson of an existing certificate.
    verification_code = models.UUIDField(default=uuid.uuid4, editable=False,
                                         unique=True, null=True)

    def get_verify_url(self):
        return reverse("academy:certificate_verify", args=[self.verification_code])

    def __str__(self):
        return f"Cert for {self.user.username} – {self.lesson.title} ({self.score}%)"

class LessonProgress(models.Model):
    user         = models.ForeignKey(User, on_delete=models.CASCADE)
    lesson       = models.ForeignKey(Lesson, on_delete=models.CASCADE)
    completed    = models.BooleanField(default=False)
    completed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "lesson")

    def __str__(self):
        status = "Done" if self.completed else "In progress"
        return f"{self.user.username} – {self.lesson.title}: {status}"

from django.db import models
from django.contrib.auth import get_user_model

User = get_user_model()

class SectionProgress(models.Model):
    user      = models.ForeignKey(User, on_delete=models.CASCADE)
    section   = models.ForeignKey(LessonSection, on_delete=models.CASCADE)
    viewed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "section")

    def __str__(self):
        return f"{self.user.username} viewed {self.section}"


class AIChatUsage(models.Model):
    """Per-user, per-day message counter for the in-app AI assistant — the cost
    guardrail. One row per (user, date); ``message_count`` is checked against
    ``settings.ACADEMY_AI_DAILY_MESSAGE_CAP`` before each assistant turn. Purely
    additive; it never touches certificates, users, or progress."""
    user          = models.ForeignKey(User, on_delete=models.CASCADE)
    date          = models.DateField()
    message_count = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ("user", "date")

    def __str__(self):
        return f"{self.user.username} — {self.date}: {self.message_count} msgs"
