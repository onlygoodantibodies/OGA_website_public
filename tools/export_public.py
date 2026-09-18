#!/usr/bin/env python3
"""Build the public snapshot of this repository.

Why this exists
---------------
The public release is a *fresh tree with fresh history*, not this repo with
files deleted: a delete leaves the file fully readable in history, and this
repo's history carries user password hashes, personal email addresses,
unpublished lab results and two secret values that were never rotated
(AUDIT.md sections 4 and 5a).

Why the rules are written the way they are
------------------------------------------
Every tracked file must be classified by exactly one rule below, SHIP or DROP.
A file matching *no* rule is a hard error that stops the export and names the
file.

That is the whole safety property, and it is the opposite of a denylist. With a
denylist ("copy everything except these"), a new export dropped into the tree
ships by default and nothing says so -- which is how `access_csvs/` and
`pipeline/data/access_update_2026_08/` came to sit in the repo in the first
place. Here a new file stops the export until a person has looked at it and
said which list it belongs on. The failure mode is "the export refuses to run",
which is loud and costs a minute; the alternative is "three thousand
unpublished results are on GitHub", which is silent and permanent.

Usage
-----
    python3 tools/export_public.py                 # dry run: print the manifest
    python3 tools/export_public.py --manifest m.txt
    python3 tools/export_public.py --apply ../oga_public

Nothing is committed or pushed. `--apply` writes a directory; the git steps are
printed for you to run yourself.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# --------------------------------------------------------------------------
# The rules. First match wins, so put specific paths above the general ones.
#
# A pattern ending in "/" matches that directory and everything under it.
# Otherwise it is an fnmatch glob against the repo-relative path.
# --------------------------------------------------------------------------

SHIP, DROP = "SHIP", "DROP"

RULES: list[tuple[str, str, str]] = [
    # ---- Lab data and PII: the blockers (AUDIT.md section 5a) -------------
    ("access_csvs/", DROP,
     "Full Access export: 3,184 antibodies with freezer locations and lot "
     "numbers, ~5,300 result rows, 18 named staff, a third party's email. "
     "About half the results are unpublished."),
    ("pipeline/data/access_update_2026_08/", DROP,
     "Second Access export (Aug 2026 delta) -- same class of data, same 18 "
     "named staff. Lives inside an app directory, which is exactly why the "
     "rules are per-path and not per-app."),
    ("YCharosDataBaseFE_NON-SPLIT.accdb", DROP,
     "Raw internal Access database; exceeds the public dataset."),
    ("data.json", DROP,
     "DB dump: 8 users with hashes, 102 session rows, 4 certificates naming "
     "learners and their scores."),
    ("dump.sql", DROP, "DB dump -- password hashes."),
    ("fixed_dump.sql", DROP, "DB dump -- password hashes."),
    ("docs/reference_sheets/", DROP,
     "Freezer inventory and real plate maps: aliquot counts, drawer/box "
     "positions, lot numbers. No code reads these."),
    ("pipeline/mapping_reports/", DROP,
     "Working artefact of the core -> pipeline migration: a row-by-row copy "
     "log naming catalogue numbers. The command that produced it "
     "(`verify_core_pipeline_mapping`) went with the legacy layer on 23 Aug "
     "2026, so nothing recreates this now -- it is a frozen record of a "
     "migration that has already happened."),
    ("embed_urls.xlsx", DROP,
     "Working artefact. It was the output of `manage.py export_embed_urls`, "
     "which read the retired core models and was deleted with them on 23 Aug "
     "2026 -- so this file is a frozen snapshot, and the numbers in it are "
     "the legacy dataset's, not the pipeline's."),
    ("no_recommended_antibodies.csv", DROP,
     "OWNER'S CALL. Named suppliers and catalogue numbers whose reagents "
     "carry no recommendation. Publishable, but it is a negative-result list "
     "about named commercial products -- decide deliberately. Move to SHIP if "
     "you want it out. Note it is now stale as well as sensitive: the script "
     "that built it (`no_recommendations_export.py`) read the retired core "
     "models and went with them on 23 Aug 2026."),

    # ---- Internal engineering records: owner's call ------------------------
    ("CLAUDE.md", DROP, "OWNER'S CALL. Internal working notes."),
    ("DECISIONS.md", DROP, "OWNER'S CALL. Internal defect and decision log."),
    ("PLATFORM_ROADMAP.md", DROP, "OWNER'S CALL. Internal programme plan."),
    ("DUPLICATE_ANTIBODY_FINDINGS.md", DROP,
     "OWNER'S CALL. Record of changes applied to live data."),
    ("AUDIT.md", DROP,
     "OWNER'S CALL. Candid internal engineering record: every finding, every "
     "defect, and the individuals involved. The original reason was narrower -- "
     "section 4 named two secrets as never rotated -- and that reason expired on "
     "23 Aug 2026 when the last of them was closed. It is dropped on the same "
     "ground as CLAUDE.md and DECISIONS.md, not because it still leaks anything."),
    (".claude/", DROP, "Internal agent skills, including production-data runbooks."),
    (".vscode/", DROP, "Personal editor settings."),
    ("benchmarks/", DROP,
     "Archive of measured artefacts, deliberately frozen. Not source."),

    # ---- Not source: scaffolds and one-off scripts (AUDIT.md section 5) ----
    ("pipeline_skeleton/", DROP, "Scaffold, not wired into INSTALLED_APPS or urls."),
    ("pipeline_skeleton.tar.gz", DROP, "Archive of the same scaffold."),
    ("script.py", DROP, "One-off dump-fixer; operates on the excluded dumps."),
    ("SYT1_report.docx", DROP, "Generated sample report, not source."),
    ("SYT1_antibody_characterization_report.docx", DROP,
     "Generated sample report, not source."),

    # ---- Development-process docs (owner's decision, 22 Aug 2026) ----------
    # The root CLAUDE.md / DECISIONS.md / PLATFORM_ROADMAP.md are dropped below;
    # these are the same genre living deeper in the tree and would otherwise ship
    # under their app's SHIP rule. Working notes and internal planning, not
    # documentation of the shipped thing.
    ("browser-extension/CLAUDE.md", DROP, "Agent working notes."),
    ("mcp_servers/CLAUDE.md", DROP, "Agent working notes."),
    ("mcp_servers/VISION.md", DROP,
     "Product vision and roadmap; also points at PLATFORM_ROADMAP.md, which is dropped."),
    ("mcp_servers/A2_SCOPING.md", DROP, "Internal scoping and design doc with build status."),
    ("pipeline/prototypes/", DROP,
     "Throwaway prototypes built before the figure cropper existed; unwired, and "
     "the README points at the dropped CLAUDE.md."),

    # ---- The snapshot's own front matter -----------------------------------
    # publish/ holds LICENSE, LICENSE-DATA, README.md and .env.example. They are
    # copied to the snapshot ROOT (see LICENCE_FILES), so the directory itself
    # must not ship as well -- otherwise the public repo carries them twice,
    # once where GitHub looks for them and once where nobody does.
    ("publish/", DROP, "Source of the snapshot's root files; copied to the root, not shipped in place."),

    # ---- Build artefacts that predate .gitignore ---------------------------
    # 39 .pyc files are still *tracked* in the private repo, committed before
    # .gitignore covered them (the same accident as db_core.sqlite3). Without
    # this rule the export copies them and `git add -A` in the new repo then
    # silently refuses them -- so the manifest would promise 837 files and
    # publish 798. Worth clearing in the private repo too:
    #     git rm --cached -r '*.pyc'
    ("*.pyc", DROP, "Stale tracked bytecode; .gitignore covers it now."),
    ("**/__pycache__/*", DROP, "Stale tracked bytecode; .gitignore covers it now."),

    # ---- Data files that are public by design ------------------------------
    # Every one of these is named exactly, never by directory. See
    # `DATA_SUFFIXES` below for why: a directory rule may drop data broadly,
    # but it may never ship it.
    ("core/data/citeab_papers_2026_05.json", SHIP,
     "Citation snapshot served publicly at /extension/citations.json."),
    ("core/data/title_normalisation_vectors.json", SHIP,
     "Shared test vectors both suites read."),
    ("browser-extension/data/index.json", SHIP, "The published dataset the extension ships with."),
    ("browser-extension/data/aliases.json", SHIP, "Gene alias table, public."),
    ("browser-extension/manifest.json", SHIP, "Extension manifest."),
    ("browser-extension/package.json", SHIP, "Defines `npm test`."),
    ("pipeline/data/antibody_availability_2026_08.csv", SHIP,
     "Supplier availability verdicts quoted from public supplier pages."),
    ("pipeline/data/horizon_hap1_ko.csv", SHIP, "Horizon's public catalogue."),
    ("pipeline/data/antibody_availability_2026_08_30.csv", SHIP,
     "The 30 Aug recheck of supplier availability; same kind of file as the 11-12 Aug pass above."),
    # The target-confusion notices and their paper lists are served to every
    # extension install through /extension/index.json (core/target_confusions.py
    # ::index_payload), so they are public by construction. Each CSV is a
    # reviewer's per-paper verdict keyed on DOI; the JSON is the notice itself.
    ("core/data/target_confusions/p16_ink4a.json", SHIP, "Published target-confusion notice."),
    ("core/data/target_confusions/p16_ink4a_papers.csv", SHIP, "Its per-paper verdicts, served in the index."),
    ("core/data/target_confusions/beta_galactosidase.json", SHIP, "Published target-confusion notice."),
    ("core/data/target_confusions/beta_galactosidase_papers.csv", SHIP, "Its per-paper verdicts, served in the index."),
    ("core/data/target_confusions/perk_for_p_erk.json", SHIP, "Published target-confusion notice."),
    ("core/data/target_confusions/perk_for_p_erk_papers.csv", SHIP, "Its per-paper verdicts, served in the index."),
    ("core/data/target_confusions/p_erk_for_perk.json", SHIP, "Published target-confusion notice."),
    ("core/data/target_confusions/p_erk_for_perk_papers.csv", SHIP, "Its per-paper verdicts, served in the index."),
    ("browser-extension/store/listing/banners.json", SHIP,
     "The store listing's banner manifest: headline text and the frame, all of it public on the listing."),
    ("pipeline/services/tests/fixtures/*.xlsx", SHIP, "Test fixture workbooks."),
    ("mcp_servers/content/plan_schema.json", SHIP, "Content schema."),
    ("mcp_servers/content/quizzes.json", SHIP, "Academy quiz content."),
    ("mcp_servers/mcp.example.json", SHIP, "Example client config, placeholders only."),
    ("mcp_servers/roles.sql", SHIP, "Role definitions; passwords are CHANGE_ME placeholders."),
    ("core/static/core/docs/*.docx", SHIP, "Public teaching material served by the site."),
    ("core/static/core/docs/*.xlsx", SHIP, "Public teaching material served by the site."),
    ("core/static/core/institutions/*.docx", SHIP, "Public teaching material served by the site."),

    # ---- Application source ------------------------------------------------
    ("core/", SHIP, "Public website app."),
    ("pipeline/", SHIP, "Lab data-entry app."),
    ("academy/", SHIP, "E-learning app."),
    ("credentials/", SHIP, "Certificate app (not secrets, despite the name)."),
    ("selector/", SHIP, "Antibody selector tool."),
    ("OGA_website/", SHIP, "Django project: settings, routing, storages."),
    ("templates/", SHIP, "Project-level templates, including 404 and 500."),
    ("browser-extension/", SHIP, "Reader-facing browser extension."),
    ("mcp_servers/", SHIP, "Reader-facing MCP servers."),
    ("bin/", SHIP, "Build scripts run at deploy."),
    ("tools/", SHIP, "Developer tools, including this script."),
    (".github/", SHIP, "CI workflows."),

    # ---- Root-level project files -----------------------------------------
    ("manage.py", SHIP, "Django entry point."),
    ("requirements.txt", SHIP, "Dependencies."),
    ("Procfile", SHIP, "Render process definition."),
    ("tailwind.config.js", SHIP, "Stylesheet build config."),
    ("server.json", SHIP, "MCP server manifest; contains no secrets."),
    ("test_map.json", SHIP, "Read by `manage.py test_changed`; excluding it breaks the runner."),
    (".gitignore", SHIP, "Ignore rules."),
    (".coveragerc", SHIP, "Coverage config."),
    (".python-version", SHIP, "Pinned Python version."),
    ("API.md", SHIP, "Public API documentation."),
    ("ANTIBODY_BOARD_GUIDE.md", SHIP, "User-facing board guide."),
    ("CELL_LINE_BOARD_GUIDE.md", SHIP, "User-facing board guide."),
    ("SESSION_BOARD_GUIDE.md", SHIP, "User-facing board guide."),
    ("TARGET_BOARD_GUIDE.md", SHIP, "User-facing board guide."),
]

# --------------------------------------------------------------------------
# Safety nets. These run over the files the rules chose to SHIP, so a rule
# written too broadly is caught by content rather than by path.
# --------------------------------------------------------------------------

#: Never shipped, whatever a rule says. A glob widened by accident cannot
#: reach these.
FORBIDDEN_SUFFIXES = (
    ".sqlite3", ".accdb", ".pem", ".key", ".p12", ".pfx", ".jks", ".env",
    ".dump", ".bak",
)

#: Content patterns that stop the export. The PBKDF2 prefix is here because
#: every dump that leaked so far was found by exactly that string.
SECRET_PATTERNS = [
    (re.compile(r"pbkdf2_sha256\$\d+\$"), "a Django password hash"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9-]{20,}"), "an Anthropic API key"),
    (re.compile(r"\bsk-[A-Za-z0-9]{32,}"), "an API key"),
    (re.compile(r"\bghp_[A-Za-z0-9]{30,}"), "a GitHub token"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}"), "a GitHub token"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "an AWS access key id"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"), "a Google API key"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), "a Slack token"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "a private key"),
    (re.compile(r"\bpostgres(?:ql)?://[^\s\"'<>]*:[^\s\"'<>@]+@"), "a database URL with a password"),
    # A live Render database host. Not a credential, which is exactly why it
    # slipped past the rule above: `postgresql:/user:secret@dpg-<real>-a...`
    # has no `://`, so it is not a URL, and the password in it was fake anyway.
    # What it publishes is the *target* -- a publicly resolvable hostname, with
    # the database name and user beside it -- and a test only needs a string of
    # the right shape and length. Placeholders go in ALLOWED_MATCHES below.
    (re.compile(r"\bdpg-[a-z0-9]{16,}"), "a live Render database host"),
]

#: Real people's addresses that appeared in the dumps, held as SHA-256 of the
#: lowercased address. Hashes rather than plaintext for the obvious reason: this
#: script ships in the public snapshot, and a guard list written in the clear
#: would publish the very addresses it exists to keep out. Institutional
#: addresses already on the live site (the champions page) are not listed.
KNOWN_PII_SHA256 = {
    "81df2b7e5af9f3158781e0d0975d3e7639dd66a09fd1156e7722c386efb47592",
    "56273ea020b0a7c00bb47046548527e8c4092683b273dc48932e489fe97956f1",
    "32d91ba2040c50f34a2a9b894516d7f8c125716c180cf9edaab0d768c420403e",
    "76b85a341edfe417ecb4b27388cb8cbbf6dd37980e2ced178caac65a01b7aa72",
}

_EMAIL_RX = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _is_known_pii(address: str) -> bool:
    return hashlib.sha256(address.lower().encode()).hexdigest() in KNOWN_PII_SHA256


#: Placeholders that legitimately look like the patterns above.
ALLOWED_MATCHES = [
    "postgresql://mcp_readonly:...@host/db",
    "postgresql://mcp_readonly:PW@INTERNAL_HOST:5432/DBNAME",
    "postgres://user:pass@localhost:5432/pipeline_pg",
    "postgresql://USER:PASSWORD@dpg-xxxxxxxxxxxx-a/oga_academy_db",
    "postgres://user:pw@host:5432/oga_academy_db",
    "dpg-xxxxxxxxxxxxxxxxxxxx-a",
]

#: Files the export writes into the snapshot root. Sourced from `publish/` in
#: this repo so their wording is reviewed and version-controlled like anything
#: else. A snapshot without a licence is not open source, it is just visible.
LICENCE_FILES = ["LICENSE", "LICENSE-DATA", "README.md", ".env.example"]

BINARY_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".pptx", ".docx", ".xlsx",
    ".zip", ".gz", ".woff", ".woff2", ".ttf", ".wasm", ".traineddata", ".mp4",
)


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files"],
        capture_output=True, text=True, check=True,
    )
    return [line for line in out.stdout.splitlines() if line]


#: Extensions that carry data rather than code. Every leak this repo has had
#: was one of these; none was ever a source file. So they get the strict
#: treatment: a directory rule may DROP them broadly, because dropping is the
#: safe direction, but only an EXACT-PATH rule may SHIP one. A new .csv under
#: `pipeline/` therefore stops the export even though `pipeline/` is a SHIP
#: rule, while a new .py under `pipeline/` ships with no ceremony -- which is
#: what keeps the script from becoming something people route around.
DATA_SUFFIXES = (
    ".csv", ".tsv", ".json", ".xlsx", ".xls", ".xlsm", ".sql", ".sqlite3",
    ".accdb", ".mdb", ".db", ".docx", ".parquet", ".pkl", ".h5", ".dta",
)


def classify(path: str):
    """Return (decision, reason, pattern, by_prefix) for the first match."""
    for pattern, decision, reason in RULES:
        if pattern.endswith("/"):
            if path.startswith(pattern):
                return decision, reason, pattern, True
        elif path == pattern or fnmatch.fnmatch(path, pattern):
            return decision, reason, pattern, False
    return None, None, None, False


def scan_content(paths: list[str]) -> list[str]:
    """Look inside every shipped text file. Returns a list of problems."""
    problems: list[str] = []
    for rel in paths:
        p = REPO / rel
        if rel.endswith(BINARY_SUFFIXES) or not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for rx, what in SECRET_PATTERNS:
            for m in rx.finditer(text):
                if any(a in m.group(0) or m.group(0) in a for a in ALLOWED_MATCHES):
                    continue
                line = text[:m.start()].count("\n") + 1
                problems.append(f"{rel}:{line} looks like {what}: {m.group(0)[:60]}")
        for m in _EMAIL_RX.finditer(text):
            if _is_known_pii(m.group(0)):
                line = text[:m.start()].count("\n") + 1
                problems.append(
                    f"{rel}:{line} contains a real personal email address")
    return problems


def ignored_by_gitignore(paths: list[str]) -> list[str]:
    """Which of `paths` would `git add` refuse, given this repo's .gitignore?"""
    out = subprocess.run(
        ["git", "-C", str(REPO), "check-ignore", "--stdin"],
        input="\n".join(paths), capture_output=True, text=True,
    )
    # Exit 0 = some ignored, 1 = none ignored, other = real error.
    if out.returncode not in (0, 1):
        raise RuntimeError(f"git check-ignore failed: {out.stderr.strip()}")
    return [line for line in out.stdout.splitlines() if line]


def human(kb: float) -> str:
    return f"{kb/1024:.1f} MB" if kb >= 1024 else f"{kb:.0f} KB"


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the public snapshot of this repo.")
    ap.add_argument("--apply", metavar="DEST",
                    help="Write the snapshot to DEST (must not already exist).")
    ap.add_argument("--manifest", metavar="FILE",
                    help="Write the shipped file list to FILE.")
    ap.add_argument("--without-licence", action="store_true",
                    help="Allow --apply even though publish/ has no licence files.")
    args = ap.parse_args()

    files = tracked_files()
    ship: list[str] = []
    drop: list[tuple[str, str]] = []
    unmatched: list[str] = []

    needs_exact: list[str] = []
    for rel in files:
        decision, reason, _, by_prefix = classify(rel)
        if decision is None:
            unmatched.append(rel)
        elif decision == SHIP and by_prefix and rel.endswith(DATA_SUFFIXES):
            # A broad directory rule tried to ship a data file. Refuse.
            needs_exact.append(rel)
        elif decision == SHIP:
            ship.append(rel)
        else:
            drop.append((rel, reason))

    # ---- The loud failure -------------------------------------------------
    if needs_exact:
        print("EXPORT REFUSED: data files reached a broad SHIP rule.\n")
        for rel in needs_exact:
            print(f"    {rel}")
        print(
            "\nA directory rule may drop data broadly, but it may never ship it.\n"
            "Every leak in this repo's history was a data file that arrived\n"
            "inside a directory somebody had already decided was safe.\n"
            "Add an exact-path rule for each file above -- SHIP with a reason if\n"
            "it is genuinely public, DROP if it is not."
        )
        return 1

    if unmatched:
        print("EXPORT REFUSED: these tracked files match no rule.\n")
        for rel in unmatched:
            print(f"    {rel}")
        print(
            "\nThis is the script working as intended. A new file is not shipped\n"
            "by default and is not dropped by default -- someone has to decide.\n"
            "Add each one to RULES in tools/export_public.py as SHIP or DROP\n"
            "with a reason, then run again."
        )
        return 1

    # ---- Belt and braces --------------------------------------------------
    forbidden = [r for r in ship if r.endswith(FORBIDDEN_SUFFIXES)]
    if forbidden:
        print("EXPORT REFUSED: a rule selected files that may never ship.\n")
        for rel in forbidden:
            print(f"    {rel}")
        print("\nA SHIP rule is too broad. Narrow it.")
        return 1

    ignored = ignored_by_gitignore(ship)
    if ignored:
        print("EXPORT REFUSED: shipped files that the shipped .gitignore excludes.\n")
        for rel in ignored[:30]:
            print(f"    {rel}")
        if len(ignored) > 30:
            print(f"    ... and {len(ignored) - 30} more")
        print(
            "\nThese would be copied into the snapshot and then silently dropped by\n"
            "`git add`, so the manifest would promise more files than get published.\n"
            "A count that disagrees with its own list is the bug this repo keeps\n"
            "finding. DROP them here, or stop tracking them upstream."
        )
        return 1

    problems = scan_content(ship)
    if problems:
        print("EXPORT REFUSED: shipped files contain secrets or personal data.\n")
        for line in problems[:40]:
            print(f"    {line}")
        if len(problems) > 40:
            print(f"    ... and {len(problems) - 40} more")
        print("\nFix the file, or narrow the rule that selected it.")
        return 1

    # ---- The manifest -----------------------------------------------------
    sizes = {r: (REPO / r).stat().st_size / 1024 for r in ship if (REPO / r).is_file()}
    total = sum(sizes.values())

    print(f"Public snapshot: {len(ship)} files, {human(total)}")
    print(f"Excluded:        {len(drop)} files\n")

    print("SHIPPED, by area")
    print("-" * 66)
    areas: dict[str, list[str]] = {}
    for rel in ship:
        areas.setdefault(rel.split("/")[0] if "/" in rel else "(root)", []).append(rel)
    for area in sorted(areas):
        n = len(areas[area])
        kb = sum(sizes.get(r, 0) for r in areas[area])
        print(f"  {area:<26} {n:>4} files  {human(kb):>9}")

    print("\nEXCLUDED, and why")
    print("-" * 66)
    by_reason: dict[str, list[str]] = {}
    for rel, reason in drop:
        by_reason.setdefault(reason, []).append(rel)
    for reason in sorted(by_reason, key=lambda r: -len(by_reason[r])):
        paths = by_reason[reason]
        head = paths[0] if len(paths) == 1 else f"{paths[0]}  (+{len(paths)-1} more)"
        print(f"  {head}")
        for line in _wrap(reason, 62):
            print(f"      {line}")
    owner_calls = [r for r in by_reason if r.startswith("OWNER'S CALL")]
    if owner_calls:
        print(f"\n  {len(owner_calls)} exclusions are marked OWNER'S CALL -- judgement, not safety.")

    print("\nLargest shipped files")
    print("-" * 66)
    for rel, kb in sorted(sizes.items(), key=lambda kv: -kv[1])[:10]:
        print(f"  {human(kb):>9}  {rel}")

    if args.manifest:
        Path(args.manifest).write_text("\n".join(ship) + "\n", encoding="utf-8")
        print(f"\nManifest written to {args.manifest}")

    if not args.apply:
        print("\nDry run. Nothing written. Re-run with --apply DEST to build it.")
        return 0

    # ---- Writing the tree -------------------------------------------------
    publish = REPO / "publish"
    missing = [f for f in LICENCE_FILES if not (publish / f).is_file()]
    if missing and not args.without_licence:
        print(f"\nEXPORT REFUSED: publish/ is missing {', '.join(missing)}.")
        print(
            "A snapshot without a licence is source-available, not open source:\n"
            "default copyright means nobody may legally reuse it. Add the files\n"
            "under publish/, or pass --without-licence if you are deliberately\n"
            "building an unlicensed preview."
        )
        return 1

    dest = Path(args.apply).resolve()
    if dest.exists():
        print(f"\nEXPORT REFUSED: {dest} already exists. Choose a new directory.")
        return 1
    if dest == REPO or REPO in dest.parents:
        print(f"\nEXPORT REFUSED: {dest} is inside the private repo. Choose a path outside it.")
        return 1

    for rel in ship:
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / rel, target)
    for name in LICENCE_FILES:
        if (publish / name).is_file():
            shutil.copy2(publish / name, dest / name)

    print(f"\nWritten: {dest}  ({len(ship)} files, {human(total)})")
    print(
        "\nNothing has been committed or pushed. To publish:\n"
        f"    cd {dest}\n"
        "    git init && git add -A\n"
        "    git commit -m 'Initial public snapshot'\n"
        "    git remote add origin git@github.com:<org>/<repo>.git\n"
        "    git push -u origin main\n"
        "\nCheck `git log` shows exactly one commit before pushing."
    )
    return 0


def _wrap(text: str, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines


if __name__ == "__main__":
    sys.exit(main())
