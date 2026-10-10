"""Which Academy modules a learner sees — the one reader, and its count.

The Academy home lists the published lessons, and the live list on 7 Oct 2026
was Modules 1–4 and the Final assessment, which the home numbers Module 5.
Pages outside the Academy typed the total by hand and had split two ways:
"5 modules" on About, the Roadmap and the Academy's own sign-in page; "four
modules" in the Champions and institutions outreach emails and "Modules 1–4"
in the handbook wording. Both were defensible readings of the same list, which
is how a typed number drifts. Every page now prints ``module_count()``, which
counts what the home lists — the assessment included, because the home calls
it a module.
"""
from __future__ import annotations

from .models import Lesson


def published_lessons():
    """The modules on the Academy home, in order. A module still being written
    (``is_published`` off) is not one a learner can take, so it is not counted."""
    return Lesson.objects.filter(is_published=True).order_by("order")


def module_count() -> int:
    return published_lessons().count()
