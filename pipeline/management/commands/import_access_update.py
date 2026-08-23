"""
Django management command: import_access_update

Applies the **delta** between the March 2026 Access snapshot already on the site
and Sara's 20 Aug 2026 export, to the McGill records in `pipeline_db`.
Everything here is McGill's bench, which the command finds by asking where the
original import put its rows rather than by assuming a short code.

    python manage.py import_access_update              # dry run — writes nothing
    python manage.py import_access_update --apply

`import_access_data` imported the whole Access database once. It cannot be run
again: it `update_or_create`s the core entities but **`create`s** every WB/IP/IF
result, so a second run would duplicate 5,192 readings, and its session grouping
heuristic (one session per procedure per target, for all of history) would
re-group what is already there. This command is the incremental half, and the
join key that makes it safe is `access_id` — every row the original import wrote
carries the Access `ID` it came from, so a re-run of *this* command matches what
it wrote last time and creates nothing twice.

What it applies (all counted and listed at the end):

  1. Companies      2 new suppliers, through `resolve_company`
  2. Antibodies     41 new (A-3085 … A-3125)
  3. Cell lines     19 Access rows → 10 cell lines + 19 freeze-down batches
  4. Experiments    124 WB + 155 IF + 113 IP readings, in 43 sessions
  5. Corrections    changed cells on existing antibodies, cell lines, WB and IF
  6. Targets        29 targets' Zenodo/F1000 DOIs, dates and status

What it deliberately does **not** apply, and why:

  * **`RRIDlink` on existing antibodies.** 1,050 records that held a working
    `https://www.antibodyregistry.org/AB_…` URL now hold an Access hyperlink
    whose address is the bare RRID (`AB_2942092#http://AB_2942092#`), which
    resolves to nothing. That is a regression in the export, not an update, so
    those cells are not in `Antibodies_changed.csv` at all.
  * **Cell-line names and parental lines.** 84 names and 21 parental lines
    changed. Those are identity, and live cell lines were deduplicated *by
    name*, so a rename can silently merge two lines or collide with a third.
    `fix_cell_line_identity` does that half, collision by collision.

Two rules decide every cell it writes:

  * **A blank never clears.** Where the export blanked a value that the site
    holds — C9orf72's Zenodo DOI is the one that matters — the stored value
    stands and the cell is counted and named. "Not written down" is not "a
    different one", and the export dropping a DOI is far likelier than the
    paper being withdrawn.
  * **A correction must match what it claims to correct.** Each changed cell
    carries the value the export believed the site held. If the site now holds
    something else, somebody edited it on a board since March and this command
    is not the authority — the cell is refused and named, never overwritten.

The RRID on a new antibody lives in **`RRIDlink`**, not `RRID`. All 41 new rows
have an empty `RRID` column, and 30 of them carry a real identifier in the
hyperlink. Reading `RRID` alone — which is the right rule for the *existing*
rows above — would drop every one of them. `rrid_utils.normalize_rrid` reads
either shape and refuses `NA#http://NA#`, so it is asked both questions in turn.

No number is invented. `AbNumber` 3085–3125 and `LabLabel` 745–763 continue the
McGill runs exactly (`lab_numbers.next_number` expects A-3085 and C-745 next),
and they are written as the lab wrote them inside `lab_numbers.suspended()` —
a number this command minted would be one no freezer agrees with.
"""

import os
from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Count

from pipeline import rrid_utils
from pipeline.services import lab_numbers
from pipeline.services import sites as sites_svc
from pipeline.services.cropper.db import resolve_company, company_label
from pipeline.services.identity import vial_clash_message
from pipeline.models import (
    Site, Member, Company, Target, Report, CellLine, CellLineVial,
    Antibody, InventoryLocation, ExperimentSession,
    WbResult, IpResult, IfResult,
)

# The Access value conventions — date formats, hyperlink unwrapping, the
# clonality and acquisition maps — have one reader, and it is the original
# importer. A second copy of "how Access spells a date" is how two importers
# come to disagree about one row.
from pipeline.management.commands.import_access_data import (
    clean_str, clean_int, clean_decimal, clean_bool, clean_date, clean_doi,
    read_csv, pk, CLONALITY_MAP, ACQUISITION_MAP, STATUS_MAP,
)

DB = "pipeline_db"
DEFAULT_DIR = os.path.join("pipeline", "data", "access_update_2026_08")

# Access CellLines.RRID holds a Cellosaurus id, not an antibody RRID.
_CVCL_PREFIX = "CVCL_"


# =============================================================================
# Which Access column writes which field
#
# A column named in none of these maps is *reported as ignored*, never dropped
# in silence: several of them (`KnockDown`, `B2`–`B7`, the `…Duplicate` set,
# CellLines `Comments`) were never mapped by the original import either, so the
# site has no value for the correction to correct.
# =============================================================================

ANTIBODY_FIELDS = {
    "Comments": "comments",
    "Antigen": "antigen",
    "SpeciesReactivity": "species_reactivity",
    "Isotype": "isotype",
    "Clone": "clone_id",
    "ExpressionSystem": "host_species",
}
ANTIBODY_BOOL_FIELDS = {
    "OutOfMarket": "out_of_market",
    "EmptyVial": "empty_vial",
    "Test": "is_test",
    "Others": "others_flag",
}
# Part of `unique_antibody_per_site_lot` — what makes the row the vial it is.
ANTIBODY_IDENTITY = {"CatNumber", "Lot", "ProteinsID", "CompaniesID"}
ANTIBODY_INVENTORY = {"BoxNumber", "Fridge4C", "Neg80C"}

CELL_LINE_FIELDS = {
    "Medium": "medium",
    "Origin": "origin",
    "OriginComments": "origin_comments",
    "KOInfo": "ko_validation_notes",
    "CatNumber": "catalogue_number",
    "Lot": "lot_number",
    "Species": "species",
    "Clone": "clone",
    "GrowthProperties": "growth_properties",
}
CELL_LINE_BOOL_FIELDS = {"KOconfirmed": "ko_validated"}
# These describe the freeze-down batch, which is where `restructure_cell_lines`
# put them — one Access row is one `CellLineVial`, not one `CellLine`.
CELL_LINE_VIAL_FIELDS = {"LocationOriginalVial": "location_original_vial"}
CELL_LINE_VIAL_BOOL_FIELDS = {"Thawed": "thawed"}
CELL_LINE_VIAL_DATE_FIELDS = {"ReceivedDate": "received_date"}
# Name, genotype, gene and parent are what a cell line *is*.
CELL_LINE_IDENTITY = {"CellLine", "ParentalLine", "WTorKO", "ProteinsID"}
# (Access column, storage_type, InventoryLocation field)
CELL_LINE_INVENTORY = {
    "TankLN": ("ln2", "freezer"), "RackLN": ("ln2", "rack"),
    "BoxLN": ("ln2", "box"), "VialsLN": ("ln2", "position"),
    "RackNeg80": ("-80", "rack"), "TrayNeg80": ("-80", "shelf"),
    "BoxNeg80": ("-80", "box"), "VialsNeg80": ("-80", "position"),
    "RackN80": ("-80", "rack"), "TrayN80": ("-80", "shelf"),
    "BoxN80": ("-80", "box"), "VialsN80": ("-80", "position"),
}

WB_FIELDS = {
    "SpecificSignal": "signal", "SelectiveSignal": "rating",
    "Gel": "gel", "Membrane": "membrane", "ECL": "ecl",
    "DetectionSystem": "detection_system", "2ndaryAb": "secondary_ab",
    "2ndaryAbDilution": "secondary_ab_dilution", "Comments": "comments",
}
# The original import wrote `1AbDilution` to both columns; a correction that
# moved only one of them would leave the row disagreeing with itself.
WB_PAIRED = {"1AbDilution": ("dilution", "primary_ab_dilution")}

IF_FIELDS = {
    "SpecificSignal": "specific_signal", "TestedConcentration1": "concentration_1",
    "TestedConcentration2": "concentration_2", "BestConcentration": "best_concentration",
    "Fixative": "fixative", "BlockingCondition": "blocking",
    "Permeabilization": "permeabilisation", "1AbDilution": "primary_ab_dilution",
    "DilutionBuffer": "dilution_buffer", "1AbCondition": "primary_ab_condition",
    "SecondaryAb": "secondary_ab", "SecondaryAbCondition": "secondary_ab_condition",
    "PlateNumber": "plate_number", "WellNumber": "well_number",
    "ImageAcquisitionBy": "image_acquired_by", "ImageAnalyzedBy": "image_analysed_by",
    "Microscope": "microscope", "Objective": "objective", "Comments": "comments",
}
IF_DECIMAL_FIELDS = {"WTKOratio1": "wt_ko_ratio_1", "WTKOratio2": "wt_ko_ratio_2"}

# Stored, but on the **session** rather than the reading. A change here says the
# reading belongs to a different run — a different day, person or pair of cell
# lines — which is a re-filing, not a cell edit, and is left to a person.
SESSION_LEVEL_COLUMNS = {
    "When", "MembersID", "MemberID", "CellLineID", "CellLine1ID", "CellLine2ID",
    "lane1CellLineID", "lane2CellLineID", "lane3CellLineID", "lane4CellLineID",
}


class Command(BaseCommand):
    help = ("Apply the March→August 2026 YCharOS Access delta to pipeline_db. "
            "Dry run unless --apply is given.")

    def add_arguments(self, parser):
        parser.add_argument(
            "--csv-dir", default=DEFAULT_DIR,
            help=f"Directory holding the delta CSVs (default: {DEFAULT_DIR})")
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually write. Without it nothing is saved.")
        parser.add_argument(
            "--site",
            help="Which bench these records belong to — a name, short code or "
                 "pk. Defaults to the site the original Access import already "
                 "put its rows on.")
        parser.add_argument(
            "--baseline-dir", default="access_csvs",
            help="Where the export's own Antibodies.csv lives, used only to "
                 "follow a reading whose antibody the original import "
                 "collapsed into a duplicate (default: access_csvs)")
        parser.add_argument(
            "--skip-inventory", action="store_true",
            help="Leave freezer locations (box, rack, tray, vial counts) alone.")

    # ------------------------------------------------------------------
    def handle(self, *args, **options):
        # Nothing here mints a number: A-3085… and C-745… were written on
        # McGill's tubes months ago, and every other row in the delta is a
        # reading or a correction that gets no number at all.
        with lab_numbers.suspended():
            return self._run(**options)

    def _run(self, **options):
        self.dry_run = not options["apply"]
        self.skip_inventory = options["skip_inventory"]
        self.baseline_dir = options["baseline_dir"]
        csv_dir = options["csv_dir"]

        if not os.path.isdir(csv_dir):
            raise CommandError(f"Directory not found: {csv_dir}")

        self.stdout.write(self.style.MIGRATE_HEADING(
            "YCharOS Access delta — March 2026 → 20 August 2026 (McGill)"))
        if self.dry_run:
            self.stdout.write(self.style.WARNING(
                "DRY RUN — nothing will be saved. Re-run with --apply to write."))

        self.site = self._resolve_site(options.get("site"))
        self.stdout.write(f"  Bench: {self.site.name} "
                          f"({self.site.short_code}, id {self.site.pk})")

        # Named gaps, printed together at the end rather than scrolling past.
        self.notes = defaultdict(list)
        self.counts = defaultdict(int)

        with transaction.atomic(using=DB):
            self._load_reference(csv_dir)
            self._companies(csv_dir)
            self._antibodies(csv_dir)
            self._cell_lines(csv_dir)
            self._experiments(csv_dir)
            self._corrections(csv_dir)
            self._targets(csv_dir)

            if self.dry_run:
                transaction.set_rollback(True, using=DB)

        self._report()



    @staticmethod
    def _access_site_counts():
        """How many Access-imported rows sit on each site, commonest first.

        Three tables rather than one: a database that has the antibodies has
        all three, but a fixture may only have a target, and answering "I
        cannot tell" to a database that plainly knows would be a refusal about
        nothing.
        """
        totals = defaultdict(int)
        for model in (Antibody, CellLine, Target):
            for row in (model.objects.using(DB)
                        .filter(access_id__isnull=False, site__isnull=False)
                        .values("site_id").annotate(n=Count("site_id"))):
                totals[row["site_id"]] += row["n"]
        return [{"site_id": k, "n": v}
                for k, v in sorted(totals.items(), key=lambda kv: -kv[1])]

    def _resolve_site(self, typed):
        """Which bench these records belong to.

        **Derived, not assumed.** This started as `short_code="MTL"`, taken from
        `import_access_data._import_site`, and refused on live — which files the
        same bench under a different code. The site an Access row belongs to is
        not a fact about a spelling, it is *where the original import already
        put these very rows*, so that is what it asks: the site carrying the
        antibodies that came in with an `access_id`. It cannot disagree with the
        rows the delta is about to join.

        `add_site` stays the only thing that creates a bench. A site invented
        here would give McGill's A-numbers a second home, and the two runs of
        numbers would silently overlap.
        """
        if typed:
            site = sites_svc.resolve(typed, db=DB)
            if site is None:
                raise CommandError(sites_svc.refusal(typed, db=DB))
            return site

        counts = self._access_site_counts()
        if counts:
            site = Site.objects.using(DB).get(pk=counts[0]["site_id"])
            if len(counts) > 1:
                # Worth saying: the Access rows should all be on one bench, and
                # if they are not, the majority is a guess rather than a fact.
                others = ", ".join(
                    f"{Site.objects.using(DB).get(pk=c['site_id']).name} ({c['n']})"
                    for c in counts[1:])
                self.notes["Access rows sit on more than one bench"].append(
                    f"{site.name} holds {counts[0]['n']}; also {others}. "
                    f"Pass --site to say which one this delta is for.")
            return site

        known = ", ".join(sites_svc.known_names(db=DB)) or "none"
        raise CommandError(
            "Cannot tell which bench this delta belongs to: no antibody on this "
            "database carries an Access id, so the original import has not run "
            f"here. Sites on file: {known}. Pass --site to name one, or point "
            "at the database that has the Access import.")

    # ------------------------------------------------------------------
    # Reference tables
    # ------------------------------------------------------------------
    def _load_reference(self, csv_dir):
        """Access IDs → names, for the two tables that have no `access_id`.

        `Company` and `Member` were imported by *name*, so the delta's
        `CompaniesID` / `MembersID` can only be followed through the export's
        own reference tables.
        """
        self.company_names = {
            clean_str(r["ID"]): clean_str(r["Company"])
            for r in read_csv(csv_dir, "Companies.csv")
        }
        self.member_names = {
            clean_str(r["ID"]): clean_str(r["Name"])
            for r in read_csv(csv_dir, "Members.csv")
        }
        self._company_cache = {}
        self._member_cache = {}

    def _company(self, access_id):
        """The `Company` an Access CompaniesID means, or None."""
        key = clean_str(access_id)
        if key in self._company_cache:
            return self._company_cache[key]
        name = self.company_names.get(key, "")
        obj = resolve_company(name, db=DB) if name else None
        if name and obj is None:
            self.notes["supplier not resolved"].append(
                f"CompaniesID {key} ({name or 'no name'})")
        self._company_cache[key] = obj
        return obj

    def _member(self, access_id):
        """The `Member` an Access MembersID means, or None.

        `Member` carries no `access_id`, so this matches on the name — and it
        has to ask **three** questions, because the lab's people reached this
        database by two different routes. `import_access_data` minted a login
        called `access_<name>` for each of the twenty-four in the Access table,
        but nine of them are real scientists who have since been given real
        accounts through `services/members.py::grant`, whose usernames are
        their own. Asking only for the `access_` spelling found the fifteen who
        never log in and missed the people actually running the experiments:
        on live it dropped **97 of 392 readings**, a quarter of the new science,
        for Riham Ayoubi and Sara Gonzalez Bolivar alone. It could not fail on
        a rebuilt copy of the Access import, where every member is synthetic.

        `display_name` is asked first because it is what every "who ran it"
        control shows (`members.experimenters` orders by it), so it is the
        spelling a person would recognise as themselves.
        """
        key = clean_str(access_id)
        if key in self._member_cache:
            return self._member_cache[key]
        name = self.member_names.get(key, "")
        member = None

        if name:
            # Active rows first, and only fall back to the whole table when
            # none are. A deactivated row is how this app retires an identity
            # — `merge_members` leaves the duplicate that way rather than
            # deleting it, because sessions point at these — so counting it as
            # a rival kept Carl Laflamme ambiguous *after* his two rows had
            # been merged, and went on refusing the reading the merge was run
            # to rescue. `members.experimenters`, the one list behind every
            # "who ran it" control, filters the same way; a lookup that did not
            # was answering a different question from the control.
            matches = list(Member.objects.using(DB)
                           .filter(display_name__iexact=name, is_active=True)[:2])
            if not matches:
                matches = list(Member.objects.using(DB)
                               .filter(display_name__iexact=name)[:2])
            if len(matches) > 1:
                # Two people of one name is a question, not a coin toss — and
                # attributing a day's work to the wrong one is not recoverable
                # by looking at it.
                self.notes["experimenter names more than one person"].append(
                    f"MembersID {key} ({name}) matches "
                    + ", ".join(f"member {m.pk}" for m in matches))
                self._member_cache[key] = None
                return None
            member = matches[0] if matches else None

        if member is None and name:
            # The login the historical import minted for someone with no account.
            username = f"access_{name.lower().replace(' ', '_')}"
            user = User.objects.using(DB).filter(username=username).first()
            if user:
                member = (Member.objects.using(DB)
                          .filter(user_id=user.pk).order_by("-is_active").first())

        if member is None and name:
            # A real account whose Member row carries no display name.
            parts = name.split()
            user = (User.objects.using(DB)
                    .filter(first_name__iexact=parts[0],
                            last_name__iexact=" ".join(parts[1:]))
                    .first()) if len(parts) > 1 else None
            if user:
                member = (Member.objects.using(DB)
                          .filter(user_id=user.pk).order_by("-is_active").first())

        if member is None:
            self.notes["experimenter not on file"].append(
                f"MembersID {key} ({name or 'no name'})")
        self._member_cache[key] = member
        return member

    def _target(self, access_id):
        return Target.objects.using(DB).filter(access_id=clean_int(access_id)).first()

    def _antibody_for_access(self, access_id):
        """The live `Antibody` an Access Antibodies.ID means.

        `access_id` answers for all but a handful. The original import hit 24
        rows whose `(catalogue, supplier, gene)` was already taken — the Access
        table holds genuine duplicates — and for those it kept the row already
        there, which carries a *different* `access_id`. Its readings went onto
        that row, so a new reading for the collapsed id has to follow them:
        dropping it would lose a real experiment because of a duplicate nobody
        has merged yet. Looked up in the export's own antibody table, which is
        the only place the collapsed id's catalogue number is still written.
        """
        value = clean_int(access_id)
        if not value:
            return None
        found = Antibody.objects.using(DB).filter(access_id=value).first()
        if found is not None:
            return found

        row = self._baseline_antibodies().get(value)
        if row is None:
            return None
        target = self._target(row.get("ProteinsID", ""))
        company = self._company(row.get("CompaniesID", ""))
        if target is None:
            return None
        found = (Antibody.objects.using(DB)
                 .filter(catalogue_number__iexact=clean_str(row.get("CatNumber", "")),
                         company_id=pk(company), target_id=target.pk)
                 .order_by("pk").first())
        if found is not None:
            self.notes["readings attached across a collapsed duplicate"].append(
                f"Access antibody {value} ({clean_str(row.get('CatNumber'))}) → "
                f"A-{found.ab_number or found.pk}, which the original import kept "
                f"in its place")
        return found

    def _baseline_antibodies(self):
        """The export's antibody table, by Access id — read only if needed."""
        if getattr(self, "_baseline_ab", None) is None:
            self._baseline_ab = {}
            path = os.path.join(self.baseline_dir, "Antibodies.csv")
            if os.path.exists(path):
                import csv as _csv
                with open(path, encoding="utf-8-sig") as handle:
                    for row in _csv.DictReader(handle):
                        self._baseline_ab[clean_int(row["ID"])] = row
        return self._baseline_ab

    def _cell_line_for_access(self, access_id):
        """The live `CellLine` an Access CellLines.ID means.

        `restructure_cell_lines` merged cell lines that shared a name and
        deleted the duplicates, so `CellLine.access_id` survives only on the
        row that won. The *vial* it left behind kept every original id, which
        is why that is asked first.
        """
        value = clean_int(access_id)
        if not value:
            return None
        vial = CellLineVial.objects.using(DB).filter(access_id=value).first()
        if vial:
            return vial.cell_line
        return CellLine.objects.using(DB).filter(access_id=value).first()

    # ------------------------------------------------------------------
    # 1. Companies
    # ------------------------------------------------------------------
    def _companies(self, csv_dir):
        rows = read_csv(csv_dir, "Companies.csv")
        before = Company.objects.using(DB).count()
        for row in rows:
            name = clean_str(row["Company"])
            if not name:
                continue
            existing = resolve_company(name, create=False, db=DB)
            if existing is None:
                obj = resolve_company(name, create=True, db=DB)
                if obj is not None:
                    self.counts["suppliers created"] += 1
                    self.notes["suppliers created"].append(company_label(obj))
        self.counts["_suppliers_seen"] = len(rows)
        self.stdout.write(
            f"\n  Suppliers: {len(rows)} in the export, "
            f"{self.counts['suppliers created']} new "
            f"({before} already on file)")

    # ------------------------------------------------------------------
    # 2. Antibodies
    # ------------------------------------------------------------------
    def _antibodies(self, csv_dir):
        rows = read_csv(csv_dir, "Antibodies_new.csv")
        self.stdout.write(f"\n  New antibodies: {len(rows)} rows")

        for row in rows:
            access_id = clean_int(row["ID"])
            ab_number = clean_int(row.get("AbNumber", ""))
            label = f"A-{ab_number}" if ab_number else f"Access id {access_id}"

            existing = Antibody.objects.using(DB).filter(access_id=access_id).first()
            target = self._target(row.get("ProteinsID", ""))
            if target is None:
                self.notes["antibodies skipped — gene not on file"].append(
                    f"{label} (ProteinsID {clean_str(row.get('ProteinsID'))})")
                continue

            company = self._company(row.get("CompaniesID", ""))
            catalogue = self._catalogue(row, label)
            lot = clean_str(row.get("Lot", ""))

            # An identical vial already on file is a refusal, not a second row.
            if existing is None:
                clash = (Antibody.objects.using(DB)
                         .filter(catalogue_number__iexact=catalogue,
                                 company_id=pk(company), target_id=target.pk,
                                 lot_number=lot, site_id=self.site.pk)
                         .first())
                if clash is not None:
                    self.notes["antibodies refused — already on file"].append(
                        f"{label}: " + vial_clash_message(
                            clash, catalogue, company_label(company),
                            target.gene_name or target.protein_name))
                    continue

            bare = (rrid_utils.normalize_rrid(row.get("RRID", ""))
                    or rrid_utils.normalize_rrid(row.get("RRIDlink", "")))
            if not bare:
                self.notes["antibodies with no RRID"].append(label)

            conc = clean_decimal(row.get("ConcentrationInUgUl", ""), 3)

            fields = {
                "ab_number": ab_number,
                "target_id": target.pk,
                "company_id": pk(company),
                "catalogue_number": catalogue,
                "lot_number": lot,
                "rrid": bare or "",
                "rrid_link": rrid_utils.registry_url(bare) if bare else "",
                "clonality": CLONALITY_MAP.get(
                    clean_str(row.get("Clonality", "")), "unknown"),
                "clone_id": clean_str(row.get("Clone", "")),
                "host_species": clean_str(row.get("ExpressionSystem", "")),
                # Access records µg/µL and the column stores µg/mL. Converted,
                # never stripped — this is the one field with no unit column.
                "concentration": conc * 1000 if conc is not None else None,
                "isotype": clean_str(row.get("Isotype", "")),
                "species_reactivity": clean_str(row.get("SpeciesReactivity", "")),
                "antigen": clean_str(row.get("Antigen", "")),
                "supplier_validated_wb": clean_bool(row.get("WBvalidatedBySupplier", "")),
                "supplier_validated_ip": clean_bool(row.get("IPvalidatedBySupplier", "")),
                "supplier_validated_if": clean_bool(row.get("IFvalidatedBySupplier", "")),
                "supplier_validated_ihc": clean_bool(row.get("IHCvalidatedBySupplier", "")),
                "supplier_validated_elisa": clean_bool(row.get("ELISAvalidatedBySupplier", "")),
                "supplier_validated_fc": clean_bool(row.get("FlowCytValidatedBySupplier", "")),
                "supplier_validated_applications": clean_str(
                    row.get("SupplierValidatedApplication", "")),
                "validated_apps_by_supplier": clean_str(
                    row.get("ValidatedApplicationsBySup", "")),
                "validation_details_wb": clean_str(
                    row.get("ValidationByManufacturerForWB", "")),
                "validation_details_if": clean_str(
                    row.get("ValidationByManufacturerForIF", "")),
                "acquisition_method": ACQUISITION_MAP.get(
                    clean_str(row.get("PurchasedORinKind", "")), "unknown"),
                "received_date": clean_date(row.get("ReceivedDate", "")),
                "out_of_market": clean_bool(row.get("OutOfMarket", "")),
                "empty_vial": clean_bool(row.get("EmptyVial", "")),
                "is_test": clean_bool(row.get("Test", "")),
                "others_flag": clean_bool(row.get("Others", "")),
                "supplier_url": clean_str(row.get("Link", "")),
                "comments": clean_str(row.get("Comments", "")),
                "site_id": self.site.pk,
            }

            obj, created = Antibody.objects.using(DB).update_or_create(
                access_id=access_id, defaults=fields)
            self.counts["antibodies created" if created
                        else "antibodies already imported"] += 1
            if created and not self.skip_inventory:
                self._antibody_storage(obj, row)

        self.stdout.write(self.style.SUCCESS(
            f"    → {self.counts['antibodies created']} created, "
            f"{self.counts['antibodies already imported']} already imported"))

    def _catalogue(self, row, label):
        """The catalogue number, with a doubled paste collapsed.

        A-3123 arrived as `\\tsc-398868\\nsc-398868` — a leading tab and the
        number pasted twice. Collapsed only when every line is the same string,
        so this repairs a paste and never invents a number; either way the
        change is named on the report.
        """
        raw = clean_str(row.get("CatNumber", ""))
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        if len(lines) > 1 and len(set(lines)) == 1:
            self.notes["catalogue numbers repaired"].append(
                f"{label}: {raw!r} → {lines[0]!r}")
            return lines[0]
        if len(lines) > 1:
            self.notes["catalogue numbers with more than one value"].append(
                f"{label}: {raw!r} — stored as typed, correct it on the board")
        return raw

    def _antibody_storage(self, antibody, row):
        box = clean_str(row.get("BoxNumber", ""))
        fridge = clean_bool(row.get("Fridge4C", ""))
        neg80 = clean_bool(row.get("Neg80C", ""))
        if not (box or fridge or neg80):
            return
        InventoryLocation.objects.using(DB).update_or_create(
            antibody_id=antibody.pk, site_id=self.site.pk,
            defaults={"storage_type": "4c" if fridge else ("-80" if neg80 else "-20"),
                      "box": box})
        self.counts["antibody storage locations"] += 1

    # ------------------------------------------------------------------
    # 3. Cell lines
    # ------------------------------------------------------------------
    def _cell_lines(self, csv_dir):
        """19 Access rows → 10 cell lines, each with its freeze-down batches.

        One Access `CellLines` row is a **batch**, not a line: nine of these
        nineteen are SK-N-AS knockouts of one gene, frozen down nine times.
        `restructure_cell_lines` settled that shape for the imported data —
        one `CellLine` per (name, site, gene), one `CellLineVial` per Access
        row carrying its own C-number — and new rows join it.
        """
        rows = read_csv(csv_dir, "CellLines_new.csv")
        self.stdout.write(f"\n  New cell-line records: {len(rows)} rows")

        groups = defaultdict(list)
        for row in rows:
            genotype = {"WT": "WT", "KO": "KO"}.get(clean_str(row.get("WTorKO", "")), "other")
            target = self._target(row.get("ProteinsID", ""))
            # A wild type has no gene: one HAP1 WT serves every knockout made
            # from it, so the gene must not enter its key.
            key = (clean_str(row.get("CellLine", "")), genotype,
                   target.pk if (target and genotype != "WT") else None)
            groups[key].append(row)

        for (name, genotype, target_pk), members in groups.items():
            line = self._cell_line_row(name, genotype, target_pk, members[0])
            if line is None:
                continue
            for row in members:
                self._cell_line_vial(line, row)

        self.stdout.write(self.style.SUCCESS(
            f"    → {len(groups)} cell lines "
            f"({self.counts['cell lines created']} created, "
            f"{self.counts['cell lines already on file']} already on file), "
            f"{self.counts['freeze-down batches created']} batches created"))

    def _cell_line_row(self, name, genotype, target_pk, row):
        """The `CellLine` this group belongs to, created if it is genuinely new."""
        if not name:
            self.notes["cell lines skipped — no name"].append(
                f"Access id {clean_str(row.get('ID'))}")
            return None

        # The same key `restructure_cell_lines` deduplicated on, so a line the
        # lab froze down again lands on the row that is already there.
        existing = CellLine.objects.using(DB).filter(
            name__iexact=name, genotype=genotype,
            target_id=target_pk, site_id=self.site.pk).first()
        if existing is not None:
            self.counts["cell lines already on file"] += 1
            return existing

        fields = {
            "name": name,
            "genotype": genotype,
            "target_id": target_pk,
            "catalogue_number": clean_str(row.get("CatNumber", "")),
            "lot_number": clean_str(row.get("Lot", "")),
            "cellosaurus_id": self._cellosaurus(row.get("RRID", "")),
            "species": clean_str(row.get("Species", "")) or "Human",
            "origin": clean_str(row.get("Origin", "")),
            "origin_comments": clean_str(row.get("OriginComments", "")),
            "clone": clean_str(row.get("Clone", "")),
            "growth_properties": clean_str(row.get("GrowthProperties", "")),
            "medium": clean_str(row.get("Medium", "")),
            "parental_line_name": clean_str(row.get("ParentalLine", "")),
            "acquisition_method": ACQUISITION_MAP.get(
                clean_str(row.get("PurchasedOrInKind", "")), "unknown"),
            "received_date": clean_date(row.get("ReceivedDate", "")),
            "site_id": self.site.pk,
            "ko_validated": clean_bool(row.get("KOconfirmed", "")),
            "ko_validation_notes": clean_str(row.get("KOInfo", "")),
        }
        line = CellLine.objects.using(DB).create(**fields)
        self.counts["cell lines created"] += 1
        self.notes["cell lines created"].append(
            f"{name} {genotype}" + (f" (C-{clean_str(row.get('LabLabel'))})"
                                    if clean_str(row.get("LabLabel")) else ""))
        return line

    def _cell_line_vial(self, line, row):
        """One freeze-down batch, carrying the C-number written on its tubes."""
        access_id = clean_int(row["ID"])
        number = clean_int(row.get("LabLabel", ""))
        label = f"C-{number}" if number else f"Access id {access_id}"

        existing = CellLineVial.objects.using(DB).filter(access_id=access_id).first()
        if existing is not None:
            self.counts["freeze-down batches already imported"] += 1
            return existing

        if number is not None:
            # A typed collision is refused by name — the other box may still be
            # in the freezer, and two batches under one number is exactly the
            # ambiguity `cell_lines.by_c_number` exists to resolve.
            holder = lab_numbers.holder(
                lab_numbers.CELL_LINE, self.site.pk, number, db=DB)
            if holder is not None:
                self.notes["freeze-down batches refused — C-number taken"].append(
                    f"{label} for {line.name}: already carried by "
                    f"{lab_numbers.holder_label(holder)}")
                return None

        vial = CellLineVial.objects.using(DB).create(
            cell_line_id=line.pk,
            c_number=number,
            received_date=clean_date(row.get("ReceivedDate", "")),
            acquisition_method=ACQUISITION_MAP.get(
                clean_str(row.get("PurchasedOrInKind", "")), "unknown"),
            thawed=clean_bool(row.get("Thawed", "")),
            location_original_vial=clean_str(row.get("LocationOriginalVial", "")),
            notes=clean_str(row.get("Comments", "")),
            site_id=self.site.pk,
            access_id=access_id,
        )
        self.counts["freeze-down batches created"] += 1
        if not self.skip_inventory:
            self._vial_storage(vial, row)
        return vial

    def _vial_storage(self, vial, row):
        for storage_type, cols in (
            ("ln2", (("TankLN", "freezer"), ("RackLN", "rack"),
                     ("BoxLN", "box"), ("VialsLN", "position"))),
            ("-80", (("RackNeg80", "rack"), ("TrayNeg80", "shelf"),
                     ("BoxNeg80", "box"), ("VialsNeg80", "position"))),
        ):
            values = {field: clean_str(row.get(col, "")) for col, field in cols}
            if not any(values.values()):
                continue
            InventoryLocation.objects.using(DB).update_or_create(
                vial_id=vial.pk, storage_type=storage_type, site_id=self.site.pk,
                defaults=values)
            self.counts["cell-line storage locations"] += 1

    def _cellosaurus(self, raw):
        """The Cellosaurus id out of an Access hyperlink, or ``""``.

        Same shape as the antibody RRID: the export wraps it as
        ``CVCL_0037#http://CVCL_0037#``, and one of the six is that broken form
        while the rest carry a real URL. The identifier is the part worth
        keeping, and it reads the same out of either.
        """
        text = clean_str(raw)
        for part in text.replace("#", " ").replace("/", " ").split():
            if part.upper().startswith(_CVCL_PREFIX):
                return part
        return ""

    # ------------------------------------------------------------------
    # 4. Experiments
    # ------------------------------------------------------------------
    def _experiments(self, csv_dir):
        self._session_cache = {}
        self._wb(read_csv(csv_dir, "Wb_new.csv"))
        self._ip(read_csv(csv_dir, "IP_new.csv"))
        self._if(read_csv(csv_dir, "IF_new.csv"))
        self.stdout.write(self.style.SUCCESS(
            f"    → {self.counts['sessions created']} sessions, "
            f"{self.counts['readings created']} readings created "
            f"({self.counts['readings already imported']} already imported)"))

    def _session(self, procedure, row, ab_access_id, member_col,
                 wt_access_id=None, ko_access_id=None):
        """The session a reading belongs to: one gene, one day, one person.

        The original import grouped *all* of history into one session per
        procedure per target, because it had no better handle on rows going
        back to 2019. These rows do: a run is a panel of antibodies against one
        gene, put up on one gel or one plate on one day by one person, and that
        is what the dates show — 392 readings fall into 43 such runs, of 3 to 22
        antibodies each. Grouping them by target alone would file eight months
        of separate experiments as one, and hang them on one arbitrary date.
        """
        antibody = self._antibody_for_access(ab_access_id)
        if antibody is None or antibody.target_id is None:
            self.notes["readings skipped — antibody not on file"].append(
                f"{procedure} reading {clean_str(row.get('ID'))}: "
                f"Access antibody {clean_str(ab_access_id)}")
            return None, None
        when = clean_date(row.get("When", ""))
        member = self._member(row.get(member_col, ""))
        key = (procedure, antibody.target_id, when, pk(member))

        if key in self._session_cache:
            return self._session_cache[key], antibody

        session = ExperimentSession.objects.using(DB).filter(
            procedure_type=procedure, target_id=antibody.target_id,
            date=when, experimenter_id=pk(member), site_id=self.site.pk,
        ).first()
        if session is None:
            if member is None:
                # Attribution is a fact about who did the work; guessing it is
                # worse than leaving the run unfiled.
                self.notes["readings skipped — no experimenter"].append(
                    f"{procedure} on {when} ({member_col} "
                    f"{clean_str(row.get(member_col))})")
                return None, None
            session = ExperimentSession.objects.using(DB).create(
                procedure_type=procedure,
                target_id=antibody.target_id,
                experimenter_id=member.pk,
                date=when,
                site_id=self.site.pk,
                cell_line_wt_id=pk(self._cell_line_for_access(wt_access_id)),
                cell_line_ko_id=pk(self._cell_line_for_access(ko_access_id)),
                status="complete",
            )
            self.counts["sessions created"] += 1
        self._session_cache[key] = session
        return session, antibody

    def _reading_exists(self, model, access_id):
        return model.objects.using(DB).filter(access_id=access_id).exists()

    def _wb(self, rows):
        self.stdout.write(f"\n  New readings: {len(rows)} WB")
        for row in rows:
            access_id = clean_int(row["ID"])
            if self._reading_exists(WbResult, access_id):
                self.counts["readings already imported"] += 1
                continue
            session, antibody = self._session(
                "WB", row, row.get("AntibodiesID", ""), "MembersID",
                row.get("lane1CellLineID", ""), row.get("lane2CellLineID", ""))
            if session is None:
                self.counts["readings skipped"] += 1
                continue

            extra_lanes = []
            for i in (3, 4):
                value = clean_str(row.get(f"lane{i}CellLineID", ""))
                if value and value != "0":
                    line = self._cell_line_for_access(value)
                    extra_lanes.append({"cell_line_id": pk(line),
                                        "access_cell_line_id": clean_int(value),
                                        "label": f"Lane {i}"})

            WbResult.objects.using(DB).create(
                session_id=session.pk, antibody_id=antibody.pk,
                signal=clean_str(row.get("SpecificSignal", "")),
                rating=clean_str(row.get("SelectiveSignal", "")),
                dilution=clean_str(row.get("1AbDilution", "")),
                gel=clean_str(row.get("Gel", "")),
                membrane=clean_str(row.get("Membrane", "")),
                ecl=clean_str(row.get("ECL", "")),
                detection_system=clean_str(row.get("DetectionSystem", "")),
                primary_ab_dilution=clean_str(row.get("1AbDilution", "")),
                secondary_ab=clean_str(row.get("2ndaryAb", "")),
                secondary_ab_dilution=clean_str(row.get("2ndaryAbDilution", "")),
                extra_lanes=extra_lanes,
                comments=clean_str(row.get("Comments", "")),
                access_id=access_id,
            )
            self.counts["readings created"] += 1

    def _ip(self, rows):
        self.stdout.write(f"  New readings: {len(rows)} IP")
        for row in rows:
            access_id = clean_int(row["ID"])
            if self._reading_exists(IpResult, access_id):
                self.counts["readings already imported"] += 1
                continue
            session, antibody = self._session(
                "IP", row, row.get("AntibodiesID", ""), "MemberID",
                row.get("CellLineID", ""))
            if session is None:
                self.counts["readings skipped"] += 1
                continue
            IpResult.objects.using(DB).create(
                session_id=session.pk, antibody_id=antibody.pk,
                enrichment=clean_str(row.get("Enrichment", "")),
                amount_of_antibody=clean_str(row.get("AmountOfAb", "")),
                amount_of_lysate=clean_str(row.get("AmountOfLysate", "")),
                protein_concentration=clean_str(
                    row.get("ConcentrationOfProtein(mg/mL)", "")),
                volume_of_lysate_ml=clean_str(
                    row.get("VolumeOfLysateOrMedium(mL)", "")),
                bead_type=clean_str(row.get("Beads", "")),
                lysis_buffer=clean_str(row.get("LysisBuffer", "")),
                detection_ab=clean_str(row.get("1AbUsedForWb", "")),
                detection_ab_dilution=clean_str(row.get("1AbDilution", "")),
                secondary_ab=clean_str(row.get("2ndaryAbForWb", "")),
                secondary_ab_dilution=clean_str(row.get("2ndaryAbDilution", "")),
                gel=clean_str(row.get("Gel", "")),
                membrane=clean_str(row.get("Membrane", "")),
                ecl=clean_str(row.get("ECL", "")),
                detection_system=clean_str(row.get("DetectionSystem", "")),
                comments=clean_str(row.get("Comments", "")),
                access_id=access_id,
            )
            self.counts["readings created"] += 1

    def _if(self, rows):
        self.stdout.write(f"  New readings: {len(rows)} IF")
        for row in rows:
            access_id = clean_int(row["ID"])
            if self._reading_exists(IfResult, access_id):
                self.counts["readings already imported"] += 1
                continue
            session, antibody = self._session(
                "IF", row, row.get("AntibodiesID", ""), "MemberID",
                row.get("CellLine1ID", ""), row.get("CellLine2ID", ""))
            if session is None:
                self.counts["readings skipped"] += 1
                continue
            IfResult.objects.using(DB).create(
                session_id=session.pk, antibody_id=antibody.pk,
                specific_signal=clean_str(row.get("SpecificSignal", "")),
                wt_ko_ratio_1=clean_decimal(row.get("WTKOratio1", ""), 4),
                wt_ko_ratio_2=clean_decimal(row.get("WTKOratio2", ""), 4),
                concentration_1=clean_str(row.get("TestedConcentration1", "")),
                concentration_2=clean_str(row.get("TestedConcentration2", "")),
                best_concentration=clean_str(row.get("BestConcentration", "")),
                fixative=clean_str(row.get("Fixative", "")),
                blocking=clean_str(row.get("BlockingCondition", "")),
                permeabilisation=clean_str(row.get("Permeabilization", "")),
                primary_ab_dilution=clean_str(row.get("1AbDilution", "")),
                dilution_buffer=clean_str(row.get("DilutionBuffer", "")),
                primary_ab_condition=clean_str(row.get("1AbCondition", "")),
                secondary_ab=clean_str(row.get("SecondaryAb", "")),
                secondary_ab_condition=clean_str(row.get("SecondaryAbCondition", "")),
                plate_number=clean_str(row.get("PlateNumber", "")),
                well_number=clean_str(row.get("WellNumber", "")),
                image_acquired_by=clean_str(row.get("ImageAcquisitionBy", "")),
                image_analysed_by=clean_str(row.get("ImageAnalyzedBy", "")),
                microscope=clean_str(row.get("Microscope", "")),
                objective=clean_str(row.get("Objective", "")),
                comments=clean_str(row.get("Comments", "")),
                access_id=access_id,
            )
            self.counts["readings created"] += 1

    # ------------------------------------------------------------------
    # 5. Corrections to records already on the site
    # ------------------------------------------------------------------
    def _corrections(self, csv_dir):
        self.stdout.write("\n  Corrections to existing records")
        self._correct_antibodies(read_csv(csv_dir, "Antibodies_changed.csv"))
        self._correct_cell_lines(read_csv(csv_dir, "CellLines_changed.csv"))
        self._correct_results(read_csv(csv_dir, "Wb_changed.csv"), WbResult,
                              WB_FIELDS, "WB", paired=WB_PAIRED)
        self._correct_results(read_csv(csv_dir, "IF_changed.csv"), IfResult,
                              IF_FIELDS, "IF", decimals=IF_DECIMAL_FIELDS)
        self.stdout.write(self.style.SUCCESS(
            f"    → {self.counts['cells corrected']} cells corrected, "
            f"{self.counts['cells refused']} refused, "
            f"{self.counts['cells ignored']} in columns the site does not store"))

    def _apply_cell(self, obj, field, new_value, old_value, what, column):
        """Write one corrected cell, if it is safe to.

        Two refusals, both counted and named. A blank `NewValue` never clears a
        stored value — "not written down" is not "a different one". And the
        correction has to match what it claims to correct: the export says what
        it believed the site held, so if the site holds something else the value
        was edited on a board since March and this is not the authority on it.
        """
        if new_value in ("", None):
            self.counts["cells left — export blanked them"] += 1
            self.notes["cells the export blanked (stored value kept)"].append(
                f"{what} {column}: site keeps {getattr(obj, field)!r}")
            return False

        current = getattr(obj, field)

        # Asked before the precondition, or a row that already says the right
        # thing is reported as a refusal — which reads as work still to do on a
        # cell where there is none. Several say it already because the Access
        # rows that merged into one live record disagreed among themselves.
        if self._same(current, new_value):
            self.counts["cells already correct"] += 1
            return False

        if not self._same(current, old_value):
            self.counts["cells refused"] += 1
            self.notes["cells refused — the site has moved on"].append(
                f"{what} {column}: export expected {old_value!r}, "
                f"site holds {current!r}, new value {new_value!r} not applied")
            return False

        setattr(obj, field, new_value)
        self.counts["cells corrected"] += 1
        return True

    @staticmethod
    def _same(current, raw):
        """Whether a stored value and an Access cell say the same thing."""
        if current is None:
            return clean_str(raw) == ""
        if isinstance(current, bool):
            return current == clean_bool(raw)
        if hasattr(current, "isoformat"):
            return current == clean_date(raw)
        return str(current).strip() == clean_str(raw)

    def _correct_antibodies(self, rows):
        for access_id, cells in self._by_id(rows):
            obj = Antibody.objects.using(DB).filter(access_id=access_id).first()
            if obj is None:
                self.counts["cells refused"] += 1
                self.notes["corrections skipped — record not on file"].append(
                    f"antibody Access id {access_id}")
                continue
            what = f"A-{obj.ab_number}" if obj.ab_number else f"antibody {obj.pk}"
            dirty = []
            for column, old, new in cells:
                if column in ANTIBODY_IDENTITY:
                    self.counts["cells refused"] += 1
                    self.notes["identity changes — not applied here"].append(
                        f"{what} {column}: {old!r} → {new!r}. Catalogue, supplier, "
                        f"gene and lot are what the vial is; change it on the "
                        f"board's identity dialog, which checks for a clash.")
                elif column in ANTIBODY_INVENTORY:
                    self._antibody_inventory_cell(obj, column, old, new, what)
                elif column == "Clonality":
                    mapped = CLONALITY_MAP.get(clean_str(new), "unknown")
                    if self._apply_cell(obj, "clonality", mapped,
                                        CLONALITY_MAP.get(clean_str(old), "unknown"),
                                        what, column):
                        dirty.append("clonality")
                elif column in ANTIBODY_FIELDS:
                    field = ANTIBODY_FIELDS[column]
                    if self._apply_cell(obj, field, clean_str(new),
                                        clean_str(old), what, column):
                        dirty.append(field)
                elif column in ANTIBODY_BOOL_FIELDS:
                    field = ANTIBODY_BOOL_FIELDS[column]
                    if self._apply_cell(obj, field, clean_bool(new),
                                        clean_bool(old), what, column):
                        dirty.append(field)
                else:
                    self._ignore(what, column, new)
            if dirty:
                obj.save(using=DB, update_fields=dirty)

    def _antibody_inventory_cell(self, antibody, column, old, new, what):
        if self.skip_inventory:
            self.counts["cells skipped — inventory"] += 1
            return
        loc = InventoryLocation.objects.using(DB).filter(
            antibody_id=antibody.pk, site_id=self.site.pk).first()
        if column == "BoxNumber":
            if loc is None:
                if clean_str(new):
                    InventoryLocation.objects.using(DB).create(
                        antibody_id=antibody.pk, site_id=self.site.pk,
                        storage_type="-20", box=clean_str(new))
                    self.counts["cells corrected"] += 1
                return
            if self._apply_cell(loc, "box", clean_str(new), clean_str(old),
                                what, column):
                loc.save(using=DB, update_fields=["box"])
        elif column in ("Fridge4C", "Neg80C") and loc is not None:
            wanted = "4c" if column == "Fridge4C" else "-80"
            if clean_bool(new) and loc.storage_type != wanted:
                loc.storage_type = wanted
                loc.save(using=DB, update_fields=["storage_type"])
                self.counts["cells corrected"] += 1

    def _correct_cell_lines(self, rows):
        for access_id, cells in self._by_id(rows):
            line = self._cell_line_for_access(access_id)
            vial = CellLineVial.objects.using(DB).filter(access_id=access_id).first()
            if line is None:
                self.counts["cells refused"] += 1
                self.notes["corrections skipped — record not on file"].append(
                    f"cell line Access id {access_id}")
                continue
            what = f"{line.name} (Access id {access_id})"
            line_dirty, vial_dirty = [], []
            for column, old, new in cells:
                if column in CELL_LINE_IDENTITY:
                    self.counts["cells deferred — identity"] += 1
                    self.notes["cell-line identity — see fix_cell_line_identity"].append(
                        f"{what} {column}: {old!r} → {new!r}")
                elif column in CELL_LINE_INVENTORY:
                    self._cell_line_inventory_cell(vial, column, old, new, what)
                elif column == "RRID":
                    value = self._cellosaurus(new)
                    if value and self._apply_cell(
                            line, "cellosaurus_id", value,
                            self._cellosaurus(old), what, column):
                        line_dirty.append("cellosaurus_id")
                elif column in CELL_LINE_FIELDS:
                    field = CELL_LINE_FIELDS[column]
                    if self._apply_cell(line, field, clean_str(new),
                                        clean_str(old), what, column):
                        line_dirty.append(field)
                elif column in CELL_LINE_BOOL_FIELDS:
                    field = CELL_LINE_BOOL_FIELDS[column]
                    if self._apply_cell(line, field, clean_bool(new),
                                        clean_bool(old), what, column):
                        line_dirty.append(field)
                elif vial is not None and column in CELL_LINE_VIAL_FIELDS:
                    field = CELL_LINE_VIAL_FIELDS[column]
                    if self._apply_cell(vial, field, clean_str(new),
                                        clean_str(old), what, column):
                        vial_dirty.append(field)
                elif vial is not None and column in CELL_LINE_VIAL_BOOL_FIELDS:
                    field = CELL_LINE_VIAL_BOOL_FIELDS[column]
                    if self._apply_cell(vial, field, clean_bool(new),
                                        clean_bool(old), what, column):
                        vial_dirty.append(field)
                elif vial is not None and column in CELL_LINE_VIAL_DATE_FIELDS:
                    field = CELL_LINE_VIAL_DATE_FIELDS[column]
                    if self._apply_cell(vial, field, clean_date(new),
                                        clean_date(old), what, column):
                        vial_dirty.append(field)
                else:
                    self._ignore(what, column, new)
            if line_dirty:
                line.save(using=DB, update_fields=line_dirty)
            if vial_dirty:
                vial.save(using=DB, update_fields=vial_dirty)

    def _cell_line_inventory_cell(self, vial, column, old, new, what):
        if self.skip_inventory:
            self.counts["cells skipped — inventory"] += 1
            return
        if vial is None:
            self.counts["cells refused"] += 1
            self.notes["corrections skipped — record not on file"].append(
                f"{what}: no freeze-down batch to hold {column}")
            return
        storage_type, field = CELL_LINE_INVENTORY[column]
        loc = InventoryLocation.objects.using(DB).filter(
            vial_id=vial.pk, storage_type=storage_type, site_id=self.site.pk).first()
        if loc is None:
            if clean_str(new):
                InventoryLocation.objects.using(DB).create(
                    vial_id=vial.pk, storage_type=storage_type,
                    site_id=self.site.pk, **{field: clean_str(new)})
                self.counts["cells corrected"] += 1
            return
        if self._apply_cell(loc, field, clean_str(new), clean_str(old), what, column):
            loc.save(using=DB, update_fields=[field])

    def _correct_results(self, rows, model, field_map, what_kind,
                         paired=None, decimals=None):
        paired = paired or {}
        decimals = decimals or {}
        for access_id, cells in self._by_id(rows):
            obj = model.objects.using(DB).filter(access_id=access_id).first()
            if obj is None:
                self.counts["cells refused"] += 1
                self.notes["corrections skipped — record not on file"].append(
                    f"{what_kind} reading Access id {access_id}")
                continue
            what = f"{what_kind} reading {access_id}"
            dirty = []
            for column, old, new in cells:
                if column in paired:
                    for field in paired[column]:
                        if self._apply_cell(obj, field, clean_str(new),
                                            clean_str(old), what, column):
                            dirty.append(field)
                elif column in decimals:
                    field = decimals[column]
                    if self._apply_cell(obj, field, clean_decimal(new, 4),
                                        clean_decimal(old, 4), what, column):
                        dirty.append(field)
                elif column in field_map:
                    field = field_map[column]
                    if self._apply_cell(obj, field, clean_str(new),
                                        clean_str(old), what, column):
                        dirty.append(field)
                elif column in SESSION_LEVEL_COLUMNS:
                    self.counts["cells deferred — session level"] += 1
                    self.notes["session-level changes — move the reading by hand"].append(
                        f"{what} {column}: {clean_str(old)!r} → {clean_str(new)!r} "
                        f"(session {obj.session_id})")
                else:
                    self._ignore(what, column, new)
            if dirty:
                obj.save(using=DB, update_fields=dirty)

    def _ignore(self, what, column, new):
        self.counts["cells ignored"] += 1
        self.notes["columns the site does not store"].append(
            f"{what} {column} → {new!r}")

    @staticmethod
    def _by_id(rows):
        """Changed cells grouped by record, so each record is saved once."""
        grouped = defaultdict(list)
        for row in rows:
            grouped[clean_int(row["ID"])].append(
                (clean_str(row["Column"]), row.get("OldValue", ""),
                 row.get("NewValue", "")))
        return sorted(grouped.items())

    # ------------------------------------------------------------------
    # 6. Targets — DOIs, publication dates, status
    # ------------------------------------------------------------------
    def _targets(self, csv_dir):
        rows = read_csv(csv_dir, "Proteins_changed.csv")
        self.stdout.write("\n  Target publication changes")

        for access_id, cells in self._by_id(rows):
            target = Target.objects.using(DB).filter(access_id=access_id).first()
            if target is None:
                self.counts["cells refused"] += 1
                self.notes["corrections skipped — record not on file"].append(
                    f"target Access id {access_id}")
                continue
            gene = target.gene_name or target.protein_name or f"target {target.pk}"
            report = None
            for column, old, new in cells:
                if column == "Status":
                    mapped = STATUS_MAP.get(clean_str(new))
                    if mapped and target.status != mapped:
                        target.status = mapped
                        target.save(using=DB, update_fields=["status"])
                        self.counts["target statuses updated"] += 1
                        self.notes["target statuses updated"].append(
                            f"{gene} → {clean_str(new)}")
                elif column in ("ZenodoDOI", "F1000DOI",
                                "DateAddedZenodo", "DateAddedF1000"):
                    report = report or self._report_for(target)
                    self._publication_cell(report, gene, column, old, new)
                elif column == "AlternProteinName":
                    if self._apply_cell(target, "alternative_name", clean_str(new),
                                        clean_str(old), gene, column):
                        target.save(using=DB, update_fields=["alternative_name"])
                else:
                    self._ignore(gene, column, new)

        self.stdout.write(self.style.SUCCESS(
            f"    → {self.counts['publication cells updated']} DOIs and dates, "
            f"{self.counts['target statuses updated']} statuses"))

    def _report_for(self, target):
        """The `Report` row this target's publication columns belong to.

        `target_board.report_showing` is what the board draws and saves through;
        a target with two reports would otherwise get its DOI read off one row
        and written to another. Falls back to the first on file, then to a new
        one, exactly as that reader's docstring says a caller should.
        """
        from pipeline.services.target_board import report_showing
        reports = list(Report.objects.using(DB).filter(target_id=target.pk).order_by("pk"))
        return (report_showing(reports, "zenodo")
                or report_showing(reports, "f1000")
                or (reports[0] if reports else
                    Report.objects.using(DB).create(target_id=target.pk,
                                                    status="generated")))

    def _publication_cell(self, report, gene, column, old, new):
        if column.endswith("DOI"):
            field = "zenodo_doi" if column.startswith("Zenodo") else "f1000_doi"
            # Access wraps a DOI as `url#url#`; both sides unwrap the same way,
            # or every cell would look like a change.
            value, expected = clean_doi(new), clean_doi(old)
        else:
            field = ("zenodo_date" if column.endswith("Zenodo") else "f1000_date")
            value, expected = clean_date(new), clean_date(old)

        if self._apply_cell(report, field, value, expected, gene, column):
            report.save(using=DB, update_fields=[field])
            self.counts["publication cells updated"] += 1
            self.notes["publication changes"].append(f"{gene} {column} → {value}")

    # ------------------------------------------------------------------
    def _report(self):
        self.stdout.write(self.style.MIGRATE_HEADING("\nSummary"))
        for key in sorted(self.counts):
            if key.startswith("_"):
                continue
            self.stdout.write(f"  {self.counts[key]:>5}  {key}")

        # A count with no list under it invents the noun, so every one of these
        # prints what it counted.
        for heading in sorted(self.notes):
            items = self.notes[heading]
            self.stdout.write(self.style.WARNING(f"\n  {len(items)} {heading}:"))
            for item in items[:40]:
                self.stdout.write(f"    · {item}")
            if len(items) > 40:
                self.stdout.write(f"    … and {len(items) - 40} more")

        if self.dry_run:
            self.stdout.write(self.style.WARNING(
                "\nDRY RUN — everything above was rolled back. "
                "Re-run with --apply to write it."))
        else:
            self.stdout.write(self.style.SUCCESS("\nWritten."))
