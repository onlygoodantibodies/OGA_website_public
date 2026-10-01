"""The Academy module editor — what would go wrong without anybody noticing.

Pinned: a certificate outlives every edit, a rename and a delete attempt; a
section edited in place keeps the learners' marks on it; a save over somebody
else's is handed back rather than taken; what cannot be shown to learners is
refused rather than stored; and a version can be put back.
"""
from __future__ import annotations

from django.contrib.auth.models import User
from django.db.models import ProtectedError
from django.test import TestCase
from django.urls import reverse

from academy import editing
from academy.models import (Answer, Certificate, Lesson, LessonRevision,
                            LessonSection, Question, Quiz, SectionProgress)
from pipeline.models import Site
from pipeline.tests_timeouts import DB, _member_client

ADB = 'academy_db'

QUIZ = """1. What does an RRID give you?
- The price
* A unique identifier to cite the antibody by

2. Which result means nobody has run it?
- Not supportive
* Not tested
- Limited support
"""


class ModuleEditorTests(TestCase):
    databases = {DB, ADB}

    def setUp(self):
        site = Site.objects.using(DB).create(name='Leicester', short_code='LEI')
        self.client = _member_client(self, site)
        self.learner = User.objects.db_manager(ADB).create_user('learner', password='pw')
        self.lesson = Lesson(title='MODULE 3 - NAVIGATING THE OGA DATABASE',
                             slug='module-3', order=3, content='', has_quiz=True)
        self.lesson.save(using=ADB)
        self.s1 = LessonSection(lesson=self.lesson, title='Old intro', body='<p>old</p>', order=1)
        self.s1.save(using=ADB)
        self.s2 = LessonSection(lesson=self.lesson, title='Homepage boxes', body='<p>gone</p>', order=2)
        self.s2.save(using=ADB)
        quiz = Quiz(lesson=self.lesson, pass_mark=80)
        quiz.save(using=ADB)
        q = Question(quiz=quiz, text='Old question?')
        q.save(using=ADB)
        Answer(question=q, text='yes', is_correct=True).save(using=ADB)
        Answer(question=q, text='no', is_correct=False).save(using=ADB)
        self.cert = Certificate(user=self.learner, lesson=self.lesson, score=100)
        self.cert.save(using=ADB)
        SectionProgress(user=self.learner, section=self.s1).save(using=ADB)
        SectionProgress(user=self.learner, section=self.s2).save(using=ADB)
        self.url = reverse('pipeline:academy_module_edit', args=[self.lesson.pk])

    def _post(self, **overrides):
        """The edit form as the page would send it: s1 kept and edited, s2
        removed, one new section, and a new title and quiz."""
        data = {
            'title': 'MODULE 3 - USING THE OGA DATABASE AND TOOLS', 'order': '3',
            'is_published': 'on', 'has_quiz': 'on', 'pass_mark': '80',
            'quiz': QUIZ, 'note': 'rewrite', 'based_on': '0',
            'sections-TOTAL_FORMS': '3', 'sections-INITIAL_FORMS': '2',
            'sections-MIN_NUM_FORMS': '0', 'sections-MAX_NUM_FORMS': '1000',
            'sections-0-id': str(self.s1.pk), 'sections-0-title': 'Intro',
            'sections-0-body': '<p>new</p>', 'sections-0-order': '1',
            'sections-1-id': str(self.s2.pk), 'sections-1-title': 'Homepage boxes',
            'sections-1-body': '<p>gone</p>', 'sections-1-order': '2',
            'sections-1-DELETE': 'on',
            'sections-2-id': '', 'sections-2-title': 'The four results',
            'sections-2-body': '<p>Supportive…</p>', 'sections-2-order': '3',
        }
        data.update(overrides)
        return self.client.post(self.url, data)

    def test_a_full_rewrite_and_rename_leaves_the_certificate_as_it_was(self):
        response = self._post()
        self.assertEqual(response.status_code, 302)
        cert = Certificate.objects.using(ADB).get(pk=self.cert.pk)
        self.assertEqual(cert.lesson_id, self.lesson.pk)
        self.assertEqual(cert.score, 100)
        self.assertEqual(cert.module_title, 'MODULE 3 - NAVIGATING THE OGA DATABASE')
        lesson = Lesson.objects.using(ADB).get(pk=self.lesson.pk)
        self.assertEqual(lesson.title, 'MODULE 3 - USING THE OGA DATABASE AND TOOLS')
        # The certificate's public verify page prints the title it was earned under.
        page = self.client.get(cert.get_verify_url())
        self.assertContains(page, 'NAVIGATING THE OGA DATABASE')

    def test_an_edited_section_keeps_its_learners_marks_and_a_removed_one_says_so(self):
        self._post()
        titles = list(self.lesson.sections.using(ADB).order_by('order')
                      .values_list('title', flat=True))
        self.assertEqual(titles, ['Intro', 'The four results'])
        self.assertTrue(LessonSection.objects.using(ADB).filter(pk=self.s1.pk).exists())
        self.assertEqual(SectionProgress.objects.using(ADB).filter(section=self.s1).count(), 1)
        # The receipt counts what went with the removed section.
        page = self.client.get(self.url)
        self.assertContains(page, '1 section removed')
        self.assertContains(page, '1 certificate for this module untouched')

    def test_the_quiz_is_rewritten_from_its_text(self):
        self._post()
        quiz = Quiz.objects.using(ADB).get(lesson=self.lesson)
        questions = list(quiz.question_set.using(ADB).order_by('pk'))
        self.assertEqual([q.text for q in questions],
                         ['What does an RRID give you?', 'Which result means nobody has run it?'])
        self.assertEqual(
            [a.text for a in questions[1].answer_set.using(ADB).filter(is_correct=True)],
            ['Not tested'])

    def test_a_module_with_certificates_cannot_be_deleted(self):
        with self.assertRaises(ProtectedError):
            self.lesson.delete(using=ADB)
        self.assertTrue(Certificate.objects.using(ADB).filter(pk=self.cert.pk).exists())

    def test_a_quiz_answer_marked_twice_is_refused_by_line_and_nothing_is_saved(self):
        response = self._post(quiz='1. Which?\n* one\n* two\n')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'has 2 answers marked correct')
        self.assertEqual(Lesson.objects.using(ADB).get(pk=self.lesson.pk).title,
                         'MODULE 3 - NAVIGATING THE OGA DATABASE')

    def test_a_script_in_a_section_is_refused_not_stored(self):
        response = self._post(**{'sections-0-body': '<p onclick="x()">hi</p>'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'event attribute')
        self.assertEqual(LessonSection.objects.using(ADB).get(pk=self.s1.pk).body, '<p>old</p>')

    def test_a_save_over_somebody_elses_is_handed_back_then_taken_on_the_second_press(self):
        self._post()                       # somebody else saves first
        stale = self._post(title='Mine', note='mine')    # still based on 0
        self.assertEqual(stale.status_code, 200)
        self.assertContains(stale, 'somebody else saved this module')
        self.assertNotEqual(Lesson.objects.using(ADB).get(pk=self.lesson.pk).title, 'Mine')
        latest = LessonRevision.objects.using(ADB).filter(lesson=self.lesson).first()
        # The page moved the stamp on, so pressing Save again is deliberate.
        self.assertContains(stale, f'name="based_on" value="{latest.pk}"')

    def test_the_first_save_keeps_the_module_as_it_was_and_restore_puts_it_back(self):
        self._post()
        baseline = LessonRevision.objects.using(ADB).filter(lesson=self.lesson).last()
        self.assertEqual(baseline.snapshot['title'], 'MODULE 3 - NAVIGATING THE OGA DATABASE')
        response = self.client.post(reverse('pipeline:academy_module_version',
                                            args=[self.lesson.pk, baseline.pk]))
        self.assertEqual(response.status_code, 302)
        lesson = Lesson.objects.using(ADB).get(pk=self.lesson.pk)
        self.assertEqual(lesson.title, 'MODULE 3 - NAVIGATING THE OGA DATABASE')
        self.assertEqual(sorted(lesson.sections.using(ADB).values_list('title', flat=True)),
                         ['Homepage boxes', 'Old intro'])
        self.assertTrue(Certificate.objects.using(ADB).filter(pk=self.cert.pk).exists())

    def test_an_unpublished_module_is_off_the_learners_list_and_hidden_from_them(self):
        self._post(is_published='')
        learner = self.client_class()
        self.assertTrue(learner.login(username='learner', password='pw'))
        self.assertNotContains(learner.get(reverse('academy:academy_home')), 'USING THE OGA')
        self.assertEqual(learner.get(reverse('academy:lesson_detail',
                                             args=[self.lesson.pk])).status_code, 404)
        # Its editors can still open it, and are told learners cannot.
        self.assertContains(self.client.get(reverse('academy:lesson_detail',
                                                    args=[self.lesson.pk])), 'Not published')


class QuizTextTests(TestCase):
    databases = set()

    def test_round_trip(self):
        questions, errors = editing.parse_quiz(QUIZ)
        self.assertEqual(errors, [])
        self.assertEqual(editing.parse_quiz(editing.quiz_text(questions)), (questions, []))

    def test_a_stray_line_is_named(self):
        _, errors = editing.parse_quiz('1. Q?\n* a\n- b\nwhatever\n')
        self.assertEqual(len(errors), 1)
        self.assertIn('Line 4', errors[0])

    def test_youtube_embeds_pass_and_other_frames_do_not(self):
        self.assertIsNone(editing.unsafe_html(
            '<iframe src="https://www.youtube-nocookie.com/embed/abc"></iframe>'))
        self.assertIsNotNone(editing.unsafe_html('<iframe src="https://evil.example/"></iframe>'))
