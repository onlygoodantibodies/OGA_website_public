"""The Partners page's organisations — one list, so the count follows it.

About printed "26+ partner organisations" twice, typed, beside a Partners page
that was a hand-written card per organisation; a partner added to one was a
number left behind on the other. Both now read this module: the Partners page
draws ``SECTIONS`` and About prints ``partner_count()``.

**Adding a partner is adding one ``Partner`` to its section.** Its logo goes in
``core/static``; ``tests_site_figures`` refuses a logo that is not there, since
the hashed static storage raises on a missing file and the page would 500.
``badges`` are keys of ``BADGES``, which also draws the page's legend.

YCharOS is drawn on its own above the sections, as the consortium OGA belongs
to rather than one partner among the rest, and is counted with them.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from django.urls import reverse

# What each label on a card means, in the order the legend shows them.
BADGES = {
    'reagents': ('Reagents', 'Supplied antibodies to the Leicester YCharOS lab'),
    'delphi': ('Delphi', 'Expert participation in the consensus study'),
    'funding': ('Funding', 'Grants, studentships, or co-investment'),
    'champions': ('Champions', 'Supports the Antibody Champions scheme'),
    'data': ('Data', 'Helps disseminate antibody characterisation data'),
    'host': ('Host', 'Hosts the OGA platform and team'),
}


@dataclass(frozen=True)
class Partner:
    name: str
    logo: str  # a path under core/static, as {% static %} takes it
    url: str
    badges: tuple[str, ...]
    # Further links under "Visit": (text, URL name, URL args), reversed when drawn.
    pages: tuple[tuple[str, str, tuple], ...] = ()


@dataclass(frozen=True)
class Section:
    label: str
    title: str
    background: str  # bg-white / bg-light
    columns: str     # extra class on the logo grid: "three-col" or ""
    partners: tuple[Partner, ...] = field(default=())


# YCharOS is drawn by itself above these (see the docstring).
CONSORTIUM = ("YCharOS",)

SECTIONS = (
    Section('Industry', 'Industry Partners', 'bg-white', 'three-col', (
        Partner('Abcam', 'core/design10.png',
                'https://www.abcam.com', ('reagents', 'delphi', 'funding', 'champions')),
        Partner('Miltenyi Biotec', 'core/Miltenyi.webp',
                'https://www.miltenyibiotec.com', ('reagents', 'champions')),
        Partner('AstraZeneca', 'core/design8.png',
                'https://www.astrazeneca.com', ('champions', 'delphi')),
        Partner('GeneTex', 'core/design9.png',
                'https://www.genetex.com', ('reagents', 'delphi')),
        Partner('Cell Signaling Technology', 'core/design13.png',
                'https://www.cellsignal.com', ('reagents', 'delphi', 'champions')),
        Partner('Proteintech', 'core/proteintech.jpg',
                'https://www.ptglab.com', ('reagents', 'delphi')),
        Partner('DSHB', 'core/design12.png',
                'https://dshb.biology.uiowa.edu', ('reagents', 'delphi')),
        Partner('Addgene', 'core/design144.png',
                'https://www.addgene.org', ('reagents', 'delphi')),
        Partner('Institute for Protein Innovation', 'core/design11.png',
                'https://proteininnovation.org', ('reagents', 'delphi')),
        Partner('Antibody Society', 'core/design15.png',
                'https://www.antibodysociety.org', ('delphi',)),
    )),
    Section('Funders', 'Research Funding Agencies', 'bg-light', 'three-col', (
        Partner('NC3Rs', 'core/design16.png',
                'https://nc3rs.org.uk', ('funding', 'champions', 'delphi')),
        Partner('Michael J. Fox Foundation', 'core/design17.png',
                'https://www.michaeljfox.org', ('funding', 'delphi'),
                pages=(('Genes characterised →', 'funder_page', ('mjff',)),)),
        Partner('MND Association', 'core/design19.png',
                'https://www.mndassociation.org', ('delphi',)),
    )),
    Section('Publishing', 'Publishers and Journals', 'bg-white', '', (
        Partner('F1000Research', 'core/design20.png',
                'https://f1000research.com', ('delphi', 'data')),
        Partner('eLife', 'core/logo22.png',
                'https://elifesciences.org', ('delphi',)),
        Partner('Nature Protocols', 'core/design222.png',
                'https://www.nature.com/nprot', ('delphi',)),
        Partner('The Company of Biologists', 'core/design23.png',
                'https://www.biologists.com', ('delphi',)),
        Partner('Wiley', 'core/design24.png',
                'https://www.wiley.com/en-gb', ('delphi',)),
    )),
    Section('Institutions', 'Research Institutions and End Users', 'bg-light', 'three-col', (
        Partner('UK Reproducibility Network', 'core/design1.png',
                'https://www.ukrn.org', ('champions', 'delphi')),
        Partner('Structural Genomics Consortium', 'core/sgc.png',
                'https://www.thesgc.org', ('funding', 'delphi')),
        Partner('Leicester Institute for Precision Health', 'core/design22.png',
                'https://le.ac.uk/research/institutes/precision-health', ('host', 'funding')),
    )),
    Section('Databases', 'Reagents Database Providers', 'bg-white', '', (
        Partner('RRID', 'core/design3.png',
                'https://www.rrids.org', ('delphi', 'data')),
        Partner('Chemical Probes Portal', 'core/design4.png',
                'https://www.chemicalprobes.org', ('delphi', 'data')),
        Partner('Biocompare', 'core/design5.png',
                'https://www.biocompare.com', ('delphi', 'data')),
        Partner('CiteAb', 'core/design6.png',
                'https://www.citeab.com', ('delphi', 'data')),
    )),
)


# The manufacturers the YCharOS consortium partners with, as its own partners
# page lists them (ycharos.com/partners/, read 7 Oct 2026): thirteen antibody
# makers and Horizon Discovery, which makes the knockout cells. This is a fact
# about the consortium and the database cannot count it — the suppliers whose
# antibodies are published (``pipeline.public.published_suppliers``, 24 that
# day) include companies that are not partners, such as Santa Cruz and Abbexa.
# About printed that 24 as its partner figure for one day; the two are kept
# apart here so the next edit cannot do it again. Add a partner here when
# YCharOS's page does.
YCHAROS_MANUFACTURER_PARTNERS = (
    "Abcam",
    "ABCD Antibodies",
    "ABclonal",
    "Aviva Systems Biology",
    "Bio-Techne",
    "Cell Signaling Technology",
    "Developmental Studies Hybridoma Bank (DSHB)",
    "GeneTex",
    "Horizon Discovery",
    "MilliporeSigma",
    "Miltenyi Biotec",
    "Proteintech",
    "Synaptic Systems",
    "Thermo Fisher Scientific",
)


def ycharos_partner_count() -> int:
    return len(YCHAROS_MANUFACTURER_PARTNERS)


def partner_count() -> int:
    return len(CONSORTIUM) + sum(len(s.partners) for s in SECTIONS)


def sections():
    """``SECTIONS`` as the Partners page draws them: each card's badges with
    their labels, and its extra links reversed to URLs."""
    drawn = []
    for section in SECTIONS:
        cards = [{
            "name": p.name, "logo": p.logo, "url": p.url,
            "badges": [(key, BADGES[key][0]) for key in p.badges],
            "pages": [(text, reverse(name, args=args)) for text, name, args in p.pages],
        } for p in section.partners]
        drawn.append({"label": section.label, "title": section.title,
                      "background": section.background,
                      "columns": section.columns, "partners": cards})
    return drawn


def legend():
    return [(key, label, meaning) for key, (label, meaning) in BADGES.items()]
