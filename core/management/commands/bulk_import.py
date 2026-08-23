"""
Django Management Command: bulk_import
Bulk imports antibody data from a CSV file and optional image folder.

Usage:
    python manage.py bulk_import data.csv --images ./images/
    python manage.py bulk_import data.csv                         # metadata only
    python manage.py bulk_import data.csv --images ./images/ --dry-run  # preview without saving

Place this file at:
    OGA_website/core/management/commands/bulk_import.py

And make sure these __init__.py files exist (create them empty if not):
    OGA_website/core/management/__init__.py
    OGA_website/core/management/commands/__init__.py
"""

import csv
import os
import re
import glob

from django.core.management.base import BaseCommand, CommandError
from django.core.files import File
from django.db import transaction

from core.models import Gene, Antibody, Experiment, Description


EXPERIMENT_TYPES = ["WB", "IP", "ICC-IF", "FC"]

# Normalised aliases so CSVs and filenames are forgiving
EXPERIMENT_TYPE_ALIASES = {
    "WB": "WB",
    "WESTERN": "WB",
    "WESTERN BLOT": "WB",
    "IP": "IP",
    "IMMUNOPRECIPITATION": "IP",
    "ICC-IF": "ICC-IF",
    "ICCIF": "ICC-IF",
    "ICC_IF": "ICC-IF",
    "ICC/IF": "ICC-IF",
    "IF": "ICC-IF",
    "FC": "FC",
    "FC-INTRA": "FC",
    "FLOW": "FC",
    "FLOW CYTOMETRY": "FC",
}

# Values treated as boolean True in CSV columns
TRUTHY_VALUES = {"yes", "y", "true", "1", "x", "✓", "✔"}

# Values treated as explicit boolean False in CSV columns
FALSY_VALUES = {"no", "n", "false", "0"}


class Command(BaseCommand):
    help = "Bulk import antibody metadata (CSV) and experiment images (folder)"

    # ── CLI arguments ──────────────────────────────────────────────────
    def add_arguments(self, parser):
        parser.add_argument(
            "csv_file",
            type=str,
            help="Path to the CSV file with antibody metadata",
        )
        parser.add_argument(
            "--images",
            type=str,
            default=None,
            help="Path to folder containing experiment images "
                 "(named GeneName_CatalogueNumber_ExperimentType.ext)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview what would be imported without saving anything",
        )
        parser.add_argument(
            "--f1000-link",
            type=str,
            default=None,
            help="F1000 report URL to set on the Gene",
        )
        parser.add_argument(
            "--citation",
            type=str,
            default=None,
            help="Citation text to set on the Gene",
        )
        parser.add_argument(
            "--cell-line-link",
            type=str,
            default=None,
            help="Cell line URL to set on the Gene",
        )

    # ── Main logic ─────────────────────────────────────────────────────
    def handle(self, *args, **options):
        csv_path = options["csv_file"]
        images_dir = options["images"]
        dry_run = options["dry_run"]
        f1000_link = options["f1000_link"]
        citation = options["citation"]
        cell_line_link = options["cell_line_link"]

        if not os.path.isfile(csv_path):
            raise CommandError(f"CSV file not found: {csv_path}")

        if images_dir and not os.path.isdir(images_dir):
            raise CommandError(f"Images directory not found: {images_dir}")

        # Build image lookup: { "GENENAME|CATNUM|WB": filepath, ... }
        image_lookup = {}
        if images_dir:
            image_lookup = self._build_image_lookup(images_dir)
            self.stdout.write(
                f"  Found {len(image_lookup)} image files in {images_dir}"
            )

        # Read CSV
        rows = self._read_csv(csv_path)
        self.stdout.write(f"  Read {len(rows)} rows from {csv_path}")

        if dry_run:
            self.stdout.write(self.style.WARNING("\n  *** DRY RUN — nothing will be saved ***\n"))

        stats = {
            "genes_created": 0,
            "genes_updated": 0,
            "antibodies_created": 0,
            "antibodies_updated": 0,
            "descriptions_created": 0,
            "descriptions_updated": 0,
            "experiments_created": 0,
            "experiments_updated": 0,
            "images_matched": 0,
            "images_missing": 0,
            "errors": [],
        }

        try:
            with transaction.atomic():
                for i, row in enumerate(rows, start=1):
                    self._process_row(
                        row, i, image_lookup, f1000_link, citation,
                        cell_line_link, stats, dry_run
                    )

                if dry_run:
                    raise _DryRunRollback()

        except _DryRunRollback:
            pass

        # ── Summary ────────────────────────────────────────────────────
        self.stdout.write("\n" + "=" * 60)
        self.stdout.write(self.style.SUCCESS("  IMPORT SUMMARY"))
        self.stdout.write("=" * 60)
        self.stdout.write(f"  Genes       : {stats['genes_created']} created, {stats['genes_updated']} updated")
        self.stdout.write(f"  Antibodies  : {stats['antibodies_created']} created, {stats['antibodies_updated']} updated")
        self.stdout.write(f"  Descriptions: {stats['descriptions_created']} created, {stats['descriptions_updated']} updated")
        self.stdout.write(f"  Experiments : {stats['experiments_created']} created, {stats['experiments_updated']} updated")
        self.stdout.write(f"  Images      : {stats['images_matched']} matched, {stats['images_missing']} not found")

        if stats["errors"]:
            self.stdout.write(self.style.ERROR(f"\n  {len(stats['errors'])} ERRORS:"))
            for err in stats["errors"]:
                self.stdout.write(self.style.ERROR(f"    • {err}"))
        else:
            self.stdout.write(self.style.SUCCESS("\n  No errors!"))

        if dry_run:
            self.stdout.write(self.style.WARNING("\n  (Dry run — no changes were saved)"))

    # ── Process a single CSV row ───────────────────────────────────────
    def _process_row(self, row, row_num, image_lookup, f1000_link, citation,
                     cell_line_link, stats, dry_run):
        gene_name = row.get("gene_name", "").strip()
        catalogue_number = row.get("catalogue_number", "").strip()

        if not gene_name:
            stats["errors"].append(f"Row {row_num}: missing gene_name")
            return
        if not catalogue_number:
            stats["errors"].append(f"Row {row_num}: missing catalogue_number")
            return

        self.stdout.write(f"\n  Row {row_num}: {gene_name} / {catalogue_number}")

        # ── Gene ───────────────────────────────────────────────────────
        gene, created = Gene.objects.get_or_create(name=gene_name)
        if created:
            stats["genes_created"] += 1
            self.stdout.write(f"    Gene '{gene_name}' CREATED")
        else:
            stats["genes_updated"] += 1
            self.stdout.write(f"    Gene '{gene_name}' already exists")

        gene_changed = False
        if f1000_link and gene.f1000_report_link != f1000_link:
            gene.f1000_report_link = f1000_link
            gene_changed = True
        if citation and gene.citation != citation:
            gene.citation = citation
            gene_changed = True
        if cell_line_link and gene.cell_line_link != cell_line_link:
            gene.cell_line_link = cell_line_link
            gene_changed = True
        if gene_changed:
            gene.save()

        # ── Antibody ──────────────────────────────────────────────────
        antibody, created = Antibody.objects.get_or_create(
            name=catalogue_number,
            gene=gene,
        )
        if created:
            stats["antibodies_created"] += 1
            self.stdout.write(f"    Antibody '{catalogue_number}' CREATED")
        else:
            stats["antibodies_updated"] += 1
            self.stdout.write(f"    Antibody '{catalogue_number}' already exists")

        # ── Description ───────────────────────────────────────────────
        raw_clonality = row.get("clonality", "").strip()
        recombinant, clean_clonality = self._parse_clonality(raw_clonality)

        clone_id_val = row.get("clone_id", "").strip()
        if clone_id_val == "-":
            clone_id_val = ""

        desc_fields = {
            "rrid": row.get("rrid", "").strip() or None,
            "supplier": row.get("company", "").strip() or None,
            "host": row.get("host", "").strip() or None,
            "clonality": clean_clonality or None,
            "clone_ID": clone_id_val or None,
            "recombinant": recombinant,
            "product_link": row.get("product_link", "").strip() or None,
            "discontinued": self._parse_bool(row.get("discontinued", "")),
            "wb_app": self._parse_bool(row.get("wb_recommended", "")),
            "icc_if_app": self._parse_bool(row.get("icc_if_recommended", "")),
            "ip_app": self._parse_bool(row.get("ip_recommended", "")),
            "fc_app": self._parse_bool(row.get("fc_recommended", "")),
        }

        # For new records, convert None booleans to False
        defaults_for_create = {
            k: (False if v is None and k in ("discontinued", "wb_app", "icc_if_app", "ip_app", "fc_app") else v)
            for k, v in desc_fields.items()
        }
        desc, created = Description.objects.get_or_create(
            antibody=antibody,
            defaults=defaults_for_create,
        )
        if created:
            stats["descriptions_created"] += 1
            self.stdout.write(f"    Description CREATED")
        else:
            updated = False
            for field, value in desc_fields.items():
                if hasattr(desc, field):
                    current = getattr(desc, field)
                    # For booleans: None = skip, True/False = apply
                    if value is None:
                        continue
                    if isinstance(value, bool):
                        if current != value:
                            setattr(desc, field, value)
                            updated = True
                    elif value and current != value:
                        setattr(desc, field, value)
                        updated = True
            if updated:
                desc.save()
                stats["descriptions_updated"] += 1
                self.stdout.write(f"    Description UPDATED")
            else:
                stats["descriptions_updated"] += 1
                self.stdout.write(f"    Description unchanged")

        # ── Experiment images ─────────────────────────────────────────
        if image_lookup:
            for exp_type in EXPERIMENT_TYPES:
                lookup_key = self._make_lookup_key(gene_name, catalogue_number, exp_type)
                image_path = image_lookup.get(lookup_key)

                if image_path:
                    stats["images_matched"] += 1
                    self.stdout.write(
                        f"    Image MATCHED: {exp_type} → {os.path.basename(image_path)}"
                    )
                    if not dry_run:
                        self._save_experiment_image(antibody, exp_type, image_path, stats)
                    else:
                        exp_exists = Experiment.objects.filter(
                            antibody=antibody, experiment_type=exp_type
                        ).exists()
                        if exp_exists:
                            stats["experiments_updated"] += 1
                        else:
                            stats["experiments_created"] += 1
                else:
                    stats["images_missing"] += 1
                    self.stdout.write(f"    Image not found: {exp_type} (key: '{lookup_key}')")

    # ── Save an experiment image ───────────────────────────────────────
    def _save_experiment_image(self, antibody, exp_type, image_path, stats):
        experiment, created = Experiment.objects.get_or_create(
            antibody=antibody,
            experiment_type=exp_type,
        )

        with open(image_path, "rb") as f:
            filename = os.path.basename(image_path)
            experiment.file_path.save(filename, File(f), save=True)

        if created:
            stats["experiments_created"] += 1
            self.stdout.write(f"      Experiment '{exp_type}' CREATED with image")
        else:
            stats["experiments_updated"] += 1
            self.stdout.write(f"      Experiment '{exp_type}' UPDATED with image")

    # ── Parse "Recombinant Monoclonal" → ("Yes", "Monoclonal") ────────
    def _parse_clonality(self, raw):
        if not raw:
            return ("No", "")
        if re.match(r"recombinant\s+", raw, re.IGNORECASE):
            clean = re.sub(r"recombinant\s+", "", raw, flags=re.IGNORECASE).strip()
            return ("Yes", raw)
        return ("No", raw)

    # ── Parse boolean CSV values ───────────────────────────────────────
    def _parse_bool(self, value):
        """Parse a CSV value as boolean. Blank = None (skip), Yes = True, No = False."""
        if not value:
            return None
        val = value.strip().lower()
        if val in TRUTHY_VALUES:
            return True
        if val in FALSY_VALUES:
            return False
        return None

    # ── Build image lookup dictionary ──────────────────────────────────
    def _build_image_lookup(self, images_dir):
        """
        Scan the images folder and build a lookup dict.
        Expected naming: GeneName_CatalogueNumber_ExperimentType.ext

        Examples:
            MMP7_ab176325_WB.png
            MMP7_10374-2-AP_ICC-IF.png
        """
        lookup = {}
        extensions = ("*.png", "*.jpg", "*.jpeg", "*.svg", "*.tif", "*.tiff", "*.gif", "*.webp")

        for ext in extensions:
            for filepath in glob.glob(os.path.join(images_dir, ext)):
                filename = os.path.basename(filepath)
                name_no_ext = os.path.splitext(filename)[0]

                parts = name_no_ext.rsplit("_", 1)
                if len(parts) != 2:
                    self.stdout.write(
                        self.style.WARNING(f"    Skipping unrecognised filename: {filename}")
                    )
                    continue

                prefix, raw_exp_type = parts
                exp_type = EXPERIMENT_TYPE_ALIASES.get(raw_exp_type.upper().strip())

                if not exp_type:
                    self.stdout.write(
                        self.style.WARNING(
                            f"    Skipping unknown experiment type '{raw_exp_type}' in: {filename}"
                        )
                    )
                    continue

                gene_cat_parts = prefix.split("_", 1)
                if len(gene_cat_parts) != 2:
                    self.stdout.write(
                        self.style.WARNING(f"    Skipping - can't parse gene/catalogue from: {filename}")
                    )
                    continue

                gene_name, catalogue_number = gene_cat_parts
                key = self._make_lookup_key(gene_name, catalogue_number, exp_type)
                lookup[key] = filepath

        return lookup

    # ── Normalised lookup key ──────────────────────────────────────────
    def _make_lookup_key(self, gene_name, catalogue_number, exp_type):
        return f"{gene_name.upper()}|{catalogue_number.upper()}|{exp_type.upper()}"

    # ── Read and validate CSV ──────────────────────────────────────────
    def _read_csv(self, csv_path):
        rows = []
        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            sample = f.read(2048)
            f.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
            except csv.Error:
                dialect = "excel"

            reader = csv.DictReader(f, dialect=dialect)
            header_map = self._build_header_map(reader.fieldnames or [])

            for raw_row in reader:
                row = {}
                for original_header, value in raw_row.items():
                    normalised = header_map.get(original_header, original_header)
                    row[normalised] = value.strip() if value else ""
                rows.append(row)

        return rows

    def _build_header_map(self, headers):
        """
        Map CSV column names to internal keys.
        Accepts the exact F1000 table headers.
        """
        COLUMN_ALIASES = {
            # gene_name (user must add this column)
            "gene_name": "gene_name",
            "gene": "gene_name",
            "target": "gene_name",
            "protein": "gene_name",
            # catalogue_number → Antibody.name
            "catalogue_number": "catalogue_number",
            "catalogue number": "catalogue_number",
            "catalog_number": "catalogue_number",
            "catalog number": "catalogue_number",
            "cat_number": "catalogue_number",
            "cat number": "catalogue_number",
            "catalogue no": "catalogue_number",
            "catalog no": "catalogue_number",
            "antibody": "catalogue_number",
            # company → Description.supplier
            "company": "company",
            "supplier": "company",
            "vendor": "company",
            "manufacturer": "company",
            # rrid
            "rrid": "rrid",
            "rrid (antibody registry)": "rrid",
            "antibody registry": "rrid",
            # clonality
            "clonality": "clonality",
            # clone_id → Description.clone_ID
            "clone_id": "clone_id",
            "clone id": "clone_id",
            "clone": "clone_id",
            # host
            "host": "host",
            "host species": "host",
            # product_link → Description.product_link
            "product_link": "product_link",
            "product link": "product_link",
            "product url": "product_link",
            "purchase link": "product_link",
            "url": "product_link",
            "link": "product_link",
            # discontinued → Description.discontinued
            "discontinued": "discontinued",
            # recommended applications → Description boolean fields
            "wb_recommended": "wb_recommended",
            "wb recommended": "wb_recommended",
            "wb": "wb_recommended",
            "western blot": "wb_recommended",
            "icc_if_recommended": "icc_if_recommended",
            "icc-if recommended": "icc_if_recommended",
            "icc/if recommended": "icc_if_recommended",
            "icc-if": "icc_if_recommended",
            "icc/if": "icc_if_recommended",
            "ip_recommended": "ip_recommended",
            "ip recommended": "ip_recommended",
            "ip": "ip_recommended",
            "immunoprecipitation": "ip_recommended",
            "fc_recommended": "fc_recommended",
            "fc recommended": "fc_recommended",
            "fc": "fc_recommended",
            "flow cytometry": "fc_recommended",
        }

        mapping = {}
        for header in headers:
            normalised = header.strip().lower()
            if normalised in COLUMN_ALIASES:
                mapping[header] = COLUMN_ALIASES[normalised]
            else:
                mapping[header] = header
        return mapping


class _DryRunRollback(Exception):
    """Raised inside atomic block to trigger rollback during dry runs."""
    pass
