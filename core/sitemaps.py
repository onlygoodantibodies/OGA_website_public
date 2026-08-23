"""What the sitemap tells a crawler exists.

The gene half of it was built from ``core.Gene`` — a table in the **legacy**
SQLite database that is committed to git, and which nothing writes to any more.
The public gene pages have come from pipeline PostgreSQL since the Phase 3
rewrite, so the two drifted the moment a gene was published: 155 rows in the
legacy table against 159 public genes live (9 Aug 2026). Both directions are
wrong and neither is visible from the outside — genes that exist are never
offered to a crawler, and a stale row points at a URL that now 404s, because
``antibody_table`` refuses a target with nothing published behind it.

``pipeline/public.py::public_targets()`` is the one definition of "a gene the
public site will show", and it is what the gene pages themselves are served
from, so a sitemap built on it cannot disagree with them.
"""
from django.contrib.sitemaps import Sitemap
from django.urls import reverse

from pipeline.public import public_targets


class StaticViewSitemap(Sitemap):
    changefreq = 'monthly'
    priority = 0.7

    def items(self):
        # `news` and `publications` were both here and both are permanent
        # redirects to `news_publications`, so the sitemap was offering a
        # crawler two 301s to one page instead of the page.
        return [
            'home',
            'about',
            'roadmap',
            'news_publications',
            'partners',
            'contact',
            'using_the_data',
            'privacy_policy',
            'tools_hub',
            'selector:tool',
            'connect_your_ai',
            # The machine-facing front doors. Both are ordinary public pages a
            # partner is meant to find without being sent the link first, and
            # neither was offered to a crawler.
            'data_access',
            'api_reference',
        ]

    def location(self, item):
        return reverse(item)

    def priority(self, item):
        if item == 'home':
            return 1.0
        if item in ('roadmap', 'about'):
            return 0.8
        return 0.6


class GeneSitemap(Sitemap):
    changefreq = 'weekly'
    priority = 0.9

    def items(self):
        return public_targets().order_by('gene_name')

    def location(self, obj):
        return reverse('antibody_table', kwargs={'gene_name': obj.gene_name})
