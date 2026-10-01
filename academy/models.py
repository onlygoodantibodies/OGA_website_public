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
    # A module somebody is still writing is kept off the learners' list until it
    # is ready. ``db_default`` as well as ``default``, so code rolled back past
    # this migration can still insert a lesson (see CLAUDE.md, rollbacks).
    is_published = models.BooleanField(default=True, db_default=True)

    def __str__(self):
        return f"{self.order}. {self.title}"

    def save(self, *args, **kwargs):
        """A certificate says the module it was earned in, not today's title.

        The PDF, the certificate page and the public verify page all printed
        ``cert.lesson.title`` live, so renaming a module rewrote every
        certificate already issued for it. Before a title changes, each of this
        module's certificates that has not got a title of its own is stamped
        with the one it was earned under. Here rather than in the editor, so
        Django admin's save does it too.
        """
        db = kwargs.get('using') or self._state.db or 'academy_db'
        if self.pk:
            old = (Lesson.objects.using(db)
                   .filter(pk=self.pk).values_list('title', flat=True).first())
            if old is not None and old != self.title:
                (Certificate.objects.using(db)
                 .filter(lesson_id=self.pk, lesson_title='')
                 .update(lesson_title=old))
        super().save(*args, **kwargs)

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
    # PROTECT: deleting a module must never take the certificates earned in it
    # with it. Django refuses the delete by name instead — in admin too.
    lesson    = models.ForeignKey(Lesson, on_delete=models.PROTECT)
    # The module's title when this certificate was earned; blank means "the
    # title has not changed since", so ``module_title`` falls back to the live
    # one. Filled at issue, and by ``Lesson.save`` before a rename.
    lesson_title = models.CharField(max_length=200, blank=True, default='',
                                    db_default='')
    issued_at = models.DateTimeField(auto_now_add=True)
    score     = models.FloatField()
    # Public, unguessable handle printed on the PDF (with a QR) so a certificate
    # can be verified at /academy/certificate/verify/<code>/. Added 2026-07;
    # existing rows are back-filled by the data migration, so this is additive
    # and never rewrites the score/user/lesson of an existing certificate.
    verification_code = models.UUIDField(default=uuid.uuid4, editable=False,
                                         unique=True, null=True)

    @property
    def module_title(self):
        """The title to print on this certificate — the one it was earned under."""
        return self.lesson_title or self.lesson.title

    def get_verify_url(self):
        return reverse("academy:certificate_verify", args=[self.verification_code])

    def __str__(self):
        return f"Cert for {self.user.username} – {self.module_title} ({self.score}%)"

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


class LessonRevision(models.Model):
    """One saved version of a module — its title, sections and quiz.

    Written by ``academy/editing.py`` every time a module is saved from the
    editor, so any earlier version can be looked at and put back. ``snapshot``
    is the whole module as ``editing.snapshot`` returns it. The person is kept
    as a username rather than a foreign key: editors sign in to the pipeline,
    and a version should outlive the account that wrote it.
    """
    lesson   = models.ForeignKey(Lesson, related_name='revisions',
                                 on_delete=models.CASCADE)
    saved_at = models.DateTimeField(auto_now_add=True)
    saved_by = models.CharField(max_length=150, blank=True)
    note     = models.CharField(max_length=300, blank=True)
    snapshot = models.JSONField()

    class Meta:
        ordering = ['-saved_at', '-pk']

    def __str__(self):
        return f"{self.lesson} — {self.saved_at:%d %b %Y %H:%M} by {self.saved_by or 'unknown'}"
