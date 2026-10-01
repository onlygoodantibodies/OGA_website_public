"""The Academy's modules, edited by the people who run the pipeline.

The modules could only be changed in Django admin, which only staff can open,
so the scientists who know what the database and the tools do now could not
correct a module that had fallen behind them. This is their editor: any
pipeline member can open it, the same gate as every other pipeline page.

Everything writes through ``academy/editing.py`` — read its docstring for the
rules. The two a reader of this page needs:

* **Certificates are out of reach.** Nothing here deletes a module, and a
  module's certificates point at the module, not at its sections or quiz, so
  every edit leaves them where they were. The page says how many there are.
* **Every save is a version and can be put back** (History → Restore), which is
  why a save goes live at once rather than waiting for a second person: a
  mistake costs one press to undo, and a module is only improved by people
  reading it.

The models live in ``academy_db``, like every other academy table; the pages
live under ``/pipeline/`` for the chrome, the sign-in and the member check.
"""
from __future__ import annotations

from django import forms
from django.contrib import messages
from django.db.models import Count
from django.forms import formset_factory
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from ckeditor.widgets import CKEditorWidget

from academy import editing
from academy.models import (Certificate, Lesson, LessonProgress, LessonRevision,
                            LessonSection, SectionProgress)
from pipeline.decorators import pipeline_member_required

DB = editing.DB

__all__ = ['academy_modules', 'academy_module_edit', 'academy_module_history',
           'academy_module_version']


def _who(request):
    return request.user.username


class ModuleForm(forms.Form):
    title = forms.CharField(max_length=200)
    order = forms.IntegerField(min_value=0, help_text='Modules are listed in this order.')
    is_published = forms.BooleanField(
        required=False, label='Published — learners can see this module')
    has_quiz = forms.BooleanField(
        required=False, label='Has a quiz — passing it issues a certificate')
    pass_mark = forms.IntegerField(min_value=1, max_value=100, initial=80,
                                   label='Pass mark (%)')
    quiz = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 18}))
    note = forms.CharField(
        required=False, max_length=300, label='What did you change?',
        help_text='Optional. Shown in the history beside this version.')
    based_on = forms.IntegerField(required=False, widget=forms.HiddenInput)

    def clean(self):
        data = super().clean()
        questions, errors = editing.parse_quiz(data.get('quiz', ''))
        for error in errors:
            self.add_error('quiz', error)
        if data.get('has_quiz') and not questions and not errors:
            self.add_error('quiz', 'The module is ticked as having a quiz, and the quiz '
                           'has no questions. Add some, or untick "Has a quiz".')
        data['questions'] = questions
        return data


class SectionForm(forms.Form):
    id = forms.IntegerField(required=False, widget=forms.HiddenInput)
    title = forms.CharField(max_length=200, required=False)
    body = forms.CharField(required=False, widget=CKEditorWidget())
    image = forms.ImageField(required=False, label='Picture shown under the text')
    remove_image = forms.BooleanField(required=False, label='Remove the picture')
    order = forms.IntegerField(min_value=0)

    def clean_body(self):
        body = self.cleaned_data.get('body', '')
        why = editing.unsafe_html(body)
        if why:
            raise forms.ValidationError(why)
        return body


SectionFormSet = formset_factory(SectionForm, extra=1, can_delete=True)


def _latest_revision(lesson):
    return (LessonRevision.objects.using(DB).filter(lesson=lesson)
            .order_by('-saved_at', '-pk').first())


@pipeline_member_required
def academy_modules(request):
    """Every module, with what hangs off it — the certificates above all."""
    if request.method == 'POST':
        title = (request.POST.get('title') or '').strip()
        if not title:
            messages.error(request, 'A new module needs a title — type one in the '
                           'box beside "Add a module".')
            return redirect('pipeline:academy_modules')
        lesson = editing.create(title[:200], by=_who(request))
        messages.success(request, f'Added "{lesson.title}". It is not published, so '
                         'learners cannot see it until you tick Published and save.')
        return redirect('pipeline:academy_module_edit', lesson.pk)

    lessons = list(
        Lesson.objects.using(DB)
        .annotate(section_total=Count('sections', distinct=True))
        .order_by('order', 'pk'))
    ids = [lesson.pk for lesson in lessons]
    certs = dict(Certificate.objects.using(DB).filter(lesson_id__in=ids)
                 .values_list('lesson').annotate(n=Count('pk')))
    holders = dict(Certificate.objects.using(DB).filter(lesson_id__in=ids)
                   .values_list('lesson').annotate(n=Count('user', distinct=True)))
    last = {}
    for rev in (LessonRevision.objects.using(DB).filter(lesson_id__in=ids)
                .order_by('lesson_id', '-saved_at', '-pk')):
        last.setdefault(rev.lesson_id, rev)
    rows = [{'lesson': lesson, 'certificates': certs.get(lesson.pk, 0),
             'holders': holders.get(lesson.pk, 0), 'last': last.get(lesson.pk)}
            for lesson in lessons]
    return render(request, 'pipeline/academy_modules.html', {
        'rows': rows,
        'certificate_total': sum(certs.values()),
    })


def _initial(lesson):
    state = editing.snapshot(lesson)
    quiz = state['quiz'] or {'pass_mark': 80, 'questions': []}
    latest = _latest_revision(lesson)
    module = {'title': state['title'], 'order': state['order'],
              'is_published': state['is_published'], 'has_quiz': state['has_quiz'],
              'pass_mark': quiz['pass_mark'],
              'quiz': editing.quiz_text(quiz['questions']),
              'based_on': latest.pk if latest else 0}
    sections = [{'id': s['id'], 'title': s['title'], 'body': s['body'],
                 'order': s['order']} for s in state['sections']]
    return module, sections


def _page(request, lesson, form, formset, conflict=None):
    existing = {s.pk: s for s in lesson.sections.using(DB).all()}
    viewed = dict(SectionProgress.objects.using(DB)
                  .filter(section__lesson=lesson).values_list('section')
                  .annotate(n=Count('pk')))
    for f in formset.forms:
        pk = f.initial.get('id') or (f.data.get(f.add_prefix('id')) if f.is_bound else None)
        try:
            pk = int(pk) if pk else None
        except (TypeError, ValueError):
            pk = None
        section = existing.get(pk)
        f.existing = section
        f.viewed = viewed.get(pk, 0)
    return render(request, 'pipeline/academy_module_edit.html', {
        'lesson': lesson,
        'form': form,
        'formset': formset,
        'conflict': conflict,
        'certificates': Certificate.objects.using(DB).filter(lesson=lesson).count(),
        'holders': Certificate.objects.using(DB).filter(lesson=lesson)
        .values('user').distinct().count(),
        'passed': LessonProgress.objects.using(DB)
        .filter(lesson=lesson, completed=True).count(),
        'latest': _latest_revision(lesson),
        'learner_url': reverse('academy:lesson_detail', args=[lesson.pk]),
    })


@pipeline_member_required
def academy_module_edit(request, pk):
    lesson = get_object_or_404(Lesson.objects.using(DB), pk=pk)
    if request.method != 'POST':
        module, sections = _initial(lesson)
        return _page(request, lesson, ModuleForm(initial=module),
                     SectionFormSet(initial=sections, prefix='sections'))

    form = ModuleForm(request.POST)
    formset = SectionFormSet(request.POST, request.FILES, prefix='sections')
    if not (form.is_valid() and formset.is_valid()):
        messages.error(request, 'Nothing was saved — the boxes marked in red below '
                       'say what needs changing.')
        return _page(request, lesson, form, formset)

    # Somebody else saved while this page was open. Their version stays; this
    # one is handed back unsaved, with the stamp moved on so a second press is
    # a deliberate overwrite rather than the same refusal again.
    latest = _latest_revision(lesson)
    latest_pk = latest.pk if latest else 0
    if (form.cleaned_data.get('based_on') or 0) != latest_pk:
        data = request.POST.copy()
        data['based_on'] = latest_pk
        return _page(request, lesson, ModuleForm(data),
                     SectionFormSet(data, request.FILES, prefix='sections'),
                     conflict=latest)

    storage = LessonSection._meta.get_field('image').storage
    existing = {s.pk: s for s in lesson.sections.using(DB).all()}
    sections = []
    for f in formset.forms:
        c = f.cleaned_data
        if not c or c.get('DELETE'):
            continue
        section = existing.get(c.get('id'))
        if section is None and not (c.get('title') or c.get('body') or c.get('image')):
            continue  # the empty "add a section" form nobody typed in
        image = section.image.name if section and section.image else ''
        if c.get('remove_image'):
            image = ''
        if c.get('image'):
            image = storage.save(f'lesson_images/{c["image"].name}', c['image'])
        sections.append({'id': section.pk if section else None,
                         'title': c.get('title', ''), 'body': c.get('body', ''),
                         'image': image, 'order': c['order']})
    sections.sort(key=lambda s: s['order'])

    d = form.cleaned_data
    state = {'title': d['title'], 'order': d['order'],
             'is_published': d['is_published'], 'has_quiz': d['has_quiz'],
             'sections': sections,
             'quiz': ({'pass_mark': d['pass_mark'], 'questions': d['questions']}
                      if d['questions'] or lesson.has_quiz or d['has_quiz'] else None)}
    receipt = editing.apply(lesson, state, by=_who(request), note=d.get('note', ''))
    messages.success(request, _receipt_sentence(receipt))
    return redirect('pipeline:academy_module_edit', lesson.pk)


def _plural(n, word):
    return f'{n} {word}{"" if n == 1 else "s"}'


def _receipt_sentence(r):
    parts = []
    if r['sections_added']:
        parts.append(f'{_plural(r["sections_added"], "section")} added')
    if r['sections_changed']:
        parts.append(f'{_plural(r["sections_changed"], "section")} changed')
    if r['sections_removed']:
        removed = f'{_plural(r["sections_removed"], "section")} removed'
        if r['viewed_marks_removed']:
            removed += (f' (with {_plural(r["viewed_marks_removed"], "learner")}\' '
                        '"viewed" mark on it)')
        parts.append(removed)
    if r['quiz_changed']:
        parts.append('quiz rewritten')
    what = ', '.join(parts) if parts else 'no section or quiz changes'
    return (f'Saved — {what}. {_plural(r["certificates"], "certificate")} for this '
            'module untouched. The previous version is in History if you need it back.')


@pipeline_member_required
def academy_module_history(request, pk):
    lesson = get_object_or_404(Lesson.objects.using(DB), pk=pk)
    revisions = list(LessonRevision.objects.using(DB).filter(lesson=lesson)
                     .order_by('-saved_at', '-pk'))
    return render(request, 'pipeline/academy_module_history.html', {
        'lesson': lesson, 'revisions': revisions,
        'current': revisions[0] if revisions else None,
    })


@pipeline_member_required
def academy_module_version(request, pk, rev):
    """One saved version, read-only — and Restore, which saves it again."""
    lesson = get_object_or_404(Lesson.objects.using(DB), pk=pk)
    revision = get_object_or_404(LessonRevision.objects.using(DB), pk=rev, lesson=lesson)
    if request.method == 'POST':
        receipt = editing.apply(
            lesson, revision.snapshot, by=_who(request),
            note=(f'Restored the version of {revision.saved_at:%d %b %Y %H:%M}'
                  + (f' by {revision.saved_by}' if revision.saved_by else '')))
        messages.success(request, 'Restored. ' + _receipt_sentence(receipt))
        return redirect('pipeline:academy_module_edit', lesson.pk)

    snap = revision.snapshot
    quiz = snap.get('quiz') or {'questions': []}
    storage = LessonSection._meta.get_field('image').storage
    sections = [dict(s, image_url=storage.url(s['image']) if s.get('image') else '')
                for s in sorted(snap.get('sections', []), key=lambda s: s.get('order', 0))]
    latest = _latest_revision(lesson)
    return render(request, 'pipeline/academy_module_version.html', {
        'lesson': lesson, 'revision': revision, 'snap': snap, 'sections': sections,
        'quiz_text': editing.quiz_text(quiz['questions']),
        'is_current': latest is not None and latest.pk == revision.pk,
        'impact': editing.impact(lesson, snap),
    })
