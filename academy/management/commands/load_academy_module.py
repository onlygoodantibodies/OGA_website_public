"""Load a module's new text from files in the repository — once.

    python manage.py load_academy_module academy/content/module3            # dry run
    python manage.py load_academy_module academy/content/module3 --apply

The folder holds ``module.json`` (the title it ``replaces``, the new title,
the sections in order with the HTML file and the picture for each, and the
quiz file), and the files it names. The quiz is in the editor's text form
(``academy/editing.py::parse_quiz``).

**It rewrites the existing module in place**, through ``editing.apply`` — the
same writer as the editor — so the module keeps its id, its quiz keeps its id,
and every certificate issued for it stays exactly as it was, printing the old
title it was earned under. The version before the load is kept in the module's
History, so the whole load is one Restore away from undone.

**Once, because the editor is where a module lives afterwards.** The module is
found by the title in ``replaces``; after a load it carries the new one, so a
second run refuses by name rather than overwriting whatever people have
improved since. ``--lesson <id>`` names a module directly, for the case where
the title had already been changed by hand.

Dry run by default: it says which module it found, what it keeps, and what it
would replace, and writes nothing — not even the pictures.
"""
import json
from pathlib import Path

from django.core.files.base import File
from django.core.management.base import BaseCommand, CommandError

from academy import editing
from academy.models import Lesson, LessonSection

DB = editing.DB


class Command(BaseCommand):
    help = "Rewrite an Academy module from a folder of files (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument('folder')
        parser.add_argument('--lesson', type=int,
                            help='Rewrite this module (its id) instead of finding it by title.')
        parser.add_argument('--apply', action='store_true', help='Write it. Without this, nothing is written.')

    def handle(self, folder, lesson=None, apply=False, **options):
        root = Path(folder)
        try:
            manifest = json.loads((root / 'module.json').read_text())
        except (OSError, ValueError) as exc:
            raise CommandError(f'Could not read {root / "module.json"}: {exc}')

        target = self._find(manifest, lesson)

        sections, problems = [], []
        for position, spec in enumerate(manifest['sections'], 1):
            body = (root / spec['body']).read_text()
            why = editing.unsafe_html(body)
            if why:
                problems.append(f'{spec["body"]}: {why}')
            image = spec.get('image') or ''
            if image and not (root / image).is_file():
                problems.append(f'{spec["title"]}: the picture {image} is not in {root}.')
            sections.append({'id': None, 'title': spec['title'], 'body': body,
                             'image': image, 'order': position})
        questions, errors = editing.parse_quiz((root / manifest['quiz']).read_text())
        problems += [f'{manifest["quiz"]}: {e}' for e in errors]
        if problems:
            raise CommandError('Nothing was written:\n  ' + '\n  '.join(problems))

        current = editing.snapshot(target)
        state = {'title': manifest['title'], 'order': target.order,
                 'is_published': target.is_published,
                 'has_quiz': manifest.get('has_quiz', True),
                 'sections': sections,
                 'quiz': {'pass_mark': manifest.get('pass_mark', 80), 'questions': questions}}
        impact = editing.impact(target, state)

        out = self.stdout.write
        out(f'Module #{target.pk}: "{target.title}"')
        out(f'  → "{state["title"]}"')
        out(f'  Keeps: its id, its quiz, and all {impact["certificates"]} certificate(s) issued for it '
            '(each goes on printing the title it was earned under).')
        out(f'  Sections: {len(current["sections"])} now → {len(sections)} new '
            f'({impact["viewed_marks_removed"]} learner "opened" mark(s) on the old ones go with them).')
        old_q = len((current['quiz'] or {}).get('questions', []))
        out(f'  Quiz: {old_q} question(s) now → {len(questions)}; pass mark {state["quiz"]["pass_mark"]}%.')
        out(f'  Pictures: {sum(1 for s in sections if s["image"])} to upload.')
        out(f'  Published: {"yes" if target.is_published else "no"} (unchanged).')

        if not apply:
            out(self.style.WARNING('Dry run — nothing written. Add --apply to load it.'))
            return

        storage = LessonSection._meta.get_field('image').storage
        for s in sections:
            if s['image']:
                with open(root / s['image'], 'rb') as fh:
                    s['image'] = storage.save(f'lesson_images/{root.name}/{s["image"]}', File(fh))
        receipt = editing.apply(target, state, by='load_academy_module',
                                note=manifest.get('note', f'Loaded from {root}'))
        out(self.style.SUCCESS(
            f'Loaded. {receipt["sections_added"]} section(s) written, '
            f'{receipt["sections_removed"]} replaced, quiz '
            f'{"rewritten" if receipt["quiz_changed"] else "unchanged"}; '
            f'{receipt["certificates"]} certificate(s) untouched. The version before this '
            f'is in the module\'s History (/pipeline/academy/{target.pk}/history/).'))

    def _find(self, manifest, lesson_id):
        lessons = Lesson.objects.using(DB)
        if lesson_id is not None:
            found = lessons.filter(pk=lesson_id).first()
            if found is None:
                raise CommandError(f'There is no module #{lesson_id}.')
            return found
        wanted = manifest['replaces']
        matches = list(lessons.filter(title__iexact=wanted))
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise CommandError(f'{len(matches)} modules are titled "{wanted}" '
                               f'(#{", #".join(str(m.pk) for m in matches)}). Name one with --lesson.')
        if lessons.filter(title__iexact=manifest['title']).exists():
            raise CommandError(
                f'A module is already titled "{manifest["title"]}", so this has been loaded '
                'before. It is edited at /pipeline/academy/ now; loading again would '
                'overwrite whatever has been improved since. Use --lesson to force it.')
        titles = '\n  '.join(f'#{m.pk} {m.title}' for m in lessons.order_by('order'))
        raise CommandError(f'No module is titled "{wanted}". The modules are:\n  {titles}\n'
                           'Name one with --lesson.')
