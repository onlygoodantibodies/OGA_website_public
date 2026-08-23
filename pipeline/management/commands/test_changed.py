"""Run the tests that actually cover what this session changed.

The whole suite took ~17 minutes and there was no way to ask for less, so every
session ran all of it regardless of what it touched. `manage.py test` with no
arguments runs everything — that is the default, and nothing else was wired up.

**Two runners, and one of them was invisible.** `manage.py test` collects
`unittest.TestCase` subclasses. Every module in `pipeline/services/tests/` is
pytest-style — bare `def test_...` functions with fixtures — so the Django
runner collects **zero** tests from all eighteen of them and says nothing about
it. 208 tests, including `test_target_board` (34) and `test_duplicates` (26),
were not running in the 17-minute run that reported `Ran 864 tests ... OK`. A
selection built on the Django runner alone would inherit that hole and look
complete, so the map is built from both runners and this command dispatches to
both.

Selection is *derived, never typed*: `test_map.json` records which test executed
each line, from a run under coverage with `dynamic_context = test_function`.
That is the only thing that sees the real coupling here, because these tests
reach their code over HTTP — `tests_navigation` imports `pipeline.models` and a
helper, and exercises thirty view modules through `self.client.get(...)`. A
hand-written "changed file -> test file" table would be another hand-maintained
list of the kind this codebase keeps having to consolidate, and it would drift
the first time somebody moved a function.

Three rules it holds, each of which is the point:

- **A selection that cannot be justified is a full run.** Anything the map
  cannot see — settings, URLconfs, migrations, `board.js`, a base template, the
  shared test helper — reaches everything, so it selects everything rather than
  guessing. Same for a source file the map has never heard of.
- **A module that was skipped when the map was built is always selected.**
  `tests_browser_board` skips without playwright and therefore covers nothing,
  so coverage cannot map it and it would silently never be selected again. It
  costs nothing to include when it would skip, and it is the one that matters
  when playwright *is* installed.
- **It says what it skipped.** A run that quietly narrowed to three tests and
  passed reads exactly like a run that checked everything.

Rebuild after adding a test module, or when selection starts looking thin:
`python manage.py test_changed --rebuild-map` (~90 seconds).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

BASE_DIR = Path(settings.BASE_DIR)
MAP_PATH = BASE_DIR / "test_map.json"

# The five apps holding Django-runner tests. Named explicitly rather than left
# to discovery, because bare `manage.py test` walks into mcp_servers/tests/,
# which is pytest-only, and reports 4 import errors on every otherwise-clean run.
DJANGO_APPS = ["pipeline", "core", "academy", "credentials", "selector"]

# Pytest roots inside the Django project. mcp_servers/ is deliberately not here:
# it is one of the two reader-facing tools, not part of the three Django apps,
# and it has its own requirements.txt. It is selected by path instead.
PYTEST_ROOTS = ["pipeline/services/tests", "pipeline/services/cropper/tests"]

# Changes coverage cannot attribute to any one test, because they are read at
# startup or shared by every page. Each reaches the whole suite, so the honest
# answer is a full run.
ALWAYS_FULL = {
    "manage.py",
    "requirements.txt",
    ".coveragerc",
    "pipeline/urls.py",
    "pipeline/context_processors.py",
    "pipeline/decorators.py",
    # board.js is the front end of all four boards; base.html is the chrome on
    # every page. The rule in CLAUDE.md is that a fix in one surface is a fix
    # for one surface — a change to the shared file is exactly when to run all.
    "pipeline/static/pipeline/board.js",
    # Imported by ~14 test modules for DB, _member_client and no_network.
    "pipeline/tests_timeouts.py",
    "pipeline/services/tests/conftest.py",
}
ALWAYS_FULL_PREFIXES = ("OGA_website/", "templates/")
ALWAYS_FULL_PARTS = ("/migrations/",)

# Docs are not code. Everything else unrecognised is a full run.
DOC_SUFFIXES = (".md", ".txt", ".rst")


def _git(*args) -> str:
    return subprocess.run(
        ["git", *args], cwd=BASE_DIR, capture_output=True, text=True, check=False
    ).stdout


def changed_files(base: str) -> list[str]:
    """Every file this branch has touched: committed since `base`, plus staged,
    unstaged and untracked work. Uncommitted edits are the common case
    mid-session, so leaving them out would select against work already pushed
    and miss the work in hand."""
    out: set[str] = set()
    merge_base = _git("merge-base", "HEAD", base).strip()
    if merge_base:
        out |= set(_git("diff", "--name-only", merge_base, "HEAD").split())
    out |= set(_git("diff", "--name-only").split())
    out |= set(_git("diff", "--cached", "--name-only").split())
    out |= set(_git("ls-files", "--others", "--exclude-standard").split())
    return sorted(f for f in out if f)


def _is_test_file(path: str) -> bool:
    """A management command may legitimately be called `test_changed.py`, and
    this file is. Without the second clause it classified *itself* as a test
    module, and pytest then tried to collect `runner_for` as a test case —
    caught by the first --full run, which is the argument for having one."""
    if "/management/commands/" in path:
        return False
    name = Path(path).name
    return name.startswith(("test_", "tests_")) or name == "tests.py"


def _defines_unittest_classes(dotted: str) -> bool:
    """Whether Django's runner would actually collect anything from a module.

    Answered by importing it, because reading the text cannot see a base class
    that is itself imported. `core/tests_bulk_archive.py` subclasses
    `ArchiveCase`, which subclasses `ManifestCase`, which is the `TestCase` —
    so the file holds no `TestCase` token anywhere and the regex below filed it
    as pytest's. pytest cannot collect it without Django, so its **53 tests ran
    nowhere at all**, and the only symptom was one `AppRegistryNotReady` line
    in the output that read exactly like a missing dependency. The same shape
    as the 208 that made this command necessary: a silent omission wearing the
    clothes of an environment problem.

    Only ever consulted to overturn a *pytest* verdict, and that asymmetry is
    the point. A wrong "django" is loud — the runner reports collecting nothing
    from a module you named. A wrong "pytest" is silent. Only the silent
    direction is worth paying an import for.

    An unimportable module keeps its pytest verdict: pytest is then the right
    place for it to fail, loudly, rather than here.
    """
    import importlib
    import inspect
    import unittest

    try:
        module = importlib.import_module(dotted)
    except Exception:
        return False
    return any(
        issubclass(obj, unittest.TestCase) and obj.__module__ == dotted
        for _, obj in inspect.getmembers(module, inspect.isclass)
    )


def runner_for(path: str) -> str | None:
    """Which runner owns a test file, and how to name it to that runner.

    Django's runner only collects unittest classes, so a file with none is
    pytest's — that is the fact that made 208 tests invisible, and it is read
    off the file rather than assumed from its directory.
    """
    p = BASE_DIR / path
    if not path.endswith(".py") or not p.exists():
        return None
    try:
        src = p.read_text()
    except OSError:
        return None
    parts = Path(path).with_suffix("").parts
    in_django_app = bool(parts) and parts[0] in DJANGO_APPS
    unittest_style = re.search(r"\(\s*(?:\w+\.)?(?:Simple|Transaction|LiveServer|StaticLiveServer)?TestCase\s*[,)]", src)
    if unittest_style:
        if in_django_app:
            return "django:" + ".".join(parts)
        return None
    # The text says pytest. That is the verdict that loses a whole module in
    # silence, so it is confirmed against what the runner would really collect.
    if in_django_app and _defines_unittest_classes(".".join(parts)):
        return "django:" + ".".join(parts)
    return "pytest:" + path


def templates_to_python(template_path: str, _seen=None) -> set[str]:
    """A template is not executed, so coverage never sees it. Find the Python
    that names it — `render(request, "pipeline/antibody_board.html")` — and let
    the map answer for that instead. Derived, so moving a template's view keeps
    working."""
    _seen = _seen or set()
    if template_path in _seen:
        return set()
    _seen.add(template_path)
    rel = re.sub(r"^.*?/templates/", "", template_path)
    if rel == template_path:
        rel = re.sub(r"^templates/", "", template_path)
    if not rel:
        return set()
    hits: set[str] = set()
    referrers = _git("grep", "-l", "--", rel).split()
    for f in referrers:
        if f.endswith(".py") and not _is_test_file(f):
            hits.add(f)
    # A partial included by another template has no Python naming it; walk up.
    if not hits:
        for f in referrers:
            if f.endswith(".html") and f != template_path:
                hits |= templates_to_python(f, _seen)
    return hits


class Command(BaseCommand):
    help = "Run only the tests that cover what changed on this branch."

    def add_arguments(self, parser):
        parser.add_argument("--base", default="beta",
                            help="Branch to diff against (default: beta, the live branch).")
        parser.add_argument("--list", action="store_true", dest="list_only",
                            help="Show the selection and why, without running anything.")
        parser.add_argument("--full", action="store_true",
                            help="Skip selection and run everything, both runners.")
        parser.add_argument("--rebuild-map", action="store_true",
                            help="Re-run both suites under coverage and rewrite test_map.json.")

    # ----------------------------------------------------------------- map

    def _rebuild_map(self):
        self.stdout.write("Rebuilding the map: both suites, under coverage.\n")
        env = dict(os.environ, COVERAGE_RCFILE=str(BASE_DIR / ".coveragerc"))
        data_file = BASE_DIR / ".coverage.testmap"
        data_file.unlink(missing_ok=True)
        env["COVERAGE_FILE"] = str(data_file)
        cov = [sys.executable, "-m", "coverage", "run"]

        failed = []
        if subprocess.run([*cov, "manage.py", "test", *DJANGO_APPS, "--keepdb"],
                          cwd=BASE_DIR, env=env, check=False).returncode:
            failed.append("the Django suite")
        if subprocess.run([*cov, "--append", "-m", "pytest", *PYTEST_ROOTS, "-q"],
                          cwd=BASE_DIR, env=env, check=False).returncode:
            failed.append("the pytest suite")
        if failed:
            self.stderr.write(self.style.WARNING(
                f"\n{' and '.join(failed)} did not pass. The map is still "
                f"written, but a test that errored covers nothing, so its lines "
                f"are missing from it — expect thin selection until it is green."))

        import coverage  # imported here so the site runs without it installed

        data = coverage.CoverageData(str(data_file))
        data.read()
        mapping: dict[str, set[str]] = {}
        for measured in data.measured_files():
            rel = os.path.relpath(measured, BASE_DIR)
            for _line, contexts in (data.contexts_by_lineno(measured) or {}).items():
                for ctx in contexts:
                    if not ctx:
                        continue
                    # "pipeline.tests_deletion.SomeTests.test_x" -> the file
                    bits = ctx.replace("|", ".").split(".")
                    for i in range(len(bits), 0, -1):
                        cand = "/".join(bits[:i]) + ".py"
                        if (BASE_DIR / cand).exists():
                            tid = runner_for(cand)
                            if tid:
                                mapping.setdefault(rel, set()).add(tid)
                            break

        mapped = {t for v in mapping.values() for t in v}
        unmapped = sorted(set(self._all_test_ids()) - mapped)
        MAP_PATH.write_text(json.dumps({
            "built_from": _git("rev-parse", "HEAD").strip(),
            # Covered nothing when the map was built — skipped, or empty. Always
            # selected, because a module that covers nothing can never be found
            # by coverage again and would drop out of every run silently.
            "always_run": unmapped,
            "map": {k: sorted(v) for k, v in sorted(mapping.items())},
        }, indent=0, sort_keys=True) + "\n")
        data_file.unlink(missing_ok=True)
        self.stdout.write(self.style.SUCCESS(
            f"\nWrote {MAP_PATH.name}: {len(mapping)} source files -> "
            f"{len(mapped)} test modules, {len(unmapped)} always-run."))

    def _all_test_ids(self) -> list[str]:
        ids = []
        for path in _git("ls-files").split():
            if path.endswith(".py") and _is_test_file(path):
                if path.startswith(("mcp_servers/", "benchmarks/")):
                    continue
                tid = runner_for(path)
                if tid:
                    ids.append(tid)
        return ids

    def _load(self) -> tuple[dict[str, list[str]], list[str]]:
        if not MAP_PATH.exists():
            return {}, []
        try:
            blob = json.loads(MAP_PATH.read_text())
        except (ValueError, OSError):
            return {}, []
        return blob.get("map", {}), blob.get("always_run", [])

    # -------------------------------------------------------------- select

    def select(self, files, mapping, always_run):
        selected: set[str] = set()
        reasons: dict[str, list[str]] = {}
        full_because: list[str] = []
        mcp = False

        def note(tid, why):
            selected.add(tid)
            reasons.setdefault(tid, []).append(why)

        for f in files:
            if (f in ALWAYS_FULL or f.startswith(ALWAYS_FULL_PREFIXES)
                    or any(p in f for p in ALWAYS_FULL_PARTS)):
                full_because.append(f)
                continue
            if f.endswith(DOC_SUFFIXES) or f.startswith("docs/"):
                continue
            if f.startswith("mcp_servers/"):
                mcp = True
                continue
            if f.startswith(("browser-extension/", "benchmarks/")):
                continue  # own CI / an archive that is never re-run
            if _is_test_file(f):
                tid = runner_for(f)
                if tid:
                    note(tid, "the test file itself changed")
                continue
            if f.endswith(".html"):
                owners = templates_to_python(f)
                if not owners:
                    full_because.append(f"{f} (no Python names this template)")
                    continue
                for owner in owners:
                    if owner in mapping:
                        for tid in mapping[owner]:
                            note(tid, f"covers {owner}, which renders {Path(f).name}")
                    else:
                        full_because.append(f"{f} -> {owner} (not in the map)")
                continue
            if f.endswith(".py"):
                if f in mapping:
                    for tid in mapping[f]:
                        note(tid, f"covers {f}")
                else:
                    full_because.append(f"{f} (not in the map)")
                continue
            full_because.append(f"{f} (unrecognised)")

        if selected:
            for tid in always_run:
                note(tid, "covered nothing when the map was built (skipped or empty)")
        return sorted(selected), reasons, full_because, mcp

    # -------------------------------------------------------------- handle

    def handle(self, *args, **opts):
        if opts["rebuild_map"]:
            return self._rebuild_map()

        if opts["full"]:
            self.stdout.write("Full run, as asked.")
            return self._dispatch(self._all_test_ids(), mcp=False)

        mapping, always_run = self._load()
        if not mapping:
            self.stderr.write(self.style.WARNING(
                f"No {MAP_PATH.name} — nothing to select with, so running "
                f"everything. Build it with --rebuild-map."))
            return self._dispatch(self._all_test_ids(), mcp=False)

        files = changed_files(opts["base"])
        if not files:
            self.stdout.write(f"Nothing has changed against {opts['base']}.")
            return

        selected, reasons, full_because, mcp = self.select(files, mapping, always_run)

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n{len(files)} changed file(s) against {opts['base']}:"))
        for f in files:
            self.stdout.write(f"  {f}")

        if full_because:
            self.stdout.write(self.style.MIGRATE_HEADING(
                "\nRunning everything. These reach the whole app, or the map "
                "cannot account for them:"))
            for why in full_because:
                self.stdout.write(f"  {why}")
            if opts["list_only"]:
                return
            return self._dispatch(self._all_test_ids(), mcp=True)

        if not selected and not mcp:
            self.stdout.write(
                "\nNothing changed that any test covers. Use --full to be sure.")
            return

        total = len(self._all_test_ids())
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\nSelected {len(selected)} of {total} test modules:"))
        for tid in selected:
            runner, name = tid.split(":", 1)
            self.stdout.write(f"  [{runner}] {name}")
            for why in dict.fromkeys(reasons[tid]):
                self.stdout.write(f"        {why}")
        # Never let a narrowed run read like a complete one.
        self.stdout.write(self.style.WARNING(
            f"\n  Not running {total - len(selected)} other test module(s). "
            f"Run --full, or let CI run the whole suite, before merging to beta."))

        if opts["list_only"]:
            return
        self._dispatch(selected, mcp=mcp)

    # ------------------------------------------------------------ dispatch

    def _dispatch(self, test_ids, mcp: bool):
        django_labels = sorted(t.split(":", 1)[1] for t in test_ids if t.startswith("django:"))
        pytest_paths = sorted(t.split(":", 1)[1] for t in test_ids if t.startswith("pytest:"))
        rc = 0
        if django_labels:
            self.stdout.write(self.style.MIGRATE_HEADING(
                f"\n== Django runner: {len(django_labels)} module(s) =="))
            rc |= subprocess.run(
                [sys.executable, "manage.py", "test", *django_labels, "--keepdb"],
                cwd=BASE_DIR, check=False).returncode
        if pytest_paths:
            self.stdout.write(self.style.MIGRATE_HEADING(
                f"\n== pytest: {len(pytest_paths)} module(s) =="))
            rc |= subprocess.run(
                [sys.executable, "-m", "pytest", *pytest_paths, "-q"],
                cwd=BASE_DIR, check=False).returncode
        if mcp:
            self.stdout.write(self.style.MIGRATE_HEADING("\n== pytest: mcp_servers =="))
            self.stdout.write(self.style.WARNING(
                "mcp_servers has its own requirements.txt; missing deps show as "
                "collection errors, not failures."))
            rc |= subprocess.run(
                [sys.executable, "-m", "pytest", "mcp_servers/tests", "-q"],
                cwd=BASE_DIR, check=False).returncode
        if rc:
            sys.exit(1)
