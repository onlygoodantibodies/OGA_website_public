"""Which pipeline tables the read-only MCP role may read — the one list.

The connector serves **only what the public site serves**. That rule is enforced
in two independent places, and this module is the second one:

  * in the ORM, by ``portal.py``'s ``_published_antibodies`` /
    ``_public_targets`` (pinned end-to-end by ``tests/test_published_scope.py``);
  * in the database, by granting ``mcp_readonly`` SELECT on these tables and
    nothing else.

The second exists because the first is a property of code that keeps changing.
On 29 Aug 2026 the connector gained a reader for a table added the day before
(``pipeline_antibodyoutcome``) and the role had no grant for it, which took six
of the nine tools down for two days — the grants and the code drift apart, so
each has to be able to state its own rule.

This module names TABLES, never models. That is what it is about, and it also
keeps it clear of ``tests_outcomes.py``'s guard that no surface outside
``core/recommendations.py`` reads the outcome model directly. Nothing here reads
any table; it says which ones the role may.

**Why an allow-list and not "every ``pipeline_*`` table".** That was the previous
rule, and it granted 39 tables to answer questions that need 9. Among the 30 it
did not need are ``pipeline_member`` and ``pipeline_manufacturercontact``, the
latter holding real people's names and email addresses at supplier companies. No
tool has ever exposed them; the grant simply reached further than any tool did.

The 39 is the count of `pipeline_*` tables in the LIVE database on 30 Aug 2026,
read off `apply_mcp_roles`'s own verification, not a count of models in
`pipeline/models.py`. Those two differ — models.py declares 33 — because the
M2M through-tables have no model class of their own. Count the tables when the
question is about grants.

**It fails closed, deliberately.** A new pipeline table is NOT readable until it
is named here. That is the same shape as the connector's public-key guard: adding
a key is a decision rather than an accident. ``tests/test_grant_scope.py`` fails
in CI when the MCP path imports a model this list does not cover, so the decision
is forced *before* a deploy rather than discovered by a reader whose tool call
failed.

What this list does NOT do is filter rows: it says the role may read
``pipeline_antibody``, not that it may read only the published ones. Row-level
containment would need a view per table and a view layer kept in sync with every
migration; the ORM boundary above is what holds it, and it is tested.
"""
from __future__ import annotations

ROLE = "mcp_readonly"

#: Every table the connector reads, and the reader that needs it. Keep the
#: reason with the name: a bare list gives a future session no way to tell a
#: table that is still needed from one that outlived its caller.
ALLOWED = {
    "pipeline_target": "gene identity; public_targets() is the public boundary",
    "pipeline_antibody": "the reagent, and the per-application recommendation flags",
    "pipeline_company": "supplier name on every serialised antibody",
    "pipeline_publicationimage": "what makes a row public at all, plus figure URLs",
    "pipeline_report": "the F1000/Zenodo DOIs in `provenance`",
    "pipeline_antibodyoutcome": "the curated per-application outcome",
    # outcomes._assemble reads these to DERIVE the verdict. The rows themselves
    # are never serialised — only the merged answer and its `verdict_source`.
    # FcResult is absent on purpose: RESULT_MODELS covers WB/ICC-IF/IP only.
    "pipeline_wbresult": "session axes behind the WB verdict (never serialised)",
    "pipeline_ifresult": "session axes behind the ICC-IF verdict (never serialised)",
    "pipeline_ipresult": "session axes behind the IP verdict (never serialised)",
}

#: Read-only convenience view from roles.sql. A plain view runs with its OWNER's
#: privileges, so it keeps working while the role's own table grants shrink.
ALLOWED_VIEWS = ("antibody_recommendations",)

#: Never readable, whatever else changes. auth_user carries pipeline members'
#: emails and password hashes; the logins themselves live in academy_db, which
#: this role cannot reach at all.
DENIED_ALWAYS = ("auth_user",)

#: Privileges the role must not hold anywhere.
WRITE_PRIVILEGES = ("INSERT", "UPDATE", "DELETE")

#: Tables matching this prefix are the lab's own; anything here that is not in
#: ALLOWED must be unreadable.
LAB_PREFIX = "pipeline_"

#: Lists every lab table actually present, so the audit can report a table that
#: is granted and should not be — which a check driven off ALLOWED alone cannot
#: see. Escaped underscore: in PostgreSQL LIKE, `_` is a single-character
#: wildcard.
LAB_TABLES_SQL = r"""
    SELECT tablename FROM pg_tables
    WHERE schemaname = 'public' AND tablename LIKE 'pipeline\_%'
    ORDER BY tablename
"""


def grant_statements():
    """The GRANT/REVOKE statements that put the role in the state above.

    Revoke first, so re-running genuinely *narrows* a role that was granted more
    earlier — a grant-only script cannot take anything away, and every role this
    has ever run against was previously granted all 39 tables.
    """
    out = [f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {ROLE};"]
    out += [f"GRANT SELECT ON public.{t} TO {ROLE};" for t in sorted(ALLOWED)]
    out += [f"GRANT SELECT ON public.{v} TO {ROLE};" for v in ALLOWED_VIEWS]
    out += [f"REVOKE ALL ON public.{t} FROM {ROLE};" for t in DENIED_ALWAYS]
    return out


def audit(present, priv):
    """Check the role's grants. Returns ``[(ok, label), ...]``.

    ``present`` is every ``pipeline_*`` table in the database; ``priv(table,
    privilege)`` answers whether the role holds it. Injecting ``priv`` is what
    lets one audit serve both callers: ``apply_mcp_roles`` asks as the owner
    (three-argument ``has_table_privilege``) and ``preflight`` asks as the role
    itself (two-argument).

    Results are aggregated rather than one line per table — 39 tables times four
    privileges is 132 lines nobody reads — but every failure NAMES the tables it
    is about. A count with no list under it invents the noun.
    """
    results = []
    present = sorted(present)

    expected = [t for t in present if t in ALLOWED]
    unreadable = [t for t in expected if not priv(t, "SELECT")]
    results.append((
        not unreadable,
        f"can SELECT all {len(expected)} tables it needs"
        if not unreadable
        else f"CANNOT SELECT {len(unreadable)} needed: {', '.join(unreadable)}"))

    # A table named in ALLOWED that no longer exists is a stale entry, not an
    # outage — say so rather than passing silently over it.
    missing = sorted(set(ALLOWED) - set(present))
    if missing:
        results.append((
            False,
            f"{len(missing)} allow-listed table(s) not in the database "
            f"(stale entry in grants.ALLOWED?): {', '.join(missing)}"))

    internal = [t for t in present if t not in ALLOWED]
    reachable = [t for t in internal if priv(t, "SELECT")]
    results.append((
        not reachable,
        f"cannot SELECT any of the {len(internal)} internal tables"
        if not reachable
        else f"CAN STILL SELECT {len(reachable)} internal: {', '.join(reachable)}"))

    writable = [t for t in expected
                for p in WRITE_PRIVILEGES if priv(t, p)]
    results.append((
        not writable,
        "holds no write privilege on any table"
        if not writable
        else f"HAS WRITE on: {', '.join(sorted(set(writable)))}"))

    for table in DENIED_ALWAYS:
        results.append((not priv(table, "SELECT"),
                        f"cannot read {table} (emails/hashes)"))
    return results
