"""Editing an Academy module — the one writer for a module's content.

Modules could only be changed in Django admin, which only staff can open, so
the people who know what the database and the tools now do — the pipeline's
members — could not correct a module that had fallen behind them. The editor at
``/pipeline/academy/`` is theirs (``pipeline/views/academy_modules.py``), and
``manage.py load_academy_module`` and *Restore* on the history page write
through the same ``apply``.

**A module's shape is one dictionary** — ``snapshot`` returns it and ``apply``
takes it — so a save, a restore and a load from a file are the same act:

    {"title", "order", "is_published", "has_quiz",
     "sections": [{"id", "title", "body", "image", "order"}, ...],
     "quiz": {"pass_mark", "questions": [{"text", "answers": [{"text", "correct"}]}]}}

Four things it holds:

* **Nothing here touches a certificate.** A certificate points at the module,
  not at its sections or questions, so rewriting both leaves every one where it
  was; ``Certificate.lesson`` is ``PROTECT`` so a module that has any cannot be
  deleted by any door; and ``Lesson.save`` stamps each one with the title it was
  earned under before a rename. ``apply`` never deletes a module.
* **Every save is a version.** The state after it is kept as a
  ``LessonRevision``, and before the first one the module as it stood is kept
  too — it was last written in admin, which kept no history — so the first save
  is as undoable as the tenth.
* **A section keeps its id while it exists**, so learners' "viewed" marks on it
  survive an edit. Deleting a section removes those marks (they are its rows);
  ``impact`` counts them for the page to say so before the press.
* **What is typed is checked, not cleaned.** A section body is HTML drawn to
  every learner with ``|safe``; one that carries a script, an event handler or
  an embed from anywhere but YouTube is refused by name rather than silently
  altered, so what is saved is what the editor saw.
"""
from __future__ import annotations

import re

from django.db import transaction
from django.utils.text import slugify

DB = 'academy_db'

#: One quiz answer is correct, and at least one other is not — the quiz page
#: grades by the first correct answer, so two would silently mark one of them
#: wrong.
MIN_ANSWERS = 2

_UNSAFE = [
    (re.compile(r'<\s*script', re.I), 'a <script> tag'),
    (re.compile(r'javascript\s*:', re.I), 'a javascript: link'),
    (re.compile(r'<[^>]*\son[a-z]+\s*=', re.I), 'an on… event attribute (onclick, onerror, …)'),
    (re.compile(r'<\s*(object|embed|form|input|button|style|link|meta|base)\b', re.I),
     'a <{0}> tag'),
]
_IFRAME = re.compile(r'<\s*iframe\b[^>]*>', re.I)
_IFRAME_OK = re.compile(
    r'src\s*=\s*["\']https://(www\.)?(youtube-nocookie\.com|youtube\.com)/embed/', re.I)


def unsafe_html(html: str) -> str | None:
    """Why this HTML may not be shown to learners, or ``None`` if it may."""
    for pattern, what in _UNSAFE:
        match = pattern.search(html or '')
        if match:
            tag = match.group(1).lower() if match.groups() else ''
            return (f'It contains {what.format(tag)}, which the Academy does not '
                    'show. Remove it (the Source button shows the HTML) and save again.')
    for frame in _IFRAME.findall(html or ''):
        if not _IFRAME_OK.search(frame):
            return ('It embeds a page from somewhere other than YouTube. Only '
                    'YouTube videos can be embedded — link to anything else instead.')
    return None


# ── The quiz as text ─────────────────────────────────────────────────────────
#
# A question and its answers are four or five short lines, and a formset of
# formsets to type them into is a page nobody finishes. So the quiz is edited
# as text a scientist can read and write:
#
#     1. What does the RRID field give you?
#     - The antibody's price
#     * A unique identifier to cite the antibody by
#     - The target's accession number
#
# A line starting with a number is a question, ``*`` marks the correct answer
# and ``-`` a wrong one. A blank line between questions is optional.

_QUESTION = re.compile(r'^\s*\d+\s*[.)]\s*(.+)$')
_ANSWER = re.compile(r'^\s*([*-])\s+(.+)$')


def quiz_text(questions) -> str:
    """The quiz in the text form ``parse_quiz`` reads back."""
    blocks = []
    for number, q in enumerate(questions, 1):
        lines = [f'{number}. {q["text"]}']
        lines += [f'{"*" if a["correct"] else "-"} {a["text"]}' for a in q['answers']]
        blocks.append('\n'.join(lines))
    return '\n\n'.join(blocks)


def parse_quiz(text: str):
    """``(questions, errors)`` — every problem at once, each naming its line."""
    questions, errors = [], []
    current = None
    for number, raw in enumerate((text or '').splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        q = _QUESTION.match(line)
        a = _ANSWER.match(line)
        if q:
            current = {'text': q.group(1).strip(), 'answers': [], 'line': number}
            questions.append(current)
        elif a and current is not None:
            answer = a.group(2).strip()
            if len(answer) > 200:
                errors.append(f'Line {number}: an answer can be at most 200 characters '
                              f'(this one is {len(answer)}).')
            current['answers'].append({'text': answer, 'correct': a.group(1) == '*'})
        elif a:
            errors.append(f'Line {number}: an answer before any question. Start the '
                          'question on its own line with a number, e.g. "1. What is…?"')
        else:
            errors.append(f'Line {number}: "{line[:60]}" is neither a question (start it '
                          'with a number, e.g. "3. …") nor an answer (start it with '
                          '"* " for the correct one or "- " for a wrong one).')
    for q in questions:
        correct = sum(1 for a in q['answers'] if a['correct'])
        where = f'Question on line {q["line"]} ("{q["text"][:50]}")'
        if len(q['answers']) < MIN_ANSWERS:
            errors.append(f'{where} needs at least {MIN_ANSWERS} answers.')
        elif correct != 1:
            errors.append(f'{where} has {correct} answers marked correct with "*"; '
                          'it needs exactly one.')
    for q in questions:
        q.pop('line', None)
    return questions, errors


# ── Snapshot and apply ───────────────────────────────────────────────────────

def _quiz_state(lesson):
    from academy.models import Quiz
    quiz = Quiz.objects.using(DB).filter(lesson=lesson).first()
    if quiz is None:
        return None
    questions = []
    for q in quiz.question_set.using(DB).order_by('pk'):
        questions.append({
            'text': q.text,
            'answers': [{'text': a.text, 'correct': a.is_correct}
                        for a in q.answer_set.using(DB).order_by('pk')],
        })
    return {'pass_mark': quiz.pass_mark, 'questions': questions}


def snapshot(lesson) -> dict:
    """The whole module as it stands, in the shape ``apply`` takes."""
    return {
        'title': lesson.title,
        'order': lesson.order,
        'is_published': lesson.is_published,
        'has_quiz': lesson.has_quiz,
        'sections': [
            {'id': s.pk, 'title': s.title, 'body': s.body,
             'image': s.image.name if s.image else '', 'order': s.order}
            for s in lesson.sections.using(DB).order_by('order', 'pk')
        ],
        'quiz': _quiz_state(lesson),
    }


def impact(lesson, state) -> dict:
    """What saving ``state`` over this module would remove, and what it keeps."""
    from academy.models import Certificate, SectionProgress
    kept = {s.get('id') for s in state.get('sections', []) if s.get('id')}
    dropped = [s for s in lesson.sections.using(DB).all() if s.pk not in kept]
    return {
        'certificates': Certificate.objects.using(DB).filter(lesson=lesson).count(),
        'sections_removed': len(dropped),
        'viewed_marks_removed': SectionProgress.objects.using(DB)
        .filter(section__in=dropped).count(),
    }


def _write_quiz(lesson, quiz_state) -> bool:
    """Replace the questions if they changed. ``True`` when anything was written."""
    from academy.models import Answer, Question, Quiz
    if quiz_state is None:
        return False
    current = _quiz_state(lesson)
    quiz = Quiz.objects.using(DB).filter(lesson=lesson).first()
    changed = False
    if quiz is None:
        quiz = Quiz(lesson=lesson, pass_mark=quiz_state.get('pass_mark', 80))
        quiz.save(using=DB)
        changed = True
    elif quiz.pass_mark != quiz_state.get('pass_mark', quiz.pass_mark):
        quiz.pass_mark = quiz_state['pass_mark']
        quiz.save(using=DB, update_fields=['pass_mark'])
        changed = True
    if current is None or current['questions'] != quiz_state['questions']:
        Question.objects.using(DB).filter(quiz=quiz).delete()
        for q in quiz_state['questions']:
            question = Question(quiz=quiz, text=q['text'])
            question.save(using=DB)
            Answer.objects.using(DB).bulk_create([
                Answer(question=question, text=a['text'], is_correct=a['correct'])
                for a in q['answers']])
        changed = True
    return changed


def apply(lesson, state, *, by: str = '', note: str = '') -> dict:
    """Make the module match ``state`` and keep the result as a version.

    Returns what was done, for the receipt: sections added, changed and
    removed, whether the quiz was rewritten, the certificates left untouched,
    and the ``LessonRevision`` written.
    """
    from academy.models import LessonRevision, LessonSection

    receipt = impact(lesson, state)
    with transaction.atomic(using=DB):
        if not LessonRevision.objects.using(DB).filter(lesson=lesson).exists():
            LessonRevision(lesson=lesson, saved_by='', snapshot=snapshot(lesson),
                           note='As it was before the first save in the editor'
                           ).save(using=DB)

        for field in ('title', 'order', 'is_published', 'has_quiz'):
            if field in state:
                setattr(lesson, field, state[field])
        lesson.save(using=DB)

        existing = {s.pk: s for s in lesson.sections.using(DB).all()}
        wanted = state.get('sections', [])
        keep = {s['id'] for s in wanted if s.get('id') in existing}
        removed = [pk for pk in existing if pk not in keep]
        LessonSection.objects.using(DB).filter(pk__in=removed).delete()

        added = changed = 0
        for position, s in enumerate(wanted, 1):
            section = existing.get(s.get('id')) or LessonSection(lesson=lesson)
            values = {'title': s.get('title', ''), 'body': s.get('body', ''),
                      'image': s.get('image') or '', 'order': s.get('order', position)}
            before = (section.title, section.body,
                      section.image.name if section.image else '', section.order)
            section.title, section.body, section.order = (
                values['title'], values['body'], values['order'])
            section.image = values['image'] or None
            if section.pk is None:
                added += 1
            elif before != (values['title'], values['body'], values['image'],
                            values['order']):
                changed += 1
            else:
                continue
            section.save(using=DB)

        quiz_changed = _write_quiz(lesson, state.get('quiz'))
        revision = LessonRevision(lesson=lesson, saved_by=by, note=note[:300],
                                  snapshot=snapshot(lesson))
        revision.save(using=DB)

    receipt.update({'sections_added': added, 'sections_changed': changed,
                    'quiz_changed': quiz_changed, 'revision': revision.pk})
    return receipt


def create(title: str, *, by: str = ''):
    """A new, unpublished module at the end of the list."""
    from django.db.models import Max
    from academy.models import Lesson
    base = slugify(title)[:40] or 'module'
    slug, n = base, 2
    while Lesson.objects.using(DB).filter(slug=slug).exists():
        slug, n = f'{base}-{n}', n + 1
    top = Lesson.objects.using(DB).aggregate(m=Max('order'))['m'] or 0
    lesson = Lesson(title=title, slug=slug, order=top + 1, content='',
                    has_quiz=False, is_published=False)
    lesson.save(using=DB)
    from academy.models import LessonRevision
    LessonRevision(lesson=lesson, saved_by=by, note='Module created',
                   snapshot=snapshot(lesson)).save(using=DB)
    return lesson
