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
-- The grants are scoped to an ALLOW-LIST of the lab tables (every `pipeline_*`
-- table) plus the read-only recommendation view. Django's auth tables — which
-- hold pipeline members' emails and password hashes — are DELIBERATELY EXCLUDED
-- from the read-only role. auth_user is never granted to mcp_readonly.
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
-- 1. Server A — mcp_readonly: SELECT on every lab (pipeline_*) table ONLY.
--    Explicitly NOT auth_user / django_* / socialaccount_* / account_*.
-- ─────────────────────────────────────────────────────────────────────────────
DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN
        SELECT tablename FROM pg_tables
        WHERE schemaname = 'public' AND tablename LIKE 'pipeline\_%'
    LOOP
        EXECUTE format('GRANT SELECT ON public.%I TO mcp_readonly;', r.tablename);
    END LOOP;
END $$;

-- ─────────────────────────────────────────────────────────────────────────────
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

-- PUBLIC-DATA SCOPING for Server A is now automated — run it AFTER this file:
--
--     python manage.py apply_mcp_published
--
-- That creates the mcp_pub_* views (published antibodies/targets only — those
-- with a publication image — and public columns only), then REVOKEs the base
-- pipeline_* tables from mcp_readonly and GRANTs it the views instead. After it,
-- the read-only role physically cannot read an unpublished row (so even Server
-- A's raw analytics_sql is contained). The boundary is defined in one place:
-- mcp_servers/common/published.py. Re-run apply_mcp_published if you ever re-run
-- this file (which re-grants the base tables to mcp_readonly).
-- =============================================================================
