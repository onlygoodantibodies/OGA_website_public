"""Changing what a record *is*, deliberately.

The boards refuse identity inline, and they are right to. An antibody is
``(catalogue, company, target)`` and a cell line is "this gene knocked out in
that parent"; retyping one part in a grid cell turns the row into a different
reagent while every result, figure and session already recorded against it stays
attached. The guides say so, and the field test quoted those sentences back as
the best-written text in the product.

But "refuse it here" only works if there is a *there*. Until now that was the
antibody and cell-line edit pages — the last legacy pages the boards had not
replaced, kept alive solely for this. So this is what replaces them: not an
inline cell, and not a whole-record form either, but a deliberate change with the
consequence shown first.

Three rules.

**Say what is attached before the change, not after.** ``attached_to`` counts the
results, figures and sessions that will follow the row to its new identity. A
number is the difference between "rename this" and "rewrite the history of eleven
experiments".

**Refuse a change that would collide.** ``unique_antibody_per_site_lot`` covers
all five parts, so editing a catalogue number onto a vial that already exists is
an IntegrityError at best and a silent merge of two reagents at worst. It is
checked first and refused by name.

**Go through the writers everything else goes through.** Companies via
``cropper/db.py::resolve_company`` — not ``Company.resolve`` directly, which
knows nothing about the Bio-Techne brand split — and genes via
``resolve_or_create_target``. A supplier typed here must not become the second
spelling of one already on file, nor a third row that makes the split
unreachable.
"""
from __future__ import annotations

from django.db.models import Q

from pipeline.models import (Antibody, CellLine, Company, ExperimentSession,
                             PublicationImage, Target)
from pipeline.services import cell_lines as cell_line_svc
from pipeline.services import targets as target_svc
from pipeline.services.cropper import db as cdb

# A parental line that really is on file, named in a refusal so the example
# is one of this lab's own rows.
_PARENT_EG = "HAP1"

DB = "pipeline_db"

# What an antibody's identity is. Lot and site are not here: they say which vial
# was tested and the board edits them inline, which is the whole distinction.
ANTIBODY_IDENTITY = ("catalogue_number", "company", "gene")

# A cell line's structure. Genotype and parent are identity for the same reason
# the gene is: a session's meaning depends on which line was which. `company` is
# here because the grid refuses it (it goes through ``Company.resolve``, so a
# typed cell would mint a second spelling of a vendor already on file) and the
# board's footnote used to send people to the retired cell-line page to change it
# — which meant nobody could change it at all.
# `clone` is here because a gene and a background define the *knockout* and the
# clone says which one — a separate single-cell origin, often a separate guide.
# Two clones of one KO are two lines, not one line with a note, so the clone is
# on the same footing as the gene and the genotype: change it and the row
# describes a different single-cell line while every session, vial and reading
# recorded against it stays attached.
CELL_LINE_IDENTITY = ("name", "gene", "genotype", "clone", "parent", "company")


def _result_count(antibody) -> int:
    total = 0
    for rel in ("wb_results", "ip_results", "if_results", "fc_results"):
        manager = getattr(antibody, rel, None)
        if manager is not None:
            total += manager.count()
    return total


def attached_to_antibody(antibody) -> dict:
    """What follows this row if its identity changes — shown before, not after."""
    return {
        "results": _result_count(antibody),
        "figures": PublicationImage.objects.using(DB)
                   .filter(antibody_id=antibody.pk).count(),
        "recommended": any(getattr(antibody, f"{a}_recommended", False)
                           for a in ("wb", "ip", "if", "fc")),
    }


def attached_to_cell_line(line) -> dict:
    """Sessions that used this line. A KO line's identity is what makes their
    results mean anything, so this is the number that matters."""
    used_by = (ExperimentSession.objects.using(DB)
               .filter(Q(cell_line_wt_id=line.pk) | Q(cell_line_ko_id=line.pk))
               .count())
    return {"sessions": used_by,
            "derivatives": CellLine.objects.using(DB)
                           .filter(parent_line_id=line.pk).count()}


def antibody_identity(antibody) -> dict:
    return {
        "id": antibody.pk,
        "catalogue_number": antibody.catalogue_number or "",
        "company": antibody.company.name if antibody.company_id else "",
        "gene": antibody.target.gene_name if antibody.target_id else "",
        "lot_number": antibody.lot_number or "",
        "site": antibody.site.name if antibody.site_id else "",
        "attached": attached_to_antibody(antibody),
    }


def cell_line_identity(line) -> dict:
    return {
        "id": line.pk,
        "name": line.name or "",
        "gene": target_svc.gene_of(line.target) if line.target_id else "",
        "genotype": line.genotype or "",
        "clone": line.clone or "",
        "parent": line.parent_line.name if line.parent_line_id else "",
        "company": line.company.name if line.company_id else "",
        "attached": attached_to_cell_line(line),
    }


class Refused(Exception):
    """A change that would damage something, explained in a sentence."""


def _target_for(gene, *, allow_blank=False):
    """The target this gene names — looked up, never created.

    Correcting which gene a reagent is *against* is a correction, not a reason to
    put a new gene on the consortium's master list. There are already too many
    routes that mint a Target (the roadmap has an item about it), and one hiding
    behind a typo in an identity dialog would be the worst of them: it would
    happen silently, on a page whose whole point is that changes here are
    deliberate. So an unknown gene is refused, and it says where genes come from.
    """
    gene = (gene or "").strip()
    if not gene:
        if allow_blank:
            return None
        raise Refused("A gene is required — an antibody without one cannot be "
                      "found by anybody looking for it.")
    target = Target.objects.using(DB).filter(gene_name__iexact=gene).first()
    if target is None:
        raise Refused(
            f"'{gene}' is not on the target list. Add it on the target board, or "
            f"look it up first on Add a gene — correcting a record here should "
            f"not quietly put a new gene on the master list.")
    return target


def find_vial_clash(antibody, *, catalogue=None, company_id=None, target_id=None,
                    lot=None, site_id=None):
    """The row this one would collide with on ``unique_antibody_per_site_lot``.

    All five parts make a row the vial it is, so any of them can be the one that
    collides. Pass the parts that are changing; the rest come from the row as it
    stands. Returns the clashing ``Antibody`` or ``None``.

    Checking beforehand rather than catching the ``IntegrityError`` is not
    fussiness: on PostgreSQL a failed statement poisons the surrounding
    transaction, so the handler that wanted to explain the refusal cannot run a
    query to do it.
    """
    return (Antibody.objects.using(DB)
            .filter(catalogue_number__iexact=(catalogue if catalogue is not None
                                              else antibody.catalogue_number),
                    company_id=(company_id if company_id is not None
                                else antibody.company_id),
                    target_id=(target_id if target_id is not None
                               else antibody.target_id),
                    lot_number=(lot if lot is not None
                                else (antibody.lot_number or "")),
                    site_id=(site_id if site_id is not None else antibody.site_id))
            .exclude(pk=antibody.pk).first())


# Re-exported: the one definition lives beside `resolved_company_name`, which
# answers the other half of the same question. See cropper/db.py::company_label.
company_label = cdb.company_label


def vial_clash_message(clash, catalogue, company_name, gene) -> str:
    """Why the save was refused, naming the row it would have collided with.

    One wording for both surfaces. The identity dialog has said this since run 2;
    editing the *lot* in the grid hits the same key and used to answer "Could not
    save that — the value has been left unchanged", which names no rule, no row
    and no way forward.
    """
    return (f"There is already a row for {catalogue} from {company_name} against "
            f"{gene}, with the same lot and site (id {clash.pk}). Two rows for one "
            f"vial is what the identity key prevents — if these are the same "
            f"antibody, merge them rather than renaming this one onto it.")


def change_antibody_identity(antibody, data) -> Antibody:
    """Move an antibody to a different catalogue number, supplier or gene."""
    catalogue = (data.get("catalogue_number") or "").strip()
    if not catalogue:
        raise Refused("A catalogue number is required — it is how anybody "
                      "orders this antibody again.")
    company_name = (data.get("company") or "").strip()
    if not company_name:
        raise Refused("A supplier is required: the same catalogue number from "
                      "two suppliers is two different antibodies.")

    target = _target_for(data.get("gene"))
    # Through `resolve_company`, not `Company.resolve` — the catalogue is what
    # disambiguates a brand.
    #
    # `Company.resolve` matches on the canonical key and **creates** when nothing
    # matches, and `canonical_key("Bio-Techne")` matches neither bracketed row. So
    # correcting a supplier to a bare "Bio-Techne" here minted a third Company
    # row — and because an exact name wins over every heuristic
    # (`cropper/db.py::resolve_company`), that row then answered every future
    # paste and the catalogue-prefix split became unreachable, site-wide and for
    # good. One edit on the surface whose whole point is that changes here are
    # deliberate, quietly retiring the rule that keeps Novus and R&D Systems
    # apart. Every other vendor resolves exactly as before: `resolve_company`
    # ends in `Company.resolve` on the ordinary path.
    company = cdb.resolve_company(company_name, catalogue, create=True, db=DB)

    clash = find_vial_clash(antibody, catalogue=catalogue, company_id=company.pk,
                            target_id=target.pk)
    if clash is not None:
        raise Refused(vial_clash_message(clash, catalogue,
                                         company_label(company),
                                         target.gene_name))

    antibody.catalogue_number = catalogue
    antibody.company_id = company.pk
    antibody.target_id = target.pk
    antibody.save(using=DB,
                  update_fields=["catalogue_number", "company", "target"])
    return antibody


def change_cell_line_identity(line, data) -> CellLine:
    """Move a cell line to a different name, gene, genotype, clone or parent."""
    name = (data.get("name") or "").strip()
    if not name:
        raise Refused("A name is required — it is what every session that used "
                      "this line calls it.")
    genotype = (data.get("genotype") or "").strip() or line.genotype

    valid = {c[0] for c in CellLine.Genotype.choices}
    if genotype not in valid:
        raise Refused(f"'{genotype}' is not a genotype. It is one of "
                      f"{', '.join(sorted(valid))}.")

    gene = (data.get("gene") or "").strip()
    # `NA` is what this lab writes when there is no gene, and it is the right
    # answer for a wild type. Read as blank rather than looked up, so the dialog
    # cannot re-attach a line to the placeholder target 96 wild types already
    # point at (services/targets.py::NOT_APPLICABLE).
    if target_svc.is_not_applicable(gene):
        gene = ""
    if genotype == "KO" and not gene:
        raise Refused("A knockout line needs the gene that is knocked out — "
                      "without it the line does not mean anything. If this is "
                      "not a knockout, set the genotype to WT.")
    if genotype == "WT" and gene:
        raise Refused(
            "A wild-type parental line is recorded once, with no gene: one HAP1 "
            "WT serves every knockout made from it. Leave the gene blank, or set "
            "the genotype to KO if this really is a knockout.")
    target = _target_for(gene, allow_blank=True)

    parent_name = (data.get("parent") or "").strip()
    parent = None
    if parent_name:
        # By name **or** by C-number, because the lab records 145 of its 169
        # parents as C-numbers. Matching on name alone refused the convention
        # every row on file is written in — see
        # ``services/cell_lines.py::by_c_number``.
        parent = (CellLine.objects.using(DB)
                  .filter(name__iexact=parent_name).exclude(pk=line.pk).first())
        if parent is None:
            parent = next((c for c in cell_line_svc.by_c_number(parent_name)
                           if c.pk != line.pk), None)
        if parent is None:
            raise Refused(f"There is no cell line called '{parent_name}'. The "
                          f"parent has to be a line already on file — name it "
                          f"({_PARENT_EG}) or give its C-number (C-48).")
        # The same check the paste door and the backfill make, in the same
        # words — a rule enforced on one surface is a rule for one surface.
        wrong = cell_line_svc.wrong_background(
            name, parent, gene=gene, site_id=line.site_id)
        if wrong:
            raise Refused(f"'{parent_name}' is {cell_line_svc.label(parent)}. {wrong}")
    # **Refused only where the edit would *remove* a parent, not where one was
    # never recorded.** The dialog pre-fills this box from the row, so a blank
    # on a line that has a parent means somebody deleted it — a KO with its
    # parental taken away stops meaning anything, and that is what this refuses.
    #
    # A blank on a line that never had one is a different fact, and refusing it
    # made the dialog unusable on **385 of the 399 knockouts on file**: none of
    # them has `parent_line` set (only 2 of 564 rows do — see
    # `cell_lines.by_c_number`, which exists because the lab records parents as
    # C-numbers this app could not read). Correcting a *clone* on any of those
    # rows was refused for an unrelated blank the reader did not touch, on the
    # one surface built for changing a clone. That is CLAUDE.md's own rule
    # arriving backwards: a blank means "not written down", so it fills and
    # leaves; it does not veto the rest of the form.
    if genotype == "KO" and parent is None and line.parent_line_id:
        raise Refused("A knockout needs its parental line — a KO only means "
                      "something alongside the wild type it came from. Name the "
                      f"line it was made from ({_PARENT_EG}) or give its "
                      f"C-number (C-48).")

    # Supplier. Blank is legitimate — a KO line made in-house was bought from
    # nobody — so a blank clears it rather than being refused. A name goes through
    # ``resolve_company`` like every other write path, so correcting a supplier
    # here cannot put a second spelling of it on file, and cannot mint a bare
    # "Bio-Techne" that disables the catalogue-prefix split for everyone.
    company_name = (data.get("company") or "").strip()
    company_id = None
    if company_name:
        company = cdb.resolve_company(company_name, line.catalogue_number or "",
                                      create=True, db=DB)
        company_id = company.pk if company else None

    # The clone. Blank is legitimate and stays legitimate — 338 of the 399
    # knockouts on file record none, and a wild type never has one, so this is
    # never required. `NA` is the Access placeholder for "not recorded" and is
    # read as blank, the same reading `gene` gets four lines up: storing it would
    # make an absence look like an identity, and `clone_suffix` would then have
    # to un-say it on every screen.
    clone = (data.get("clone") or "").strip()
    if clone.upper() == "NA":
        clone = ""
    if clone and genotype != "KO":
        raise Refused(
            "Only a knockout has a clone — it is which single-cell line the "
            "knockout came from. A wild-type parental is recorded once and "
            "serves every knockout made from it.")
    if clone and _clone_taken(line, name, target, clone):
        raise Refused(
            f"There is already a {name} knockout on file at this site recorded "
            f"as clone {clone}. Two rows for one clone split its vials and its "
            f"readings between them, with both drawn as complete — open that "
            f"row instead, or give this one the clone it actually is.")

    line.name = name
    line.genotype = genotype
    line.target_id = target.pk if target else None
    line.clone = clone
    # A blank never clears a stored parent: the only way to reach here with none
    # is a row that had none, which the guard above has already established.
    if parent is not None:
        line.parent_line_id = parent.pk
    line.company_id = company_id
    line.save(using=DB, update_fields=["name", "genotype", "target", "clone",
                                       "parent_line", "company"])
    return line


def _clone_taken(line, name, target, clone) -> bool:
    """Is another row already this clone of this knockout at this bench?

    The same rule `unique_antibody_per_site_lot` gives the antibody dialog, but
    checked rather than enforced: `CellLine` carries no unique constraint, and
    adding one would be a migration against live PostgreSQL over a column 15 rows
    still hold the string `NA` in. Checked **before** the save for the reason
    CLAUDE.md records: on PostgreSQL a failed statement poisons the transaction,
    so a handler that catches an IntegrityError cannot then run the query it
    needs to explain itself.
    """
    qs = (CellLine.objects.using(DB)
          .filter(genotype="KO", name__iexact=name, clone__iexact=clone)
          .exclude(pk=line.pk))
    qs = qs.filter(target_id=target.pk) if target else qs.filter(target__isnull=True)
    qs = (qs.filter(site_id=line.site_id) if line.site_id
          else qs.filter(site__isnull=True))
    return qs.exists()
