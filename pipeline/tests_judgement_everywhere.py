"""One judgement, three screens, and a public page that shows it.

Pinned here are the silent halves of 25 Sep 2026's change:

* a release or a withdrawal that the edge goes on hiding for a week — the purge
  must run, and only once the write has committed;
* flow's non-specific background caveat landing where it should: only on a
  recommended antibody, and on the public wording when it is set;
* the cropper judging nothing, and a re-crop never moving a verdict;
* the retired Set recommendations URL still landing somewhere that works.

Not pinned: any page's layout or wording beyond the public sentence, which is
the one string a reader outside the lab sees.
"""
from __future__ import annotations

import io
import json
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from PIL import Image

from core import recommendations as recs
from OGA_website import edge_cache
from pipeline.models import (Antibody, AntibodyOutcome, Company, CropperImage,
                             CropperSession, Member, PendingPublicationImage,
                             PublicationImage, Site, Target)
from pipeline.services import outcomes as outcome_svc
from pipeline.services import review as review_svc
from pipeline.services.cropper import commit as commit_mod
from pipeline.tests_timeouts import DB, _member_client


def _png():
    buf = io.BytesIO()
    Image.new("RGB", (60, 60), (5, 5, 5)).save(buf, "PNG")
    return buf.getvalue()


class _Base(TestCase):
    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.site)
        self.member = Member.objects.using(DB).get(site_id=self.site.pk)
        self.company = Company.objects.using(DB).create(name="Abcam")
        self.target = Target.objects.using(DB).create(gene_name="TMEFF2")
        self.ab = Antibody.objects.using(DB).create(
            target=self.target, company=self.company,
            catalogue_number="ab1", site=self.site)

    def _superuser(self):
        for alias in ("academy_db", DB):
            u = User(username="root", is_superuser=True, is_staff=True)
            u.set_password("pw")
            u.save(using=alias)
        Member.objects.using(DB).create(
            user_id=User.objects.using(DB).get(username="root").pk,
            site_id=self.site.pk, role="admin", is_active=True)
        c = Client()
        self.assertTrue(c.login(username="root", password="pw"))
        return c

    def _post(self, client, url, body):
        return client.post(url, data=json.dumps(body),
                           content_type="application/json")


class ReleasingClearsTheEdgeTests(_Base):
    """The home page kept its old counts after TMEFF2 went public: the edge
    holds public pages for a week. So releasing purges — after the commit, or
    the edge refills with the page from before it."""

    def test_release_purges_after_the_figures_are_public(self):
        item = review_svc.stage(antibody=self.ab, application_type="WB",
                                content=_png(), filename="TMEFF2_ab1_WB.png",
                                recommended=True, staged_by="x")
        seen = {}

        def fake_purge():
            seen["public_at_purge"] = PublicationImage.objects.using(DB).count()
            return True, "cleared"

        with mock.patch.object(edge_cache, "purge_public_pages",
                               side_effect=fake_purge) as purge:
            resp = self._post(self._superuser(), "/pipeline/review/release/",
                              {"ids": [item.pk], "count": 1})
        self.assertEqual(resp.status_code, 200, resp.content)
        purge.assert_called_once()
        self.assertEqual(seen["public_at_purge"], 1,
                         "purged before the release was written")
        self.assertEqual(resp.json()["purge_note"], "cleared")

    def test_withdraw_purges_too(self):
        PublicationImage.objects.using(DB).create(
            antibody=self.ab, application_type="WB",
            image=ContentFile(_png(), name="TMEFF2_ab1_WB.png"))
        with mock.patch.object(edge_cache, "purge_public_pages",
                               return_value=(True, "cleared")) as purge:
            resp = self._post(self._superuser(), "/pipeline/outcomes/withdraw/",
                              {"gene": "TMEFF2", "count": 1})
        self.assertEqual(resp.status_code, 200, resp.content)
        purge.assert_called_once()
        self.assertEqual(PublicationImage.objects.using(DB).count(), 0)

    def test_an_unconfigured_purge_says_so_and_calls_nothing(self):
        with mock.patch.object(edge_cache, "_under_test", return_value=False), \
                mock.patch.dict("os.environ", {"CLOUDFLARE_ZONE_ID": "",
                                               "CLOUDFLARE_PURGE_TOKEN": ""}), \
                mock.patch("urllib.request.urlopen") as urlopen:
            done, note = edge_cache.purge_public_pages()
        self.assertFalse(done)
        self.assertIn("up to a week", note)
        urlopen.assert_not_called()

    def test_a_release_forgets_the_site_s_own_search_lists(self):
        """PARP1 went public and the search box said "No match for parp" for
        an hour, because the gene list was cached in Django, not Cloudflare —
        and the unconfigured purge above is the case it has to hold in."""
        from django.core.cache import cache

        from core import public_search
        from core.extension_index import EXTENSION_INDEX_CACHE_KEY
        from core.views import GENE_SEARCH_INDEX_CACHE_KEY

        keys = [GENE_SEARCH_INDEX_CACHE_KEY, public_search.CACHE_KEY,
                EXTENSION_INDEX_CACHE_KEY]
        cache.set_many({k: ["stale"] for k in keys})
        with mock.patch.dict("os.environ", {"CLOUDFLARE_ZONE_ID": "",
                                            "CLOUDFLARE_PURGE_TOKEN": ""}):
            edge_cache.purge_public_pages()
        self.assertEqual(cache.get_many(keys), {})

    def test_a_release_gives_the_gene_list_a_new_address(self):
        """Emptying the server's copy is half of it: a browser keeps the list
        for an hour, so the address it is fetched from has to change, on the
        home page's box and on the header's one alike."""
        import re

        def version(path):
            html = self.client.get(path).content.decode()
            found = set(re.findall(r'data-genes-url="/api/internal/genes/\?v=(\d+)"', html))
            self.assertEqual(len(found), 1, f"{path} draws no versioned gene list")
            return found.pop()

        before = {p: version(p) for p in ("/", "/about/")}
        edge_cache.purge_public_pages()
        after = {p: version(p) for p in ("/", "/about/")}
        self.assertEqual(before["/"], before["/about/"])
        self.assertNotEqual(before["/"], after["/"])
        self.assertEqual(after["/"], after["/about/"])

    def test_a_release_purges_everything(self):
        """A tag purge was answered `success` by Cloudflare and dropped
        nothing (30 Sep 2026), so the home page went on counting the day
        before two releases. The release purges the zone, as a deploy does."""
        sent = {}

        class _Reply:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(req, timeout):
            sent["body"] = json.loads(req.data)
            return _Reply()

        with mock.patch.object(edge_cache, "_under_test", return_value=False), \
                mock.patch.dict("os.environ", {"CLOUDFLARE_ZONE_ID": "z",
                                               "CLOUDFLARE_PURGE_TOKEN": "t"}), \
                mock.patch("urllib.request.urlopen", side_effect=fake_urlopen), \
                mock.patch("json.load", return_value={"success": True}):
            done, _ = edge_cache.purge_public_pages()
        self.assertTrue(done)
        self.assertEqual(sent["body"], {"purge_everything": True})

    def test_a_test_run_never_purges_the_live_site(self):
        """The cloud sessions carry the real token."""
        with mock.patch.dict("os.environ", {"CLOUDFLARE_ZONE_ID": "z",
                                            "CLOUDFLARE_PURGE_TOKEN": "t"}), \
                mock.patch("urllib.request.urlopen") as urlopen:
            done, _ = edge_cache.purge_public_pages()
        self.assertFalse(done)
        urlopen.assert_not_called()


class FlowsBackgroundCaveatTests(_Base):
    """A caveat on a recommendation: settable only on a recommended antibody,
    and printed on the public page when set."""

    def setUp(self):
        super().setUp()
        PublicationImage.objects.using(DB).create(
            antibody=self.ab, application_type="FC",
            image=SimpleUploadedFile("fc.png", b"x"))

    def _save(self, value):
        return self._post(self.client, "/pipeline/outcomes/save/",
                          {"antibody_id": self.ab.pk, "application": "FC",
                           "axis": "background", "value": value})

    def test_refused_on_an_antibody_that_is_not_recommended(self):
        self.assertEqual(self._save("yes").status_code, 400)
        self.assertFalse(AntibodyOutcome.objects.using(DB).exists())

    def test_set_on_a_recommended_antibody_reaches_the_public_wording(self):
        self.ab.fc_recommended = True
        self.ab.save(using=DB)
        resp = self._save("yes")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["public_sentence"],
                         "Supportive — with non-specific background")
        axes = recs.capability_axes([self.ab.pk])[(self.ab.pk, "FC")]
        public = recs.describe(self.ab, "FC", {"FC"}, True, axes)
        self.assertEqual(public["sentence"],
                         "Supportive — with non-specific background")
        # The extension draws it from a code; one it cannot read draws nothing.
        self.assertEqual(recs.qualifier_code("FC", recs.RECOMMENDED, axes), "nb")

    def test_the_fast_pass_lists_only_recommended_flow_figures(self):
        other = Antibody.objects.using(DB).create(
            target=self.target, company=self.company, catalogue_number="ab2",
            site=self.site)
        PublicationImage.objects.using(DB).create(
            antibody=other, application_type="FC",
            image=SimpleUploadedFile("fc2.png", b"x"))
        self.ab.fc_recommended = True
        self.ab.save(using=DB)
        data = self.client.get("/pipeline/outcomes/antibodies/",
                               {"fcreview": 1, "app": "FC"}).json()
        self.assertEqual([r["name"] for r in data["antibodies"]], ["ab1"])

    def test_the_review_queue_holds_the_same_rule(self):
        item = review_svc.stage(antibody=self.ab, application_type="FC",
                                content=_png(), filename="TMEFF2_ab1_FC.png",
                                recommended=False, staged_by="x")
        body = {"ids": [item.pk], "axis": "background", "value": "yes"}
        self.assertEqual(self._post(self.client, "/pipeline/review/judge/",
                                    body).status_code, 400)
        review_svc.set_recommended([item], True)
        self.assertEqual(self._post(self.client, "/pipeline/review/judge/",
                                    body).status_code, 200)


class TheCropperMakesNoJudgementTests(_Base):
    """The cropper stages figures and judges nothing (owner, 25 Sep 2026). The
    silent risk is the verdict: `release` writes the staged row's value onto the
    antibody in both directions, so a crop that defaulted to "not recommended"
    would un-recommend an antibody at release with nobody having decided it."""

    def _session(self):
        session = CropperSession.objects.using(DB).create(
            owner_username="member", gene="TMEFF2", cell_line="HAP1",
            antibody_list="ab1", metadata={"ab1": {"company": "Abcam"}},
            # An old tick from before the change — must not be read.
            recommended={"ab1": {"WB": True}})
        image = CropperImage(
            session=session, application_type="WB", name="fig.png",
            nat_w=60, nat_h=60,
            grid={"bounds": {"top": 0, "bottom": 60}, "hLines": [],
                  "bandLeft": [0], "bandRight": [60], "vLines": [[]]},
            mapping={"0_0": {"assigned": True, "ab": 0}})
        image.image.save("fig.png", ContentFile(_png()), save=False)
        image.save(using=DB)
        return session

    def _staged(self):
        return PendingPublicationImage.objects.using(DB).get(antibody=self.ab)

    def test_a_new_crop_starts_from_the_antibody_s_own_verdict(self):
        commit_mod.apply(self._session(), actor_member=self.member)
        self.assertFalse(self._staged().recommended)
        self.assertFalse(AntibodyOutcome.objects.using(DB).exists())

    def test_re_cropping_keeps_the_meeting_s_verdict(self):
        session = self._session()
        commit_mod.apply(session, actor_member=self.member)
        review_svc.set_recommended([self._staged()], True)
        commit_mod.apply(session, actor_member=self.member)
        self.assertTrue(self._staged().recommended)

    def test_re_cropping_a_recommended_antibody_does_not_un_recommend_it(self):
        self.ab.wb_recommended = True
        self.ab.save(using=DB)
        commit_mod.apply(self._session(), actor_member=self.member)
        review_svc.release([self._staged()], actor="x")
        self.ab.refresh_from_db()
        self.assertTrue(self.ab.wb_recommended)


class TheCropperListsTheGenesAntibodiesTests(_Base):
    def test_one_entry_per_catalogue_with_its_supplier(self):
        other = Site.objects.using(DB).create(name="McGill", short_code="MCG")
        Antibody.objects.using(DB).create(
            target=self.target, company=self.company, catalogue_number="ab1",
            site=other)          # the same product at a second bench
        data = self.client.get("/pipeline/cropper/gene-status/",
                               {"gene": "TMEFF2"}).json()
        self.assertEqual([a["catalogue"] for a in data["antibodies"]], ["ab1"])
        self.assertEqual(data["antibodies"][0]["company"], "Abcam")


class SetRecommendationsRetiredTests(_Base):
    def test_the_old_address_lands_on_judge_outcomes_with_the_gene(self):
        resp = self.client.get("/pipeline/recommendations/", {"gene": "TMEFF2"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], "/pipeline/outcomes/?gene=TMEFF2")
