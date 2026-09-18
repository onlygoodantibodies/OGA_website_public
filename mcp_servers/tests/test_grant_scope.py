"""The grant boundary: the role may read the tables the connector needs, and no
others.

``test_published_scope.py`` pins the ORM boundary — an unpublished gene answers
``found: false``. This pins the second, independent half: which tables the
database role may read at all.

The failure it exists to prevent has happened, on 29-30 Aug 2026. A migration
added ``AntibodyOutcome``; the connector gained a reader for it the next day; the
role's grants were a snapshot taken before the table existed. Six of the nine
tools returned ``permission denied for table pipeline_antibodyoutcome`` for two
days, and both guards reported the role healthy because both sampled a single
hardcoded table.

So the scan below is deliberately *static*. It needs no database and no live
role: it fails in CI, on the commit that adds the import, rather than after a
deploy on a reader's tool call.
"""
from __future__ import annotations

import ast
import os

import pytest

from mcp_servers.common import grants

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: The modules whose `from pipeline.models import ...` decides what the connector
#: touches: portal.py is the connector's own reader, and the rest are the shared
#: chain behind a serialised antibody — public.py draws the public boundary,
#: api_views.py serialises, recommendations.py and outcomes.py derive the verdict.
#:
#: A new name imported here is a DECISION, not a chore: either the table joins
#: grants.ALLOWED (and the live role needs re-granting), or the import does not
#: reach the connector and belongs behind a narrower module.
MCP_PATH_MODULES = (
    "mcp_servers/common/portal.py",
    "pipeline/public.py",
    "pipeline/services/outcomes.py",
    "core/recommendations.py",
    "core/api_views.py",
)


def _models_imported(relpath):
    """Every name imported from ``pipeline.models`` in one file."""
    with open(os.path.join(_REPO, relpath), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "pipeline.models":
            names.update(a.name for a in node.names)
    return names


# ── the pure guard logic ────────────────────────────────────────────────────
# audit() is what four green PASS lines used to be. Testing it needs no database:
# `priv` is injected, so a fake role is three dict lookups.

_PRESENT = ["pipeline_antibody", "pipeline_target", "pipeline_company",
            "pipeline_publicationimage", "pipeline_report",
            "pipeline_antibodyoutcome", "pipeline_wbresult", "pipeline_ifresult",
            "pipeline_ipresult", "pipeline_member", "pipeline_manufacturercontact",
            "pipeline_experimentsession"]


def _role(readable):
    """A fake role that can SELECT `readable` and write nothing."""
    return lambda table, privilege: privilege == "SELECT" and table in readable


def test_audit_passes_on_a_correctly_scoped_role():
    ok = _role(set(grants.ALLOWED))
    assert all(passed for passed, _ in grants.audit(_PRESENT, ok))


def test_audit_catches_a_needed_table_that_is_not_granted():
    """The 29 Aug outage, reproduced: every table but the new one."""
    role = _role(set(grants.ALLOWED) - {"pipeline_antibodyoutcome"})
    failures = [label for passed, label in grants.audit(_PRESENT, role) if not passed]
    assert len(failures) == 1
    assert "pipeline_antibodyoutcome" in failures[0]


def test_audit_catches_an_internal_table_left_readable():
    """The state before 30 Aug 2026: every pipeline table granted."""
    role = _role(set(_PRESENT))
    failures = [label for passed, label in grants.audit(_PRESENT, role) if not passed]
    assert len(failures) == 1
    assert "pipeline_manufacturercontact" in failures[0]
    assert "pipeline_member" in failures[0]


def test_audit_catches_a_readable_auth_user():
    role = _role(set(grants.ALLOWED) | {"auth_user"})
    failures = [label for passed, label in grants.audit(_PRESENT, role) if not passed]
    assert any("auth_user" in f for f in failures)


def test_audit_names_a_stale_allow_list_entry():
    """An allow-listed table that no longer exists is a stale entry, not an outage."""
    present = [t for t in _PRESENT if t != "pipeline_report"]
    failures = [label for passed, label in grants.audit(present, _role(set(grants.ALLOWED)))
                if not passed]
    assert any("pipeline_report" in f and "stale" in f for f in failures)


# ── the allow-list actually covers the connector's readers ──────────────────

@pytest.mark.parametrize("relpath", MCP_PATH_MODULES)
def test_mcp_path_models_are_allow_listed(relpath, _seeded_pipeline_db):
    """Every model the MCP path imports maps to a table the role may read.

    The table name comes from Django's own ``_meta.db_table`` rather than from
    re-deriving ``pipeline_<lowercase>`` here: the convention is Django's to
    state, and a model that ever sets ``db_table`` would silently defeat a
    hand-rolled copy of it.
    """
    from django.apps import apps

    for name in _models_imported(relpath):
        table = apps.get_model("pipeline", name)._meta.db_table
        assert table in grants.ALLOWED, (
            f"{relpath} imports pipeline.models.{name} ({table}), which "
            f"mcp_readonly cannot read. Add it to grants.ALLOWED with the reason "
            f"and re-run `manage.py apply_mcp_roles`, or move the import out of "
            f"the connector's path.")
