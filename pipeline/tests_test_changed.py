"""The test selector's own routing — the one thing about it that fails silently.

`manage.py test_changed` sends each test module to one of two runners, and a
module sent to the wrong one does not error in a way anybody reads: pytest
handed a Django `TestCase` module answers `AppRegistryNotReady`, which is one
line in the output and looks exactly like a missing dependency. It is not. It
means every test in that module ran nowhere.

That is not hypothetical. `core/tests_bulk_archive.py` subclasses `ArchiveCase`
-> `ManifestCase` -> `TestCase`, so the file contains no `TestCase` token and
the text-matching in `runner_for` filed it as pytest's. **53 tests**, including
the one its own docstring calls the test that matters most in the file — the
guard against handing a catalogue-scoped manufacturer an archive of every
competitor's figures — ran in neither runner, under a green `OK`.

So this asks the question the selector cannot ask itself: does the runner each
module is sent to actually collect it? It is the cheapest thing here that can
fail, and it is the same family as `test_map.json` going stale and a falling
test count — a silent omission wearing the clothes of success.
"""
from __future__ import annotations

import importlib
import inspect
import subprocess
import unittest

from django.test import SimpleTestCase

from pipeline.management.commands.test_changed import (
    DJANGO_APPS, _is_test_file, runner_for,
)


def _tracked_test_files():
    """Every test file git knows about, minus the two trees with own runners.

    Read from git rather than walked, because that is what `_all_test_ids` does
    — and it is why an *untracked* new test module is invisible even to a
    `--full` run, which is worth knowing before trusting one.
    """
    listing = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, check=True).stdout
    return [p for p in listing.split()
            if p.endswith(".py") and _is_test_file(p)
            and not p.startswith(("mcp_servers/", "benchmarks/"))]


def _dotted(path):
    return path[:-len(".py")].replace("/", ".")


def _unittest_classes(dotted):
    """TestCase subclasses *defined* in this module, as the runner would find."""
    try:
        module = importlib.import_module(dotted)
    except Exception:
        return []
    return [obj for _, obj in inspect.getmembers(module, inspect.isclass)
            if issubclass(obj, unittest.TestCase) and obj.__module__ == dotted]


class EveryTestModuleReachesARunnerThatCanCollectItTests(SimpleTestCase):

    def test_a_module_holding_testcases_is_routed_to_the_django_runner(self):
        """The direction that loses tests in silence.

        Deliberately checks what the module *is*, not what its text looks like:
        a base class imported from another file is exactly what the text-based
        answer cannot see, and is what cost the 53.
        """
        misrouted = []
        for path in _tracked_test_files():
            dotted = _dotted(path)
            if dotted.split(".")[0] not in DJANGO_APPS:
                continue
            classes = _unittest_classes(dotted)
            if classes and not (runner_for(path) or "").startswith("django:"):
                misrouted.append(f"{path} ({len(classes)} TestCase classes) "
                                 f"-> {runner_for(path)}")
        self.assertEqual(misrouted, [], "\n".join(
            ["these modules define unittest tests and are sent to pytest, "
             "which cannot collect them — they run nowhere:"] + misrouted))

    def test_a_module_the_django_runner_gets_has_something_to_collect(self):
        """The loud direction, pinned anyway so the fix cannot overshoot.

        A module sent to Django with no TestCase in it reports collecting
        nothing, which somebody notices — but it would also mean the
        confirmation step above had started guessing.
        """
        empty = [path for path in _tracked_test_files()
                 if (runner_for(path) or "").startswith("django:")
                 and not _unittest_classes(_dotted(path))]
        self.assertEqual(empty, [], "\n".join(
            ["these are sent to the Django runner, which will collect nothing "
             "from them:"] + empty))

    def test_the_module_that_cost_fifty_three_tests_is_named_correctly(self):
        """The specific regression, kept as its own line so a failure says which."""
        self.assertEqual(runner_for("core/tests_bulk_archive.py"),
                         "django:core.tests_bulk_archive")

    def test_a_pytest_style_module_is_still_pytest(self):
        """The fix must not sweep the eighteen pytest modules into the wrong
        runner in the other direction — they hold no unittest classes at all."""
        self.assertEqual(
            runner_for("pipeline/services/tests/test_target_board.py"),
            "pytest:pipeline/services/tests/test_target_board.py")
