"""Adding a site.

Carl Laflamme: *"McGill is included by default when adding a new target entry
(ideally there would be a drop down menu with McGill, Leicester, uOttawa, UBC,
Cornell, etc)"* — and it was not McGill. He and Riham Ayoubi had moved to
uOttawa, which was not a Site at all, so their Member rows still said Montreal
and every target they added was nominated there. Nothing in the app could add
the missing institution.

The command creates the site and stops. Who works where is the people board's
job, and a second way to do the same thing is how two surfaces end up
disagreeing.
"""
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from pipeline.models import Site

DB = "pipeline_db"


class AddSiteTests(TestCase):
    databases = {DB}

    def setUp(self):
        Site.objects.using(DB).create(name="Montreal", short_code="MTL")
        Site.objects.using(DB).create(name="Leicester", short_code="LEI")

    def _run(self, *extra, name="uOttawa", code="UOT"):
        out = StringIO()
        call_command("add_site", "--name", name, "--code", code, *extra, stdout=out)
        return out.getvalue()

    def test_the_dry_run_writes_nothing_and_says_what_is_there(self):
        text = self._run()
        self.assertIn("WOULD CREATE 'uOttawa' (UOT)", text)
        self.assertIn("DRY RUN", text)
        # Naming what is already on file is what makes the answer checkable
        # before the write rather than after it.
        self.assertIn("Leicester", text)
        self.assertIn("Montreal", text)
        self.assertFalse(Site.objects.using(DB).filter(name="uOttawa").exists())

    def test_apply_creates_it_once(self):
        self._run("--apply")
        site = Site.objects.using(DB).get(name="uOttawa")
        self.assertEqual(site.short_code, "UOT")
        self.assertTrue(site.is_active)

    def test_running_it_twice_is_not_a_second_site(self):
        self._run("--apply")
        text = self._run("--apply")
        self.assertIn("already on file", text)
        self.assertEqual(Site.objects.using(DB).filter(name="uOttawa").count(), 1)

    def test_a_clashing_short_code_is_refused_by_name(self):
        with self.assertRaises(CommandError) as e:
            self._run(code="MTL")
        self.assertIn("Montreal", str(e.exception))
        self.assertFalse(Site.objects.using(DB).filter(name="uOttawa").exists())

    def test_it_points_at_the_people_board_rather_than_moving_anybody(self):
        text = self._run("--apply")
        self.assertIn("/pipeline/users/board/", text)
        src = Path("pipeline/management/commands/add_site.py").read_text()
        self.assertNotIn("--move", src, "this command must not also assign people")
