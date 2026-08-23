"""Tests for the in-app AI assistant + certificate verification/QR additions.

No live Claude call is made — the network turn is patched. These prove the
Django wiring: hub renders, both chat modes render, the API enforces the daily
cap and degrades gracefully with no key, the public verify page works, and the
certificate PDF carries a verification code.
"""
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from academy.models import (
    AIChatUsage, Certificate, Lesson, LessonProgress, LessonSection, SectionProgress,
)

User = get_user_model()


class AssistantAndCertTests(TestCase):
    databases = "__all__"

    def setUp(self):
        self.user = User.objects.create_user(
            username="dr_smith", password="pw12345", first_name="Sam", last_name="Smith")
        self.lesson = Lesson.objects.create(title="Validation basics", slug="vb", order=1,
                                            content="<p>x</p>")

    # --- hub + chat pages --------------------------------------------------
    def test_the_other_tools_are_still_offered(self):
        # The page leads with the modules now (see AcademyHomeTests below), but
        # neither of the other two tools was retired in the process — they moved
        # down the page, they did not go. The AI tutor and the AI database search
        # really were retired, in favour of these two.
        self.client.force_login(self.user)
        r = self.client.get(reverse("academy:academy_home"))
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn("Antibody Selection Tool", body)
        self.assertIn("Check a paper with your AI", body)
        # "Existing e-learning" was the modules' own heading, and the word made
        # the Academy read as the leftover option beside two prototypes.
        self.assertNotIn("Existing e-learning", body)

    def test_hub_does_not_vary_with_the_retired_tutor_switch(self):
        # AI_TUTOR_OFFLINE is still live, and still governs the assistant page
        # and the API — see the maintenance-switch tests below. What it no
        # longer touches is this page, because the card it used to disable is
        # gone. This pins that: the flag having a visible effect here again
        # would mean a tutor card had come back without anyone deciding to.
        self.client.force_login(self.user)
        with override_settings(AI_TUTOR_OFFLINE=True):
            offline = self.client.get(reverse("academy:academy_home")).content.decode()
        with override_settings(AI_TUTOR_OFFLINE=False):
            online = self.client.get(reverse("academy:academy_home")).content.decode()
        self.assertEqual(offline, online)
        # Two identical redirects would also compare equal, so check the page
        # actually rendered before reading anything into the equality.
        self.assertIn("The modules", offline)
        self.assertNotIn("Offline for improvements", offline)

    def test_chat_pages_render_both_modes(self):
        self.client.force_login(self.user)
        for mode in ("elearning", "database"):
            r = self.client.get(reverse("academy:assistant", args=[mode]))
            self.assertEqual(r.status_code, 200, mode)

    def test_chat_page_bad_mode_404(self):
        self.client.force_login(self.user)
        r = self.client.get(reverse("academy:assistant", args=["nonsense"]))
        self.assertEqual(r.status_code, 404)

    def test_chat_requires_login(self):
        r = self.client.get(reverse("academy:assistant", args=["database"]))
        self.assertEqual(r.status_code, 302)  # -> login

    # --- API: config + quota ----------------------------------------------
    @override_settings(ANTHROPIC_API_KEY="")
    def test_api_not_configured(self):
        self.client.force_login(self.user)
        r = self.client.post(reverse("academy:assistant_api"),
                             data={"mode": "database", "messages": [{"role": "user", "content": "hi"}]},
                             content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["error"], "not_configured")
        # A blocked-before-call request must NOT consume the daily quota.
        self.assertFalse(AIChatUsage.objects.filter(user=self.user).exists())

    @override_settings(ANTHROPIC_API_KEY="test-key", ACADEMY_AI_DAILY_MESSAGE_CAP=2)
    def test_api_quota_and_success(self):
        self.client.force_login(self.user)
        fake = {"ok": True, "reply": "Here you go.", "usage": {}}
        with mock.patch("academy.views.assistant.run_turn", return_value=fake) as m:
            body = {"mode": "database", "messages": [{"role": "user", "content": "q"}]}
            r1 = self.client.post(reverse("academy:assistant_api"), data=body,
                                  content_type="application/json")
            r2 = self.client.post(reverse("academy:assistant_api"), data=body,
                                  content_type="application/json")
            r3 = self.client.post(reverse("academy:assistant_api"), data=body,
                                  content_type="application/json")
        self.assertEqual(r1.json()["reply"], "Here you go.")
        self.assertEqual(r1.json()["remaining"], 1)
        self.assertEqual(r2.json()["remaining"], 0)
        # Third call is over the cap of 2 — blocked, points to BYO AI.
        self.assertEqual(r3.json()["error"], "limit")
        self.assertIn("your own AI", r3.json()["reply"])
        self.assertEqual(m.call_count, 2)  # third never reached the model

    @override_settings(ANTHROPIC_API_KEY="test-key")
    def test_request_caches_system_prefix(self):
        from academy import assistant
        captured = {}

        class FakeResp:
            status_code = 200
            def json(self):
                return {"content": [{"type": "text", "text": "hi"}],
                        "stop_reason": "end_turn",
                        "usage": {"input_tokens": 10, "output_tokens": 5,
                                  "cache_read_input_tokens": 0,
                                  "cache_creation_input_tokens": 8}}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["json"] = json
            return FakeResp()

        with mock.patch("academy.assistant.requests.post", side_effect=fake_post):
            res = assistant.run_turn("database", [{"role": "user", "content": "hi"}])

        self.assertTrue(res["ok"])
        body = captured["json"]
        # system is sent as a content-block list carrying the cache breakpoint,
        # which caches the tools too (render order tools -> system).
        self.assertIsInstance(body["system"], list)
        self.assertEqual(body["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertTrue(body["tools"])
        self.assertEqual(body["model"], "claude-haiku-4-5")
        # cache token counts surface through to the caller
        self.assertIn("cache_creation_input_tokens", res["usage"])

    @override_settings(ANTHROPIC_API_KEY="test-key")
    def test_api_bad_mode(self):
        self.client.force_login(self.user)
        r = self.client.post(reverse("academy:assistant_api"),
                             data={"mode": "hacking", "messages": []},
                             content_type="application/json")
        self.assertEqual(r.status_code, 400)

    # --- AI tutor maintenance switch (elearning only) ----------------------
    @override_settings(ANTHROPIC_API_KEY="test-key", AI_TUTOR_OFFLINE=True)
    def test_tutor_offline_page_shows_banner_and_hides_chat(self):
        self.client.force_login(self.user)
        r = self.client.get(reverse("academy:assistant", args=["elearning"]))
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn("offline for improvements", body)
        self.assertNotIn('id="chatInput"', body)  # input not rendered → can't chat

    @override_settings(ANTHROPIC_API_KEY="test-key", AI_TUTOR_OFFLINE=True)
    def test_tutor_offline_api_blocks_without_consuming_quota(self):
        self.client.force_login(self.user)
        with mock.patch("academy.views.assistant.run_turn") as m:
            r = self.client.post(reverse("academy:assistant_api"),
                                 data={"mode": "elearning",
                                       "messages": [{"role": "user", "content": "hi"}]},
                                 content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["error"], "tutor_offline")
        self.assertEqual(m.call_count, 0)  # model never reached
        self.assertFalse(AIChatUsage.objects.filter(user=self.user).exists())

    @override_settings(ANTHROPIC_API_KEY="test-key", AI_TUTOR_OFFLINE=True)
    def test_database_search_still_works_when_tutor_offline(self):
        self.client.force_login(self.user)
        fake = {"ok": True, "reply": "Here you go.", "usage": {}}
        with mock.patch("academy.views.assistant.run_turn", return_value=fake) as m:
            r = self.client.post(reverse("academy:assistant_api"),
                                 data={"mode": "database",
                                       "messages": [{"role": "user", "content": "q"}]},
                                 content_type="application/json")
        self.assertTrue(r.json()["ok"])
        self.assertEqual(m.call_count, 1)  # database mode is unaffected

    @override_settings(ANTHROPIC_API_KEY="test-key", AI_TUTOR_OFFLINE=False)
    def test_tutor_back_online_renders_chat(self):
        self.client.force_login(self.user)
        r = self.client.get(reverse("academy:assistant", args=["elearning"]))
        self.assertEqual(r.status_code, 200)
        self.assertIn('id="chatInput"', r.content.decode())

    # --- certificate verification + QR ------------------------------------
    def test_certificate_gets_code_and_verifies(self):
        cert = Certificate.objects.create(user=self.user, lesson=self.lesson, score=90.0)
        self.assertIsNotNone(cert.verification_code)  # model default
        r = self.client.get(reverse("academy:certificate_verify",
                                    args=[cert.verification_code]))  # public, no login
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Certificate verified")
        self.assertContains(r, "Validation basics")

    def test_verify_unknown_code(self):
        import uuid
        r = self.client.get(reverse("academy:certificate_verify", args=[uuid.uuid4()]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "not found")

    def test_certificate_pdf_has_code(self):
        cert = Certificate.objects.create(user=self.user, lesson=self.lesson, score=90.0)
        self.client.force_login(self.user)
        r = self.client.get(reverse("academy:generate_pdf", args=[cert.id]))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/pdf")
        self.assertTrue(r.content.startswith(b"%PDF"))


class AcademyHomeTests(TestCase):
    """The first page after signing in leads with the modules.

    It used to draw three cards of equal size — the modules, the Selection Tool
    and bring-your-own-AI — so the page answered "what am I here for?" three
    ways at once, and the modules' card was titled *Existing e-learning* beside
    two badged *Prototype*. What is pinned here is the half that would be wrong
    silently rather than loudly: the panel naming a module the learner has
    already passed, a button telling somebody where they are and getting it
    wrong, and a certificate offered for a module that has none.
    """
    databases = "__all__"

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="learner", password="pw12345")
        cls.modules = []
        for order in (1, 2):
            lesson = Lesson.objects.create(title=f"Module {order} title",
                                           slug=f"m{order}", order=order, content="<p>x</p>")
            for n in (1, 2):
                LessonSection.objects.create(lesson=lesson, title=f"s{n}", order=n)
            cls.modules.append(lesson)

    def get(self):
        self.client.force_login(self.user)
        r = self.client.get(reverse("academy:academy_home"))
        self.assertEqual(r.status_code, 200)
        return r

    def test_a_fresh_learner_is_pointed_at_the_first_module(self):
        r = self.get()
        self.assertEqual(r.context["next_up"]["lesson"], self.modules[0])
        self.assertFalse(r.context["next_up"]["started"])
        self.assertIn("Start this module", r.content.decode())

    def test_a_part_read_module_is_continued_not_started(self):
        SectionProgress.objects.create(user=self.user,
                                       section=self.modules[0].sections.first())
        r = self.get()
        item = r.context["progress_data"][0]
        self.assertTrue(item["started"])
        self.assertFalse(item["completed"])
        self.assertEqual(item["progress"], 50)          # 1 of 2 sections
        self.assertIn("Continue this module", r.content.decode())

    def test_a_passed_module_is_not_offered_as_the_next_step(self):
        LessonProgress.objects.create(user=self.user, lesson=self.modules[0], completed=True)
        r = self.get()
        # The panel must move on, or it sends somebody back through work they
        # have finished — and the count above it would disagree with the list.
        self.assertEqual(r.context["next_up"]["lesson"], self.modules[1])
        self.assertEqual(r.context["completed_total"], 1)
        self.assertEqual(r.context["module_total"], 2)
        self.assertEqual(r.context["progress_data"][0]["progress"], 100)

    def test_every_module_passed_says_so_rather_than_naming_another(self):
        for lesson in self.modules:
            LessonProgress.objects.create(user=self.user, lesson=lesson, completed=True)
        r = self.get()
        self.assertIsNone(r.context["next_up"])
        self.assertIn("passed every module", r.content.decode())

    def test_a_certificate_is_reachable_from_the_module_that_earned_it(self):
        # Before this the only route to a certificate was the page shown right
        # after the quiz; leaving it made the PDF unreachable from the whole site.
        cert = Certificate.objects.create(user=self.user, lesson=self.modules[0], score=90.0)
        r = self.get()
        self.assertEqual(r.context["progress_data"][0]["certificate"], cert)
        self.assertIsNone(r.context["progress_data"][1]["certificate"])
        self.assertIn(reverse("academy:certificate", args=[cert.id]), r.content.decode())

    def test_another_learners_certificate_is_not_offered(self):
        other = User.objects.create_user(username="someone_else", password="pw12345")
        Certificate.objects.create(user=other, lesson=self.modules[0], score=90.0)
        r = self.get()
        self.assertIsNone(r.context["progress_data"][0]["certificate"])

    def test_no_modules_published_draws_no_next_step(self):
        Lesson.objects.all().delete()
        r = self.get()
        self.assertIsNone(r.context["next_up"])
        self.assertEqual(r.context["module_total"], 0)
        # "0 of 0 modules complete" over an empty list is a count with nothing
        # behind it; the page says there are none instead.
        body = r.content.decode()
        self.assertIn("No modules are published yet", body)
        self.assertNotIn("of 0 module", body)


class SlimAntibodyTests(TestCase):
    """_slim_antibody passes the portal/MCP record through VERBATIM except for the
    bulky media keys — so every field (incl. metadata.product_link) is preserved
    exactly, and nothing can be silently renamed/dropped."""
    databases = "__all__"

    def test_slim_is_verbatim_minus_bulky_keys(self):
        from academy.assistant import _slim_antibody
        # Shape mirrors _serialise_antibody exactly.
        serialised = {
            "antibody_name": "105008",
            "gene": "SYT1",
            "gene_page_url": "https://onlygoodantibodies.co.uk/antibodies/SYT1/",
            "metadata": {
                "rrid": "AB_887835",
                "supplier": "Synaptic Systems",
                "product_link": "https://www.sysy.com/product/105008",
                "clone_id": None, "clonality": "polyclonal",
            },
            "recommendations": {"WB": True, "ICC-IF": True, "IP": False, "FC": False},
            "assessment": {"WB": {"status": "recommended"}},
            "summary": "105008 against SYT1 is in the OGA dataset.",
            "provenance": {"rrid": "AB_887835"},
            "experiments": [{"image_url": "https://big/img.png"}],   # bulky -> dropped
            "embed_urls": {"all": "https://…", "WB": "https://…"},   # bulky -> dropped
        }
        slim = _slim_antibody(serialised)
        # keys the MCP server returns are preserved verbatim
        self.assertEqual(slim["antibody_name"], "105008")
        self.assertEqual(slim["metadata"]["rrid"], "AB_887835")
        self.assertEqual(slim["metadata"]["product_link"],
                         "https://www.sysy.com/product/105008")   # the URL survives
        self.assertEqual(slim["recommendations"]["WB"], True)
        self.assertEqual(slim["gene"], "SYT1")
        # only the bulky media keys are dropped
        self.assertNotIn("experiments", slim)
        self.assertNotIn("embed_urls", slim)

    def test_slim_handles_missing_product_url(self):
        from academy.assistant import _slim_antibody
        slim = _slim_antibody({"antibody_name": "X1", "gene": "MAPT", "metadata": {}})
        self.assertEqual(slim["antibody_name"], "X1")
        self.assertIsNone(slim["metadata"].get("product_link"))   # blank -> absent, no crash


class SharedGuidanceTests(TestCase):
    """The tutoring/data behaviour is single-sourced in tutor_guidance and used by
    BOTH the hosted chat prompts AND the MCP server instructions — so they can't
    drift apart."""

    def test_hosted_prompts_use_shared_guidance(self):
        from academy import assistant
        from mcp_servers.common import tutor_guidance
        self.assertIn(tutor_guidance.TUTOR_STYLE, assistant._SYSTEM["elearning"])
        self.assertIn(tutor_guidance.DATA_STYLE, assistant._SYSTEM["database"])

    def test_server_instructions_use_shared_guidance(self):
        from mcp_servers.common import tutor_guidance
        self.assertIn(tutor_guidance.TUTOR_STYLE, tutor_guidance.SERVER_INSTRUCTIONS)
        self.assertIn(tutor_guidance.DATA_STYLE, tutor_guidance.SERVER_INSTRUCTIONS)
        # the lessons we care about are actually present
        for phrase in ("ONE clear question", "INSTITUTIONAL email", "FAST-TRACK",
                       "TEACH ONLY THE GAPS", "not a verdict on quality"):
            self.assertIn(phrase, tutor_guidance.SERVER_INSTRUCTIONS)


class QuizGradingTests(TestCase):
    """Quiz grading must be deterministic and accept plain letters, so the model
    never has to do letter->index arithmetic (the cause of a real mis-grade where
    a perfect Framework score was reported as 50%)."""

    def test_letters_and_indices_grade_equivalently(self):
        from mcp_servers.common import content_pack as cp
        q = cp._quizzes()["modules"]["framework"]
        idx = [x["correct"] for x in q["questions"]]
        letters = [chr(ord("a") + i) for i in idx]
        self.assertEqual(cp.grade_module_quiz("framework", letters)["score"], 1.0)
        self.assertEqual(cp.grade_module_quiz("framework", idx)["score"], 1.0)
        # case-insensitive
        self.assertEqual(
            cp.grade_module_quiz("framework", [s.upper() for s in letters])["score"], 1.0)

    def test_regression_bcbb_is_full_marks(self):
        # The exact transcript answers that were wrongly graded 50%.
        from mcp_servers.common import content_pack as cp
        idx = [x["correct"] for x in cp._quizzes()["modules"]["framework"]["questions"]]
        if [chr(ord("a") + i) for i in idx] == ["b", "c", "b", "b"]:
            r = cp.grade_module_quiz("framework", ["b", "c", "b", "b"])
            self.assertTrue(r["passed"])
            self.assertEqual(r["score"], 1.0)


class PublicPagesRenderTests(TestCase):
    """The public pages I edited must still render (a broken {% url %} would 500)."""
    databases = "__all__"

    def test_edited_public_pages_render(self):
        for name in ("home", "tools_hub", "champions", "roadmap",
                     "roadmap_institutions", "roadmap_funders", "connect_your_ai"):
            r = self.client.get(reverse(name))
            self.assertEqual(r.status_code, 200, f"{name} did not render")
