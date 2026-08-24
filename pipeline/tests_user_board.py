"""The people board — logins and pipeline access, on one screen.

It exists because a working account is three rows in two databases and nothing
showed all three: on 3 Aug 2026 a password changed in Django admin reported
success and changed nothing (that form's password field is a read-only hash
behind a separate button), while the same person turned out to be a superuser
with ``is_staff`` off and a second Member row under an Access-import username.

What is pinned here is the part that would be expensive to get wrong: the gate,
the three-rows-or-none rule, and the guards that stop a superuser locking every
superuser out of the page that grants the flag.
"""
from django.contrib.auth import authenticate
from django.contrib.auth.models import User
from django.test import Client, TestCase

from pipeline.models import Member, Site
from pipeline.services import members as member_svc

DB = "pipeline_db"
ACADEMY = "academy_db"


def _person(username, site, *, role="experimenter", superuser=False,
            staff=False, login=True, password="pw", member_active=True,
            login_active=True):
    """One person, made the way the app makes them: three rows in two databases."""
    if login:
        au = User(username=username, email=f"{username}@lab.org",
                  is_superuser=superuser, is_staff=staff, is_active=login_active)
        au.set_password(password)
        au.save(using=ACADEMY)
    pu = User(username=username, is_superuser=superuser, is_staff=staff)
    pu.set_unusable_password()
    pu.save(using=DB)
    return Member.objects.using(DB).create(
        user_id=pu.pk, site_id=site.pk, role=role, is_active=member_active,
        display_name=username.title())


class TheBoardIsSuperusersOnlyTests(TestCase):
    databases = {DB, ACADEMY}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        _person("boss", self.site, role="admin", superuser=True)
        _person("bench", self.site)

    def _client(self, username):
        c = Client()
        self.assertTrue(c.login(username=username, password="pw"))
        return c

    def test_a_superuser_gets_in(self):
        self.assertEqual(self._client("boss").get("/pipeline/users/board/").status_code, 200)

    def test_an_ordinary_member_is_refused_and_told_why(self):
        resp = self._client("bench").get("/pipeline/users/board/")
        self.assertEqual(resp.status_code, 403)
        body = resp.content.decode()
        self.assertIn("superuser", body.lower())
        # Names what the page is for, so the refusal is actionable.
        self.assertIn("Ask one of them", body)

    def test_every_endpoint_is_gated_not_just_the_page(self):
        """A gate on the page and not on its endpoints is not a gate."""
        c = self._client("bench")
        for url in ("/pipeline/users/board/rows/",):
            self.assertEqual(c.get(url).status_code, 403, url)
        for url in ("/pipeline/users/board/patch/", "/pipeline/users/board/check/",
                    "/pipeline/users/board/commit/", "/pipeline/users/board/password/"):
            self.assertEqual(c.post(url, {}).status_code, 403, url)

    def test_a_refused_fetch_answers_json_not_html(self):
        """`requestJson` reads a body: an HTML 403 arrives as 'unexpected token
        <' and the grid says the rows could not be loaded without saying why."""
        resp = self._client("bench").get("/pipeline/users/board/rows/",
                                         HTTP_X_REQUESTED_WITH="fetch")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp["Content-Type"], "application/json")
        self.assertIn("superusers", resp.json()["error"])


class TheBoardShowsBothHalvesTests(TestCase):
    databases = {DB, ACADEMY}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        _person("boss", self.site, role="admin", superuser=True)
        self.client = Client()
        self.assertTrue(self.client.login(username="boss", password="pw"))

    def _rows(self):
        return {r["username"]: r
                for r in self.client.get("/pipeline/users/board/rows/").json()["rows"]}

    def test_someone_with_no_login_is_shown_as_unable_to_sign_in(self):
        """The state `pipeline_users list` calls MISSING — and the state that
        looks identical from outside to a wrong password."""
        _person("ghost", self.site, login=False)
        row = self._rows()["ghost"]
        self.assertFalse(row["login_exists"])
        self.assertFalse(row["can_sign_in"])
        self.assertTrue(row["can_use_pipeline"], "their pipeline half is fine")

    def test_a_deactivated_login_is_told_apart_from_a_wrong_password(self):
        """`is_active=False` makes the RIGHT password fail as 'invalid username
        or password'. The board has to name it, or it is invisible again."""
        _person("dormant", self.site, login_active=False)
        row = self._rows()["dormant"]
        self.assertTrue(row["login_exists"])
        self.assertFalse(row["login_active"])
        self.assertFalse(row["can_sign_in"])

    def test_the_flags_come_from_the_academy_row_which_is_the_one_that_counts(self):
        _person("odd", self.site, superuser=True, staff=False)
        row = self._rows()["odd"]
        self.assertTrue(row["is_superuser"])
        self.assertFalse(row["is_staff"], "a superuser without staff cannot open /admin/")

    def test_every_value_in_a_row_is_json(self):
        """One un-serialisable value 500s the whole response and the board shows
        nothing while blaming the filters — see cell_line_board.row_for."""
        import json
        _person("someone", self.site)
        for row in self._rows().values():
            json.dumps(row)
            for k, v in row.items():
                self.assertIsInstance(v, (str, int, float, bool, type(None)), k)

    def test_the_count_is_the_length_of_the_list(self):
        data = self.client.get("/pipeline/users/board/rows/").json()
        self.assertEqual(data["count"], len(data["rows"]))


class OnlyPipelineMembershipIsEditableTests(TestCase):
    """The page manages membership, not Django-level permissions.

    `is_superuser` and `is_staff` were editable here for a few hours. They are
    not pipeline permissions — `is_staff` is raw access to every table on the
    site, the academy and public pages included — so they went back to Django
    admin, where the change is logged.
    """

    databases = {DB, ACADEMY}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        Site.objects.using(DB).create(name="McGill", short_code="MCG")
        self.boss = _person("boss", self.site, role="admin", superuser=True)
        self.carl = _person("carl", self.site)
        self.client = Client()
        self.assertTrue(self.client.login(username="boss", password="pw"))

    def _patch(self, member, field, value):
        return self.client.post("/pipeline/users/board/patch/",
                                {"target_id": member.pk, "field": field, "value": value})

    def test_the_django_flags_cannot_be_set_from_here(self):
        for field in ("is_superuser", "is_staff", "login_active"):
            with self.subTest(field=field):
                resp = self._patch(self.carl, field, "yes")
                self.assertEqual(resp.status_code, 400)
                self.assertIn("not editable", resp.json()["error"])
        au = User.objects.using(ACADEMY).get(username="carl")
        self.assertFalse(au.is_superuser)
        self.assertFalse(au.is_staff)

    def test_the_page_no_longer_draws_them(self):
        body = self.client.get("/pipeline/users/board/").content.decode()
        self.assertNotIn("Django admin</span></th>", body)
        self.assertNotIn("is_staff", body)
        # Who *is* a superuser is still visible — you need to know who else can
        # open this page — just not clickable.
        self.assertIn("superuser", body)

    def test_membership_is_still_fully_editable(self):
        for field, value, read in (
                ("role", "pi", lambda: Member.objects.using(DB).get(pk=self.carl.pk).role),
                ("site", "MCG", lambda: Member.objects.using(DB).get(pk=self.carl.pk).site.short_code),
                ("display_name", "Carl L", lambda: Member.objects.using(DB).get(pk=self.carl.pk).display_name),
                ("email", "c@new.org", lambda: User.objects.using(ACADEMY).get(username="carl").email),
                ("is_active", "", lambda: Member.objects.using(DB).get(pk=self.carl.pk).is_active)):
            with self.subTest(field=field):
                resp = self._patch(self.carl, field, value)
                self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(read(), False)   # is_active, the last one set

    def test_you_cannot_deactivate_yourself(self):
        """The page is superusers only *and* needs an active Member row, so
        turning your own access off closes the door behind you."""
        resp = self._patch(self.boss, "is_active", "")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("cannot deactivate yourself", resp.json()["error"])
        self.assertTrue(Member.objects.using(DB).get(pk=self.boss.pk).is_active)

    def test_the_last_superuser_cannot_be_deactivated_at_all(self):
        """Through the page the self-guard catches this first, so it earns its
        keep one layer down — a script calling set_field with no actor."""
        with self.assertRaises(member_svc.Refused) as caught:
            member_svc.set_field(self.boss.pk, "is_active", "", actor_username="")
        self.assertIn("only active superuser", str(caught.exception))
        self.assertTrue(Member.objects.using(DB).get(pk=self.boss.pk).is_active)

    def test_deactivating_an_ordinary_member_still_works(self):
        resp = self._patch(self.carl, "is_active", "")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Member.objects.using(DB).get(pk=self.carl.pk).is_active)


class ImportedRecordsAreOutOfTheWayNotGoneTests(TestCase):
    """Fifteen of twenty-four rows were Access-import placeholders with no login.

    They are attached to real historical sessions, so they cannot be deleted —
    but they are not people who sign in, and listing them by default buried the
    nine who are.
    """

    databases = {DB, ACADEMY}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        _person("boss", self.site, role="admin", superuser=True)
        _person("jemma", self.site)
        _person("access_linda_li", self.site, login=False)
        _person("access_joel_ryan", self.site, login=False)
        self.client = Client()
        self.assertTrue(self.client.login(username="boss", password="pw"))

    def _rows(self, **params):
        return self.client.get("/pipeline/users/board/rows/", params).json()

    def test_they_are_hidden_by_default(self):
        names = {r["username"] for r in self._rows()["rows"]}
        self.assertEqual(names, {"boss", "jemma"})

    def test_the_chip_says_how_many_are_hidden(self):
        """A short list that does not say it is short reads as the whole list."""
        self.assertEqual(self._rows()["hidden_imported"], 2)

    def test_one_tick_brings_them_back(self):
        names = {r["username"] for r in self._rows(imported="yes")["rows"]}
        self.assertIn("access_linda_li", names)
        self.assertEqual(self._rows(imported="yes")["hidden_imported"], 0)

    def test_the_count_still_matches_the_list_either_way(self):
        for params in ({}, {"imported": "yes"}):
            d = self._rows(**params)
            self.assertEqual(d["count"], len(d["rows"]))

    def test_hiding_them_does_not_hide_a_real_person_who_has_no_login(self):
        """A newly-made person whose login creation failed is exactly the row
        you need to see — it must not be swept up with the imports."""
        _person("newbie", self.site, login=False)
        names = {r["username"] for r in self._rows()["rows"]}
        self.assertIn("newbie", names)


class SettingAPasswordActuallySetsItTests(TestCase):
    """The thing Django admin's change form cannot do, which is why this page
    exists at all."""

    databases = {DB, ACADEMY}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        _person("boss", self.site, role="admin", superuser=True)
        self.carl = _person("carl", self.site, role="admin")
        self.client = Client()
        self.assertTrue(self.client.login(username="boss", password="pw"))

    def test_a_reset_lands_where_login_reads_and_is_returned_once(self):
        resp = self.client.post("/pipeline/users/board/password/",
                                {"member_id": self.carl.pk, "password": "YCharOS2026"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["password"], "YCharOS2026")
        self.assertIsNotNone(authenticate(username="carl", password="YCharOS2026"))

    def test_a_blank_password_generates_one_and_hands_it_back(self):
        resp = self.client.post("/pipeline/users/board/password/",
                                {"member_id": self.carl.pk, "password": ""})
        generated = resp.json()["password"]
        self.assertGreaterEqual(len(generated), 8)
        self.assertIsNotNone(authenticate(username="carl", password=generated))

    def test_a_reset_reactivates_the_login(self):
        """A reset against a deactivated login is pointless — the new password
        would fail as 'invalid username or password', which is the exact
        confusion the whole page is for."""
        au = User.objects.using(ACADEMY).get(username="carl")
        au.is_active = False
        au.save(using=ACADEMY)
        self.client.post("/pipeline/users/board/password/",
                         {"member_id": self.carl.pk, "password": "YCharOS2026"})
        self.assertTrue(User.objects.using(ACADEMY).get(username="carl").is_active)
        self.assertIsNotNone(authenticate(username="carl", password="YCharOS2026"))

    def test_a_short_password_is_refused_by_name(self):
        resp = self.client.post("/pipeline/users/board/password/",
                                {"member_id": self.carl.pk, "password": "abc"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("8 characters", resp.json()["error"])

    def test_someone_with_no_login_says_so_rather_than_crashing(self):
        ghost = _person("ghost", self.site, login=False)
        resp = self.client.post("/pipeline/users/board/password/",
                                {"member_id": ghost.pk})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("no login account", resp.json()["error"])


class AddingSomeoneWritesAllThreeRecordsTests(TestCase):
    databases = {DB, ACADEMY}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        Site.objects.using(DB).create(name="McGill", short_code="MCG")
        _person("boss", self.site, role="admin", superuser=True)
        self.client = Client()
        self.assertTrue(self.client.login(username="boss", password="pw"))

    HEADER = "username\tfirst name\tlast name\temail\tsite\trole\tdisplay name"

    def _commit(self, body):
        import json
        return self.client.post(
            "/pipeline/users/board/commit/",
            data=json.dumps({"text": f"{self.HEADER}\n{body}"}),
            content_type="application/json")

    def _check(self, body):
        import json
        return self.client.post(
            "/pipeline/users/board/check/",
            data=json.dumps({"text": f"{self.HEADER}\n{body}"}),
            content_type="application/json")

    def test_all_three_records_appear_together(self):
        resp = self._commit("jsmith\tJo\tSmith\tj@lab.org\tLEI\texperimenter\tJo Smith")
        self.assertEqual(resp.status_code, 200)
        result = resp.json()["result"]
        self.assertEqual(len(result["created"]), 1)

        au = User.objects.using(ACADEMY).get(username="jsmith")
        pu = User.objects.using(DB).get(username="jsmith")
        m = Member.objects.using(DB).get(user_id=pu.pk)
        self.assertEqual(m.site.short_code, "LEI")
        self.assertEqual(m.role, "experimenter")
        self.assertFalse(pu.has_usable_password(), "the pipeline row never authenticates")

        pw = result["passwords"][0]["password"]
        self.assertIsNotNone(authenticate(username="jsmith", password=pw))
        self.assertTrue(au.is_active)

    def test_an_existing_login_is_linked_and_its_password_left_alone(self):
        """Somebody who already has an academy account — a course learner, say —
        must not have their password reset by being given pipeline access."""
        au = User(username="known", email="k@lab.org")
        au.set_password("their-own-password")
        au.save(using=ACADEMY)

        self.assertIn("a login already exists",
                      self._check("known\t\t\tk@lab.org\tLEI\texperimenter\t").json()
                      ["items"][0]["note"])
        result = self._commit("known\t\t\tk@lab.org\tLEI\texperimenter\t").json()["result"]
        self.assertEqual(result["passwords"], [], "no new password was minted")
        self.assertIsNotNone(authenticate(username="known", password="their-own-password"))

    def test_a_bad_site_is_refused_by_name_with_the_ones_on_file(self):
        item = self._check("x\t\t\t\tAtlantis\texperimenter\t").json()["items"][0]
        self.assertEqual(item["status"], "blocked")
        self.assertIn("Leicester", item["note"])
        self.assertIn("McGill", item["note"])

    def test_a_bad_role_names_the_roles(self):
        item = self._check("x\t\t\t\tLEI\twizard\t").json()["items"][0]
        self.assertEqual(item["status"], "blocked")
        self.assertIn("experimenter", item["note"])

    def test_a_typed_password_is_the_one_that_works(self):
        """The job this page exists for: create somebody and read them out a
        password that signs them in."""
        import json
        resp = self.client.post(
            "/pipeline/users/board/commit/",
            data=json.dumps({"text": f"{self.HEADER}\tpassword\n"
                                     "newbie\tNew\tBie\tn@lab.org\tLEI\t"
                                     "experimenter\tNew Bie\tChosenPass2026"}),
            content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        result = resp.json()["result"]
        self.assertEqual(result["passwords"][0]["password"], "ChosenPass2026")
        self.assertIsNotNone(authenticate(username="newbie", password="ChosenPass2026"))
        # …and all three records, so they get past the pipeline gate too.
        pu = User.objects.using(DB).get(username="newbie")
        self.assertTrue(Member.objects.using(DB).filter(user_id=pu.pk,
                                                        is_active=True).exists())

    def test_a_short_typed_password_is_blocked_at_the_check_not_the_save(self):
        import json
        d = self.client.post(
            "/pipeline/users/board/check/",
            data=json.dumps({"text": f"{self.HEADER}\tpassword\n"
                                     "short\t\t\t\tLEI\texperimenter\t\tabc"}),
            content_type="application/json").json()
        self.assertEqual(d["items"][0]["status"], "blocked")
        self.assertIn("at least 8 characters", d["items"][0]["note"])

    def test_the_check_writes_nothing(self):
        self._check("nobody\t\t\t\tLEI\texperimenter\t")
        self.assertFalse(User.objects.using(DB).filter(username="nobody").exists())
        self.assertFalse(User.objects.using(ACADEMY).filter(username="nobody").exists())


class OneWritePathForBothDoorsTests(TestCase):
    """The command and the board must not grow two implementations of "make a
    login work" — that is precisely how the three rows drifted apart."""

    databases = {DB, ACADEMY}

    def test_the_command_calls_the_service(self):
        from pathlib import Path
        from django.conf import settings
        src = (Path(settings.BASE_DIR)
               / "pipeline/management/commands/pipeline_users.py").read_text()
        self.assertIn("services import members", src.replace("pipeline.", ""))
        self.assertIn("member_svc.grant", src)

    def test_the_service_creates_the_same_three_rows_the_command_documents(self):
        site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        out = member_svc.grant([{"username": "direct", "site": "LEI",
                                 "role": "experimenter", "email": "d@lab.org"}])
        self.assertEqual(len(out["created"]), 1)
        pu = User.objects.using(DB).get(username="direct")
        self.assertTrue(Member.objects.using(DB).filter(user_id=pu.pk).exists())
        self.assertTrue(User.objects.using(ACADEMY).filter(username="direct").exists())
        self.assertEqual(Member.objects.using(DB).get(user_id=pu.pk).site_id, site.pk)
