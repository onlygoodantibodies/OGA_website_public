"""A pipeline member is found by username, never by the login's pk.

``request.user`` comes from ``academy_db``; the ``Member`` points at a
``pipeline_db`` user. The two ``auth_user`` tables number their rows
independently, so the pks agree only by luck — which is why every test fixture,
creating one user in each on an empty table, never saw it. The workbook upload
compared them and told a newly granted member "no pipeline member profile for
your account" (Mickey, 24 Sep 2026) while the gate had let them in by username.
"""
from django.contrib.auth.models import User
from django.test import TestCase

from pipeline.models import Member, Site
from pipeline.views.session_entry import _get_member_or_none

DB = "pipeline_db"


class MemberIsFoundByUsernameTests(TestCase):
    databases = {"pipeline_db", "academy_db"}

    @classmethod
    def setUpTestData(cls):
        lei = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        mcg = Site.objects.using(DB).create(name="McGill", short_code="MCG")
        # pipeline_db: sara is row 1, mickey row 2.
        sara = User(username="sara"); sara.save(using=DB)
        mickey = User(username="mickey"); mickey.save(using=DB)
        Member.objects.using(DB).create(user_id=sara.pk, site_id=mcg.pk, role="experimenter")
        Member.objects.using(DB).create(user_id=mickey.pk, site_id=lei.pk, role="experimenter")
        # academy_db: mickey is the only login, so a different row number.
        cls.login = User(username="mickey"); cls.login.save(using="academy_db")
        cls.lei = lei

    def test_the_pks_really_do_differ(self):
        self.assertNotEqual(
            self.login.pk, User.objects.using(DB).get(username="mickey").pk)

    def test_the_login_finds_its_own_member_not_whoever_shares_the_number(self):
        member = _get_member_or_none(self.login)
        self.assertIsNotNone(member, "a granted member was told they have no profile")
        self.assertEqual(member.user_id, User.objects.using(DB).get(username="mickey").pk)
        self.assertEqual(member.site_id, self.lei.pk)

    def test_a_login_with_no_member_is_still_none(self):
        stranger = User(username="stranger"); stranger.save(using="academy_db")
        self.assertIsNone(_get_member_or_none(stranger))


class AMemberIsNamedFromThePipelineUserTests(TestCase):
    """``Member.__str__`` fell back to ``self.user.get_full_name()`` when the
    display name was blank, and the router sends every ``auth.User`` read to
    academy_db — so it named whichever *login* shared the pipeline user's pk.
    Jemma and Michael have blank display names on live (24 Sep 2026), and that
    string is what the experimenter pickers, bench sheets, workbooks and a
    Zenodo deposit's creator list print."""

    databases = {"pipeline_db", "academy_db"}

    @classmethod
    def setUpTestData(cls):
        site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        filler = User(username="filler"); filler.save(using=DB)
        jemma = User(username="jemma", first_name="Jemma", last_name="Cooper")
        jemma.save(using=DB)
        # academy_db: a different person holds jemma's pipeline pk.
        User(username="first").save(using="academy_db")
        User(username="other", first_name="Somebody", last_name="Else").save(using="academy_db")
        assert User.objects.using("academy_db").get(username="other").pk == jemma.pk
        cls.member = Member.objects.using(DB).create(
            user_id=jemma.pk, site_id=site.pk, role="experimenter", display_name="")

    def test_a_blank_display_name_names_the_pipeline_person(self):
        member = Member.objects.using(DB).get(pk=self.member.pk)
        self.assertEqual(str(member), "Jemma Cooper")

    def test_a_display_name_still_wins(self):
        member = Member.objects.using(DB).get(pk=self.member.pk)
        member.display_name = "J. Cooper"
        self.assertEqual(str(member), "J. Cooper")


class TheAdminLinkIsForStaffTests(TestCase):
    """Django admin refuses anyone without ``is_staff``, so a link to it drawn
    for every member is a dead end for all of them but the owner."""

    databases = {"pipeline_db", "academy_db"}

    def _client(self, staff):
        from pipeline.tests_timeouts import _member_client
        site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        client = _member_client(self, site)
        User.objects.using("academy_db").filter(username="carl").update(is_staff=staff)
        return client

    def _admin_links(self, client):
        return client.get("/pipeline/start/").content.decode().count('href="/admin/"')

    def test_a_member_without_staff_is_not_offered_admin(self):
        self.assertEqual(self._admin_links(self._client(False)), 0)

    def test_staff_still_are(self):
        self.assertEqual(self._admin_links(self._client(True)), 2)
