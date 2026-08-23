"""One place where a lab number is issued.

There are five ways an ``Antibody`` gets created in this app —
``bulk_antibodies.apply``, ``bulk_sessions.resolve_or_create``,
``dataset.apply``, ``cropper/commit.apply`` and the Django admin — and three
ways a ``CellLine`` does. Putting "give it the next number" in each of them is
the exact shape of nearly every defect CLAUDE.md records: the rule lands in the
edit path and not the write path, or in one paste box and not the other, and
the door that was missed is silent about it.

So it goes on ``pre_save``, once. What that buys is that a number is issued
wherever a record is born, including doors nobody has written yet, and what it
costs is one query per created record — a ``MAX()`` on an indexed column,
scoped to a site.

``services/lab_numbers.py::assign`` holds every rule about *when*; this file
only decides *where*.
"""
from __future__ import annotations

from django.db.models.signals import pre_save
from django.dispatch import receiver

from pipeline.models import Antibody, CellLine, CellLineVial
from pipeline.services import lab_numbers


@receiver(pre_save, sender=Antibody, dispatch_uid="pipeline_issue_ab_number")
def issue_ab_number(sender, instance, using=None, **kwargs):
    # `using` is the alias the save is actually going to, which is not always
    # `pipeline_db`: the test runner and any `save(using=…)` caller say so
    # explicitly, and asking the wrong database for the highest number in use
    # would answer from rows that are not the ones being written.
    lab_numbers.assign(instance, db=using or lab_numbers.DB)


@receiver(pre_save, sender=CellLine, dispatch_uid="pipeline_issue_c_number")
def issue_c_number(sender, instance, using=None, **kwargs):
    lab_numbers.assign(instance, db=using or lab_numbers.DB)


@receiver(pre_save, sender=CellLineVial, dispatch_uid="pipeline_issue_batch_c_number")
def issue_batch_c_number(sender, instance, using=None, **kwargs):
    """A freeze-down batch takes the site's next C-number, from the same run.

    `bulk_cell_lines._ensure_vial` always passes an explicit number and returns
    early without one, so this changes nothing there — `assign` never overwrites
    a number that is already set.
    """
    lab_numbers.assign(instance, db=using or lab_numbers.DB)
