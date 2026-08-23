"""Adding a gene: the site is chosen, and a UniProt ID is a way in.

Two things a real user asked for. Carl Laflamme, uOttawa:

* *"McGill is included by default when adding a new target entry (ideally there
  would be a drop down menu with McGill, Leicester, uOttawa, UBC, Cornell,
  etc)"* — it was not McGill, it was whatever his account said, reported in a
  note *after* the write. The same fact arriving too late to be a decision.
* *"Can we add a target by simply indicating the Uniprot ID?"* — no, because the
  one search box only ever called `lookup_gene`, which searches `gene:P37840`
  and finds nothing.

And from the owner's review: *"An excel sheet to upload a one column list is not
needed."*
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import TestCase

from pipeline.models import (GrantingAgency, Project, Site, Target,
                             TargetNomination)
from pipeline.services import bulk_targets, uniprot
from pipeline.tests_timeouts import DB, _member_client


def _uniprot_says(*, found=True, unavailable=False, gene_name="",
                  protein_name="", uniprot_id="", mass_kda=None, synonyms=(),
                  error=None):
    """Patch what UniProt answers, for a test that is about something else.

    A definite answer is simulated here, where the caller reads it. An
    **outage** is the case that must not be faked this way: `lookup_gene`
    catches its own `RequestException` and returns, so a fixture that raises
    exercises a branch the real path cannot reach — patch `requests` for that,
    or set `unavailable` to the value the real function would have returned.
    """
    return mock.patch.object(uniprot, "lookup_gene", return_value={
        "found": found, "unavailable": unavailable, "gene_name": gene_name,
        "protein_name": protein_name, "uniprot_id": uniprot_id,
        "mass_kda": mass_kda, "gene_synonyms": list(synonyms), "error": error})


class AUniProtIdIsAWayInTests(TestCase):
    def test_an_accession_is_told_from_a_symbol(self):
        for accession in ("P37840", "Q9Y6K9", "A0A0B4J2F0", "p37840"):
            self.assertTrue(uniprot.looks_like_accession(accession), accession)
        # No HGNC symbol has a digit in the second position, which is what makes
        # the two namespaces safe to tell apart on shape alone.
        for symbol in ("SNCA", "TP53", "ELP3", "SEC61B", "NA", "STMN2"):
            self.assertFalse(uniprot.looks_like_accession(symbol), symbol)

    def test_lookup_routes_on_what_was_typed(self):
        with mock.patch.object(uniprot, "lookup_accession",
                               return_value={"found": True}) as by_acc, \
             mock.patch.object(uniprot, "lookup_gene",
                               return_value={"found": True}) as by_gene:
            self.assertEqual(uniprot.lookup("P37840")["resolved_from"], "accession")
            self.assertEqual(uniprot.lookup("SNCA")["resolved_from"], "gene")
        by_acc.assert_called_once_with("P37840")
        by_gene.assert_called_once_with("SNCA")

    def test_an_accession_answers_with_the_symbol(self):
        """`lookup_accession` returned no `gene_name` at all, for anybody.

        `lookup` promises one shape whichever way in you took, and every local
        check downstream is keyed on gene symbol — so *"the local checks follow
        the symbol, not the string that was typed"* simply never fired for an
        accession, and adding by one created a Target whose **gene name** was
        `P37840`. The field was already being asked of the API and thrown away.
        The suite could not see it because the one test that reads the symbol
        back mocks this whole function.
        """
        entry = {
            "primaryAccession": "P37840",
            "proteinDescription": {
                "recommendedName": {"fullName": {"value": "Alpha-synuclein"}}},
            "genes": [{"geneName": {"value": "SNCA"},
                       "synonyms": [{"value": "PARK1"}, {"value": "NACP"}]}],
            "sequence": {"molWeight": 14460},
        }
        with mock.patch.object(uniprot.requests, "get") as get:
            get.return_value = mock.Mock(**{"json.return_value": entry,
                                            "raise_for_status.return_value": None})
            out = uniprot.lookup_accession("P37840")
        self.assertTrue(out["found"])
        self.assertEqual(out["gene_name"], "SNCA")
        self.assertEqual(out["gene_synonyms"], ["PARK1", "NACP"])


class TheLocalChecksFollowTheSymbolTests(TestCase):
    """DepMap, proteomics, Horizon and this pipeline are all keyed on symbol, so
    an accession has to be resolved before any of them is asked — otherwise a
    lookup by P37840 reports, accurately and uselessly, that nothing called
    P37840 is on file."""

    databases = {"default", DB, "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.site)
        Target.objects.using(DB).create(gene_name="SNCA", protein_name="Syn")

    def test_an_accession_finds_the_gene_already_on_file(self):
        with mock.patch.object(uniprot, "lookup_accession", return_value={
                "found": True, "gene_name": "SNCA", "uniprot_id": "P37840",
                "protein_name": "Alpha-synuclein", "gene_synonyms": []}):
            data = self.client.get("/pipeline/feasibility/lookup/",
                                   {"gene": "P37840"}).json()
        self.assertEqual(data["resolved_gene"], "SNCA")
        self.assertEqual(data["resolved_from"], "accession")
        self.assertTrue(data["pipeline_status"]["exists"],
                        "the accession did not reach the gene already on file")


class TheSiteIsChosenTests(TestCase):
    databases = {"default", DB, "academy_db"}

    def setUp(self):
        self.mine = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.theirs = Site.objects.using(DB).create(name="uOttawa", short_code="UOT")
        self.client = _member_client(self, self.mine)
        self.agency = GrantingAgency.objects.using(DB).create(name="CIHR")
        self.project = Project.objects.using(DB).create(name="AMP-AD", is_active=True)

    def _add(self, **extra):
        body = {"gene_name": "SNCA", "protein_name": "Alpha-synuclein"}
        body.update(extra)
        # The press confirms the gene against UniProt now, so a test about
        # *sites* has to say what UniProt answered — otherwise it is really a
        # test about dev's blocked network, and it would refuse the add for a
        # reason it never meant to exercise.
        with _uniprot_says(gene_name="SNCA", protein_name="Alpha-synuclein",
                           uniprot_id="P37840"):
            return self.client.post("/pipeline/feasibility/add/",
                                    data=json.dumps(body),
                                    content_type="application/json")

    def test_the_page_offers_every_active_site_with_yours_selected(self):
        html = self.client.get("/pipeline/feasibility/").content.decode()
        self.assertIn("uOttawa", html)
        self.assertIn("Leicester", html)
        self.assertIn('id="add-site"', html)

    def test_a_chosen_site_is_the_one_written(self):
        resp = self._add(site_id=self.theirs.pk)
        self.assertEqual(resp.status_code, 200)
        nom = TargetNomination.objects.using(DB).get()
        self.assertEqual(nom.site_id, self.theirs.pk)
        self.assertEqual(resp.json()["site"], "uOttawa")

    def test_no_choice_still_falls_back_to_your_own_site(self):
        self._add()
        self.assertEqual(TargetNomination.objects.using(DB).get().site_id,
                         self.mine.pk)

    def test_a_funder_is_recorded_and_marks_it_funded(self):
        resp = self._add(granting_agency_id=self.agency.pk,
                         project_id=self.project.pk)
        nom = TargetNomination.objects.using(DB).get()
        self.assertEqual(nom.granting_agency_id, self.agency.pk)
        self.assertEqual(nom.project_id, self.project.pk)
        self.assertTrue(nom.funded)
        self.assertIn("funded", resp.json()["nominated_note"])

    def test_no_funder_is_still_the_honest_starting_state(self):
        self._add()
        nom = TargetNomination.objects.using(DB).get()
        self.assertFalse(nom.funded)
        self.assertIsNone(nom.granting_agency_id)

    def test_a_stale_site_id_does_not_lose_the_nomination(self):
        """A pk naming no site is a stale page, not a value.

        Written straight into the FK it is a dangling reference — SQLite lets it
        through and PostgreSQL refuses the INSERT, so on live the nomination
        would vanish into an exception handler and the page would report "No
        site was recorded" about a target it had just created. It falls back to
        the member's own site, which is what the dropdown defaults to anyway.
        """
        resp = self._add(site_id=999999)
        self.assertEqual(resp.status_code, 200)
        nom = TargetNomination.objects.using(DB).get()
        self.assertEqual(nom.site_id, self.mine.pk)
        self.assertEqual(resp.json()["site"], "Leicester")


class TheTwoDoorsToAGeneAgreeTests(TestCase):
    """One screen stated a rule, enforced it in one path, and offered a live
    button that broke it in the other.

    The twelfth field test looked up a deliberately fake `ZZZZZZ`. The verdict
    card was honest — *"No human protein found for gene 'ZZZZZZ'"* — and **Add
    to Pipeline** sat beside it fully enabled, while Bulk Add Targets four
    inches down the same page answered "Nothing to add" and printed *a gene
    UniProt cannot confirm is not added*. The tester declined to press it, which
    is the only reason there is no junk row to clean up: the endpoint behind it
    created whatever was posted to it. This is the one screen where a typo'd
    gene symbol walks straight onto the master list the whole consortium works
    from.
    """

    databases = {"default", DB, "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.site)

    def _add(self, gene="ZZZZZZ"):
        return self.client.post(
            "/pipeline/feasibility/add/",
            data=json.dumps({"gene_name": gene}),
            content_type="application/json")

    def test_a_gene_uniprot_does_not_have_is_not_created(self):
        with _uniprot_says(found=False,
                           error="No human protein found for gene 'ZZZZZZ'"):
            resp = self._add()
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.json()["created"])
        self.assertFalse(Target.objects.using(DB).filter(gene_name="ZZZZZZ").exists(),
                         "a gene UniProt could not confirm reached the master list")
        self.assertFalse(TargetNomination.objects.using(DB).exists())

    def test_the_refusal_names_the_gene_and_the_remedy(self):
        with _uniprot_says(found=False):
            error = self._add().json()["error"]
        self.assertIn("ZZZZZZ", error)
        self.assertIn("Check the spelling", error)

    def test_an_outage_is_not_an_accusation(self):
        """`found=False` is two different answers, and this door had neither.

        "No such gene" is permanent and the reader's to fix; "the request
        failed" is transient and nobody's fault. The bulk door learned that the
        hard way — a blocked proxy reported as *"TRPA1 not in UniProt — check
        the spelling"* about a real gene — and this one must not relearn it.
        """
        with _uniprot_says(found=False, unavailable=True):
            resp = self._add(gene="TRPA1")
        self.assertEqual(resp.status_code, 400)
        error = resp.json()["error"]
        self.assertIn("could not be reached", error)
        self.assertNotIn("spelling", error)
        self.assertFalse(Target.objects.using(DB).exists())

    def test_both_doors_refuse_in_the_same_words(self):
        """The wording is one writer's, or the two doors drift apart again."""
        lookup = {"found": False, "unavailable": False}
        row = {"status": bulk_targets.NOT_FOUND, "gene": "ZZZZZZ"}
        self.assertEqual(bulk_targets.why_not_added(lookup, "ZZZZZZ"),
                         bulk_targets.row_refusal(row))
        self.assertEqual(
            bulk_targets.why_not_added({"found": False, "unavailable": True}, "TRPA1"),
            bulk_targets.row_refusal({"status": bulk_targets.UNCHECKED,
                                      "gene": "TRPA1"}))

    def test_a_confirmed_gene_still_goes_in_with_what_the_server_confirmed(self):
        with _uniprot_says(gene_name="SNCA", protein_name="Alpha-synuclein",
                           uniprot_id="P37840", mass_kda=14.46,
                           synonyms=["PARK1", "NACP"]):
            resp = self._add(gene="SNCA")
        self.assertEqual(resp.status_code, 200)
        target = Target.objects.using(DB).get(gene_name="SNCA")
        # Not the payload the page was carrying — what the press just confirmed.
        self.assertEqual(target.protein_name, "Alpha-synuclein")
        self.assertEqual(target.uniprot_id, "P37840")
        self.assertIn("PARK1", target.alternative_name)
        self.assertTrue(resp.json()["created"])

    def test_a_synonym_resolves_rather_than_duplicating(self):
        """`PARK8` matched no `gene_name`, so it created a second LRRK2.

        `plan` answers this from `Target.alternative_name` with no network call
        at all — the cache the pipeline already had and this door never asked.
        """
        lrrk2 = Target.objects.using(DB).create(
            gene_name="LRRK2", alternative_name="PARK8, DARDARIN")
        with _uniprot_says(found=False) as looked_up:
            resp = self._add(gene="PARK8")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertFalse(body["created"])
        self.assertEqual(body["target_id"], lrrk2.pk)
        self.assertIn("LRRK2", body["message"])
        self.assertEqual(Target.objects.using(DB).count(), 1)
        looked_up.assert_not_called()

    def test_the_button_carries_the_reason_before_it_is_pressed(self):
        """Disabled with the reason **on the page**, not on `title`.

        A tooltip is no explanation on a touch screen and none at all to
        anybody who does not hover a button that looks broken. The sentence is
        the server's, so what the button says and what the press answers cannot
        disagree.
        """
        with _uniprot_says(found=False):
            data = self.client.get("/pipeline/feasibility/lookup/",
                                   {"gene": "ZZZZZZ"}).json()
        self.assertIn("ZZZZZZ", data["add_refusal"])

        with _uniprot_says(gene_name="SNCA"):
            ok = self.client.get("/pipeline/feasibility/lookup/",
                                 {"gene": "SNCA"}).json()
        self.assertEqual(ok["add_refusal"], "",
                         "a confirmed gene must not grey its own Add button")

    def test_the_page_renders_that_reason_rather_than_deciding_for_itself(self):
        html = self.client.get("/pipeline/feasibility/").content.decode()
        self.assertIn("data.add_refusal", html)
        # Greyed *and* said out loud, in the page rather than a tooltip.
        self.assertIn("${refusal ? 'disabled' : ''}", html)
        self.assertIn('id="add-why"', html)


class TheOneColumnTemplateIsGoneTests(TestCase):
    databases = {"default", DB, "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.site)

    def test_the_page_no_longer_offers_it(self):
        html = (Path(settings.BASE_DIR)
                / "pipeline/templates/pipeline/feasibility.html").read_text()
        self.assertNotIn("import_template", html)

    def test_but_it_still_points_at_the_board_that_does_the_same_job(self):
        html = self.client.get("/pipeline/feasibility/").content.decode()
        self.assertIn("/pipeline/targets/board/", html)

    def test_the_targets_template_itself_still_works(self):
        """Removed from one page, not from the app — the target board offers it."""
        self.assertEqual(
            self.client.get("/pipeline/import/template/targets/").status_code, 200)


class TheFeasibilityPageSendsYouToTheBoardTests(TestCase):
    """The one-column template is gone, and the page names the targets board.

    **This was a browser test, and did not need to be.** Both strings are in
    `feasibility.html` outside any `<script>` — server-rendered — so a response
    sees exactly what Chromium saw, at about 5 ms instead of 870. It was written
    in the browser file because its siblings were, which is how that file grows:
    the cost is per test and the reason for paying it was never checked.

    The rule it now stands as the worked example for: **write the cheapest test
    that can fail.** A browser is for a failure that is invisible without one.
    """

    databases = {"default", DB, "academy_db"}

    def test_the_page_points_at_the_board_and_not_a_one_column_template(self):
        site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        client = _member_client(self, site)
        body = client.get("/pipeline/feasibility/").content.decode()
        self.assertNotIn("Blank Excel template", body)
        self.assertIn("targets board", body)
