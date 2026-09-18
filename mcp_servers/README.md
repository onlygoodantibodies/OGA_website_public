# MCP server for the YCharOS pipeline database

> **⚑ Servers B and C were retired (2026-07)** and their code deleted. Database
> *editing* no longer runs through an MCP — it's a **download → edit → upload**
> file round-trip through the app's validated, fill-only-blank import (internal /
> pipeline-members only). Only **Server A (read-only)** remains as the LLM-facing
> tool. Rationale is in the `VISION.md` banner.

[Model Context Protocol](https://modelcontextprotocol.io) server that lets an LLM
assistant read the pipeline's data. It is a **separate process** — not wired into
the Django site, not deployed by the main Render web service (it runs as its own
`oga-mcp` service). Everything here was built and tested against a **local SQLite
`pipeline_db`** (the dev fallback in `CLAUDE.md`).

**Where Server A is headed** (its job, the A2 academy/workshop track, and the
roadmap) is in [`VISION.md`](./VISION.md).

| | Server | File | Trust | Who | Credential (env var) |
|---|---|---|---|---|---|
| **A** | Read-only analytics | `server_a_readonly.py` | low | team, maybe wider | `MCP_READONLY_DATABASE_URL` |

Server A gets its **own Postgres role and credential** (see `roles.sql`), so it
can be granted, scoped, and revoked independently.

The interactive-education (tutor) tools in `server_a2_academy.py` are **not
registered on this connector today** — the registration was removed while the
conversational tutor is reworked, so the public connector is the antibody
database only. The implementation is retained and can be re-attached; see the
comment at the end of `build_server()` in `server_a_readonly.py`.

---

## What each server does

### Server A — read-only analytics
Answers questions over the live data: *"recombinant antibodies for SNCA?"*,
*"which antibodies is the data **not** supportive for in WB, and why?"*, *"is this
paper's antibody in the OGA dataset?"*. It can only ever read. Tools:
`list_targets`, `target_report`, `antibodies_by_support` (one rung at a time —
supportive, limited support, not supportive or not tested — with the supporting
evidence), `antibody_validation` (look one up
by RRID / catalogue / gene → verdict per app + KO-controlled evidence +
published-image apps + F1000/Zenodo report DOIs — the "how robust independently?"
lookup), and `search_antibodies` (locate one antibody by catalogue/RRID/gene).

**Two things a caller now declares, both added 18 Aug 2026.**

`scan_controls` takes **`sections_read`** — which parts of the paper the caller
actually read (`methods`, `results`, `figure_legends`, `supplementary`, or
`"full text"`). It exists because `absent` asserts that no genetic manipulation of
this target appears anywhere in the paper, which is a statement about the authors'
work, and a caller that read only the Methods produced a whole table of it that
the server had no basis for. The declaration is **cross-checked against the
payload and can only ever tighten** — saying you read the legends while carrying
nothing out of them buys nothing, because a model asked whether it read something
is free to say yes. Where the two disagree, `coverage.sections.contradictions`
names it. Rows that cannot be supported come back `not_assessed` rather than
`absent`; `demonstrated` and `present_unlinked` are findings and are served
regardless. Omitting the parameter leaves every existing caller unaffected.

Both tools take **`paper_doi` / `paper_pmid` / `paper_title`**. With one of them
the reply carries what CiteAb's citation record says that paper used each reagent
for — an independent source, recorded by somebody who was not reading the paper
just now. Where it disagrees with what the caller read, a `scan_controls` row
carries `application_conflict` naming both sides and **choosing neither**: the
ordinary explanations are that the paper used the reagent twice, that CiteAb
attributed the wrong product (their join is on catalogue number, which is not
unique across suppliers), or that the use is in a section nobody read, and the
server can tell none of them apart. The `citeab` block has three states and only
one is about the paper — `not_covered` is an absence in the snapshot, `unavailable`
is a fact about this server, and neither is evidence the paper cites nothing.

The snapshot is `core/data/citeab_papers_*.json`, read through `core/citations.py`
and shared with the browser extension. Refreshing it is the **`citation-snapshot`
skill**.

**No whole-database analytics — by policy.** The tools answer per-antibody and
per-gene questions only. There is no free-form SQL tool and no cross-gene
antibody list (`antibodies_by_support` requires a gene; `search` never
matches by company), so the server cannot be used to compare vendors by how often
their antibodies are supported across the database.

The real guarantee is the **`mcp_readonly` Postgres role**: `GRANT SELECT` on the
lab tables only, `default_transaction_read_only = on`, a `statement_timeout`, and
**no access to `auth_user`** (member emails + password hashes stay unreachable).

**Output matches the public data portal / website exactly, plus a factual layer.**
The tools serialise through the same code the public API uses
(`core/api_views.py::_serialise_antibody`, via `common/portal.py`) over the ORM,
bounded to public rows by the identical `publication_images__isnull=False` filter
the API applies. Each antibody also carries a controlled-vocabulary verdict per
application — `recommended` / `not_recommended` (tested with KO controls, didn't
meet the bar) / `not_tested` — with the consensus-protocol DOI it was assessed
under, and a lookup that finds nothing returns `in_dataset: false` (so "not in the
dataset" is never confused with a verdict).

**Two invariants to keep:**
- **Never let Server A use a write DSN.** `portal.setup()` forces the read-only
  DSN; keep it that way. The Postgres role is the backstop, not the only guard.
- **The boundary is the app-level filter**, `publication_images__isnull=False` —
  the exact filter the public API uses — not physical view scoping. So
  `mcp_readonly` deliberately **keeps SELECT on the base `pipeline_*` tables**
  (the ORM serialiser needs them) while `auth_user` stays revoked. There is no
  view-scoping step to run.

---

## Run locally

```bash
# From the repo root:
python -m venv .venv-mcp && . .venv-mcp/bin/activate
pip install -r requirements.txt              # app deps (Server A boots Django)
pip install -r mcp_servers/requirements.txt  # mcp[cli], psycopg2, sqlparse, pytest

# One-time: migrate a local pipeline_db and seed scratch data.
python manage.py migrate --database=pipeline_db
python mcp_servers/seed_scratch.py

# Run a server (stdio transport — how an MCP client launches it):
python -m mcp_servers.server_a_readonly
```

With no `MCP_READONLY_DATABASE_URL` set, Server A connects to the local SQLite
`db_pipeline.sqlite3`. So the default, zero-config behaviour is **local only**.

### Register with an MCP client
Point your client (e.g. Claude Desktop) at each server with the env it needs, for
example:

```jsonc
{
  "mcpServers": {
    "oga-readonly": {
      "command": "/path/to/.venv-mcp/bin/python",
      "args": ["-m", "mcp_servers.server_a_readonly"],
      "cwd": "/path/to/OGA_website",
      "env": { "MCP_READONLY_DATABASE_URL": "postgresql://mcp_readonly:...@host/db" }
    }
  }
}
```

## Environment variables (names only — never commit values)

| Var | Used by | Meaning |
|---|---|---|
| `MCP_READONLY_DATABASE_URL` | server | DSN for the `mcp_readonly` role. Unset ⇒ local SQLite. |
| `PIPELINE_BASE_URL` | links | Base URL for links back into the app (default the prod site; `http://localhost:8000` for dev). |
| `MCP_AUDIT_LOG` | audit | Path to the JSONL audit log. Default `mcp_servers/audit.log`. |
| `MCP_OAUTH_ISSUER`, `MCP_OAUTH_JWKS_URI`, `MCP_PUBLIC_URL` | HTTP transport | OAuth mode (see below). |
| `MCP_READONLY_TOKEN` | HTTP transport | Bearer token for the simpler non-OAuth mode. |
| `MCP_ALLOWED_HOSTS` | HTTP transport | Host allow-list for the hosted service. |

`MCP_ACTOR_USERNAME` may still be set on the `oga-mcp` service — it is a leftover
from the retired write servers and is read by nothing.

## Tests

```bash
python -m pytest mcp_servers/tests/ -q      # all on the isolated local SQLite db
```

The suite builds a throwaway `mcp_servers/tests/test_pipeline.sqlite3` (migrated +
seeded), never your dev DB and never production. It includes
`test_mcp_transport.py`, which drives each server over the **real MCP stdio
protocol** (subprocess + client), so the transport path is covered, not just the
in-process functions.

## Benchmarks

`check_manuscript` and `scan_controls` were measured against 100 (paper ×
antibody) pairs with a manually curated reference standard. **The server as it
stood for that run is kept**, alongside the brief it produced and the triage of
what was adopted, in
[`benchmarks/2026-08-01-extension-mcp/`](../benchmarks/2026-08-01-extension-mcp/) —
the findings are claims about that code, and stop being checkable once it moves.

An archive can only pin the code. This server answers from live PostgreSQL, so a
result that no longer reproduces may be a regression or may be the database being
months newer, and nothing in the archive can tell you which.

Its cases live on as fixtures in `tests/`. Do not re-run the corpus to check a
fix — it is a known quantity now, so a re-run measures tuning rather than quality.

## Test via a Claude interface (Claude Code / Desktop)

Prove the servers work over the protocol, then connect them as tools.

1. **Smoke-test end-to-end** (spawns each server, lists + calls tools against the
   local seeded DB — no config needed):
   ```bash
   python mcp_servers/seed_scratch.py
   python mcp_servers/smoke_client.py
   ```
   You should see the Server A connector respond with **9 tools** — the
   per-antibody / per-gene data tools (`antibody_validation`, `target_report`,
   `antibodies_by_support`, `list_targets`, `search_antibodies`) plus
   `check_manuscript`, `scan_controls`, `controls_rubric` and
   `how_to_read_a_paper`. (It read **7** until 14 Sep 2026, which was already
   two short before `antibodies_by_recommendation` was replaced — counted off
   the registrations rather than carried forward.)

2. **Register the servers with your client.** Copy `mcp.example.json`, replace
   `ABSOLUTE_REPO_PATH` with your checkout path, and drop it in:
   - **Claude Code:** save as `.mcp.json` at the repo root (git-ignored) and start
     a new session — the tools appear namespaced under `oga-readonly`.
   - **Claude Desktop:** merge the `mcpServers` block into
     `claude_desktop_config.json` and restart.

   With no `MCP_READONLY_DATABASE_URL` set, it talks to the local SQLite
   `db_pipeline.sqlite3` — safe for testing. Point it at production only after
   running `roles.sql` and setting the role DSN (see the checklist below).

3. **Try it.** Ask the assistant things like *"which SNCA antibodies aren't
   recommended for WB, and why?"* or *"is this paper's antibody in the OGA
   dataset?"*.

---

## Deploy as a hosted HTTPS service (Render)

`http_server.py` exposes the servers over HTTPS behind bearer tokens, so any
client — this Claude Code interface, Claude chat, Desktop, a teammate — connects
to one URL. **One Render Web Service** hosts up to three token-gated mounts:

| Mount | Server | Token env var | DB DSN env var |
|---|---|---|---|
| `/readonly/mcp` | A (read-only) | `MCP_READONLY_TOKEN` | `MCP_READONLY_DATABASE_URL` |
| `/tools/mcp` | B (safe writes) | `MCP_TOOLS_TOKEN` | `PIPELINE_DATABASE_URL` |

**A tier is live only when its token is set.** So you deploy Server A alone now,
and turn B/C on later by adding their token + DSN env vars — no code change.
Suspending the one service takes all three offline (the kill-switch).

### Create the service (Server A first)
Render → **New → Web Service** → this repo, branch `beta`, **region Frankfurt**
(same as `ycharos-pipeline-db`, so it reaches the DB over the private network):

- **Build:** `pip install -r requirements.txt -r mcp_servers/requirements.txt`
- **Start:** `python -m mcp_servers.http_server`  (binds Render's `$PORT`)
- **Health check path:** `/healthz`
- **Env vars:**
  - `MCP_READONLY_TOKEN` = a strong random token (`openssl rand -hex 32`)
  - `MCP_READONLY_DATABASE_URL` = the DB's **Internal** connection string with the
    `mcp_readonly` role's credentials
    (`postgresql://mcp_readonly:PW@INTERNAL_HOST:5432/DBNAME`)

Server A uses raw psycopg2, so it does **not** boot Django — no other config
needed. (When you later enable the **tools** tier, that one boots Django: also set
`DEBUG` and `PIPELINE_BASE_URL=https://onlygoodantibodies.co.uk`
so verification links point at the public site.)

### Connect Claude
Add a **custom connector** (Claude chat/Desktop → Connectors → Add custom):
- URL: `https://YOUR-SERVICE.onrender.com/readonly/mcp`
- Header: `Authorization: Bearer <the MCP_READONLY_TOKEN>`

`GET /healthz` (no auth) shows which tiers are live. Everything here is verified
locally by `test_http_transport.py` (spawns the app, checks 401s, the disabled-
tier 404, a full MCP round-trip, and per-tier token isolation).

### OAuth mode (production — works in claude.ai / ChatGPT / Claude Code)

Bearer tokens only work in developer clients. For the **claude.ai website /
Claude app / ChatGPT**, the server speaks **OAuth 2.1**: it becomes a *resource
server* that validates access tokens issued by a managed **Authorization Server**
(provider). This is verified locally in `test_oauth.py` against a mock AS
(401 challenge, protected-resource metadata, JWKS token verification, per-tier
scope + audience isolation).

**Auth mode is chosen by env:** set `MCP_OAUTH_ISSUER` → OAuth mode; leave it
unset → bearer mode. In OAuth mode a tier is enabled when its **DB DSN** is set
(not a token), and per-tier scopes are opt-in via `MCP_<TIER>_SCOPE`.

**1. Stand up the Authorization Server — WorkOS AuthKit (free, recommended).**
- Create a WorkOS account + environment; enable **AuthKit** (hosted login).
- Enable **Dynamic Client Registration (DCR)** — required so claude.ai/ChatGPT can
  self-register. (WorkOS: Authentication → enable DCR / follow their MCP guide.)
- Add redirect URIs: `https://claude.ai/api/mcp/auth_callback`,
  `http://localhost/callback`, `http://127.0.0.1/callback`.
- Note the **issuer URL** (your AuthKit domain, e.g. `https://your-tenant.authkit.app`)
  and its **JWKS URI** (usually `<issuer>/oauth2/jwks`).

**2. Switch the Render service to OAuth.** On the `oga-mcp` service → Environment,
add:
- `MCP_OAUTH_ISSUER` = the AuthKit issuer URL
- `MCP_OAUTH_JWKS_URI` = `<issuer>/oauth2/jwks` (optional; auto-discovered if omitted)
- `MCP_PUBLIC_URL` = `https://oga-mcp.onrender.com` (so token audiences match)
- keep `MCP_READONLY_DATABASE_URL` (enables the readonly tier). You can delete
  `MCP_READONLY_TOKEN` — it's ignored in OAuth mode.

Redeploy, then verify:
- `GET /healthz` → `{"enabled":["readonly"],...}`
- `curl -i https://oga-mcp.onrender.com/readonly/mcp` → **401** with
  `WWW-Authenticate: Bearer resource_metadata=...`
- `GET /.well-known/oauth-protected-resource/readonly/mcp` → JSON naming your AS.

**3. Connect claude.ai.** Settings → Connectors → **Add custom connector** →
URL `https://oga-mcp.onrender.com/readonly/mcp`. Claude discovers the login,
sends you through WorkOS, and connects. The same URL works in ChatGPT and
Claude Code.

## Going live on the real database

**Where it can run.** Production `pipeline_db` is **not reachable from the dev
environment** — outbound there is HTTPS-only, so a direct Postgres (5432)
connection is blocked. Two places the DB *is* reachable:

- **Local / Claude Desktop** — your machine reaches Render Postgres externally;
  run over stdio (`mcp.example.json`).
- **Hosted on Render (HTTPS)** — `http_server.py` as a Web Service in the same
  region as the DB, reaching it over the private network. This is the path that
  works from anywhere including the Claude web interfaces, and is the shareable
  one. See "Deploy as a hosted HTTPS service" above.

**Rollout.** Backups on → `roles.sql` → `preflight.py` → point Server A at prod.
There is no write stage any more: Server A cannot write, so it is safe to explore
freely once preflight passes. Spot-check its answers against the public website.

**`preflight.py`** verifies the read-only role's grants actually enforce the
policy (readonly can SELECT the lab tables, can't write, and can't see
`auth_user`). Run it right after `roles.sql`:

```bash
MCP_READONLY_DATABASE_URL=… python mcp_servers/preflight.py
  # must be ALL CHECKS PASSED before trusting Server A
```

**Kill-switch.** `ALTER ROLE mcp_readonly NOLOGIN;` (or rotate its password) —
the connection dies immediately. Ultimate rollback is Render point-in-time
restore.

## Owner's checklist (Server A only)

1. **Turn on Render point-in-time backups** for the pipeline PostgreSQL service.
2. **Run `roles.sql`** (via `python manage.py apply_mcp_roles`) against the live
   pipeline_db as the DB owner, after replacing `CHANGE_ME_readonly` with a
   freshly generated strong password. This creates the one least-privilege
   `mcp_readonly` role (`GRANT SELECT` on the lab tables; `auth_user` revoked).
3. **Set `MCP_READONLY_DATABASE_URL`** in the `oga-mcp` service environment (with
   the role's password). Keep it out of git.
4. **Keep the full app deps installed on `oga-mcp`** — Server A boots Django to
   serialise through the public API code, so it needs the app's `requirements.txt`
   (not just `mcp_servers/requirements.txt`).
5. **Verify** with `preflight.py`, then drive Server A read-only against prod and
   confirm it answers from claude.ai, watching the audit log.

The public boundary is enforced in the ORM (portal.py's
`publication_images__isnull=False` filter) — there is no separate view-scoping
step to run any more, and no write servers to roll out.
