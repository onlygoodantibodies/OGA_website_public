"""Seed deterministic SCRATCH data into the LOCAL SQLite ``pipeline_db``.

This exists so the three servers can be developed and tested without ever
touching production. It is idempotent (safe to re-run) and only ever writes to
the local dev database (``db_pipeline.sqlite3``) — it refuses to run against a
Postgres ``PIPELINE_DATABASE_URL`` so it can never seed a real server.

Run:
    python mcp_servers/seed_scratch.py

Creates two sites, a few members, a handful of suppliers, four targets with a
realistic spread of antibody recommendation states (WB/IP/IF/FC) plus the
per-session result rows that explain *why* something is (not) recommended, and a
WT/KO cell-line pair. See README.md for the shape.
"""
from __future__ import annotations

import os
import sys

# Make ``mcp_servers`` importable when run as a script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp_servers.common.django_bootstrap import setup_django  # noqa: E402

DB = "pipeline_db"


def _guard_local_only():
    """Refuse to seed anything that isn't a LOCAL dev database.

    Allowed: SQLite (the dev fallback) or a PostgreSQL on localhost (used to
    validate the Postgres code paths before going live). Refused: a Postgres on
    any remote host — that's how production is configured, and seeding must never
    touch it.
    """
    from django.conf import settings

    cfg = settings.DATABASES[DB]
    engine = cfg["ENGINE"]
    if "sqlite" in engine:
        return
    if "postgresql" in engine:
        host = (cfg.get("HOST") or "").lower()
        if host in ("", "localhost", "127.0.0.1"):
            return
        raise SystemExit(
            f"REFUSING to seed: pipeline_db PostgreSQL host '{host}' looks remote. "
            "Seeding is only for a local SQLite or localhost-Postgres dev database."
        )
    raise SystemExit(
        f"REFUSING to seed: pipeline_db is '{engine}', not a local dev database."
    )


def run():
    setup_django()
    _guard_local_only()

    from django.contrib.auth.models import User
    from pipeline.models import (
        Site, Member, Company, Target, Antibody, AntibodyOutcome,
        ExperimentSession, WbResult, IpResult, IfResult, FcResult, Report,
        PublicationImage,
    )

    # --- Sites -------------------------------------------------------------
    montreal, _ = Site.objects.using(DB).get_or_create(
        short_code="MTL", defaults={"name": "Montreal"})
    leicester, _ = Site.objects.using(DB).get_or_create(
        short_code="LEI", defaults={"name": "Leicester"})

    # --- Members (auth.User lives in pipeline_db for the pipeline user pool) --
    def member(username, first, last, site, role):
        user = User.objects.using(DB).filter(username=username).first()
        if user is None:
            user = User(username=username, first_name=first, last_name=last,
                        email=f"{username}@example.invalid", is_active=True)
            user.set_unusable_password()
            user.save(using=DB)
        m = Member.objects.using(DB).filter(user_id=user.pk).first()
        if m is None:
            m = Member.objects.using(DB).create(
                user_id=user.pk, site=site, role=role,
                display_name=f"{first} {last}")
        return m

    sara = member("sara", "Sara", "Scratch", montreal, Member.Role.ADMIN)
    member("michael", "Michael", "Mock", leicester, Member.Role.EXPERIMENTER)

    # --- Suppliers (resolve = dedup-safe) ----------------------------------
    abcam, _ = Company.resolve("Abcam", db=DB)
    cst, _ = Company.resolve("Cell Signaling Technology", db=DB)
    novus, _ = Company.resolve("Bio-Techne (Novus Biologicals)", db=DB)
    # Two more suppliers so a gene can have alternatives from several companies —
    # the case where capping the shortlist alphabetically returned one supplier's
    # catalogue and hid everyone else's.
    genetex, _ = Company.resolve("GeneTex", db=DB)
    ptg, _ = Company.resolve("Proteintech", db=DB)
    # Synaptic Systems print their catalogue numbers with a digit-group SPACE, and
    # seven published rows are stored that way. Papers usually close it up, so this
    # is the case where normalising only the incoming string is not enough — the
    # stored side carries typesetting too.
    synaptic, _ = Company.resolve("Synaptic Systems", db=DB)

    # --- Targets -----------------------------------------------------------
    def target(gene, protein, uniprot, status=Target.Status.IN_PROGRESS, aliases=""):
        t, _ = Target.objects.using(DB).get_or_create(
            gene_name=gene,
            defaults={"protein_name": protein, "uniprot_id": uniprot,
                      "status": status, "created_by": sara, "aliases": aliases})
        return t

    # SNCA carries a protein name + HGNC aliases so the alias-resolution path is
    # exercised (a paper writing "Alpha-synuclein" or "PARK1" -> SNCA).
    snca = target("SNCA", "Alpha-synuclein", "P37840", Target.Status.PUBLISHED,
                  aliases="PARK1, NACP")
    syt1 = target("SYT1", "Synaptotagmin-1", "P21579", Target.Status.IF_COMPLETE)
    mapt = target("MAPT", "Microtubule-associated protein tau", "P10636")
    gfap = target("GFAP", "Glial fibrillary acidic protein", "P14136")
    # QPRT is PUBLIC (its antibody has a published figure) but has NO recommendation
    # on any antibody — the gene has not been curated yet. This is the case the
    # verdict derivation must not read as "tested and failed".
    qprt = target("QPRT", "Nicotinate-nucleotide pyrophosphorylase", "Q15274")
    # SQSTM1 is the "well-worked gene" case: MORE recommended antibodies than the
    # alternatives shortlist holds, spread across three suppliers, and a mix of
    # applications. It is also the gene papers write as "SQSTM1/p62", which is why
    # it carries the alias.
    sqstm1 = target("SQSTM1", "Sequestosome-1", "Q13501", Target.Status.PUBLISHED,
                    aliases="p62, A170, OSIL")

    # --- Antibodies with a spread of recommendation states -----------------
    def antibody(target_obj, company, catalogue, clonality, recombinant,
                 recs, site=montreal, **extra):
        ab, created = Antibody.objects.using(DB).get_or_create(
            target=target_obj, company=company, catalogue_number=catalogue,
            lot_number=extra.get("lot_number", ""), site=site,
            defaults={
                "clonality": clonality, "is_recombinant": recombinant,
                "wb_recommended": "WB" in recs, "ip_recommended": "IP" in recs,
                "if_recommended": "IF" in recs, "fc_recommended": "FC" in recs,
                "created_by": sara,
                **{k: v for k, v in extra.items() if k != "lot_number"},
            })
        return ab

    R = Antibody.Clonality.RECOMBINANT
    M = Antibody.Clonality.MONOCLONAL
    P = Antibody.Clonality.POLYCLONAL

    # SNCA: one strong recombinant (WB+IP+IF), one not recommended for WB.
    snca_good = antibody(snca, abcam, "ab212184", R, True, {"WB", "IP", "IF"},
                         rrid="AB_2895247", host_species="Rabbit")
    snca_bad = antibody(snca, cst, "2642", M, False, set(),
                        rrid="AB_10695412", host_species="Mouse")
    # …and one that is ALSO not recommended for WB, but which the bench watched
    # detect its target. This is the middle rung — `limited_support` — and
    # nothing in this fixture could reach it until 14 Sep 2026, so every test
    # naming it passed over an empty list. Its axes are seeded below, next to
    # the publication images, because the rung needs a published figure too.
    snca_qualified = antibody(snca, abcam, "ab27766", P, False, set(),
                              rrid="AB_2192497", host_species="Rabbit")
    # SYT1: recombinant recommended for IF+FC only.
    syt1_ab = antibody(syt1, novus, "NB120-1234", R, True, {"IF", "FC"},
                       host_species="Rabbit")
    # …and one whose catalogue number is STORED with a digit-group space. Kept to
    # IF+FC like the row above, so "SYT1 has nothing recommended for WB" — which
    # several alternatives tests rest on — stays true.
    syt1_spaced = antibody(syt1, synaptic, "107 011", M, False, {"IF", "FC"},
                           host_species="Mouse")
    # MAPT: a polyclonal recommended for nothing (no clean result).
    mapt_ab = antibody(mapt, abcam, "ab8069", P, False, set(),
                       host_species="Mouse")
    # GFAP: recommended for WB, but no publication image -> stays non-public.
    gfap_ab = antibody(gfap, cst, "3670", M, False, {"WB"}, host_species="Mouse")
    # QPRT: a published figure but NO recommendation anywhere on the gene.
    qprt_ab = antibody(qprt, abcam, "ab171939", P, False, set(), host_species="Rabbit")

    # SQSTM1: SEVEN recommended antibodies — more than the shortlist cap of five —
    # from three suppliers, six of them recommended for WB and one for FC only.
    # This is the shape that exposed both selection bugs: alphabetically the five
    # Abcam catalogue numbers won outright, so GeneTex and Proteintech were invisible
    # AND an FC-only antibody was offered ahead of WB-recommended ones.
    sqstm1_abs = [
        antibody(sqstm1, abcam, "ab109012", R, True, {"WB", "IP", "IF"},
                 rrid="AB_2810880", host_species="Rabbit"),
        antibody(sqstm1, abcam, "ab56416", M, False, {"WB"}, host_species="Mouse"),
        antibody(sqstm1, abcam, "ab91526", P, False, {"WB"}, host_species="Rabbit"),
        antibody(sqstm1, abcam, "ab207305", M, False, {"FC"}, host_species="Mouse"),
        antibody(sqstm1, genetex, "GTX100685", R, True, {"WB", "IF"},
                 host_species="Rabbit"),
        antibody(sqstm1, genetex, "GTX111393", P, False, {"WB"}, host_species="Rabbit"),
        antibody(sqstm1, ptg, "18420-1-AP", P, False, {"WB"}, host_species="Rabbit"),
    ]
    # …plus one tested and NOT recommended, so "recommended" is a real filter here.
    sqstm1_bad = antibody(sqstm1, ptg, "66184-1-Ig", M, False, set(),
                          host_species="Mouse")

    # --- Publication images = the PUBLIC boundary --------------------------
    # An antibody is public iff it has >=1 publication image; a target is public
    # iff it has >=1 such antibody (mirrors core/views.py). This gives a clean
    # published/unpublished split for the public-scoping tests:
    #   public targets     : SNCA (snca_good, snca_bad), SYT1 (syt1_ab),
    #                        QPRT (qprt_ab — public but UNCURATED: no recommendation
    #                        anywhere on the gene)
    #   NON-public targets  : MAPT, GFAP (their antibodies have no figure)
    def pub_image(ab, app):
        PublicationImage.objects.using(DB).get_or_create(
            antibody=ab, application_type=app,
            defaults={"image": f"publication_images/2026/{ab.catalogue_number}_{app}.png"})

    for app in ("WB", "IP", "ICC-IF"):
        pub_image(snca_good, app)          # strong ab: figures for WB/IP/IF
    pub_image(snca_bad, "WB")              # published WB figure, but NOT recommended
    pub_image(snca_qualified, "WB")        # published WB figure; negative, but it detects
    for app in ("ICC-IF", "FC"):
        pub_image(syt1_ab, app)            # SYT1 recombinant: IF/FC figures
    pub_image(syt1_spaced, "ICC-IF")       # the stored-with-a-space catalogue
    pub_image(qprt_ab, "WB")               # published WB figure on an UNCURATED gene
    # --- The capability axes = the MIDDLE RUNG ------------------------------
    # Without an `AntibodyOutcome` row nothing in this fixture can resolve to
    # `limited_support`, and every test naming that rung passes by reaching an
    # empty list — the shape this repo calls "a guard reading a mark nobody
    # applies always passes". It was exactly that until 14 Sep 2026: the table
    # has existed since 29 Aug and this seed created no rows in it, so the whole
    # capability layer was unreachable from the connector's suite.
    #
    # It goes on a NEW antibody rather than on `snca_bad`. Seeding the axes onto
    # that row instead would have moved the fixture's only plain negative up a
    # rung and left `not_supportive` unreachable — trading one missing rung for
    # another, on the row a dozen negative-path tests already rest on. The two
    # negatives have to be distinguishable from EACH OTHER, not merely from
    # `supportive`, so SNCA/WB now holds one of each.
    AntibodyOutcome.objects.using(DB).get_or_create(
        antibody=snca_qualified, application_type="WB",
        defaults={"detects": "yes", "selective": "no"})

    stored_as = {"WB": "WB", "IP": "IP", "IF": "ICC-IF", "FC": "FC"}
    for ab in sqstm1_abs:                  # every SQSTM1 antibody is public
        for a in ("WB", "IP", "IF", "FC"):
            if getattr(ab, f"{a.lower()}_recommended"):
                pub_image(ab, stored_as[a])
    pub_image(sqstm1_bad, "WB")
    # mapt_ab and gfap_ab get NO figure on purpose -> MAPT + GFAP stay non-public.

    # --- Result rows: the evidence behind (non-)recommendations ------------
    def session(target_obj, proc):
        from datetime import date
        s, _ = ExperimentSession.objects.using(DB).get_or_create(
            target=target_obj, procedure_type=proc, experimenter=sara,
            site=montreal,
            defaults={"date": date(2026, 1, 15),
                      "status": ExperimentSession.SessionStatus.COMPLETE})
        return s

    wb = session(snca, ExperimentSession.ProcedureType.WB)
    WbResult.objects.using(DB).get_or_create(
        session=wb, antibody=snca_good,
        defaults={"dilution": "1/1000", "signal": "Clean single band at 14 kDa",
                  "rating": "Recommended"})
    WbResult.objects.using(DB).get_or_create(
        session=wb, antibody=snca_bad,
        defaults={"dilution": "1/500", "signal": "Multiple non-specific bands",
                  "rating": "Not recommended"})

    ifs = session(syt1, ExperimentSession.ProcedureType.IF)
    IfResult.objects.using(DB).get_or_create(
        session=ifs, antibody=syt1_ab,
        defaults={"specific_signal": "Punctate synaptic signal, WT>>KO",
                  "best_concentration": "1/200"})

    ip = session(snca, ExperimentSession.ProcedureType.IP)
    IpResult.objects.using(DB).get_or_create(
        session=ip, antibody=snca_good,
        defaults={"enrichment": "Strong enrichment vs KO",
                  "ip_assessment": "Recommended"})

    fc = session(syt1, ExperimentSession.ProcedureType.FC)
    FcResult.objects.using(DB).get_or_create(
        session=fc, antibody=syt1_ab,
        defaults={"histogram_shift": "Clear WT/KO separation"})

    # MAPT WB result showing why it isn't recommended.
    mapt_wb = session(mapt, ExperimentSession.ProcedureType.WB)
    WbResult.objects.using(DB).get_or_create(
        session=mapt_wb, antibody=mapt_ab,
        defaults={"signal": "Band present in KO — not specific",
                  "rating": "Not recommended"})

    # SNCA has a published report with both DOIs; SYT1 deliberately has none
    # (a "missing DOI" gap for the report-links tooling to fill).
    Report.objects.using(DB).get_or_create(
        target=snca,
        defaults={"status": Report.ReportStatus.PUBLISHED,
                  "f1000_doi": "https://doi.org/10.12688/f1000research.snca",
                  "zenodo_doi": "https://doi.org/10.5281/zenodo.snca"})

    counts = {
        "sites": Site.objects.using(DB).count(),
        "members": Member.objects.using(DB).count(),
        "companies": Company.objects.using(DB).count(),
        "targets": Target.objects.using(DB).count(),
        "antibodies": Antibody.objects.using(DB).count(),
        "wb_results": WbResult.objects.using(DB).count(),
        "publication_images": PublicationImage.objects.using(DB).count(),
    }
    return counts


if __name__ == "__main__":
    result = run()
    print("Seeded local scratch pipeline_db:")
    for k, v in result.items():
        print(f"  {k:12} {v}")
