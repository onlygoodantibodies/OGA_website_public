"""The portal's behaviour is one inline ``<script>``, and nothing parsed it.

``core/templates/core/portal.html`` is a self-contained page — its own ``<head>``,
its own CSS, and every one of its behaviours in a single inline script several
hundred lines long. A stray brace or an unclosed template literal in there is
invisible to ``manage.py check``, to the view test, and to anything that asserts
on the rendered HTML: the page still returns 200 and the dashboard simply never
appears.

``pipeline/tests_board_fieldtest.py::BoardScriptsParseTests`` sweeps the pipeline
templates for exactly this and names ten of them, including two — ``cropper.html``
and ``recommendations.html`` — that were added once somebody noticed they were
large inline-JS pages that happened not to use ``OGABoard``. The portal is the
same kind of page in a different app, so that sweep could never see it.

The second test is the one that is hard to spot by eye. Two ``function foo(){}``
declarations in one scope are valid JavaScript: the later wins for the whole
scope and the earlier is dead wherever it sits. That shipped once already in
``data_io.html``, where two functions called ``chip`` meant every field in the
download picker was labelled by the wrong one.
"""
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

TEMPLATE = Path(settings.BASE_DIR) / "core/templates/core/portal.html"


def _inline_scripts():
    """Every inline script, with the Django tags taken out.

    ``{% %}`` goes entirely and ``{{ }}`` becomes a string literal, so what is
    checked is the JavaScript rather than the template.
    """
    source = TEMPLATE.read_text()
    for match in re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
                             source, re.S):
        js = re.sub(r"\{%.*?%\}", "", match.group(1), flags=re.S)
        yield re.sub(r"\{\{.*?\}\}", '"x"', js, flags=re.S)


class ThePortalScriptParsesTests(SimpleTestCase):

    def setUp(self):
        if not shutil.which("node"):
            self.skipTest("node is not installed")

    def _check(self, js, label):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(js)
            tmp = fh.name
        try:
            done = subprocess.run(["node", "--check", tmp],
                                  capture_output=True, text=True)
        finally:
            os.unlink(tmp)
        self.assertEqual(done.returncode, 0,
                         f"{label} does not parse:\n{done.stderr}")

    def test_the_portal_script_parses(self):
        scripts = list(_inline_scripts())
        self.assertTrue(scripts, "No inline script found — has the portal been "
                                 "restructured? This test would then be passing "
                                 "by checking nothing.")
        for i, js in enumerate(scripts):
            with self.subTest(script=i):
                self._check(js, f"portal.html script {i}")

    def test_no_function_name_is_declared_twice(self):
        for i, js in enumerate(_inline_scripts()):
            names = re.findall(r"^\s*function\s+([A-Za-z_$][\w$]*)\s*\(",
                               js, re.M)
            duplicates = sorted({n for n in names if names.count(n) > 1})
            self.assertEqual(
                duplicates, [],
                f"portal.html script {i} declares these twice: {duplicates}. "
                "The later declaration silently wins for the whole scope.")


class EveryButtonCallsSomethingThatExistsTests(SimpleTestCase):
    """An ``onclick`` naming a function nobody declared is a dead button.

    It fails at the click, in the console, with the page rendering perfectly —
    so `manage.py check` passes, `node --check` passes, and every response test
    passes, because the markup is all present and correct. The same shape as the
    ``board.js`` helper declared in the wrong function that CLAUDE.md records:
    valid syntax, and the error only exists when the line runs.

    Cheap enough to be worth it on its own: it reads two regexes and needs no
    browser.
    """

    def test_every_onclick_names_a_declared_function(self):
        source = TEMPLATE.read_text()
        declared = set()
        for js in _inline_scripts():
            declared |= set(re.findall(
                r"function\s+([A-Za-z_$][\w$]*)\s*\(", js))
            # `const f = function …`, `const f = (…) => …` and the single-param
            # arrow `const hide = id => …`, which is how half of these are
            # written here.
            declared |= set(re.findall(
                r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?"
                r"(?:function\b|\(|[A-Za-z_$][\w$]*\s*=>)", js))

        # Only the handlers in the markup — the inline scripts are checked by
        # node above, and `this.closest(...)` style handlers name no function.
        markup = re.sub(r"<script.*?</script>", "", source, flags=re.S)
        called = set(re.findall(r'on(?:click|change|input)="\s*([A-Za-z_$][\w$]*)\s*\(',
                                markup))

        # `onclick="if(...) hide(...)"` is a statement, not a call.
        keywords = {'if', 'for', 'while', 'switch', 'return', 'typeof'}
        missing = sorted(called - declared - keywords)
        self.assertEqual(
            missing, [],
            f"portal.html wires these handlers to functions nothing declares: "
            f"{missing}. The button renders and does nothing when pressed.")


class ThePillsSayWhatTheDataShowsTests(SimpleTestCase):
    """The portal draws a manufacturer their own results, so what it omits is
    as much a decision as what it draws.

    A pill is drawn where OGA recommends, and — since 29 Aug 2026 — where the
    antibody did the thing its application is for and still did not meet the
    bar. A plain negative deliberately draws nothing: that half is what stops a
    supplier being shown a claim nobody ran the test for, and it is unchanged.
    """

    def test_the_figure_is_wired_to_the_field_that_feeds_it(self):
        """A helper declared in one function and called from another is valid
        syntax, parses clean, and only fails when the line runs — the defect
        this file exists for. So this asserts `oga_display` is read inside the
        function that builds the thumbnails, not merely that the string appears
        somewhere in the file.

        The portal presents results the way a public gene page does since
        29 Aug 2026: the verdict is on the figure and its sentence, not on a row
        of pills above them. A supplier comparing their row here with the page
        it links to must meet one visual language.
        """
        source = TEMPLATE.read_text()
        start = source.index("const thumbs = filteredExps.map(")
        body = source[start:source.index("}).join('');", start)]
        self.assertIn("oga_display", body,
                      "the figure must read the field the server sends")
        self.assertIn("d.sentence", body, "the figure must carry its sentence")
        self.assertIn("exp-supportive", body)
        self.assertIn("has-caveat", body)

    def test_a_payload_without_the_new_key_still_draws(self):
        """A cached response predates it, and every read is guarded."""
        source = TEMPLATE.read_text()
        start = source.index("const thumbs = filteredExps.map(")
        body = source[start:source.index("}).join('');", start)]
        self.assertIn("(ab.oga_display || {})", body)
        self.assertIn("isRec", body, "no fallback to the boolean")

    def test_the_supported_applications_line_is_not_a_second_copy(self):
        """The heading comes from the gene page's own constant through
        `json_script`, so the two surfaces cannot word it differently."""
        source = TEMPLATE.read_text()
        self.assertIn('json_script:"supports-label"', source)
        self.assertIn("SUPPORTS_LABEL", source)
        self.assertNotIn("Characterisation data supports", source)

    def test_the_legend_names_every_state_the_page_can_draw(self):
        """A pill with no legend entry is one a manufacturer has to guess at."""
        source = TEMPLATE.read_text()
        legend = source[source.index('id="result-legend"'):]
        legend = legend[:legend.index("</div>")]
        for cls in ("lg-swatch lg-supportive", "lg-swatch lg-supportive lg-tab",
                    "lg-swatch"):
            self.assertIn(cls, legend, cls)



class ThePortalUsesNoDjangoCommentDelimitersTests(SimpleTestCase):
    """``{#`` does not span lines and ends at the first ``#}``.

    So a short-form comment that mentions the delimiters cuts itself in half and
    prints its own tail into the page. Inside a ``<script>`` the damage is worse
    than cosmetic: what leaks out is executed.
    """

    def test_no_short_form_django_comments(self):
        source = TEMPLATE.read_text()
        for delimiter in ("{#", "#}"):
            self.assertNotIn(
                delimiter, source,
                f"portal.html contains {delimiter!r}. Use "
                "{% comment %} for anything longer than a few words.")
