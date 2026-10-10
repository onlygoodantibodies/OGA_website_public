"""The totals public pages quote are counted from the list they total.

About said "14 manufacturers" while 24 suppliers had published antibodies;
"5 modules" sat beside "four modules" in the same week; "16 researchers in 14
institutions" was typed on four pages and drawn as two hand-kept lists on two
more. Each was right on the day it was typed, and nothing on any page could
contradict it afterwards. What is pinned here is the silent half: a list that
two pages draw going out of step, a file it names going missing (the hashed
static storage raises, so that is a 500, not a gap), and a page printing
anything but the count.
"""
from django.contrib.staticfiles import finders
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from academy.models import Lesson
from core import champions, partners, target_confusions
from core.views import _mixed_results
from pipeline.models import Antibody, Company, PublicationImage, Target
from pipeline.public import published_suppliers, supplier_count


class TheListsHoldTogetherTests(SimpleTestCase):

    def test_every_champion_is_on_the_map_and_every_pin_has_a_champion(self):
        listed = {inst.name for inst in champions.INSTITUTIONS}
        hosting = {c.institution for c in champions.CHAMPIONS}
        self.assertEqual(hosting - listed, set(),
                         "a Champion whose institution has no pin on the map")
        self.assertEqual(listed - hosting, set(),
                         "a pin on the map with no Champion")

    def test_every_photo_and_logo_is_a_static_file(self):
        for path in ([c.photo for c in champions.CHAMPIONS]
                     + [p.logo for s in partners.SECTIONS for p in s.partners]):
            self.assertIsNotNone(finders.find(path), path)

    def test_every_badge_is_one_the_legend_explains(self):
        for section in partners.SECTIONS:
            for partner in section.partners:
                for key in partner.badges:
                    self.assertIn(key, partners.BADGES, partner.name)


class PagesPrintTheCountTests(TestCase):
    databases = {"pipeline_db", "academy_db"}

    @classmethod
    def setUpTestData(cls):
        target = Target.objects.create(gene_name="ALPHA")

        def antibody(company, catalogue, published=True):
            ab = Antibody.objects.create(target=target, company=company,
                                         catalogue_number=catalogue)
            if published:
                PublicationImage.objects.create(
                    antibody=ab, application_type="WB",
                    image=f"pubs/{catalogue}_WB.png")

        # Two rows that print as one supplier, one more supplier, and one
        # whose only antibody is unpublished: two suppliers.
        antibody(Company.objects.create(name="Abcam"), "ab1")
        antibody(Company.objects.create(name="Abcam Ltd", display_name="abcam"), "ab2")
        antibody(Company.objects.create(name="GeneTex"), "GTX1")
        antibody(Company.objects.create(name="Unpublished Co"), "U1", published=False)

        for order, published in ((1, True), (2, True), (3, False)):
            Lesson.objects.create(title=f"M{order}", slug=f"m{order}", order=order,
                                  content="", is_published=published)

    def test_suppliers_are_counted_once_per_printed_name_and_only_if_published(self):
        self.assertEqual(published_suppliers(), ["Abcam", "GeneTex"])
        self.assertEqual(supplier_count(), 2)

    def test_about(self):
        page = self.client.get(reverse("about")).content.decode()
        self.assertIn("Compare antibodies from 2 suppliers", page)
        self.assertIn(f"working with {partners.partner_count()} partner organisations", page)
        self.assertIn("Two modules with quizzes", page)
        self.assertIn(f"embedded in {champions.figures()['champion_institution_count']} UK", page)
        self.assertNotIn("14 manufacturers", page)
        # The hero's partner figure is YCharOS's list, never the supplier count:
        # the two are different questions and the live supplier count is larger.
        self.assertIn(f'<div class="abt-hero-stat-num">{partners.ycharos_partner_count()}</div>\n'
                      '        <div class="abt-hero-stat-label">manufacturer partners</div>', page)
        self.assertIn(f"works with {partners.ycharos_partner_count()} manufacturer partners", page)

    def test_the_module_count_is_the_academys_own_list(self):
        for name, sentence in (
                ("roadmap", "<strong>2 modules</strong>"),
                ("champions", "(2 modules, ~45 minutes total"),
                ("roadmap_institutions", "OGA Academy Modules 1–2 before")):
            self.assertContains(self.client.get(reverse(name)), sentence)
        login = self.client.get(reverse("academy:login")).content.decode()
        self.assertIn('<div class="acl-hero-stat-num">2</div>', login)

    def test_the_champions_pages_draw_one_list(self):
        figures = champions.figures()
        page = self.client.get(reverse("champions")).content.decode()
        self.assertEqual(page.count('class="ch-person-card"'), figures["champion_count"])
        self.assertIn(f"{figures['champion_count']} researchers in "
                      f"{figures['champion_institution_count']} UK", page)
        institutions = self.client.get(reverse("roadmap_institutions"))
        self.assertEqual(institutions.context["institutions"],
                         champions.institutions_for_map())
        self.assertContains(institutions, 'id="ins-institutions"')

    def test_the_partners_page_draws_every_partner_it_counts(self):
        page = self.client.get(reverse("partners")).content.decode()
        # YCharOS is drawn as its own feature, not as a card.
        self.assertEqual(page.count('class="logo-card"'),
                         partners.partner_count() - len(partners.CONSORTIUM))
        self.assertIn(reverse("funder_page", args=["mjff"]), page)


@override_settings(EXTENSION_PAGE_PUBLIC=True)
class TheExtensionPageCountsTests(TestCase):
    """"Two thirds" of TDP-43's antibodies, and "317 of 406" beside a card that
    read the same review's tally from the data: both counted now."""
    databases = {"pipeline_db", "academy_db"}

    @classmethod
    def setUpTestData(cls):
        target = Target.objects.create(gene_name="TARDBP")
        company = Company.objects.create(name="Abcam")

        def antibody(catalogue, figures, **flags):
            ab = Antibody.objects.create(target=target, company=company,
                                         catalogue_number=catalogue, **flags)
            for app in figures:
                PublicationImage.objects.create(
                    antibody=ab, application_type=app,
                    image=f"pubs/{catalogue}_{app}.png")

        antibody("mixed", ("WB", "IP"), wb_recommended=True)
        antibody("all-good", ("WB", "IP"), wb_recommended=True, ip_recommended=True)
        antibody("none", ("WB",))
        antibody("unpublished", (), wb_recommended=True)

    def test_mixed_is_supportive_somewhere_and_not_elsewhere(self):
        self.assertEqual(_mixed_results("TARDBP"), {"mixed": 1, "published": 3})

    def test_the_page_quotes_the_counts(self):
        page = " ".join(self.client.get(reverse("extension")).content.decode().split())
        self.assertIn("Of the 3 antibodies we have published against TDP-43, 1 is supportive", page)
        p16 = target_confusions.counts_by_list()["p16_ink4a"]
        self.assertIn(f"{p16['mistaken']} of {p16['papers']}</strong>", page)
        self.assertNotIn("two thirds", page)
