-- =============================================================================
-- roles.sql — Postgres role + grants for the read-only MCP server (Server A)
-- =============================================================================
-- Run this against the LIVE pipeline_db (Render PostgreSQL) as a superuser /
-- the database owner.
--
-- The write servers (B/C) were retired — editing is now a download → edit →
-- upload file round-trip through the app, not an MCP. So only ONE least-
-- privilege role remains:
--   mcp_readonly   -> Server A  (read-only analytics)     : SELECT on lab tables
--
-- >>> DO NOT COMMIT REAL PASSWORDS. <<<
-- Replace 'CHANGE_ME_readonly' below with a strong password at run time (e.g.
-- generate with `openssl rand -base64 24`) and keep it only in the server's
-- environment (MCP_READONLY_DATABASE_URL). Rotate any password that ever lands
-- in a shell history or a file.
--
-- The table grants are an ALLOW-LIST of the NINE tables the connector actually
-- reads, defined in ONE place: mcp_servers/common/grants.py. They are applied by
-- `manage.py apply_mcp_roles`, not by this file — see section 1.
--
-- Until 30 Aug 2026 this file granted every `pipeline_*` table: 39 of them, to
-- answer questions that need 9. Among the 30 it did not need were
-- pipeline_member and pipeline_manufacturercontact, the latter holding real
-- people's names and email addresses at supplier companies. Django's auth tables
-- — pipeline members' emails and password hashes — were and remain excluded.
-- =============================================================================

-- ─────────────────────────────────────────────────────────────────────────────
-- 0. Role (LOGIN). Replace the password before running.
-- ─────────────────────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mcp_readonly') THEN
        CREATE ROLE mcp_readonly LOGIN PASSWORD 'CHANGE_ME_readonly';
    END IF;
END $$;

-- Session-level safety defaults baked into the role.
ALTER ROLE mcp_readonly SET default_transaction_read_only = on;
ALTER ROLE mcp_readonly SET statement_timeout = '15s';

-- Baseline: let the role reach the schema (but grant NO table rights yet).
GRANT USAGE ON SCHEMA public TO mcp_readonly;

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. Server A — mcp_readonly: NO table grants in this file.
--
--    The allow-list lives in mcp_servers/common/grants.py and is applied by
--    `manage.py apply_mcp_roles` immediately after this file runs. It is not
--    duplicated here: two lists of which tables are public is how they come to
--    disagree, and the Python one is the copy the preflight and the CI test can
--    read.
--
--    So this file REVOKES instead. Running roles.sql on its own leaves the role
--    able to read nothing, which is the safe direction to fail in — and it is
--    what makes re-running genuinely narrow a role that was granted more
--    earlier. A grant-only script cannot take anything away, and every role this
--    has run against was previously granted all 39 lab tables.
-- ─────────────────────────────────────────────────────────────────────────────
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM mcp_readonly;

-- ────────────────────────────────────────────────────────────────────────────
-- 2. auth_user stays dark for the read-only role — member emails + password
--    hashes are never granted to mcp_readonly.
-- ─────────────────────────────────────────────────────────────────────────────
REVOKE ALL ON public.auth_user FROM mcp_readonly;

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. Read-only recommendation view for Server A.
--    Surfaces the recommended flags alongside the supporting per-session
--    assessments, so the assistant explains (non-)recommendations the same way
--    the website does instead of re-deriving them ad hoc. Server A's tools also
--    work without this view (they query the base tables), so it is an optional
--    convenience — but it is the recommended shape.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW antibody_recommendations AS
SELECT
    a.id                AS antibody_id,
    t.gene_name,
    t.protein_name,
    a.catalogue_number,
    a.rrid,
    a.clonality,
    a.is_recombinant,
    c.name              AS company,
    a.wb_recommended,
    a.ip_recommended,
    a.if_recommended,
    a.fc_recommended,
    (SELECT string_agg(DISTINCT NULLIF(w.rating, ''), '; ')
       FROM pipeline_wbresult w WHERE w.antibody_id = a.id)          AS wb_ratings,
    (SELECT string_agg(DISTINCT NULLIF(w.signal, ''), '; ')
       FROM pipeline_wbresult w WHERE w.antibody_id = a.id)          AS wb_signals,
    (SELECT string_agg(DISTINCT NULLIF(i.ip_assessment, ''), '; ')
       FROM pipeline_ipresult i WHERE i.antibody_id = a.id)          AS ip_assessments,
    (SELECT string_agg(DISTINCT NULLIF(f.specific_signal, ''), '; ')
       FROM pipeline_ifresult f WHERE f.antibody_id = a.id)          AS if_signals,
    (SELECT string_agg(DISTINCT NULLIF(fc.histogram_shift, ''), '; ')
       FROM pipeline_fcresult fc WHERE fc.antibody_id = a.id)        AS fc_shifts
FROM pipeline_antibody a
JOIN pipeline_target t   ON t.id = a.target_id
LEFT JOIN pipeline_company c ON c.id = a.company_id;

GRANT SELECT ON antibody_recommendations TO mcp_readonly;

-- ─────────────────────────────────────────────────────────────────────────────
-- 4. Belt-and-braces: make sure no future default privileges leak the auth
--    tables to the read-only role, and that mcp_readonly cannot write anywhere.
-- ─────────────────────────────────────────────────────────────────────────────
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;   -- drop the implicit PUBLIC grants
REVOKE CREATE ON SCHEMA public FROM mcp_readonly;        -- no DDL via this role

-- PUBLIC-DATA SCOPING. The connector serves only what the public site serves.
-- That is enforced in the ORM by portal.py's _published_antibodies() /
-- _public_targets(), pinned end-to-end by mcp_servers/tests/test_published_scope.py
-- (an unpublished gene answers `found: false`). The table allow-list above is the
-- second, independent half: it decides which tables the role may read at all.
--
-- Row-level views (a `mcp_pub_*` per table, so the role could not read an
-- unpublished ROW either) were considered on 30 Aug 2026 and NOT built. They
-- need the ORM pointed at views and a view kept in sync with every future
-- migration, and the drift between this role and the schema is the failure that
-- has actually happened here — twice in one week. The ORM boundary holds the
-- rows; this file holds the tables.
--
-- An earlier version of this note told the operator to run
-- `manage.py apply_mcp_published`, from a module `mcp_servers/common/published.py`.
-- NEITHER HAS EVER EXISTED IN THIS REPO. Read as an instruction it sends you
-- looking for a command that is not there; read as a description it claims a
-- containment that was never in place. Removed rather than implemented, per the
-- decision above.
-- =============================================================================
