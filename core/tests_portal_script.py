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


def _function_source(js, name):
    """The source of ``function name(...) {...}`` (or ``async function``)."""
    start = re.search(r"(async\s+)?function\s+" + re.escape(name) + r"\s*\(", js)
    assert start, f"no function {name}"
    i = js.index("{", start.end())
    depth = 0
    for j in range(i, len(js)):
        depth += {"{": 1, "}": -1}.get(js[j], 0)
        if depth == 0:
            return js[start.start():j + 1]
    raise AssertionError(f"unbalanced {name}")


class TheReviewControlsSayWhatTheyDidTests(SimpleTestCase):
    """Field test, 29 Sep 2026, on the test key's portal.

    *Clear reviewed marks* with none to clear asked *"Clear the reviewed mark
    from 0 antibodies?"* and answered *"Reviewed marks cleared — 0"*; and a toast
    shown within 2.5 s of another vanished early, because each set its own timer
    and the first one's fired over the second — *"Saved — 1 antibody marked
    reviewed"* was gone in under a second. Run in node, since the portal is one
    inline script and the behaviour is the timing."""

    def setUp(self):
        if not shutil.which("node"):
            self.skipTest("node is not installed")
        self.js = "\n".join(_inline_scripts())

    def _run(self, body):
        script = body
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(script)
            tmp = fh.name
        try:
            done = subprocess.run(["node", tmp], capture_output=True, text=True)
        finally:
            os.unlink(tmp)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.strip().splitlines()

    def test_a_second_toast_keeps_its_full_time_on_screen(self):
        toast = _function_source(self.js, "toast")
        timer = "let toastTimer = null;" if "toastTimer" in self.js else ""
        out = self._run(f"""
            let now = 0; const timers = [];
            global.setTimeout = (f, ms) => {{ timers.push({{at: now + ms, f}}); return timers.length; }};
            global.clearTimeout = id => {{ if (id) timers[id - 1].f = () => {{}}; }};
            const el = {{textContent: '', style: {{}}, classList: {{s: new Set(),
              add(c) {{ this.s.add(c); }}, remove(c) {{ this.s.delete(c); }},
              contains(c) {{ return this.s.has(c); }} }}}};
            const $ = () => el;
            {timer}
            {toast}
            const tick = t => {{ now = t; timers.filter(x => x.at <= t && !x.done)
              .forEach(x => {{ x.done = true; x.f(); }}); }};
            toast('first'); tick(2000); toast('second'); tick(2600);
            console.log(el.classList.contains('show') ? 'showing' : 'hidden');
        """)
        self.assertEqual(out, ["showing"], "the second toast was hidden by the first's timer")

    def test_nothing_to_clear_is_said_without_asking(self):
        clear = _function_source(self.js, "clearReviewProgress")
        if "function doClearReviewProgress" in self.js:
            clear += "\n" + _function_source(self.js, "doClearReviewProgress")
        helper = (_function_source(self.js, "antibodyCount")
                  if "function antibodyCount" in self.js else "")
        out = self._run(f"""
            const S = {{reviewed: new Set(), reviewedDirty: new Set()}};
            let asked = 0; global.confirm = () => {{ asked++; return true; }};
            const askOnPage = (m, label, yes) => {{ asked++; return yes(); }};
            const said = []; const toast = m => said.push(m);
            const apiDelete = async () => ({{deleted: S.reviewed.size}});
            const refreshNewStat = () => {{}}, applyFilters = () => {{}}, updateActionBar = () => {{}};
            {helper}
            {clear}
            Promise.resolve(clearReviewProgress()).then(() => {{
              console.log(String(asked)); console.log(said.join(' | '));
              S.reviewed.add('ab1'); asked = 0; said.length = 0;
              return Promise.resolve(clearReviewProgress()); }}).then(() => {{
              console.log(String(asked)); console.log(said.join(' | ')); }});
        """)
        self.assertEqual(out[0], "0", "asked a question about nothing")
        self.assertNotIn("— 0", out[1])
        self.assertEqual(out[2], "1")
        self.assertIn("1 antibody", out[3])


class ThePortalAsksOnThePageTests(SimpleTestCase):
    """Field test, 29 Sep 2026: Clear reviewed marks and Mark all reviewed
    asked through the browser's own confirm box while everything else on the
    portal answers on the page. The page asks now, with the count on the
    button."""

    def test_no_native_confirm_is_left(self):
        js = "\n".join(_inline_scripts())
        self.assertNotIn("confirm(", js.replace("confirmMarkReviewed(", ""))
        self.assertIn("function askOnPage", js)
        self.assertIn("'confirm-bar'", js, "Escape must close the question too")
