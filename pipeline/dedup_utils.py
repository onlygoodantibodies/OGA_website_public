"""
Shared helpers for antibody dedup/merge management commands
(`merge_duplicate_antibodies`, `merge_duplicate_companies`, ...).

The core operation is merging a set of "loser" Antibody rows into one survivor:
re-point child records, respect the unique (antibody, application_type) image
constraint, backfill blank survivor fields, then delete the losers.
"""

import re

from pipeline.models import Antibody

CHILD_RELATIONS = ["wb_results", "ip_results", "if_results", "fc_results",
                   "publication_images", "locations"]
SIMPLE_RELATIONS = [r for r in CHILD_RELATIONS if r != "publication_images"]

OR_BOOL_FIELDS = {
    "wb_recommended", "ip_recommended", "if_recommended", "fc_recommended",
    "supplier_validated_wb", "supplier_validated_ip", "supplier_validated_if",
    "supplier_validated_ihc", "supplier_validated_elisa", "supplier_validated_fc",
    "is_recombinant",
}
# company is set explicitly by callers; catalogue/lot are unique-key fields, so
# backfilling a blank one from a loser can collide on the unique constraint.
NEVER_BACKFILL = {"id", "access_id", "created_at", "updated_at", "ab_number",
                  "company_id", "company", "catalogue_number", "lot_number"}


def normcat(s):
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())


def payload(ab):
    return sum(getattr(ab, r).count() for r in CHILD_RELATIONS)


def is_empty(v):
    return v is None or v == "" or v == {}


def plan_backfill(survivor, losers):
    """Fields to fill on the survivor from the losers (blanks only; OR for flags)."""
    changes = {}
    for field in Antibody._meta.get_fields():
        if not getattr(field, "concrete", False) or field.many_to_many:
            continue
        name, attname = field.name, field.attname
        if name in NEVER_BACKFILL or attname in NEVER_BACKFILL:
            continue
        if name in OR_BOOL_FIELDS:
            if not getattr(survivor, name) and any(getattr(l, name) for l in losers):
                changes[name] = True
            continue
        if not is_empty(getattr(survivor, attname)):
            continue
        for l in losers:
            v = getattr(l, attname)
            if not is_empty(v):
                changes[attname] = v
                break
    return changes


def plan_images(survivor, losers):
    """Loser images to move (survivor lacks that app) vs drop (duplicate app)."""
    seen = set(survivor.publication_images.values_list("application_type", flat=True))
    move, drop = [], []
    for l in losers:
        for img in l.publication_images.all():
            if img.application_type in seen:
                drop.append(img)
            else:
                move.append(img)
                seen.add(img.application_type)
    return move, drop


def apply_merge(survivor, losers, using, target_company_id=None):
    """
    Merge losers into survivor inside the caller's transaction:
    re-point simple children, move/drop images per the unique constraint,
    backfill blanks, optionally set the survivor's company, delete losers.
    Returns (children_moved, images_dropped).
    """
    move_imgs, drop_imgs = plan_images(survivor, losers)
    backfill = plan_backfill(survivor, losers)
    moved = 0
    for rel in SIMPLE_RELATIONS:
        for l in losers:
            moved += getattr(l, rel).update(antibody=survivor)
    for img in move_imgs:
        img.antibody = survivor
        img.save(using=using, update_fields=["antibody"])
        moved += 1
    for img in drop_imgs:
        img.delete(using=using)
    for f, v in backfill.items():
        setattr(survivor, f, v)
    if target_company_id is not None and survivor.company_id != target_company_id:
        survivor.company_id = target_company_id
    survivor.save(using=using)
    for l in losers:
        l.delete(using=using)
    return moved, len(drop_imgs)
