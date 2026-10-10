"""The Antibody Champions and the institutions that host them — one list.

Two pages drew this list by hand: ``champions.html`` as sixteen cards and
``roadmap_institutions.html`` as a map, each with its own copy of every name,
institution and photo, and four more pages typed the totals ("16 researchers in
14 UK research institutions"). A new cohort meant editing six pages, and any one
missed went on quoting last year's numbers with nothing to contradict it. So
both pages draw from here, and every total is ``figures()``.

**Adding a Champion is adding one ``Champion``**, and a new institution one
``Institution`` as well. ``tests_site_figures`` refuses a Champion whose
institution is not listed (they would be on the Champions page and missing from
the map) and an institution with nobody in it (a pin with no Champion), and a
photo that is not in ``core/static`` — the hashed static storage raises on a
missing file, so that would be a 500 on the Champions page, not a gap.

``level`` is the institutions page's commitments ladder: 1 hosts a Champion,
2 offers workshop access to every antibody user, 3 has adopted the policy text.
``note`` is one line for a step that is not a rung of the ladder, drawn on the
institution's tile and in its map popup.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Institution:
    name: str
    city: str
    region: str
    lat: float
    lng: float
    level: int = 1
    note: str = ""


@dataclass(frozen=True)
class Champion:
    name: str
    institution: str
    photo: str  # a path under core/static, as {% static %} takes it
    bio: str


# In the order the institutions page has always listed them.
INSTITUTIONS = (
    Institution('University of Plymouth', 'Plymouth', 'South West', 50.3754, -4.1427),
    Institution('University of Nottingham', 'Nottingham', 'East Midlands', 52.9382, -1.1965),
    Institution('Imperial College London', 'London', 'London', 51.4988, -0.1749),
    Institution('University College London', 'London', 'London', 51.5246, -0.1340),
    Institution('Queen Mary University of London', 'London', 'London', 51.5246, -0.0406),
    Institution('Manchester Metropolitan University', 'Manchester', 'North West', 53.4708, -2.2399),
    Institution('University of Manchester', 'Manchester', 'North West', 53.4668, -2.2339),
    Institution('Royal Veterinary College', 'London', 'London', 51.5260, -0.1404),
    Institution('University of Strathclyde', 'Glasgow', 'Scotland', 55.8617, -4.2422),
    Institution('University of Glasgow', 'Glasgow', 'Scotland', 55.8720, -4.2880),
    Institution('University of Leicester', 'Leicester', 'East Midlands', 52.6219, -1.1248,
                note='Antibody validation training is mandatory for its bioscience postgraduate researchers'),
    Institution('Lancaster University', 'Lancaster', 'North West', 54.0100, -2.7857),
    Institution('University of Dundee', 'Dundee', 'Scotland', 56.4586, -2.9844),
    Institution('Waltham Petcare Science Institute', 'Melton Mowbray', 'East Midlands', 52.7644, -0.8856),
)

# The 2026 cohort, in the order the Champions page has always shown them.
CHAMPIONS = (
    Champion('Abigail Parsons', 'University of Plymouth',
             'core/champions/abigail parsons photo.jpg',
             'Postdoctoral Research Fellow developing novel non-invasive treatments for aggressive brain cancer, using Drosophila models and patient-derived stem cells.'),
    Champion('Avika Srivastava', 'University of Nottingham',
             'core/champions/Avika Srivastava.jpeg',
             'PhD candidate using glycosaminoglycans as biomarkers in the ageing and cancer matrix, with a background in neurobiology from Canada.'),
    Champion('Ben Moss', 'Imperial College London',
             'core/champions/Ben_Moss.jpg',
             'BHF–NC3Rs funded PhD student exploring how amyloid aggregates affect vascular smooth muscle cells, applying non-animal antibody development methods to discover potential protein inhibitors.'),
    Champion('Charlie Arber', 'University College London',
             'core/champions/Charlie Arber.jpg',
             'Stem cell biologist working with patient-derived models of dementia, using antibodies to detect and measure the earliest changes that might underlie the disease.'),
    Champion('Christina Gkantsinikoudi', 'Queen Mary University of London',
             'core/champions/Christina Gkantsinikoudi_Headshot.JPG',
             'Postdoctoral research associate exploring immune responses in cardiovascular disease through flow cytometry and immunofluorescence imaging, with a strong interest in antibody validation and reproducible research.'),
    Champion('Ella Turner', 'Manchester Metropolitan University',
             'core/champions/ella turner.jpeg',
             'Life sciences PhD student researching maternal and fetal health, developing a model to study changes in blood flow seen in pre-eclamptic pregnancies and how these affect blood clotting and endothelial activation.'),
    Champion('Jeyapriya Thimukonda Jegadeesan', 'University of Manchester',
             'core/champions/Jeyapriya Thimukonda Jegadeesan.jpg',
             'BBSRC DTP student working on regenerative medicine for nerve applications, following the 3Rs principles.'),
    Champion('Lorna Milne', 'University of Nottingham',
             'core/champions/Lorna Milne.jpg',
             'Postdoctoral researcher improving analytical and imaging technologies in glycobiology, enabling multi-modal imaging and multi-omic analysis of the kidney.'),
    Champion('Lovely Monney', 'Queen Mary University of London',
             'core/champions/Lovely Monney.jpeg',
             'PhD researcher in the Centre for Predictive in vitro Models, studying how inflammation and mechanical stimuli regulate primary cilia in kidney epithelial cells, with relevance to polycystic kidney disease.'),
    Champion('Neil Marr', 'Royal Veterinary College',
             'core/champions/NeilMarr.png',
             'Researcher focused on skeletal tissue biology across species, with extensive experience validating antibodies that are both target-specific and conserved across species for musculoskeletal research.'),
    Champion('Ridvan Kucuk', 'University of Strathclyde',
             'core/champions/Ridvan photo.jpg',
             'NC3Rs-funded PhD student developing a human stroke-on-a-chip model using iPSCs and microfluidic technology to reduce animal use in stroke research.'),
    Champion('Rosie Fellows', 'University of Glasgow',
             'core/champions/Rosie_Headshot.jpg',
             'PhD student in parasitology with previous research on the complement system, where antibody optimisation was a central part of the work.'),
    Champion('Siâny Vincent-Simpson', 'University of Leicester',
             'core/champions/Siany Vincent-Simpson.jpg',
             'MRC AIM DTP PhD student with a background in immunology and biomedical science. A qualified teacher passionate about science communication, optimising research, and everything antibodies-related.'),
    Champion('Stefanie Menzies', 'Lancaster University',
             'core/champions/Stefanie Menzies.jpg',
             'Lecturer in Molecular Cell Biology leading research on antibody discovery, snake venom biology, and translational approaches to diagnostics and therapeutics, with prior research roles at the Liverpool School of Tropical Medicine.'),
    Champion('Sukriti Maity', 'University of Dundee',
             'core/champions/Sukriti Maity.jpeg',
             'Graduate student at the MRC Protein Phosphorylation and Ubiquitylation Unit, studying proteins through structural and biochemical approaches, with a keen interest in antibody validation pipelines.'),
    Champion('Wiktoria Tomalik', 'Waltham Petcare Science Institute',
             'core/champions/Wiktoria Tomalik.jpeg',
             'Bioscience laboratory scientist with an MSc in Immunology & Immunotherapy and a BSc in Biotechnology from the University of Nottingham, bringing a passion for innovative research to advance understanding of pet health.'),
)


def institutions_for_map():
    """Each institution with its Champions, as the institutions page's map and
    tiles read them (``json_script``)."""
    hosted = {}
    for champion in CHAMPIONS:
        hosted.setdefault(champion.institution, []).append(
            {"name": champion.name, "photo": champion.photo})
    return [dict(asdict(inst), champions=hosted.get(inst.name, []))
            for inst in INSTITUTIONS]


def figures():
    """The totals every page quotes. Institutions are counted from the
    Champions, so the number is "institutions hosting a Champion" whatever
    ``INSTITUTIONS`` holds."""
    return {
        "champion_count": len(CHAMPIONS),
        "champion_institution_count": len({c.institution for c in CHAMPIONS}),
    }
